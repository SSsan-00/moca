"""Junie CLIの対話起動、通信再試行、セッションイベントの読み取り。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from .files import has_symlink_component


USAGE_FIELDS = ("input", "output", "cache_read", "cache_write", "responses")


class JunieConnectionError(OSError):
    """Junieがモデルサービスとの通信を再試行しても続行できなかった。"""


def require_interactive_cli() -> None:
    """実行前に対話端末とJunie CLIが利用できることを確認する。"""
    if not sys.stdin.isatty():
        raise RuntimeError("Junie run requires an interactive terminal")
    if shutil.which("junie") is None:
        raise RuntimeError("Junie CLI was not found on PATH")


def build_command(project: Path, prompt: str, max_steps: int | None) -> list[str]:
    command = ["junie", "--project", str(project)]
    if max_steps is not None:
        command.extend(["--max-steps", str(max_steps)])
    # 再試行時に続きを促す文を付けるため、プロンプトを末尾に置く。
    return [*command, "--prompt", prompt]


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
        self.sessions = Path(os.environ.get("JUNIE_HOME") or Path.home() / ".junie").expanduser() / "sessions"
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
        except JunieConnectionError:
            if attempt == len(delays):
                raise JunieConnectionError(
                    "Junie could not reach the model service after 2 retries. Resume this Run after the connection recovers."
                ) from None
            print(f"Retrying Junie after a connection failure ({attempt + 1}/{len(delays)})", flush=True)
            time.sleep(delays[attempt])


def _run_session(
    command: list[str], completion_path: Path, token: str, targets: tuple[Path, ...],
    *, report_usage: Callable[[list[int]], None] | None = None,
) -> int:
    """対話用の端末を引き継ぎ、成果物と完了記録がそろえばセッションを閉じる。"""
    root = completion_path.parent
    restore_terminal = _save_terminal_mode()
    events = _JunieSessionEvents(token)

    def poll() -> bool:
        failed = events.read_new_events()
        if report_usage is not None and any(events.usage):
            delta = events.usage
            events.usage = [0] * len(USAGE_FIELDS)
            report_usage(delta)
        return failed

    next_poll = time.monotonic()
    # 標準入出力を引き継ぎ、承認や追加入力はJunieの画面で受け付ける。
    process = subprocess.Popen(command)
    try:
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
                return exit_code
    except (KeyboardInterrupt, OSError):
        if process.poll() is None:
            _close_junie(process)
        raise
    finally:
        try:
            if report_usage is not None:
                poll()
        finally:
            if process.poll() is not None:
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
