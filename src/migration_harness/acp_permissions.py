from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path

from .execution import check_cancelled
from .files import safe_target

MAX_REFERENCE_BYTES = 4 * 1024 * 1024
MAX_RESPONSE_CHARS = 32_768
KINDS = frozenset({"read", "edit", "delete", "move", "search", "execute", "think", "fetch", "switch_mode", "other"})
_REFERENCE_PATH = re.compile(r"references\.json|(?:rule|reference)-\d+\.txt")
_DIGEST = re.compile(r"[a-f0-9]{64}")


def metrics_kind(value: object) -> str:
    return value if isinstance(value, str) and value in KINDS else "unknown"


class ReadRefused(ValueError):
    pass


class PinnedReferenceReads:
    def __init__(self, workspace: Path, materials: dict[str, str], allowed_rules: set[str]):
        self.workspace = workspace.resolve()
        self.pins = {
            Path(".migration-materials") / name: digest
            for name, digest in materials.items()
            if _REFERENCE_PATH.fullmatch(name) and _DIGEST.fullmatch(digest)
            and (not name.startswith("rule-") or name in allowed_rules)
        }

    def read(self, params: dict) -> str:
        if params.keys() - {"sessionId", "path", "line", "limit"} or not isinstance(params.get("path"), str):
            raise ReadRefused("unknown_request")
        raw = params["path"]
        if not raw or "\x00" in raw:
            raise ReadRefused("unknown_request")
        path = Path(raw)
        if not path.is_absolute() or ".." in path.parts or path == self.workspace:
            raise ReadRefused("target_not_pinned")
        relative = path.relative_to(self.workspace) if path.is_relative_to(self.workspace) else None
        if relative not in self.pins:
            raise ReadRefused("target_not_pinned")
        line, limit = params.get("line"), params.get("limit")
        if any(item is not None and (type(item) is not int or not 1 <= item <= 2**31 - 1) for item in (line, limit)):
            raise ReadRefused("unknown_request")
        check_cancelled()
        try:
            target = safe_target(self.workspace, relative)
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
            with os.fdopen(os.open(target, flags), "rb") as stream:
                saved = os.fstat(stream.fileno())
                if not stat.S_ISREG(saved.st_mode) or saved.st_size > MAX_REFERENCE_BYTES:
                    raise ReadRefused("unavailable_reference")
                content = stream.read(MAX_REFERENCE_BYTES + 1)
        except ReadRefused:
            raise
        except (OSError, ValueError) as exc:
            raise ReadRefused("unavailable_reference") from exc
        if len(content) > MAX_REFERENCE_BYTES or hashlib.sha256(content).hexdigest() != self.pins[relative]:
            raise ReadRefused("reference_changed")
        try:
            text = content.decode("utf-8")
        except UnicodeError as exc:
            raise ReadRefused("unavailable_reference") from exc
        if line is not None or limit is not None:
            # ACPの行番号は1始まり。改行自体は固定資料のまま返す。
            start = (line or 1) - 1
            rows = re.findall(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+$", text)
            text = "".join(rows[start:] if limit is None else rows[start:start + limit])
        if len(text) > MAX_RESPONSE_CHARS:
            raise ReadRefused("bounded_read_required")
        check_cancelled()
        return text
