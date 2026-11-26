"""Actionable diagnostics without source lines, locals, or environment inspection."""

from __future__ import annotations

import json
import re
import shlex
import traceback
from collections.abc import Sequence
from os import PathLike

from .log_text import decode_log_text


Command = str | bytes | PathLike[str] | Sequence[str | bytes | PathLike[str]]


class CheckerRepairBlocked(ValueError):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


_REDACTED = "[REDACTED]"
_FLAG = re.compile(r"(?<![\w-])(?:--[\w-]+|-a\b)")
_VALUE = re.compile(
    r'''(?:['"]?\s*,\s*|=|\s+)(\[REDACTED\]|(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\\.|[^\s'"\\])+|['"][\s\S]*)'''
)


def _text(value: object) -> str:
    """例外や値を安全に表示できる文字列へ変換する。"""
    return decode_log_text(value) if isinstance(value, bytes) else str(value)


def _sensitive(flag: str) -> bool:
    name = flag.lstrip("-").replace("-", "").replace("_", "").lower()
    return any(word in name for word in (
        "token", "password", "passwd", "passphrase", "secret", "credential",
        "apikey", "accesskey", "privatekey", "auth", "prompt",
    )) or name in {"a", "key", "task"}


def _redact_flags(text: str, values: set[str]) -> str:
    replacements = []
    end = 0
    for flag in _FLAG.finditer(text):
        if flag.start() < end or not _sensitive(flag.group()):
            continue
        value = _VALUE.match(text, flag.end())
        if value is None:
            continue
        raw = value[1]
        values.add(raw)
        if raw[:1] in ("'", '"') and raw[-1:] == raw[:1]:
            values.add(raw[1:-1])
            try:
                values.update(shlex.split(raw))
            except ValueError:
                pass
        start, end = value.span(1)
        replacements.append((start, end))
    for start, end in reversed(replacements):
        text = text[:start] + _REDACTED + text[end:]
    return text


def _command(command: Command, values: set[str]) -> str:
    """例外情報から実行コマンドを抽出する。"""
    if isinstance(command, (str, bytes, PathLike)):
        return _redact_flags(_text(command), values)
    args = [_text(arg) for arg in command]
    hide_next = False
    for index, arg in enumerate(args):
        if hide_next:
            values.add(arg)
            args[index] = _REDACTED
            hide_next = False
        else:
            flag, equals, value = arg.partition("=")
            if flag.startswith("-") and _sensitive(flag):
                if equals:
                    values.add(value)
                    args[index] = flag + "=" + _REDACTED
                else:
                    hide_next = True
    return shlex.join(args)


def _redact(text: str, values: set[str]) -> str:
    _redact_flags(text, values)
    variants = set()
    for value in values - {"", _REDACTED, repr(_REDACTED)}:
        variants.update((value, repr(value)[1:-1], json.dumps(value)[1:-1], shlex.quote(value)))
    if variants:
        pattern = "|".join(re.escape(value) for value in [_REDACTED, *sorted(variants, key=len, reverse=True)])
        text = re.sub(pattern, lambda _: _REDACTED, text)
    return _redact_flags(text, set())


def safe_command(command: Command) -> str:
    """Return display-only argv/string text with prompt/auth values redacted.

    Does not mutate argv, execute commands, resolve executables, or read env.
    An argv sequence preserves argument boundaries; prefer it to a shell string.
    """
    values: set[str] = set()
    return _redact(_command(command, values), values)


def format_error(
    exc: BaseException, *, command: Command | None = None,
    cwd: str | PathLike[str] | None = None, executable: str | PathLike[str] | None = None,
) -> str:
    """Format the complete exception chain, frame locations and process output.

    Explicit command values also redact echoes in every message/output. Only
    supplied context and exception attributes are inspected, never environment.
    """
    values: set[str] = set()
    seen: set[int] = set()
    messages: dict[int, str] = {}
    contexts: dict[int, list[str]] = {}

    def collect(error: BaseException) -> None:
        """例外連鎖から表示する診断を集める。"""
        if id(error) in messages:
            return
        messages[id(error)] = _text(error)
        details = []
        error_command = command if error is exc and command is not None else getattr(error, "cmd", None)
        if error_command is None:
            error_command = getattr(error, "command", None)
        if error_command is not None:
            details.append("Command: " + _command(error_command, values))
        for label, value in (
            ("Working directory", cwd if error is exc and cwd is not None else getattr(error, "cwd", None)),
            ("Executable", executable if error is exc and executable is not None else getattr(error, "executable", None)),
        ):
            if value is not None:
                details.append(f"{label}: {_text(value)}")
        returncode = getattr(error, "returncode", None)
        if returncode is not None:
            details.append(f"Return code: {returncode}")
        for name in ("stdout", "stderr"):
            output = getattr(error, name, None)
            if output is not None:
                details.append(f"{name}:\n{_text(output)}")
        contexts[id(error)] = details
        _redact_flags(messages[id(error)], values)
        for detail in details:
            _redact_flags(detail, values)
        for linked in (error.__cause__, error.__context__):
            if linked is not None:
                collect(linked)
        if isinstance(error, BaseExceptionGroup):
            for child in error.exceptions:
                collect(child)

    def render(error: BaseException) -> str:
        """例外と診断を利用者向けの安全な文面にする。"""
        if id(error) in seen:
            return ""
        seen.add(id(error))
        message = _redact(messages[id(error)], values)
        parts = []
        for linked, relation in (
            (error.__cause__, "The above exception was the direct cause of the following exception:"),
            (error.__context__, "During handling of the above exception, another exception occurred:"),
        ):
            if linked is not None:
                previous = render(linked)
                # Launcher errors may already embed format_error(cause) for persistence.
                if previous and previous not in message:
                    parts.extend((previous, relation))
        frames = [
            f'  File "{frame.f_code.co_filename}", line {line}, in {frame.f_code.co_name}'
            for frame, line in traceback.walk_tb(error.__traceback__)
        ]
        if frames:
            parts.append("Traceback (most recent call last):\n" + "\n".join(frames))
        parts.append(f"{type(error).__name__}: {message}" if message else type(error).__name__)
        parts.extend(_redact(detail, values) for detail in contexts[id(error)])
        if isinstance(error, BaseExceptionGroup):
            for index, child in enumerate(error.exceptions, 1):
                parts.append(f"Grouped exception {index}:\n{render(child)}")
        return "\n".join(parts)

    collect(exc)
    return _redact(render(exc), values)
