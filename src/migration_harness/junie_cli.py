"""Junie CLIの対話起動、通信再試行、セッションイベントの読み取り。"""

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
from collections.abc import Callable, Iterator
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue
from threading import Thread
from typing import Any

from .errors import format_error, safe_command
from .execution import (
    HEARTBEAT_SECONDS, OperationCancelled, check_cancelled, is_controlled, junie_dashboard_control, report_activity,
)
from .files import has_symlink_component


USAGE_FIELDS = ("input", "output", "cache_read", "cache_write", "responses")


class JunieConnectionError(OSError):
    """Junieがモデルサービスとの通信を再試行しても続行できなかった。"""


class JunieStopError(OSError):
    """終了を確認できなかったため、編集の反映や次の起動へ進めない。"""


class JunieLaunchError(OSError):
    """起動診断を文字列にも残し、保存済みRunから原因を確認できるようにする。"""

    def __init__(
        self, command: list[str], cwd: str | None, executable: str | None, cause: Exception,
        *, interpreter: str | None = None,
    ) -> None:
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
    """終了コードが0でも、対話セッションを開始できなかった終了を報告する。"""

    def __init__(self, command: list[str], exit_code: int, details: str) -> None:
        self.command = command.copy()
        self.returncode = exit_code
        super().__init__(
            f"Junie exited with code {exit_code} before starting a matching session or writing a completion record.\n"
            + launch_diagnostics(command) + "\n" + details
        )


