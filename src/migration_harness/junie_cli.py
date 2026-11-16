"""Junie CLIの対話起動、通信再試行、セッションイベントの読み取り。"""

from __future__ import annotations

import errno
import json
import ntpath
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from .errors import format_error, safe_command
from .files import has_symlink_component


USAGE_FIELDS = ("input", "output", "cache_read", "cache_write", "responses")


class JunieConnectionError(OSError):
    """Junieがモデルサービスとの通信を再試行しても続行できなかった。"""


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
    return (
        f"Command: {safe_command(command)}\nCWD: {cwd}\nExecutable: {executable}\n"
        "Junie inherits this terminal's stdin/stdout/stderr. Check its output and the log file "
        "reported at startup (normally under JUNIE_HOME/logs or ~/.junie/logs)."
    )


def require_interactive_cli() -> None:
    """実行前に対話端末とJunie CLIが利用できることを確認する。"""
    if not sys.stdin.isatty():
        raise RuntimeError("Junie run requires an interactive terminal")
    try:
        _resolve_executable("junie")
    except OSError as exc:
        raise JunieLaunchError(["junie"], None, None, exc) from exc


def build_command(project: Path, prompt: str, max_steps: int | None) -> list[str]:
    command = ["junie", "--project", str(project)]
    if max_steps is not None:
        command.extend(["--max-steps", str(max_steps)])
    # 再試行時に続きを促す文を付けるため、プロンプトを末尾に置く。
    return [*command, "--prompt", prompt]


def _windows_batch_command(command: list[str]) -> tuple[str, str, dict[str, str]]:
    """cmdの展開対象にプロンプトを含めず、Junieの公式環境変数で渡す。"""
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
        variable = {"--prompt": "JUNIE_PROMPT", "--project": "JUNIE_PROJECT", "-p": "JUNIE_PROJECT"}.get(option)
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


def _launch_process(command: list[str]) -> subprocess.Popen:
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
            return subprocess.Popen(line, executable=interpreter, cwd=cwd, env=environment)
        return subprocess.Popen(resolved, cwd=cwd)
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
        self.existing = set(self._paths())
        self.path: Path | None = None
        self.offset = 0
        self.task: str | None = None
        self.usage = [0] * len(USAGE_FIELDS)

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

    def read_new_events(self) -> bool:
        """今回のセッションの新着行から、使用量と通信失敗を読む。"""
        paths = [self.path] if self.path else [path for path in self._paths() if path not in self.existing]
        for path in paths:
            try:
                with path.open("rb") as stream:
                    stream.seek(self.offset if self.path else 0)
                    for line in iter(stream.readline, b""):
                        if not line.endswith(b"\n"):
                            break
                        try:
                            entry = json.loads(line)
                        except (ValueError, UnicodeError):
                            continue
                        if not isinstance(entry, dict):
                            continue
                        kind = entry.get("kind")
                        if self.path is None:
                            # 別の端末で起動したJunieの使用量は、このRunに含めない。
                            if kind != "UserPromptEvent":
                                continue
                            if self.token not in str(entry.get("prompt", "")):
                                self.existing.add(path)
                                break
                            self.path = path
                        self.offset = stream.tell()
                        if kind == "TaskStartedEvent":
                            self.task = entry.get("taskId")
                        if self.task and entry.get("taskId") == self.task:
                            if kind == "SessionA2uxEvent":
                                self._count_usage(entry.get("event"))
                            if _is_junie_connection_failure(entry):
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
            time.sleep(delays[attempt])


def _run_session(
    command: list[str], completion_path: Path, token: str, targets: tuple[Path, ...],
    *, report_usage: Callable[[list[int]], None] | None = None,
) -> int:
    """対話用の端末を引き継ぎ、成果物と完了記録がそろえばセッションを閉じる。"""
    root = completion_path.parent
    restore_terminal = _save_terminal_mode()
    process = None

    def poll() -> bool:
        failed = events.read_new_events()
        if report_usage is not None and any(events.usage):
            delta = events.usage
            events.usage = [0] * len(USAGE_FIELDS)
            report_usage(delta)
        return failed

    try:
        events = _JunieSessionEvents(token)
        next_poll = time.monotonic()
        # 標準入出力を引き継ぎ、承認や追加入力はJunieの画面で受け付ける。
        process = _launch_process(command)
        if report_usage is not None:
            report_usage([0] * len(USAGE_FIELDS))
        while True:
            try:
                exit_code = process.wait(timeout=0.25)
            except subprocess.TimeoutExpired:
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
                    next_poll = time.monotonic() + 1
                    if poll():
                        _close_junie(process)
                        raise JunieConnectionError("Junie model service connection failed")
            else:
                if poll() and not has_completion_record(completion_path, token):
                    raise JunieConnectionError("Junie model service connection failed")
                if events.path is None and not has_completion_record(completion_path, token):
                    raise JunieSessionError(
                        command, exit_code, events.startup_diagnostics(process.pid, command),
                    )
                return exit_code
    except (KeyboardInterrupt, OSError):
        if process is not None and process.poll() is None:
            _close_junie(process)
        raise
    finally:
        try:
            if process is not None and report_usage is not None:
                poll()
        finally:
            if process is None or process.poll() is not None:
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
