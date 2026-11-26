from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..files import safe_target
from .checker_output import decode_checker_output


def review_log(run_path: Path, name: str) -> tuple[str, bool]:
    log = safe_target(run_path, Path(name))
    if not log.is_file():
        return "", False
    # 表示・修正指示には直近の診断を優先して末尾を使う。元のログファイルは切り詰めない。
    with log.open("rb") as stream:
        start = max(0, stream.seek(0, os.SEEK_END) - 65_536)
        stream.seek(start)
        data = stream.read(65_536)
    if start and b"\n" in data:
        data = data.split(b"\n", 1)[1]
    if name == "checker.log":
        try:
            return decode_checker_output(data), bool(start)
        except UnicodeDecodeError:
            pass
    return data.decode("utf-8-sig", errors="replace"), bool(start)


def remember_diagnostic(run_path: Path, record: dict[str, Any], kind: str) -> None:
    code = record.get(f"{kind}_exit_code")
    if code is None:
        return
    output, truncated = review_log(run_path, f"{kind}.log")
    record.setdefault("last_diagnostics", {})[kind] = {
        "code": code,
        "error": record.get("error") if (record["status"] == "build_failed") == (kind == "build") else None,
        "log": {"kind": kind, "text": output[-8_000:], "truncated": truncated or len(output) > 8_000} if output else None,
    }
