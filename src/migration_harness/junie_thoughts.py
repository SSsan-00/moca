"""Collect Junie's recorded thought entries for one dashboard Run."""

from __future__ import annotations

import json
import os
import re
import sys
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from threading import Thread

from .errors import safe_command
from .files import safe_target


FILENAME = "junie-thoughts.jsonl"
_STARTUP = re.compile(r"^Junie .* \(pid=(\d+)\): log file: (.+\.log)$")
_TRAJECTORY = "[ANALYTICS:EVENT] type=trajectory_element properties=element="
_DECODER = json.JSONDecoder()
_SOURCE_CHUNK = 1024 * 1024
_RESPONSE_CHUNK = 256 * 1024
_THOUGHT_LIMIT = 32_000


def _entry(line: bytes, token: str) -> dict | None:
    """Junieイベントを画面表示用の思考項目へ変換する。"""
    if b"AssistantTrajectoryElement" not in line:
        return None
    text = line.decode("utf-8", errors="replace")
    _, separator, payload = text.partition(_TRAJECTORY)
    if not separator:
        return None
    try:
        item, _ = _DECODER.raw_decode(payload)
        at = datetime.fromisoformat(text[:23]).astimezone().isoformat(timespec="milliseconds")
    except ValueError:
        return None
    if not isinstance(item, dict) or str(item.get("type", "")).rsplit(".", 1)[-1] != "AssistantTrajectoryElement":
        return None
    agent = item.get("subagent")
    thought = item.get("thought")
    if (not isinstance(agent, dict) or agent.get("agent_type") != "primary"
            or not isinstance(thought, str) or not thought.strip(" .\t\r\n")):
        return None
    thought = safe_command(thought.strip())
    if token:
        thought = thought.replace(token, "[REDACTED]")
    if len(thought) > _THOUGHT_LIMIT:
        thought = thought[:_THOUGHT_LIMIT] + "\n（以降省略）"
    return {"at": at, "thought": thought}


class JunieThoughts:
    def __init__(self, stderr, log_directory: Path, existing_logs: set[Path], target: Path, token: str) -> None:
        """Junie出力の追跡位置と保存状態を初期化する。"""
        self.directory = log_directory.resolve()
        self.existing_logs = existing_logs
        self.target = target
        self.token = token
        self.source: Path | None = None
        self.offset = 0
        self.discarding = False
        self.reader = Thread(target=self._read_stderr, args=(stderr,), daemon=True)
        self.reader.start()

    def _read_stderr(self, stderr) -> None:
        # Junie のラッパーと実プロセスの PID は異なる。起動したプロセス自身の出力から特定する。
        """標準エラーから新しい進捗行を読み取る。"""
        with stderr:
            for raw in stderr:
                line = raw.decode("utf-8", errors="replace")
                match = _STARTUP.fullmatch(line.rstrip()) if self.source is None else None
                if match:
                    candidate = Path(match[2])
                    with suppress(OSError, ValueError):
                        if (candidate.is_absolute() and candidate.parent.resolve() == self.directory
                                and candidate.name.startswith("log-") and candidate.name.endswith(f"-pid-{match[1]}.log")
                                and candidate not in self.existing_logs and not candidate.is_symlink()):
                            self.source = candidate
                with suppress(OSError, UnicodeError, ValueError):
                    if sys.stderr is not None:
                        sys.stderr.write(line)
                        sys.stderr.flush()

    def poll(self) -> None:
        """新しいJunie出力を取り込んで画面状態を更新する。"""
        source = self.source
        if source is None or source.is_symlink() or not source.is_file():
            return
        with source.open("rb") as stream:
            stream.seek(self.offset)
            chunk = stream.read(_SOURCE_CHUNK)
        complete, newline, _ = chunk.rpartition(b"\n")
        if not newline:
            # 追記途中の行を残す。上限以上の診断行は次の改行まで読み飛ばす。
            if len(chunk) == _SOURCE_CHUNK:
                self.offset += len(chunk)
                self.discarding = True
            return
        lines = complete.split(b"\n")
        rows = [entry for line in lines[int(self.discarding):] if (entry := _entry(line, self.token))]
        if rows:
            target = safe_target(self.target.parent.parent, Path(self.target.parent.name, FILENAME))
            flags = os.O_RDWR | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            with os.fdopen(os.open(target, flags, 0o600), "ab+") as stream:
                # 強制終了で書きかけの行があっても、再開後の最初のメモを独立した行にする。
                if stream.seek(0, 2):
                    stream.seek(-1, 2)
                    if stream.read(1) != b"\n":
                        stream.write(b"\n")
                for row in rows:
                    stream.write(json.dumps(row, ensure_ascii=False).encode("utf-8") + b"\n")
        self.offset += len(complete) + 1
        self.discarding = False

    def finish(self) -> None:
        """終了時の出力を確定して読み取り資源を閉じる。"""
        self.reader.join(timeout=1)
        while True:
            offset = self.offset
            self.poll()
            if offset == self.offset:
                return


def read_junie_thoughts(run_root: Path, run_id: str, cursor: int) -> dict:
    """保存済みJunie出力を画面表示用に読み取る。"""
    path = safe_target(run_root, Path(run_id, FILENAME))
    if not path.is_file():
        return {"cursor": 0, "entries": [], "reset": bool(cursor), "more": False}
    with path.open("rb") as stream:
        size = stream.seek(0, 2)
        reset = cursor > size
        start = 0 if reset else cursor
        stream.seek(start)
        chunk = stream.read(_RESPONSE_CHUNK)
    complete, newline, _ = chunk.rpartition(b"\n")
    if not newline:
        return {"cursor": start, "entries": [], "reset": reset, "more": False}
    entries = []
    for line in complete.split(b"\n"):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get("at"), str) and isinstance(row.get("thought"), str):
            entries.append({"at": row["at"], "thought": row["thought"]})
    end = start + len(complete) + 1
    return {"cursor": end, "entries": entries, "reset": reset, "more": len(chunk) == _RESPONSE_CHUNK and end < size}
