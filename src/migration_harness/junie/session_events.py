"""今回の Junie 起動と照合できたイベントログだけを増分で読む。"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import format_error
from ..execution import check_cancelled, junie_dashboard_control, report_activity
from ..log_text import decode_log_text
from .usage import UsageDelta


def _is_junie_connection_failure(entry: dict[str, Any]) -> bool:
    # ツール単位の失敗では再起動せず、MainAgentの接続失敗が確定したイベントだけを拾う。
    event = entry.get("event")
    if entry.get("kind") != "SessionA2uxEvent" or not isinstance(event, dict) or event.get("state") != "FAILED":
        return False
    failure = event.get("agentEvent")
    return (
        isinstance(failure, dict)
        and failure.get("kind") == "AgentFailureEvent"
        and failure.get("errorCode") == "ConnectionFailed"
        and isinstance(failure.get("agent"), dict)
        and failure["agent"].get("kind") == "MainAgent"
    )


@dataclass
class _EventCursor:
    # ファイルの置換をidentity、追記位置をoffsetで識別し、同じイベントの二重集計を防ぐ。
    identity: tuple[int, int]
    offset: int = 0
    pending: bytes = b""  # 監視中に届いた未完の行は、次の追記とつなげて読む。
    startup_boundary: bool = False  # 起動前からある末尾が、行の途中だったかを初回読取時に調べる。
    discard: bool = False  # 起動前に書き始められた行の続きを、次の改行まで読み捨てる。


def _event_activity(entry: dict[str, Any]) -> str:
    """イベントの自由入力を出さず、固定の種類名だけを通知する。"""
    event = entry.get("event")
    agent = event.get("agentEvent") if isinstance(event, dict) else None
    if isinstance(agent, dict):
        if event.get("state") == "INPUT_REQUIRED" and isinstance(agent.get("approvalRequest"), dict):
            if junie_dashboard_control() is not None:
                return "Junieが操作の確認を待っています。"
            return "Junieが操作の確認を待っています。起動した端末で確認してください。"
        kind = agent.get("kind")
        if kind == "LlmResponseMetadataEvent":
            return "Junieのモデル応答を受信しました。"
        tool = {
            "TerminalBlockUpdatedEvent": "terminal",
            "ViewFilesBlockUpdatedEvent": "view_files",
            "FileChangesBlockUpdatedEvent": "file_changes",
        }.get(kind) if isinstance(kind, str) else None
        if kind == "ToolBlockUpdatedEvent":
            name = agent.get("toolType")
            normalized = re.sub(r"[-_ ]", "", name).casefold() if isinstance(name, str) else ""
            tool = {
                "search": "search", "skill": "skill", "readfile": "read_file",
                "writefile": "write_file", "editfile": "edit_file", "terminal": "terminal",
            }.get(normalized, "tool")
        if tool:
            action = {
                "terminal": "コマンドの実行", "view_files": "ファイルの読み取り",
                "file_changes": "ファイルの変更", "search": "ソースの検索", "skill": "作業手順の確認",
                "read_file": "ファイルの読み取り", "write_file": "ファイルの書き込み",
                "edit_file": "ファイルの編集", "tool": "ツールの実行",
            }[tool]
            return f"Junieから{action}の報告が届きました。"
    return "Junieのセッションに更新がありました。"


class SessionEvents:
    """今回の起動と照合できたJunieのセッションだけを監視する。"""

    def __init__(self, token: str, *, headless_project: Path | None = None) -> None:
        self.token = token
        self.headless_project = headless_project
        try:
            self.launch_cwd = os.getcwd()
        except OSError:
            self.launch_cwd = None
        self.sessions = Path(os.environ.get("JUNIE_HOME") or Path.home() / ".junie").expanduser() / "sessions"
        self.logs = self.sessions.parent / "logs"
        self.existing_logs = set(self._log_paths())
        self.cursors: dict[Path, _EventCursor] = {}
        self.existing_events = set(self._paths())
        # 過去のイベントは集計せず、この起動より後に追記される部分だけを候補にする。
        for path in self.existing_events:
            try:
                stat = path.stat()
            except OSError:
                continue
            self.cursors[path] = _EventCursor(
                (stat.st_dev, stat.st_ino), stat.st_size, startup_boundary=bool(stat.st_size),
            )
        self.path: Path | None = None
        self.task: str | None = None
        self.result: str | None = None
        self.steering_tokens: set[str] = set()
        self.finished_steering: set[str] = set()
        self._prompt_steering: str | None = None
        self._task_steering: str | None = None
        self.usage = UsageDelta()
        self.last_event_at: float | None = None

    def _paths(self) -> Iterator[Path]:
        try:
            yield from self.sessions.glob("*/events.jsonl")
        except OSError:
            return

    def _log_paths(self) -> Iterator[Path]:
        try:
            yield from self.logs.glob("log-*.log")
        except OSError:
            return

    def startup_diagnostics(self, pid: int, command: list[str]) -> str:
        """起動に対応するログのエラーとスタックを秘密情報を伏せて取り込む。"""
        new_logs = [path for path in self._log_paths() if path not in self.existing_logs]
        matched = [path for path in new_logs if path.name.endswith(f"-pid-{pid}.log")]
        # ランチャーの子が別PIDでログを書く場合は、新規ログが1件のときだけ追加照合へ進む。
        paths = matched or (new_logs if len(new_logs) == 1 else [])
        if not paths:
            return f"Junie log directory: {self.logs} (no unambiguous log file for this launch)"
        details = [f"Junie startup log: {path}" for path in paths]
        for path in paths:
            if path.is_symlink():
                continue
            try:
                errors = []
                in_error = False
                terminal_failure = False
                startup_matches = False
                cwd_matches = False
                with path.open("rb") as stream:
                    for raw in stream:
                        line = decode_log_text(raw).replace("\r\n", "\n")
                        startup = re.search(r"\] INFO\s+JunieRunner - Startup: Junie .* \(pid=(\d+)\); log file: (.+)", line)
                        if startup:
                            startup_matches = (
                                path.name.endswith(f"-pid-{startup[1]}.log")
                                and Path(startup[2]).resolve() == path.resolve()
                            )
                        directory = re.search(r"\] INFO\s+JunieRunner - user\.dir: (.+)", line)
                        if directory and self.launch_cwd is not None:
                            cwd_matches = Path(directory[1]).resolve() == Path(self.launch_cwd).resolve()
                        if re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", line):
                            in_error = bool(re.search(r"\]\s+(ERROR|FATAL)\s", line))
                        terminal_failure |= "JLine returned dumb" in line or "Unable to initialize an interactive terminal" in line
                        if in_error:
                            errors.append(line)
                correlated = path in matched or (startup_matches and cwd_matches)
                if errors and correlated:
                    text = "".join(errors).rstrip()
                    # 環境変数は秘密値の伏せ字処理にだけ使い、変数名や一覧は診断へ出さない。
                    secrets = [value for name, value in os.environ.items() if value and re.search(
                        r"TOKEN|PASSWORD|PASSWD|SECRET|CREDENTIAL|API_?KEY|AUTH|JUNIE_(PROMPT|TASK)", name, re.I,
                    ) and not (name.upper() == "TOKEN_BUDGET" and re.fullmatch(r"[0-9]+", value))]
                    for value in sorted(secrets, key=len, reverse=True):
                        for variant in (value, repr(value)[1:-1], json.dumps(value)[1:-1]):
                            text = text.replace(variant, "[REDACTED]")
                    details.append("Junie startup error log:\n" + format_error(RuntimeError(text), command=command))
                if terminal_failure and correlated:
                    details.append(
                        "Junie could not initialize its interactive terminal (JLine returned dumb). "
                        "Run from an interactive terminal without redirected stdin/stdout; "
                        "if TERM is unset or dumb, use a TERM value supported by your terminal "
                        "(for example xterm-256color) and resume."
                    )
            except OSError:
                details.append("Could not read the startup log; check its file permissions.")
        return "\n".join(details)

    def _new_lines(self, path: Path, *, cancellable: bool) -> Iterator[bytes]:
        stat = path.stat()
        identity = (stat.st_dev, stat.st_ino)
        cursor = self.cursors.get(path)
        if cursor is None or cursor.identity != identity or stat.st_size < cursor.offset:
            if cursor is not None and path == self.path:
                # ログが置換・切り詰めされたら、今回の起動との対応を照合し直す。
                self.path = None
                self.task = None
            cursor = self.cursors[path] = _EventCursor(identity)
        if stat.st_size == cursor.offset:
            return
        with path.open("rb") as stream:
            if cursor.startup_boundary:
                stream.seek(cursor.offset - 1)
                cursor.discard = stream.read(1) != b"\n"
                cursor.startup_boundary = False
            stream.seek(cursor.offset)
            # Junieが追記し続けても制御を返せるよう、この読取開始時のサイズで区切る。
            while cursor.offset < stat.st_size:
                if cancellable:
                    check_cancelled()
                line = stream.readline(stat.st_size - cursor.offset)
                if not line:
                    break
                cursor.offset = stream.tell()
                if cursor.discard:
                    cursor.discard = not line.endswith(b"\n")
                    continue
                line = cursor.pending + line
                cursor.pending = b""
                if not line.endswith(b"\n"):
                    cursor.pending = line
                    break
                yield line

    def read_new_events(self, *, cancellable: bool = True) -> bool:
        paths = [self.path] if self.path else self._paths()
        reported: set[str] = set()
        for path in paths:
            if cancellable:
                check_cancelled()
            try:
                if self.path is None and self.headless_project is not None:
                    if path in self.existing_events:
                        continue
                    # --taskのイベントにはプロンプトや各行のtaskIdが含まれない版がある。
                    try:
                        summary = json.loads(path.with_name("summary.json").read_bytes())
                    except (OSError, ValueError):
                        continue
                    if not isinstance(summary, dict):
                        continue
                    project = summary.get("projectDir")
                    if not isinstance(project, str) or Path(project) != self.headless_project:
                        continue
                    self.path = path
                for line in self._new_lines(path, cancellable=cancellable):
                    try:
                        entry = json.loads(line)
                    except (ValueError, UnicodeError):
                        continue
                    if not isinstance(entry, dict):
                        continue
                    kind = entry.get("kind")
                    if not isinstance(kind, str) or not kind:
                        continue
                    if self.path is None:
                        if kind != "UserPromptEvent":
                            continue
                        prompt = entry.get("prompt")
                        # プロンプト中の今回のトークンで、並行して動く他のJunieセッションを除外する。
                        if not isinstance(prompt, str) or self.token not in prompt:
                            continue
                        self.path = path
                    if self.path != path:
                        continue
                    if kind == "UserPromptEvent":
                        prompt = entry.get("prompt")
                        self._prompt_steering = next(
                            (token for token in self.steering_tokens if token in prompt), None,
                        ) if isinstance(prompt, str) else None
                    if kind == "TaskStartedEvent":
                        self.task = entry.get("taskId")
                        self.result = None
                        self._task_steering = self._prompt_steering
                        self._prompt_steering = None
                    # 同じセッション内でも、別タスクの結果や通信失敗を今回のものとして扱わない。
                    if entry.get("taskId") is not None and entry.get("taskId") != self.task:
                        continue
                    matches_task = self.task and (entry.get("taskId") == self.task or self.headless_project is not None)
                    if matches_task and kind == "SessionA2uxEvent":
                        event = entry.get("event")
                        self.usage.record(event)
                        agent = event.get("agentEvent") if isinstance(event, dict) else None
                        if (
                            isinstance(agent, dict) and event.get("state") == "COMPLETED"
                            and agent.get("kind") == "ResultBlockUpdatedEvent"
                            and isinstance(agent.get("agent"), dict) and agent["agent"].get("kind") == "MainAgent"
                        ):
                            if self._task_steering:
                                self.finished_steering.add(self._task_steering)
                            if agent.get("cancelled") is False and isinstance(agent.get("result"), str):
                                self.result = agent["result"]
                    self.last_event_at = time.monotonic()
                    message = _event_activity(entry)
                    # 1回の読取で同じ状況通知はまとめるが、使用量は応答ごとに集計する。
                    if message not in reported:
                        report_activity(message)
                        reported.add(message)
                    if matches_task and _is_junie_connection_failure(entry):
                        return True
            except OSError:
                continue
            if self.path is not None:
                break
        return False
