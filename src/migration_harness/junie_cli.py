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
from collections.abc import Callable, Iterator
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue
from threading import Thread
from typing import Any

from .errors import format_error, safe_command
from .execution import (
    HEARTBEAT_SECONDS, ExecutionControl, OperationCancelled, check_cancelled, is_controlled,
    junie_dashboard_control, report_activity,
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
    """終了コードが0でも、セッションを開始できなかった終了を報告する。"""

    def __init__(self, command: list[str], exit_code: int, details: str) -> None:
        self.command = command.copy()
        self.returncode = exit_code
        super().__init__(
            f"Junie exited with code {exit_code} before starting a matching session or writing a completion record.\n"
            + launch_diagnostics(command) + "\n" + details
        )


class JunieAcpModelUnavailable(JunieSessionError):
    """ACPがモデル不足のためプロンプトを処理する前に拒否した。"""


def _resolve_executable(name: str) -> str:
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


def build_command(project: Path, prompt: str, max_steps: int | None, *, guidelines: Path | None = None) -> list[str]:
    command = ["junie", "--project", str(project), "--brave"]
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
            resolved, interpreter, environment = _windows_batch_command(resolved)
            streams["executable"] = interpreter
        else:
            environment = os.environ.copy()
        # JunieのPythonツールが補助モジュールを読んでも、読み取り専用資料の隣にキャッシュを作らせない。
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
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
            return f"Junieのツール実行: {tool}"
    return "Junieのセッションに更新がありました。"


class _JunieSessionEvents:
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
                    if kind == "TaskStartedEvent":
                        self.task = entry.get("taskId")
                        self.result = None
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
                            and agent.get("cancelled") is False
                            and isinstance(agent.get("agent"), dict) and agent["agent"].get("kind") == "MainAgent"
                            and isinstance(agent.get("result"), str)
                        ):
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
    return [part for part in command[:-2] if part != "--brave"] + ["--task", command[-1]]


def run_interactive(
    command: list[str], completion_path: Path, token: str, targets: tuple[Path, ...],
    *, report_usage: Callable[[list[int]], None] | None = None,
    accept_result: Callable[[str], None] | None = None,
    prefer_task: bool = False,
) -> int:
    """Junieを選択したモードで起動し、通信障害時は同じ作業場所で再試行する。"""
    # 終了コードだけでは移行完了を保証しない。呼び出し元でも完了記録と成果物を検証する。
    task_supported = None
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
            try:
                return _run_session(invocation, completion_path, token, targets,
                                    report_usage=report_usage, accept_result=accept_result)
            except JunieAcpModelUnavailable:
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
            return _run_session([*command[:-1], invocation[-1]], completion_path, token, targets,
                                report_usage=report_usage, accept_result=accept_result)
        except JunieConnectionError as exc:
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
        # stdoutの行待ちを別スレッドに任せ、監視側は停止要求や許可への回答を確認し続ける。
        self.reader = Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self) -> None:
        try:
            for line in self.process.stdout:
                self.messages.put(line)
        finally:
            # キューが一時的に空の状態と、接続が終了した状態を区別するための印。
            self.messages.put(b"")

    def send(self, **message: Any) -> None:
        self.process.stdin.write((json.dumps({"jsonrpc": "2.0", **message}) + "\n").encode("utf-8"))
        self.process.stdin.flush()

    def request(self, request_id: int, method: str, **params: Any) -> None:
        self.send(id=request_id, method=method, params=params)

    def permission(self, request_id: int | str, params: dict) -> None:
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

    def advance(self) -> bool:
        """受信済みのメッセージを処理し、プロンプトの終了時にTrueを返す。"""
        if self.pending:
            # 回答待ちではブロックせず監視側へ戻し、停止要求を受け付けられるようにする。
            request_id, reply = self.pending
            if not reply.done():
                return False
            check_cancelled()
            self.send(id=request_id, result={"outcome": {"outcome": "selected", "optionId": reply.result()}})
            self.pending = None
        # 更新が続いても、プロセス監視や完了記録の確認へ定期的に制御を戻す。
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
    accept_result: Callable[[str], None] | None = None,
) -> int:
    """成果物と完了記録がそろえばJunieのセッションを閉じる。"""
    check_cancelled()
    root = completion_path.parent
    dashboard = junie_dashboard_control()
    headless = command[-2:-1] == ["--task"]
    restore_terminal = _save_terminal_mode() if dashboard is None and not headless else None
    process = None
    acp = None

    def poll(*, cancellable: bool = True) -> bool:
        failed = events.read_new_events(cancellable=cancellable)
        if report_usage is not None and any(events.usage):
            # 読み取った差分を渡したら集計を空にし、次回や終了時に二重加算しない。
            delta = events.usage
            events.usage = [0] * len(USAGE_FIELDS)
            report_usage(delta)
        # 明示した読み取り専用タスクだけ、最終回答を呼び出し元で検証・保存する。
        if cancellable and accept_result is not None and events.result is not None:
            result, events.result = events.result, None
            accept_result(result)
        return failed

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
        if headless:
            process = _launch_process(command, stdin=subprocess.DEVNULL)
        elif dashboard is None:
            # 対話入力をJunieへ直接渡すため、端末の標準入出力を引き継ぐ。
            process = _launch_process(command)
        else:
            # ACPではプロンプトを起動引数から外し、初期化後のsession/prompt要求で送る。
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
                # 完了記録での早期終了は対話・ACPだけ。非対話モードはプロセスの自然終了を待つ。
                if (
                    not headless and has_completion_record(completion_path, token)
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
                # 終了コード0でも、対応するセッションも完了記録もなければ起動失敗として診断する。
                if events.path is None and not has_completion_record(completion_path, token):
                    raise JunieSessionError(
                        command, exit_code, events.startup_diagnostics(process.pid, command),
                    )
                return exit_code
    except (KeyboardInterrupt, OSError, ValueError) as exc:
        if process is not None and process.poll() is None:
            try:
                if acp is not None and isinstance(exc, KeyboardInterrupt):
                    try:
                        acp.cancel()
                    except OSError:
                        pass
                _close_junie(process)
            except OSError as cleanup_error:
                # 書き込みが続いている可能性があるため、通常の通信再試行には進ませない。
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
                # 停止要求が出ていても最後の使用量を回収する。最終回答の採用処理は行わない。
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
