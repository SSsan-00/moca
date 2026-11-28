"""Junie CLI の引数変換、Windows ランチャーの起動とエラー診断。"""

from __future__ import annotations

import errno
import ntpath
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from ..errors import format_error, safe_command
from ..execution import check_cancelled, junie_dashboard_control
from .junie_process import JunieProcess


class JunieLaunchError(OSError):

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

    def __init__(self, command: list[str], exit_code: int, details: str) -> None:
        self.details = details
        super().__init__(command, exit_code, details)


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


def _task_command(command: list[str]) -> list[str]:
    return [part for part in command[:-2] if part != "--brave"] + ["--task", command[-1]]


def acp_command(command: list[str]) -> list[str]:
    return [part for part in command[:-2] if part != "--brave"] + ["--acp=true"]


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


def _launch_process(command: list[str], *, owned=False, cwd: Path | None = None, **streams: Any) -> subprocess.Popen:
    check_cancelled()
    executable = None
    interpreter = None
    try:
        cwd = str(cwd) if cwd is not None else os.getcwd()
        if not command:
            raise ValueError("Junie command is empty")
        executable = _resolve_executable(command[0])
        resolved = [executable, *command[1:]]
        if ntpath.splitext(executable)[1].lower() in {".bat", ".cmd"}:
            resolved, interpreter, environment = _windows_batch_command(resolved)
            streams["executable"] = interpreter
        else:
            environment = os.environ.copy()
        # JunieのPythonツールが補助モジュールを読んでも、読み取り専用資料の隣にキャッシュを作らせない。
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        if owned:
            return JunieProcess(resolved, cwd=cwd, env=environment, **streams)
        return subprocess.Popen(resolved, cwd=cwd, env=environment, **streams)
    except (OSError, ValueError) as exc:
        raise JunieLaunchError(command, cwd, executable, exc, interpreter=interpreter) from exc
