"""Junie CLIの起動、通信再試行、セッションイベントの読み取り。"""

# run_interactiveがモード選択と再試行、_run_sessionが1回の起動から終了までを受け持つ。
# 通信は端末・非対話（--task）・ダッシュボード用ACPの3通り。イベントログで使用量も追跡する。

from __future__ import annotations

import errno
import json
import ntpath
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterator
from concurrent.futures import Future
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue
from tempfile import TemporaryFile
from threading import Event, Thread
from typing import TYPE_CHECKING, Any

from .errors import format_error, safe_command
from .execution import (
    HEARTBEAT_SECONDS, ExecutionControl, OperationCancelled, check_cancelled, is_controlled,
    junie_dashboard_control, report_activity,
)
from .files import has_symlink_component, safe_target
from .junie_process import JunieProcess
from .junie_thoughts import JunieThoughts

if TYPE_CHECKING:
    from .editor_queue import LivePrompts


USAGE_FIELDS = ("input", "output", "cache_read", "cache_write", "responses")
ACP_TIMEOUT = 30
MODEL_LIST_TIMEOUT = 10
DASHBOARD_PROGRESS_INSTRUCTION = (
    "進捗と最終回答は日本語で書いてください。重要な確認や変更に進むときは、"
    "確認できた事実、判断の理由、次に行うことを日本語で1〜2文にまとめて利用者に伝えてください。"
)


class JunieEditHandoff(KeyboardInterrupt):
    """修正セッションを閉じ、保存した編集と指示を自動で引き継ぐ。"""


class JunieConnectionError(OSError):
    """Junieがモデルサービスとの通信を再試行しても続行できなかった。"""


class JunieStopError(OSError):
    """終了を確認できなかったため、編集の反映や次の起動へ進めない。"""


def unconfirmed_junie_stop(error: BaseException) -> JunieStopError | None:
    """診断の保存などで別の例外が出ても、未確認の終了を見落とさない。"""
    pending = [error]
    seen = set()
    while pending:
        current = pending.pop()
        if isinstance(current, JunieStopError):
            return current
        if id(current) not in seen:
            seen.add(id(current))
            pending.extend(link for link in (current.__context__, current.__cause__) if link is not None)
    return None


class JunieLaunchError(OSError):
    """起動診断を文字列にも残し、保存済みRunから原因を確認できるようにする。"""

    def __init__(
        self, command: list[str], cwd: str | None, executable: str | None, cause: Exception,
        *, interpreter: str | None = None,
    ) -> None:
        """Junieの起動失敗と診断情報を保持する。"""
        self.command = command.copy()
        self.cwd = cwd
        self.executable = executable
        super().__init__(
            "Could not launch Junie. Check the executable, working directory and file permissions; "
            "after installing Junie or changing PATH, reopen the terminal and resume this Run.\n"
            + launch_diagnostics(command, cwd=cwd, executable=executable or "<not resolved>")
            + (f"\nBatch interpreter: {interpreter}" if interpreter else "")
            + "\n" + format_error(cause, command=command, cwd=cwd)
        )


class JunieSessionError(OSError):
    """終了コードが0でも、セッションを開始できなかった終了を報告する。"""

    def __init__(self, command: list[str], exit_code: int, details: str) -> None:
        """Junieセッション失敗と通信情報を保持する。"""
        self.command = command.copy()
        self.returncode = exit_code
        super().__init__(
            f"Junie exited with code {exit_code} before starting a matching session or writing a completion record.\n"
            + launch_diagnostics(command) + "\n" + details
        )


class JunieAcpModelUnavailable(JunieSessionError):
    """ACPがモデル不足のためプロンプトを処理する前に拒否した。"""


def _resolve_executable(name: str) -> str:
    """Junie実行ファイルの場所を環境から解決する。"""
    executable = shutil.which(name)
    if executable is None:
        raise FileNotFoundError(errno.ENOENT, "Junie CLI was not found on PATH (or is not executable)", name)
    # ランチャーが自身の場所からインストール先を探す場合に備え、シンボリックリンクは解決しない。
    return os.path.abspath(executable)


def launch_diagnostics(
    command: list[str], *, cwd: str | None = None, executable: str | None = None,
) -> str:
    """端末出力を奪わず、保存するエラーに安全な起動情報を添える。"""
    if cwd is None:
        try:
            cwd = os.getcwd()
        except OSError:
            cwd = "<working directory unavailable>"
    if executable is None:
        try:
            executable = _resolve_executable(command[0])
        except (OSError, ValueError, IndexError):
            executable = "<not resolved>"
    if command[-2:-1] == ["--task"]:
        streams = "Junie ran in headless mode. Check the terminal output and the log file "
    elif junie_dashboard_control():
        streams = "Junie runs over ACP. Check the dashboard for permission requests and the log file "
    else:
        streams = "Junie inherits this terminal's stdin/stdout/stderr. Check its output and the log file "
    return (
        f"Command: {safe_command(command)}\nCWD: {cwd}\nExecutable: {executable}\n"
        f"{streams}reported at startup (normally under JUNIE_HOME/logs or ~/.junie/logs)."
    )


def require_interactive_cli() -> None:
    """Junie CLIと、ダッシュボードなしの実行に必要な対話端末を確認する。"""
    if junie_dashboard_control() is None and not sys.stdin.isatty():
        raise RuntimeError("Junie run requires an interactive terminal")
    try:
        _resolve_executable("junie")
    except OSError as exc:
        raise JunieLaunchError(["junie"], None, None, exc) from exc