def _resolve_executable(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise FileNotFoundError(errno.ENOENT, "Junie CLI was not found on PATH (or is not executable)", name)
    # Preserve the launcher path: resolving a symlink can change a wrapper's installation directory.
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
    streams = (
        "Junie runs over ACP. Check the dashboard for permission requests and the log file "
        if junie_dashboard_control() else
        "Junie inherits this terminal's stdin/stdout/stderr. Check its output and the log file "
    )
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


def build_command(project: Path, prompt: str, max_steps: int | None, *, guidelines: Path | None = None) -> list[str]:
    command = ["junie", "--project", str(project)]
    if guidelines is not None:
        # 本文をコマンド引数に展開せず、既存のJunie指示に追加する（Windowsの長さ制限も回避）。
        command.extend(["--ide-guidelines", str(guidelines)])
    if max_steps is not None:
        command.extend(["--max-steps", str(max_steps)])
    # 再試行時に続きを促す文を付けるため、プロンプトを末尾に置く。
    return [*command, "--prompt", prompt]


def _windows_batch_command(command: list[str]) -> tuple[str, str, dict[str, str]]:
    """本文とファイルパスはBATの引数処理を避け、Junieの公式環境変数で渡す。"""
    environment = os.environ.copy()
    # os.environ is case-insensitive on Windows; its plain-dict copy is not.
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
            "--prompt": "JUNIE_PROMPT", "--project": "JUNIE_PROJECT", "-p": "JUNIE_PROJECT",
            "--ide-guidelines": "JUNIE_IDE_GUIDELINES",
        }.get(option)
        if variable:
            if not separator:
                try:
                    value = next(remaining)
                except StopIteration:
                    raise ValueError(f"{option} requires a value") from None
            # cmd silently drops inherited variables over its 8191-character limit.
            if len((variable + "=" + value).encode("utf-16-le")) // 2 > 8191:
                raise ValueError(f"{option} exceeds the Windows batch launcher limit; shorten it or use a native Junie executable")
            if "\0" in value:
                raise ValueError(f"{option} contains a NUL character")
            environment[variable] = value
            continue
        # Remaining harness arguments are flags/numbers. Reject text that could break cmd quotes.
        if any(character in argument for character in ('"', "\r", "\n", "\0")):
            raise ValueError("A Windows batch launcher argument contains quotes or control characters; use --prompt for prompt text")
        name = f"PHP_TO_CSHARP_JUNIE_ARG_{len(arguments)}"
        # Batch launchers forward %* to a native binary, whose argv parser consumes trailing backslashes.
        trailing = len(argument) - len(argument.rstrip("\\"))
        environment[name] = argument + "\\" * trailing
        arguments.append(f'"%{name}%"')

    if any(character in command[0] for character in ('"', "\r", "\n", "\0")):
        raise ValueError("The Junie batch launcher path contains invalid characters")
    expanded_size = sum(len(environment[item[2:-2]].encode("utf-16-le")) // 2 + 3 for item in arguments)
    if expanded_size > 8191:
        raise ValueError("Junie arguments exceed the Windows batch command limit; shorten the paths or arguments")
    # No CALL (second expansion), AutoRun, delayed !expansion!, or shell=True argv conversion.
    line = subprocess.list2cmdline([interpreter]) + ' /d /v:off /s /c "' + " ".join(arguments) + '"'
    return line, interpreter, environment


def _launch_process(command: list[str], **streams: Any) -> subprocess.Popen:
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
            line, interpreter, environment = _windows_batch_command(resolved)
            return subprocess.Popen(line, executable=interpreter, cwd=cwd, env=environment, **streams)
        return subprocess.Popen(resolved, cwd=cwd, **streams)
    except (OSError, ValueError) as exc:
        raise JunieLaunchError(command, cwd, executable, exc, interpreter=interpreter) from exc


def has_completion_record(path: Path, token: str) -> bool:
    """現在の呼び出しが残した完了トークンだけを受理する。"""
    if path.is_symlink() or not path.is_file():
        return False
    expected = token.encode("ascii")
    try:
        with path.open("rb") as stream:
            value = stream.read(len(expected) + 3)
    except OSError:
        return False
    return value in (expected, expected + b"\n", expected + b"\r\n")


def _close_junie(process: subprocess.Popen) -> int:
    """Junieの終了を待ち、次のカプセルと端末操作が重ならないようにする。"""
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
            # Keep Junie in the foreground terminal group; kill only its own descendants.
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
    identity: tuple[int, int]
    offset: int = 0
    pending: bytes = b""
    startup_boundary: bool = False
    discard: bool = False


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
            return f"Junieのツール実行: {tool}"
    return "Junieのセッションに更新がありました。"


class _JunieSessionEvents:
    """今回のプロンプトと照合できたJunieのセッションだけを監視する。"""

    def __init__(self, token: str) -> None:
        self.token = token
        try:
            self.launch_cwd = os.getcwd()
        except OSError:
            self.launch_cwd = None
        self.sessions = Path(os.environ.get("JUNIE_HOME") or Path.home() / ".junie").expanduser() / "sessions"
        self.logs = self.sessions.parent / "logs"
        self.existing_logs = set(self._log_paths())
        self.cursors: dict[Path, _EventCursor] = {}
        for path in self._paths():
            try:
                stat = path.stat()
            except OSError:
                continue
            self.cursors[path] = _EventCursor(
                (stat.st_dev, stat.st_ino), stat.st_size, startup_boundary=bool(stat.st_size),
            )
        self.path: Path | None = None
        self.task: str | None = None
        self.usage = [0] * len(USAGE_FIELDS)
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
        # Both Windows and Unix launchers can spawn a child with a different PID.
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
                    # Only use secret values for redaction; never include environment keys or dumps.
                    secrets = [value for name, value in os.environ.items() if value and re.search(
                        r"TOKEN|PASSWORD|PASSWD|SECRET|CREDENTIAL|API_?KEY|AUTH|JUNIE_(PROMPT|TASK)", name, re.I,
                    )]
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
            cursor = self.cursors[path] = _EventCursor(identity)
            if path == self.path:
                # A replacement log must match the launch token again before being trusted.
                self.path = None
                self.task = None
        if stat.st_size == cursor.offset:
            return
        with path.open("rb") as stream:
            if cursor.startup_boundary:
                stream.seek(cursor.offset - 1)
                cursor.discard = stream.read(1) != b"\n"
                cursor.startup_boundary = False
            stream.seek(cursor.offset)
            # Bound this poll to its initial size even if Junie keeps appending.
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
                        if not isinstance(prompt, str) or self.token not in prompt:
                            continue
                        self.path = path
                    if self.path != path:
                        continue
                    if kind == "TaskStartedEvent":
                        self.task = entry.get("taskId")
                    if entry.get("taskId") is not None and entry.get("taskId") != self.task:
                        continue
                    if self.task and entry.get("taskId") == self.task and kind == "SessionA2uxEvent":
                        self._count_usage(entry.get("event"))
                    self.last_event_at = time.monotonic()
                    message = _event_activity(entry)
                    # Coalesce repeated status updates in one poll; usage still counts every response.
                    if message not in reported:
                        report_activity(message)
                        reported.add(message)
                    if self.task and entry.get("taskId") == self.task and _is_junie_connection_failure(entry):
                        return True
            except OSError:
                continue
            if self.path is not None:
                break
        return False

    def _count_usage(self, event: Any) -> None:
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


def run_interactive(
    command: list[str], completion_path: Path, token: str, targets: tuple[Path, ...],
    *, report_usage: Callable[[list[int]], None] | None = None,
) -> int:
    """Junie自身の通信復旧が失敗したときは同じ作業場所で起動し直す。"""
    delays = (1, 3)
    for attempt in range(len(delays) + 1):
        check_cancelled()
        try:
            invocation = command
            if attempt:
                invocation = [*command[:-1], command[-1] + (
                    " A model service connection failed in the previous session. Review the edits already in the "
                    "workspace and continue the remaining work without duplicating completed changes."
                )]
            return _run_session(invocation, completion_path, token, targets, report_usage=report_usage)
        except JunieConnectionError as exc:
            if attempt == len(delays):
                raise JunieConnectionError(
                    "Junie could not reach the model service after 2 retries. Resume this Run after the connection recovers."
                ) from exc
            print(f"Retrying Junie after a connection failure ({attempt + 1}/{len(delays)})", flush=True)
            report_activity("Junieの通信に失敗しました。再試行を待っています。")
            check_cancelled()
            if is_controlled():
                for _ in range(delays[attempt] * 4):
                    time.sleep(0.25)
                    check_cancelled()
            else:
                time.sleep(delays[attempt])
            check_cancelled()


class _AcpSession:
    """ダッシュボード実行をACPの標準入出力につなぐ。"""

    def __init__(self, process: subprocess.Popen, command: list[str], project: Path, show_request: Callable) -> None:
        self.process = process
        self.command = command
        self.project = project
        self.show_request = show_request
        self.messages: Queue[bytes] = Queue()
        self.session_id = None
        self.tool_calls: dict[str, dict] = {}
        self.pending: tuple[int | str, Future[str]] | None = None
        self.startup_deadline = time.monotonic() + 30
        self.reader = Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self) -> None:
        try:
            for line in self.process.stdout:
                self.messages.put(line)
        finally:
            self.messages.put(b"")

    def send(self, **message: Any) -> None:
        self.process.stdin.write((json.dumps({"jsonrpc": "2.0", **message}) + "\n").encode("utf-8"))
        self.process.stdin.flush()

    def request(self, request_id: int, method: str, **params: Any) -> None:
        self.send(id=request_id, method=method, params=params)

    def permission(self, request_id: int | str, params: dict) -> None:
        tool = params["toolCall"]
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

    def advance(self) -> bool:
        """受信済みのメッセージを処理し、プロンプトの終了時にTrueを返す。"""
        if self.pending:
            request_id, reply = self.pending
            if not reply.done():
                return False
            check_cancelled()
            self.send(id=request_id, result={"outcome": {"outcome": "selected", "optionId": reply.result()}})
            self.pending = None
        for _ in range(128):
            check_cancelled()
            try:
                line = self.messages.get_nowait()
            except Empty:
                break
            if not line:
                raise JunieSessionError(self.command, self.process.poll() or 1, "JunieのACP接続が終了しました。")
            try:
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise ValueError
            except (UnicodeError, ValueError):
                raise JunieSessionError(self.command, 1, "JunieのACP応答を読み取れませんでした。") from None
            request_id = message.get("id")
            method = message.get("method")
            params = message.get("params", {})
            if method and request_id is not None:
                if method == "session/request_permission" and params.get("sessionId") == self.session_id:
                    self.permission(request_id, params)
                    if self.pending:
                        return False
                else:
                    self.send(id=request_id, error={"code": -32601, "message": "Method not supported"})
            elif method == "session/update" and params.get("sessionId") == self.session_id:
                update = params.get("update", {})
                if update.get("sessionUpdate") in ("tool_call", "tool_call_update") and "toolCallId" in update:
                    self.tool_calls.setdefault(update["toolCallId"], {}).update(update)
            elif not method and request_id in (1, 2, 3):
                if "error" in message:
                    code = message["error"].get("code")
                    detail = ("Junie CLIでログインしてから再開してください。" if code == -32000 else
                              f"JunieのACP要求に失敗しました（要求 {request_id}、コード {code}）。")
                    raise JunieSessionError(self.command, 1, detail)
                result = message.get("result", {})
                if request_id == 1 and result.get("protocolVersion") == 1:
                    self.request(2, "session/new", cwd=str(self.project), mcpServers=[])
                elif request_id == 2 and result.get("sessionId"):
                    self.session_id = result["sessionId"]
                    self.request(3, "session/prompt", sessionId=self.session_id,
                                 prompt=[{"type": "text", "text": self.command[-1]}])
                elif request_id == 3:
                    return True
                else:
                    raise JunieSessionError(self.command, 1, "JunieのACP初期化応答が無効です。")
        if self.session_id is None and time.monotonic() >= self.startup_deadline:
            raise JunieSessionError(self.command, 1, "JunieのACP接続がタイムアウトしました。Junie CLIのバージョンとログを確認してください。")
        return False

    def cancel(self) -> None:
        if self.pending:
            self.send(id=self.pending[0], result={"outcome": {"outcome": "cancelled"}})
        if self.session_id:
            self.send(method="session/cancel", params={"sessionId": self.session_id})

    def close(self) -> None:
        self.process.stdin.close()
        self.reader.join(timeout=1)
        if not self.reader.is_alive():
            self.process.stdout.close()


