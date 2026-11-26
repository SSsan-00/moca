from __future__ import annotations

import json
import re
from typing import Any


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate JSON key")
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON number: {value}")


def saved_object(text: str, label: str) -> dict[str, Any]:
    try:
        value = json.loads(text, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (ValueError, RecursionError) as error:
        raise ValueError(f"{label} のJSONが壊れています。元の記録は変更していません。") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} はJSONオブジェクトである必要があります。")
    return value


def versionless_record(value: dict[str, Any], label: str) -> None:
    if "schema_version" in value:
        raise ValueError(f"{label} の保存形式の版に対応していません。保存した版で開いてください。")


def nonempty_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def content_hash(value: Any, *, missing: bool = False) -> bool:
    return (missing and value is None) or isinstance(value, str) and _SHA256.fullmatch(value) is not None