def build_command(
    project: Path, prompt: str, max_steps: int | None, *, guidelines: Path | None = None, model: str | None = None,
    effort: str | None = None,
) -> list[str]:
    """Junie CLIの対話起動コマンドを組み立てる。"""
    command = ["junie", "--project", str(project), "--brave"]
    if guidelines is not None:
        # 本文をコマンド引数に展開せず、既存のJunie指示に追加する（Windowsの長さ制限も回避）。
        command.extend(["--ide-guidelines", str(guidelines)])
    if max_steps is not None:
        command.extend(["--max-steps", str(max_steps)])
    if model:
        command.extend(["--model", model])
    if effort:
        if not model:
            raise ValueError("推論の強さを指定する場合は、Junieで使うモデルも選択してください。")
        command.extend(["--effort", effort])
    # 再試行時に続きを促す文を付けるため、プロンプトを末尾に置く。
    return [*command, "--prompt", prompt]


def list_models() -> list[dict[str, Any]]:
    """インストール済みCLIのモデル候補を、タスクを開始せずに取得する。"""
    # 親が先に終了しても、継承されたパイプのEOFを待たず所有プロセス群を閉じる。
    with TemporaryFile() as output:
        process = _launch_process(["junie", "list-models"], owned=True, stdin=subprocess.DEVNULL,
                                  stdout=output, stderr=subprocess.DEVNULL)
        try:
            process.wait_for_launcher(timeout=MODEL_LIST_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise ValueError("Junieのモデル一覧の取得がタイムアウトしました。") from None
        finally:
            _close_junie(process)
        if process.returncode:
            raise ValueError("このJunie CLIではモデル一覧を取得できませんでした。")
        output.seek(0)
        data = output.read()
    try:
        models = json.loads(data)["models"]
        if not isinstance(models, list) or any(
            not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]
            or not isinstance(item.get("displayName"), str) for item in models
        ):
            raise ValueError
        if any("efforts" in item and (not isinstance(item["efforts"], list)
                                     or any(not isinstance(effort, str) for effort in item["efforts"])) for item in models):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise ValueError("Junieのモデル一覧を読み取れませんでした。") from None
    return [{"id": item["id"], "name": item["displayName"],
             **({"efforts": item["efforts"]} if "efforts" in item else {})} for item in models]