def _run_session(
    command: list[str], completion_path: Path, token: str, targets: tuple[Path, ...],
    *, report_usage: Callable[[list[int]], None] | None = None,
) -> int:
    """成果物と完了記録がそろえばJunieのセッションを閉じる。"""
    check_cancelled()
    root = completion_path.parent
    dashboard = junie_dashboard_control()
    restore_terminal = _save_terminal_mode() if dashboard is None else None
    process = None
    acp = None

    def poll(*, cancellable: bool = True) -> bool:
        failed = events.read_new_events(cancellable=cancellable)
        if report_usage is not None and any(events.usage):
            delta = events.usage
            events.usage = [0] * len(USAGE_FIELDS)
            report_usage(delta)
        return failed

    try:
        if dashboard is not None:
            project = str(root.resolve())
            if project not in dashboard.trusted_junie_workspaces:
                # ACPはプロジェクトの設定を信頼確認なしで読み込むため、起動前に確認する。
                reply = dashboard.on_junie_request({
                    "title": "Junie の作業フォルダーを確認",
                    "detail": f"このフォルダーの設定を読み込んで Junie を実行します。\n\n{project}",
                    "options": [
                        {"id": "stop", "label": "停止", "kind": "reject_once"},
                        {"id": "trust", "label": "信頼して続行", "kind": "allow_once"},
                    ],
                })
                check_cancelled()
                while not reply.done():
                    dashboard.cancelled.wait(0.25)
                    check_cancelled()
                if reply.result() != "trust":
                    raise OperationCancelled("Junieの作業フォルダーの確認を取り消しました。")
                dashboard.trusted_junie_workspaces.add(project)
        events = _JunieSessionEvents(token)
        next_poll = time.monotonic()
        if dashboard is None:
            process = _launch_process(command)
        else:
            acp_command = [part for part in command[:-2] if part != "--brave"] + ["--acp=true"]
            process = _launch_process(acp_command, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            acp = _AcpSession(process, command, Path(project), dashboard.on_junie_request)
            acp.request(1, "initialize", protocolVersion=1, clientCapabilities={})
        started = time.monotonic()
        report_activity("Junieのプロセスを起動しました。")
        next_heartbeat = started + HEARTBEAT_SECONDS
        if report_usage is not None:
            report_usage([0] * len(USAGE_FIELDS))
        while True:
            check_cancelled()
            try:
                exit_code = process.wait(timeout=0.25)
            except subprocess.TimeoutExpired:
                check_cancelled()
                if (
                    has_completion_record(completion_path, token)
                    and all(
                        path.is_file() and not has_symlink_component(root, path.relative_to(root))
                        and path.resolve().is_relative_to(root.resolve())
                        for path in targets
                    )
                ):
                    return _close_junie(process)
                if time.monotonic() >= next_poll:
                    next_poll = time.monotonic() + 0.5
                    if poll():
                        _close_junie(process)
                        raise JunieConnectionError("Junie model service connection failed")
                if acp is not None:
                    try:
                        finished = acp.advance()
                    except (JunieSessionError, KeyError, TypeError, AttributeError) as exc:
                        if poll() and not has_completion_record(completion_path, token):
                            raise JunieConnectionError("Junie model service connection failed") from exc
                        if isinstance(exc, JunieSessionError):
                            raise
                        raise JunieSessionError(command, 1, "JunieのACP応答に必要な項目がありません。") from None
                    if finished:
                        _close_junie(process)
                        if poll() and not has_completion_record(completion_path, token):
                            raise JunieConnectionError("Junie model service connection failed")
                        return 0
                now = time.monotonic()
                if now >= next_heartbeat and process.poll() is None:
                    elapsed = int(now - (events.last_event_at if events.last_event_at is not None else started))
                    detail = (
                        f"最後のイベントから{elapsed}秒" if events.last_event_at is not None
                        else f"起動から{elapsed}秒、セッションのイベントはまだ確認できていません"
                    )
                    report_activity(f"Junieのプロセスは起動中です（{detail}）。", kind="heartbeat")
                    next_heartbeat = now + HEARTBEAT_SECONDS
            else:
                check_cancelled()
                if poll() and not has_completion_record(completion_path, token):
                    raise JunieConnectionError("Junie model service connection failed")
                if events.path is None and not has_completion_record(completion_path, token):
                    raise JunieSessionError(
                        command, exit_code, events.startup_diagnostics(process.pid, command),
                    )
                return exit_code
    except (KeyboardInterrupt, OSError) as exc:
        if process is not None and process.poll() is None:
            try:
                if acp is not None and isinstance(exc, KeyboardInterrupt):
                    try:
                        acp.cancel()
                    except OSError:
                        pass
                _close_junie(process)
            except OSError as cleanup_error:
                raise JunieStopError(
                    "Junieの終了を確認できませんでした。起動した端末で終了してから再開してください。"
                ) from cleanup_error
        raise
    finally:
        try:
            if dashboard is not None:
                dashboard.on_junie_request(None)
            if acp is not None and process.poll() is not None:
                acp.close()
            if process is not None and report_usage is not None:
                poll(cancellable=False)
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
