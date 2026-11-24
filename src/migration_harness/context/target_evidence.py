from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat

from ..files import has_symlink_component
from .source_context import target_context_snapshot


_LIMIT = 2 * 1024 * 1024
_EXTENSIONS = {".cs", ".cshtml", ".razor"}
_DIAGNOSTIC_PATH = re.compile(
    r'(?i)(?:[A-Z]:)?[^\s"<>|:*?()\[\],;：]+\.(?:cshtml|razor|cs)\b(?:\((\d{1,7})(?:,\d{1,7})?\))?',
)

_MSBUILD_PATH = re.compile(
    r"^\s*(?:[0-9]+>)?\s*(?P<path>.+\.(?:cshtml|razor|cs))"
    r"\((?P<line>\d{1,7})(?:,\d{1,7}){0,3}\)\s*:\s*(?:fatal error|error|warning|info)\b",
    flags=re.IGNORECASE,
)


def _reported_paths(findings):
    for row in findings[:16_384].splitlines():
        standard = _MSBUILD_PATH.match(row)
        if standard is not None:
            yield standard["path"], int(standard["line"])
        else:
            for match in _DIAGNOSTIC_PATH.finditer(row):
                yield match.group().split("(", 1)[0], int(match[1]) if match[1] is not None else None


def _safe_path(root, relative):
    path = root / relative
    if has_symlink_component(root, relative) or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("target reference is a symlink or outside the pinned snapshot")
    return path


def _load_bytes(path, max_bytes):
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError("pinned target reference must be a regular file")
    with path.open("rb") as stream:
        data = stream.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError("pinned target reference exceeds its evidence budget")
    return data


def target_references(capsule):
    path = _safe_path(target_context_snapshot(capsule), Path("manifest.json"))
    if not path.exists():
        return {}
    manifest = json.loads(_load_bytes(path, _LIMIT))
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(files, list) or len(files) > 10_000:
        raise ValueError("invalid pinned target reference manifest")
    entries = {}
    for item in files:
        name = item.get("path") if isinstance(item, dict) else None
        if not isinstance(name, str) or "\\" in name or PureWindowsPath(name).anchor:
            raise ValueError("invalid pinned target reference path")
        normal = PurePosixPath(name)
        if not normal.parts or ".." in normal.parts or name != normal.as_posix():
            raise ValueError("invalid pinned target reference path")
        if normal.suffix.lower() not in _EXTENSIONS:
            continue
        digest, size = item.get("sha256"), item.get("bytes")
        if (name in entries or not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                or type(size) is not int or size < 0):
            raise ValueError("invalid pinned target reference version or duplicate path")
        entries[name] = {"id": f"target:{name}", "path": name, "sha256": digest, "bytes": size}
    return entries


def _pinned_bytes(capsule, entry):
    if entry["bytes"] > _LIMIT:
        raise ValueError("pinned target reference exceeds its evidence budget")
    path = _safe_path(target_context_snapshot(capsule), Path("files", *PurePosixPath(entry["path"]).parts))
    data = _load_bytes(path, _LIMIT)
    if len(data) != entry["bytes"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
        raise ValueError("pinned target reference hash mismatch")
    return data


def _checked_entry(capsule, entries, source_id):
    entry = entries.get(source_id.removeprefix("target:"))
    if entry is None or entry["id"] != source_id:
        raise ValueError("unknown pinned target reference ID")
    return entry, _pinned_bytes(capsule, entry)


def pinned_text_encoding(data: bytes) -> str:
    if data.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        return "utf-32"
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    return "utf-8-sig" if data.startswith(b"\xef\xbb\xbf") else "utf-8"


def _decode(data):
    encoding = pinned_text_encoding(data)
    return encoding, data.decode(encoding)


def read_target_reference(capsule, source_id, start, end, *, offset=0, max_chars=16_384):
    if not 1 <= start <= end or offset < 0 or not 1 <= max_chars <= 16_384:
        raise ValueError("invalid target evidence range, offset or page size")
    entry, data = _checked_entry(capsule, target_references(capsule), source_id)
    encoding, text = _decode(data)
    count = 0
    chunks = []
    number = 0
    with io.StringIO(text, newline="") as stream:
        for number, line in enumerate(stream, 1):
            if number >= start:
                begin, finish = max(0, offset - count), min(len(line), offset + max_chars - count)
                if begin < finish:
                    chunks.append(line[begin:finish])
                count += len(line)
            if number == end:
                break
    if number < end or offset > count:
        raise ValueError("target evidence range or offset exceeds the fixed source")
    page = "".join(chunks)
    next_offset = offset + max_chars
    return {**entry, "role": "reference", "encoding": encoding, "start_line": start, "end_line": end,
            "offset": offset, "chars": len(page), "utf8_bytes": len(page.encode("utf-8")),
            "next_offset": next_offset if next_offset < count else None, "text": page}


def related_target_references(capsule, source_id):
    entries = target_references(capsule)
    source, _ = _checked_entry(capsule, entries, source_id)
    name = source["path"]
    lowered = name.lower()
    pair = name[:-3] if lowered.endswith((".cshtml.cs", ".razor.cs")) else (
        name + ".cs" if lowered.endswith((".cshtml", ".razor")) else None
    )
    references = []
    if pair in entries:
        entry = entries[pair]
        _pinned_bytes(capsule, entry)
        references.append({**entry, "reason": "exact Razor companion path convention, not type binding"})
    return {"source": source, "references": references, "relationship_status": "syntactic-navigation-only"}


def _candidate(name, by_basename):
    name = name.replace("\\", "/").removeprefix("./")
    candidates = by_basename.get(PurePosixPath(name).name.casefold(), [])
    if "/" in name:
        exact = [path for path in candidates if path.casefold() == name.casefold()]
        candidates = exact or [
            path for path in candidates if "/" in path and name.casefold().endswith("/" + path.casefold())
        ]
    return candidates[0] if len(candidates) == 1 else None


def diagnostic_references(capsule, findings):
    try:
        entries = target_references(capsule)
    except (OSError, ValueError):
        return []
    by_basename = {}
    for name in entries:
        by_basename.setdefault(PurePosixPath(name).name.casefold(), []).append(name)
    results, checked, seen = [], {}, set()
    remaining_bytes = _LIMIT * 2
    for name, line in _reported_paths(findings):
        path = _candidate(name, by_basename)
        if path is None or line == 0 or (path, line) in seen:
            continue
        if len(seen) == 32:
            break
        seen.add((path, line))
        entry = entries[path]
        if path not in checked:
            checked[path] = None
            if entry["bytes"] <= min(remaining_bytes, _LIMIT):
                remaining_bytes -= entry["bytes"]
                try:
                    _, text = _decode(_pinned_bytes(capsule, entry))
                    with io.StringIO(text, newline="") as stream:
                        checked[path] = sum(1 for _ in stream)
                except (OSError, ValueError):
                    pass
        lines = checked[path]
        if lines is not None and (line is None or line <= lines):
            results.append({**entry, "line": line, "reason": "unique pinned path named in diagnostic"})
        if len(results) == 5:
            break
    return results