def _windows_batch_command(command: list[str]) -> tuple[str, str, dict[str, str]]:
    """本文とファイルパスはBATの引数処理を避け、Junieの公式環境変数で渡す。"""
    environment = os.environ.copy()
    # Windowsの環境変数名は大文字・小文字を区別しないが、dictへのコピー後は区別される。
    system_root = os.environ.get("SystemRoot")
    interpreter = ntpath.join(system_root, "System32", "cmd.exe") if system_root else environment.get("COMSPEC", "")
    if not ntpath.isabs(interpreter) or ntpath.basename(interpreter).lower() != "cmd.exe":
        raise OSError("Cannot locate cmd.exe; check the SystemRoot/COMSPEC settings and reopen the terminal")

    environment["PHP_TO_CSHARP_JUNIE_LAUNCHER"] = command[0]
    arguments = ['"%PHP_TO_CSHARP_JUNIE_LAUNCHER%"']
    remaining = iter(command[1:])
    for argument in remaining:
        option, separator, value = argument.partition("=")
        variable = {
            "--prompt": "JUNIE_PROMPT", "--task": "JUNIE_TASK", "--project": "JUNIE_PROJECT", "-p": "JUNIE_PROJECT",
            "--ide-guidelines": "JUNIE_IDE_GUIDELINES",
            "--model": "JUNIE_MODEL",
            "--effort": "JUNIE_EFFORT",
        }.get(option)
        if variable:
            if not separator:
                try:
                    value = next(remaining)
                except StopIteration:
                    raise ValueError(f"{option} requires a value") from None
            # cmdが上限を超える環境変数を黙って落とす前に、UTF-16の文字数で検出する。
            if len((variable + "=" + value).encode("utf-16-le")) // 2 > 8191:
                raise ValueError(f"{option} exceeds the Windows batch launcher limit; shorten it or use a native Junie executable")
            if "\0" in value:
                raise ValueError(f"{option} contains a NUL character")
            environment[variable] = value
            continue
        # 残るフラグ・数値はcmdに渡すため、引用符を壊す文字や制御文字を拒否する。
        if any(character in argument for character in ('"', "\r", "\n", "\0")):
            raise ValueError("A Windows batch launcher argument contains quotes or control characters; use --prompt for prompt text")
        name = f"PHP_TO_CSHARP_JUNIE_ARG_{len(arguments)}"
        # BATからネイティブ実行ファイルへ渡る際、末尾のバックスラッシュが消費される分を補う。
        trailing = len(argument) - len(argument.rstrip("\\"))
        environment[name] = argument + "\\" * trailing
        arguments.append(f'"%{name}%"')

    if any(character in command[0] for character in ('"', "\r", "\n", "\0")):
        raise ValueError("The Junie batch launcher path contains invalid characters")
    expanded_size = sum(len(environment[item[2:-2]].encode("utf-16-le")) // 2 + 3 for item in arguments)
    if expanded_size > 8191:
        raise ValueError("Junie arguments exceed the Windows batch command limit; shorten the paths or arguments")
    # CALLによる再展開、AutoRun、!変数!の遅延展開を避け、組み立てた行をcmdへ直接渡す。
    line = subprocess.list2cmdline([interpreter]) + ' /d /v:off /s /c "' + " ".join(arguments) + '"'
    return line, interpreter, environment


def _launch_process(command: list[str], *, owned=False, terminal=False, **streams: Any) -> subprocess.Popen:
    """所有範囲を設定してJunieプロセスを起動する。"""
    check_cancelled()
    executable = None
    interpreter = None
    cwd = None
    try:
        cwd = os.getcwd()
        if not command:
            raise ValueError("Junie command is empty")
        executable = _resolve_executable(command[0])
        resolved = [executable, *command[1:]]
        if sys.platform == "win32" and ntpath.splitext(executable)[1].lower() in {".bat", ".cmd"}:
            resolved, interpreter, environment = _windows_batch_command(resolved)
            streams["executable"] = interpreter
        else:
            environment = os.environ.copy()
        # JunieのPythonツールが補助モジュールを読んでも、読み取り専用資料の隣にキャッシュを作らせない。
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        if owned:
            return JunieProcess(resolved, terminal=terminal, cwd=cwd, env=environment, **streams)
        return subprocess.Popen(resolved, cwd=cwd, env=environment, **streams)
    except (OSError, ValueError) as exc:
        raise JunieLaunchError(command, cwd, executable, exc, interpreter=interpreter) from exc


def has_completion_record(path: Path, token: str) -> bool:
    """現在の呼び出しが残した完了トークンだけを受理する。"""
    if path.is_symlink() or not path.is_file():
        return False
    expected = token.encode("ascii")
    try:
        with path.open("rb") as stream:
            # 許容するのは末尾の改行だけ。最大許容長より1バイト多く読み、追記された本文を拒否する。
            value = stream.read(len(expected) + 3)
    except OSError:
        return False
    return value in (expected, expected + b"\n", expected + b"\r\n")


def _close_junie(process: subprocess.Popen) -> int:
    """Junieの終了を待ち、次のカプセルと端末操作が重ならないようにする。"""
    if isinstance(process, JunieProcess):
        return process.stop()
    if process.poll() is not None:
        return process.returncode
    try:
        if os.name == "nt":
            # Windowsのランチャーが起動した子プロセスも閉じてから次へ進む。
            taskkill = Path(os.environ["SystemRoot"]) / "System32" / "taskkill.exe"
            subprocess.run(
                [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                check=True, capture_output=True, timeout=10,
            )
        else:
            # 端末の前景グループは共有したまま、同じ端末の他プロセスを巻き込まずJunieの子孫だけを止める。
            listing = subprocess.run(
                ["ps", "-eo", "pid=,ppid="], check=True, capture_output=True, text=True, timeout=10,
            )
            children: dict[int, list[int]] = {}
            for row in listing.stdout.splitlines():
                pid, parent = map(int, row.split())
                children.setdefault(parent, []).append(pid)
            descendants = []
            pending = [process.pid]
            while pending:
                nested = children.get(pending.pop(), [])
                descendants.extend(nested)
                pending.extend(nested)
            for pid in reversed(descendants):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.terminate()
        return process.wait(timeout=10)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        if process.poll() is not None:
            return process.returncode
        raise OSError("Could not close Junie. Close the current Junie session before resuming.") from exc


def _is_junie_connection_failure(entry: dict[str, Any]) -> bool:
    # ツール単位の失敗では再起動せず、MainAgentの接続失敗が確定したイベントだけを拾う。
    """例外連鎖がJunieの通信失敗か判定する。"""
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


class _JunieSessionEvents:
    """今回の起動と照合できたJunieのセッションだけを監視する。"""

    def __init__(self, token: str, *, headless_project: Path | None = None) -> None:
        """セッションログの読取位置と解析状態を初期化する。"""
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
        self.usage = [0] * len(USAGE_FIELDS)
        self.last_event_at: float | None = None

    def _paths(self) -> Iterator[Path]:
        """セッションで参照するイベントファイルを列挙する。"""
        try:
            yield from self.sessions.glob("*/events.jsonl")
        except OSError:
            return

    def _log_paths(self) -> Iterator[Path]:
        """セッションのログファイルを列挙する。"""
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
                with path.open(encoding="utf-8", errors="replace") as stream:
                    for line in stream:
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
        """前回以降に追加されたログ行を読み取る。"""
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
        """今回のセッションの新着行から、使用量と通信失敗を読む。"""
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
                        self._count_usage(event)
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

    def _count_usage(self, event: Any) -> None:
        """イベントからトークン使用量の差分を数える。"""
        agent = event.get("agentEvent") if isinstance(event, dict) else None
        if not isinstance(agent, dict) or agent.get("kind") != "LlmResponseMetadataEvent":
            return
        usage = agent.get("modelUsage")
        if not isinstance(usage, list):
            return
        for entry in usage:
            if not isinstance(entry, dict) or "inputTokens" not in entry or "outputTokens" not in entry:
                continue
            counts = [entry.get(key, 0) for key in ("inputTokens", "outputTokens", "cacheInputTokens", "cacheCreateTokens")]
            if all(type(value) is int and value >= 0 for value in counts):
                uncached, output, cache_read, cache_write = counts
                # 入力合計はキャッシュ読み込み分と作成分を含む。
                input_total = uncached + cache_read + cache_write
                for i, value in enumerate((input_total, output, cache_read, cache_write, 1)):
                    self.usage[i] += value


def _confirm_dashboard_launch(
    dashboard: ExecutionControl, title: str, detail: str, option: str, label: str,
) -> bool:
    """画面で今回のJunie起動方法を利用者に確認する。"""
    reply = dashboard.on_junie_request({
        "title": title, "detail": detail,
        "options": [
            {"id": "stop", "label": "停止", "kind": "reject_once"},
            {"id": option, "label": label, "kind": "allow_once"},
        ],
    })
    check_cancelled()
    if reply is None:
        return False
    while not reply.done():
        dashboard.cancelled.wait(0.25)
        check_cancelled()
    check_cancelled()
    return reply.result() == option


def _supports_task(executable: str) -> bool:
    """実行するランチャーのヘルプから、非対話オプションの有無を調べる。"""
    process = _launch_process(
        [executable, "--help"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    with process:
        try:
            output, _ = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            return False
        finally:
            if process.poll() is None:
                _close_junie(process)
            check_cancelled()
    return process.returncode == 0 and re.search(rb"(?m)^[ \t]*--task(?:[= \t]|\r?$)", output) is not None


def _task_command(command: list[str]) -> list[str]:
    """Junieの非対話タスク起動コマンドを組み立てる。"""
    return [part for part in command[:-2] if part != "--brave"] + ["--task", command[-1]]


launch_input_observer: ContextVar[Callable[[str], None] | None] = ContextVar("launch_input_observer", default=None)
steering_input_observer: ContextVar[Callable[[str], bool] | None] = ContextVar("steering_input_observer", default=None)
session_closed_observer: ContextVar[Callable[[], None] | None] = ContextVar("session_closed_observer", default=None)


def run_interactive(
    command: list[str], completion_path: Path, token: str, targets: tuple[Path, ...],
    *, report_usage: Callable[[list[int]], None] | None = None,
    accept_result: Callable[[str], None] | None = None,
    prefer_task: bool = False,
    prompts: LivePrompts | None = None,
) -> int:
    """Junieを選択したモードで起動し、通信障害時は同じ作業場所で再試行する。"""
    if junie_dashboard_control() is not None:
        command = [*command[:-1], f"{command[-1]}\n\n{DASHBOARD_PROGRESS_INSTRUCTION}"]
    # 終了コードだけでは移行完了を保証しない。呼び出し元でも完了記録と成果物を検証する。
    task_supported = None
    if prompts is not None:
        prompts.recover()
    if prefer_task:
        task_supported = _supports_task(command[0])
        if task_supported:
            command = _task_command(command)
            report_activity("Junie を非対話モードで起動します。")
        else:
            require_interactive_cli()
            report_activity("Junie の --task を確認できないため、従来のモードで起動します。")
    delays = (1, 3)
    for attempt in range(len(delays) + 1):
        check_cancelled()
        try:
            invocation = command
            if attempt:
                # 作業場所と完了トークンを引き継ぎ、既に保存された編集の続きを促す。
                invocation = [*command[:-1], command[-1] + (
                    " A model service connection failed in the previous session. Review the edits already in the "
                    "workspace and continue the remaining work without duplicating completed changes."
                )]
            if observe := launch_input_observer.get():
                observe(invocation[-1])
            try:
                return _run_session(invocation, completion_path, token, targets,
                                    report_usage=report_usage, accept_result=accept_result, prompts=prompts)
            except JunieAcpModelUnavailable as exc:
                if unconfirmed_junie_stop(exc) is not None:
                    raise
                # 非対話モードでは操作ごとの確認がなくなるため、自動では切り替えない。
                dashboard = junie_dashboard_control()
                if dashboard is None or task_supported is False:
                    raise
                if task_supported is None and not _supports_task(command[0]):
                    raise
                if not _confirm_dashboard_launch(
                    dashboard, "Junie を非対話モードで再試行",
                    "ACP で利用できるモデルがありません。同じ作業フォルダーと指示で再試行できます。\n"
                    "非対話モードでは、Junie の操作ごとの許可確認はダッシュボードに表示されません。\n\n"
                    f"{completion_path.parent.resolve()}",
                    "headless", "今回のみ非対話モードで再試行",
                ):
                    raise
                report_activity("Junie を非対話モードで再試行します。")
                command = _task_command(command)
            if observe is not None:
                observe(invocation[-1])
            return _run_session([*command[:-1], invocation[-1]], completion_path, token, targets,
                                report_usage=report_usage, accept_result=accept_result, prompts=prompts)
        except JunieConnectionError as exc:
            if unconfirmed_junie_stop(exc) is not None:
                raise
            if attempt == len(delays):
                raise JunieConnectionError(
                    "Junie could not reach the model service after 2 retries. Resume this Run after the connection recovers."
                ) from exc
            print(f"Retrying Junie after a connection failure ({attempt + 1}/{len(delays)})", flush=True)
            report_activity("Junieの通信に失敗しました。再試行を待っています。")
            check_cancelled()
            if is_controlled():
                # 待機時間は維持しつつ、ダッシュボードの停止要求を短い間隔で確認する。
                for _ in range(delays[attempt] * 4):
                    time.sleep(0.25)
                    check_cancelled()
            else:
                time.sleep(delays[attempt])
            check_cancelled()


@dataclass
class _SteeringRequest:
    identifiers: list[str]
    receipt: Path
    token: str
    accepted: bool = False
    new_turn: bool = False


class _AcpSession:
    """ダッシュボード実行をACPの標準入出力につなぐ。"""

    def __init__(
        self, process: subprocess.Popen, command: list[str], project: Path, show_request: Callable,
        completion_path: Path, token: str, prompts: LivePrompts | None,
    ) -> None:
        """ACPセッションと入出力・完了状態を初期化する。"""
        self.process = process
        self.command = command
        self.project = project
        self.show_request = show_request
        self.messages: Queue[bytes] = Queue()
        self.closing = Event()
        self.outgoing: Queue[tuple[bytes, Future[None]] | None] = Queue()
        self.writes: deque[tuple[float, Future[None]]] = deque()
        self.session_id = None
        self.tool_calls: dict[str, dict] = {}
        self.pending: tuple[int | str, Future[str]] | None = None
        self.startup_deadline = time.monotonic() + ACP_TIMEOUT
        self.permission_started = 0.0
        self.prompts = prompts
        self.completion_path = completion_path
        self.token = token
        self.steering_supported = False
        self.steering_requests: dict[int, _SteeringRequest] = {}
        self.steering_waiting: int | None = None
        self.steering_deadline = 0.0
        self.next_request_id = 4
        self.prompt_finished = False
        self.steering_new_turn = False
        # stdoutの行待ちを別スレッドに任せ、監視側は停止要求や許可への回答を確認し続ける。
        self.reader = Thread(target=self._read, daemon=True)
        self.writer = Thread(target=self._write, daemon=True)
        self.reader.start()
        self.writer.start()

    def _read(self) -> None:
        """標準出力から次のACPメッセージを読み取る。"""
        try:
            for line in self.process.stdout:
                self.messages.put(line)
        finally:
            # キューが一時的に空の状態と、接続が終了した状態を区別するための印。
            self.messages.put(b"")

    def send(self, **message: Any) -> None:
        """監視を止めず、送信順にACPメッセージを登録する。"""
        data = (json.dumps({"jsonrpc": "2.0", **message}) + "\n").encode("utf-8")
        written: Future[None] = Future()
        self.writes.append((time.monotonic() + ACP_TIMEOUT, written))
        self.outgoing.put((data, written))

    def _write(self) -> None:
        """ブロックし得る書込みとパイプのクローズを送信側だけで行う。"""
        try:
            while (item := self.outgoing.get()) is not None and not self.closing.is_set():
                data, written = item
                try:
                    self.process.stdin.write(data)
                    self.process.stdin.flush()
                except OSError as exc:
                    written.set_exception(exc)
                    return
                written.set_result(None)
        finally:
            try:
                self.process.stdin.close()
            except OSError:
                pass

    def request(self, request_id: int, method: str, **params: Any) -> None:
        """応答を待つACPリクエストを送信する。"""
        self.send(id=request_id, method=method, params=params)

    def steer(self) -> None:
        """実行中のJunieへ追加指示を送る。"""
        if (self.prompts is None or not self.steering_supported or self.session_id is None
                or self.pending or self.steering_waiting is not None or self.prompt_finished
                or has_completion_record(self.completion_path, self.token)):
            return
        token = uuid.uuid4().hex
        receipt = safe_target(self.project, Path(".migration-scratch") / f"steering-{token}.txt")
        receipt.parent.mkdir(exist_ok=True)
        identifiers, text = self.prompts.take()
        if not identifiers:
            return
        # 元の完了記録との競合を避け、追加指示にも今回専用の完了記録を要求する。
        instruction = (
            "Operator follow-up for the current task. Apply this instruction now, within the original "
            "workspace and target constraints, preserving human edits and completed work:\n"
            f"{text}\n\nContinue the original assignment as well. After fully applying this follow-up, "
            f"write exactly {token} to `.migration-scratch/{receipt.name}`. "
            "Do not write this receipt while blocked or awaiting an operator answer. "
            f"After all work is complete, write exactly {self.token} to `{self.completion_path.name}` "
            "as the final file action, even if that file already exists."
        )
        try:
            observer = steering_input_observer.get()
            accepted = observer is None or observer(instruction)
        except BaseException:
            self.prompts.finish(identifiers, False)
            raise
        if not accepted:
            self.prompts.finish(identifiers, False)
            self.steering_supported = False
            return
        request_id = self.next_request_id
        self.next_request_id += 1
        self.steering_requests[request_id] = _SteeringRequest(identifiers, receipt, token)
        self.steering_waiting = request_id
        self.steering_deadline = time.monotonic() + ACP_TIMEOUT
        self.request(request_id, "_session/steering", sessionId=self.session_id,
                     prompt=[{"type": "text", "text": instruction}])
        report_activity("実行中のJunieへ追加指示を送っています。")

    def steering_complete(self) -> bool:
        """送信した追加指示が反映されたか判定する。"""
        return self.steering_waiting is None and all(
            request.accepted and not has_symlink_component(self.project, request.receipt.relative_to(self.project))
            and has_completion_record(request.receipt, request.token)
            for request in self.steering_requests.values()
        )

    def check_steering_turns(self, events: _JunieSessionEvents, *, exited: bool = False) -> None:
        """終わった追加ターンに反映の証拠がなければ確認待ちに戻す。"""
        for request in self.steering_requests.values():
            if request.new_turn and (exited or request.token in events.finished_steering):
                if (has_symlink_component(self.project, request.receipt.relative_to(self.project))
                        or not has_completion_record(request.receipt, request.token)):
                    raise JunieSessionError(
                        self.command, 1, "追加ターンが完了記録を残さず終了しました。指示を送信待ちに戻します。",
                    )
                request.new_turn = False
        self.steering_new_turn = any(request.new_turn for request in self.steering_requests.values())

    def finish_prompts(self, success: bool) -> None:
        """配信済み追加指示の処理結果を確定する。"""
        if self.prompts is not None:
            for request in self.steering_requests.values():
                self.prompts.finish(request.identifiers, success and request.accepted)

    def permission(self, request_id: int | str, params: dict) -> None:
        """Junieの操作許可要求を利用者へ渡して回答を返す。"""
        tool = params["toolCall"]
        # 許可要求だけでは省略される情報を、先に受け取ったツール更新から補う。
        details = {**self.tool_calls.get(tool["toolCallId"], {}), **tool}
        content = []
        for item in details.get("content") or []:
            block = item.get("content", {})
            if item.get("type") == "content" and block.get("type") == "text":
                content.append(block["text"])
        for location in details.get("locations") or []:
            content.append(location["path"])
        if details.get("rawInput") is not None:
            content.append(json.dumps(details["rawInput"], ensure_ascii=False, indent=2))
        options = [
            {"id": option["optionId"], "label": option["name"], "kind": option["kind"]}
            for option in params["options"] if isinstance(option.get("optionId"), str)
        ]
        if not options:
            self.send(id=request_id, error={"code": -32602, "message": "No permission options"})
            return
        reply = self.show_request({
            "title": "Junie の操作を確認",
            "detail": "\n\n".join([details.get("title") or "Junie が操作の許可を求めています。", *content]),
            "options": options,
        })
        if reply is None:
            check_cancelled()
            self.send(id=request_id, result={"outcome": {"outcome": "cancelled"}})
        else:
            self.pending = request_id, reply
            self.permission_started = time.monotonic()

    def _check_writes(self) -> None:
        """送信済み応答とパイプが詰まった場合の期限を確認する。"""
        while self.writes and self.writes[0][1].done():
            _, written = self.writes.popleft()
            if written.exception() is not None:
                raise JunieSessionError(self.command, 1, "JunieへのACP送信に失敗しました。") from written.exception()
        if self.writes and time.monotonic() >= self.writes[0][0]:
            raise JunieSessionError(self.command, 1, "JunieへのACP送信がタイムアウトしました。")

    def _accept_steering_response(self, request_id, message) -> None:
        """追加指示の受理を記録し、未受理ならキューに戻す。"""
        request = self.steering_requests[request_id]
        result = message.get("result")
        outcome = result.get("outcome") if isinstance(result, dict) else None
        self.steering_waiting = None
        if "error" not in message and outcome in ("injected", "startedNewTurn"):
            request.accepted = True
            request.new_turn = outcome == "startedNewTurn"
            self.steering_new_turn |= request.new_turn
            report_activity("Junieが追加指示を受け取りました。処理の完了を待っています。")
        else:
            self.steering_supported = False
            self.prompts.finish(request.identifiers, False)
            del self.steering_requests[request_id]
            report_activity("追加指示を送信待ちに戻しました。画面に表示されたタイミングで渡します。")

    def _advance_prompt(self, request_id, message) -> bool:
        """初期化、セッション作成、実行の応答を順に処理する。"""
        # 要求IDは1=初期化、2=セッション作成、3=プロンプト実行。応答を待って次へ進む。
        if "error" in message:
            error = message["error"]
            code = error.get("code")
            if request_id == 3 and code == -32002 and (error.get("data") or {}).get("reason") == "model_not_available":
                raise JunieAcpModelUnavailable(self.command, 1, "Junie の ACP で利用できるモデルがありません。")
            detail = ("Junie CLIでログインしてから再開してください。" if code == -32000 else
                      f"JunieのACP要求に失敗しました（要求 {request_id}、コード {code}）。")
            raise JunieSessionError(self.command, 1, detail)
        result = message.get("result", {})
        if request_id == 1 and result.get("protocolVersion") == 1:
            # バージョン番号では推測せず、接続先が宣言する対応能力で切り替える。
            metadata = result.get("_meta")
            steering = metadata.get("steering") if isinstance(metadata, dict) else None
            self.steering_supported = isinstance(steering, dict) and steering.get("supported") is True
            self.request(2, "session/new", cwd=str(self.project), mcpServers=[])
        elif request_id == 2 and result.get("sessionId"):
            self.session_id = result["sessionId"]
            self.request(3, "session/prompt", sessionId=self.session_id,
                         prompt=[{"type": "text", "text": self.command[-1]}])
        elif request_id == 3:
            self.prompt_finished = True
            if self.steering_waiting is None and not self.steering_new_turn:
                return True
        else:
            raise JunieSessionError(self.command, 1, "JunieのACP初期化応答が無効です。")
        return False

    def _receive_message(self, message) -> bool:
        """ACP応答を振り分け、プロンプトの終了を返す。"""
        request_id = message.get("id")
        method = message.get("method")
        params = message.get("params", {})
        if not method and request_id == self.steering_waiting and request_id is not None:
            self._accept_steering_response(request_id, message)
        elif method and request_id is not None:
            if method == "session/request_permission" and params.get("sessionId") == self.session_id:
                self.permission(request_id, params)
            else:
                self.send(id=request_id, error={"code": -32601, "message": "Method not supported"})
        elif method == "session/update" and params.get("sessionId") == self.session_id:
            update = params.get("update", {})
            if update.get("sessionUpdate") in ("tool_call", "tool_call_update") and "toolCallId" in update:
                self.tool_calls.setdefault(update["toolCallId"], {}).update(update)
        elif not method and request_id in (1, 2, 3):
            return self._advance_prompt(request_id, message)
        return False

    def advance(self) -> bool:
        """受信済みのメッセージを処理し、プロンプトの終了時にTrueを返す。"""
        self._check_writes()
        if self.pending:
            # 回答待ちではブロックせず監視側へ戻し、停止要求を受け付けられるようにする。
            request_id, reply = self.pending
            if not reply.done():
                return False
            check_cancelled()
            self.send(id=request_id, result={"outcome": {"outcome": "selected", "optionId": reply.result()}})
            self.pending = None
            self.steering_deadline += time.monotonic() - self.permission_started
        # 更新が続いても、プロセス監視や完了記録の確認へ定期的に制御を戻す。
        for _ in range(128):
            check_cancelled()
            try:
                line = self.messages.get_nowait()
            except Empty:
                break
            if not line:
                if self.prompt_finished and self.steering_waiting is None:
                    return True
                raise JunieSessionError(self.command, self.process.returncode or 1, "JunieのACP接続が終了しました。")
            try:
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise ValueError
            except (UnicodeError, ValueError):
                raise JunieSessionError(self.command, 1, "JunieのACP応答を読み取れませんでした。") from None
            if self._receive_message(message):
                return True
            if self.pending:
                return False
        if self.session_id is None and time.monotonic() >= self.startup_deadline:
            raise JunieSessionError(self.command, 1, "JunieのACP接続がタイムアウトしました。Junie CLIのバージョンとログを確認してください。")
        if self.steering_waiting is not None and time.monotonic() >= self.steering_deadline:
            raise JunieSessionError(self.command, 1, "追加指示の受信を確認できませんでした。指示を送信待ちに戻します。実行を再開してください。")
        return self.prompt_finished and self.steering_waiting is None and not self.steering_new_turn

    def cancel(self) -> None:
        """実行中のACPリクエストを取り消す。"""
        if self.pending:
            self.send(id=self.pending[0], result={"outcome": {"outcome": "cancelled"}})
        if self.session_id:
            self.send(method="session/cancel", params={"sessionId": self.session_id})

    def close(self) -> None:
        """ACP標準入出力とセッション資源を閉じる。"""
        self.closing.set()
        self.outgoing.put(None)
        self.writer.join(timeout=1)
        self.reader.join(timeout=1)
        if not self.reader.is_alive():
            self.process.stdout.close()


def _launch_session_process(command, *, headless, dashboard, thought_path):
    """モードに応じて標準入出力を構成し、監視対象のプロセスを返す。"""
    streams = {"stderr": subprocess.PIPE} if thought_path is not None else {}
    if headless:
        return _launch_process(command, owned=True, stdin=subprocess.DEVNULL, **streams)
    if dashboard is None:
        # 対話入力をJunieへ直接渡すため、端末の標準入出力を引き継ぐ。
        return _launch_process(command, owned=True, terminal=True)
    # ACPの指示は初期化後に送るため、起動時の引数から外す。
    acp_command = [part for part in command[:-2] if part != "--brave"] + ["--acp=true"]
    return _launch_process(acp_command, owned=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE, **streams)


def _run_session(
    command: list[str], completion_path: Path, token: str, targets: tuple[Path, ...],
    *, report_usage: Callable[[list[int]], None] | None = None,
    accept_result: Callable[[str], None] | None = None,
    prompts: LivePrompts | None = None,
) -> int:
    """起動から子プロセスの終了確認、使用量回収、端末の復元までを担当する。

    対話・ACPは完了記録と対象を確認して早期終了できる。非対話は自然終了を待つ。
    子プロセス群の終了後に終了observerへ通知し、終了時に残った使用量を回収する。
    """
    check_cancelled()
    root = completion_path.parent
    dashboard = junie_dashboard_control()
    headless = command[-2:-1] == ["--task"]
    restore_terminal = _save_terminal_mode() if dashboard is None and not headless else None
    process = None
    acp = None
    thoughts = None
    result_ready = False
    stopped = False

    def stop() -> int:
        """実行中のJunieセッションと子プロセスを停止する。"""
        nonlocal stopped
        if not stopped:
            try:
                code = _close_junie(process)
            except (OSError, subprocess.SubprocessError, ValueError) as exc:
                raise JunieStopError(
                    "Junieの終了を確認できませんでした。起動した端末で終了してから再開してください。"
                ) from exc
            stopped = True
            if observe := session_closed_observer.get():
                observe()
            return code
        return process.returncode

    def result_files_ready() -> bool:
        """セッション完了を確定できる状態か判定する。"""
        return (
            has_completion_record(completion_path, token)
            and (acp is None or acp.steering_complete())
            and all(
                path.is_file() and not has_symlink_component(root, path.relative_to(root))
                and path.resolve().is_relative_to(root.resolve()) for path in targets
            )
        )

    def collect_session_events(*, cancellable: bool = True) -> bool:
        """セッション終了とイベント更新を監視する。"""
        if thoughts is not None:
            collect_thoughts(thoughts.poll)
        failed = events.read_new_events(cancellable=cancellable)
        if report_usage is not None and any(events.usage):
            # 読み取った差分を渡したら集計を空にし、次回や終了時に二重加算しない。
            delta = events.usage
            events.usage = [0] * len(USAGE_FIELDS)
            report_usage(delta)
        # 明示した読み取り専用タスクだけ、最終回答を呼び出し元で検証・保存する。
        if cancellable and accept_result is not None and events.result is not None:
            result, events.result = events.result, None
            stop()
            check_cancelled()
            accept_result(result)
        return failed

    def collect_thoughts(read: Callable[[], None]) -> None:
        """セッション中に記録された思考表示を回収する。"""
        nonlocal thoughts
        try:
            read()
        except (OSError, ValueError):
            thoughts = None
            report_activity("Junie の詳細ログを保存できませんでした。")

    try:
        if dashboard is not None:
            project = str(root.resolve())
            if project not in dashboard.trusted_junie_workspaces:
                # ACPはプロジェクトの設定を信頼確認なしで読み込むため、起動前に確認する。
                if not _confirm_dashboard_launch(
                    dashboard, "Junie の作業フォルダーを確認",
                    f"このフォルダーの設定を読み込んで Junie を実行します。\n\n{project}",
                    "trust", "信頼して続行",
                ):
                    raise OperationCancelled("Junieの作業フォルダーの確認を取り消しました。")
                dashboard.trusted_junie_workspaces.add(project)
        events = _JunieSessionEvents(token, headless_project=root.resolve() if headless else None)
        next_poll = time.monotonic()
        thought_path = dashboard.junie_thoughts_path if dashboard is not None else None
        process = _launch_session_process(command, headless=headless, dashboard=dashboard, thought_path=thought_path)
        if dashboard is not None and not headless:
            acp = _AcpSession(process, command, Path(project), dashboard.on_junie_request,
                              completion_path, token, prompts)
            acp.request(1, "initialize", protocolVersion=1, clientCapabilities={})
        if thought_path is not None:
            thoughts = JunieThoughts(process.stderr, events.logs, events.existing_logs, thought_path, token)
        started = time.monotonic()
        report_activity("Junieのプロセスを起動しました。")
        next_heartbeat = started + HEARTBEAT_SECONDS
        if report_usage is not None:
            report_usage([0] * len(USAGE_FIELDS))
        while True:
            check_cancelled()
            if dashboard is not None and prompts is not None and prompts.needs_handoff():
                report_activity("保存した編集と追加指示を反映するため、現在のJunie修正を切り替えます。")
                raise JunieEditHandoff()
            try:
                if isinstance(process, JunieProcess):
                    process.wait_for_launcher(timeout=0.25)
                    exit_code = stop()
                    if process.terminal_interrupted():
                        raise KeyboardInterrupt
                else:
                    exit_code = process.wait(timeout=0.25)
            except subprocess.TimeoutExpired:
                check_cancelled()
                # 完了記録での早期終了は対話・ACPだけ。非対話モードはプロセスの自然終了を待つ。
                if not headless and result_files_ready():
                    exit_code = stop()
                    check_cancelled()
                    result_ready = result_files_ready()
                    return exit_code
                if time.monotonic() >= next_poll:
                    next_poll = time.monotonic() + 0.5
                    if collect_session_events():
                        stop()
                        raise JunieConnectionError("Junie model service connection failed")
                    if stopped:
                        result_ready = result_files_ready()
                        return 0 if acp is not None else process.returncode
                if acp is not None:
                    try:
                        prompt_finished = acp.advance()
                    except (JunieSessionError, KeyError, TypeError, AttributeError) as exc:
                        if collect_session_events() and not has_completion_record(completion_path, token):
                            raise JunieConnectionError("Junie model service connection failed") from exc
                        if isinstance(exc, JunieSessionError):
                            raise
                        raise JunieSessionError(command, 1, "JunieのACP応答に必要な項目がありません。") from None
                    if prompt_finished:
                        acp.check_steering_turns(events, exited=True)
                        stop()
                        check_cancelled()
                        if collect_session_events() and not has_completion_record(completion_path, token):
                            raise JunieConnectionError("Junie model service connection failed")
                        result_ready = result_files_ready()
                        return 0
                    acp.check_steering_turns(events)
                    acp.steer()
                    events.steering_tokens.update(request.token for request in acp.steering_requests.values())
                    dashboard.junie_steering = bool(prompts is not None and acp.steering_supported and acp.session_id
                                                   and not acp.pending and not acp.prompt_finished)
                now = time.monotonic()
                if now >= next_heartbeat:
                    elapsed = int(now - (events.last_event_at if events.last_event_at is not None else started))
                    detail = (
                        f"最後のイベントから{elapsed}秒" if events.last_event_at is not None
                        else f"起動から{elapsed}秒、セッションのイベントはまだ確認できていません"
                    )
                    report_activity(f"Junieのプロセスは起動中です（{detail}）。", kind="heartbeat")
                    next_heartbeat = now + HEARTBEAT_SECONDS
            else:
                check_cancelled()
                if acp is not None and acp.steering_requests:
                    # プロセスが先に終了しても、直前に届いた受信確認を取りこぼさない。
                    acp.reader.join(timeout=1)
                    acp.advance()
                if collect_session_events() and not has_completion_record(completion_path, token):
                    raise JunieConnectionError("Junie model service connection failed")
                if acp is not None:
                    acp.check_steering_turns(events, exited=True)
                # 終了コード0でも、対応するセッションも完了記録もなければ起動失敗として診断する。
                if events.path is None and not has_completion_record(completion_path, token):
                    raise JunieSessionError(
                        command, exit_code, events.startup_diagnostics(process.pid, command),
                    )
                result_ready = result_files_ready()
                return exit_code
    except JunieStopError:
        raise
    except BaseException as exc:
        if process is not None and not stopped and (isinstance(process, JunieProcess) or process.poll() is None):
            if acp is not None and isinstance(exc, KeyboardInterrupt):
                try:
                    acp.cancel()
                except OSError:
                    pass
            stop()
        raise
    finally:
        try:
            if thoughts is not None:
                collect_thoughts(thoughts.finish)
            if dashboard is not None:
                dashboard.junie_steering = False
                dashboard.on_junie_request(None)
            if acp is not None:
                try:
                    acp.finish_prompts(result_ready)
                finally:
                    acp.close()
            if process is not None and report_usage is not None:
                # 停止要求が出ていても最後の使用量を回収する。最終回答の採用処理は行わない。
                collect_session_events(cancellable=False)
        finally:
            if restore_terminal is not None and (process is None or process.poll() is not None):
                restore_terminal()


def _save_terminal_mode() -> Callable[[], None]:
    """強制終了で端末設定が残る場合に備えて、起動前の復元処理を作る。"""
    if os.name == "nt":
        import ctypes
        import msvcrt
        from ctypes import wintypes

        kernel = ctypes.windll.kernel32
        modes = []
        for stream in (sys.stdin, sys.stdout, sys.stderr):
            if stream.isatty():
                handle = wintypes.HANDLE(msvcrt.get_osfhandle(stream.fileno()))
                mode = wintypes.DWORD()
                if kernel.GetConsoleMode(handle, ctypes.byref(mode)):
                    modes.append((handle, mode))

        def restore() -> None:
            """Junieを起動する前の端末設定に戻す。"""
            for handle, mode in modes:
                kernel.SetConsoleMode(handle, mode)

        return restore

    import termios

    fd = sys.stdin.fileno()
    try:
        mode = termios.tcgetattr(fd)
    except termios.error:
        return lambda: None

    def restore() -> None:
        """Junieを起動する前の端末設定に戻す。"""
        termios.tcsetattr(fd, termios.TCSANOW, mode)

    return restore
