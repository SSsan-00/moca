from __future__ import annotations

import io
from bisect import bisect_left
from pathlib import Path
from typing import Any

from .capsules import is_php_input, load_capsule_manifest, read_pinned_php, verify_pinned_php


_MAX_PAGE = 16_384


def _covering_ranges(ranges: list[dict[str, Any]], start: int, end: int) -> bool:
    next_line = start
    for item in sorted(ranges, key=lambda row: (row["start_line"], row["end_line"])):
        if item["start_line"] > next_line:
            break
        next_line = max(next_line, item["end_line"] + 1)
        if next_line > end:
            return True
    return False


def _catalog(manifest: dict[str, Any], through: int | None) -> list[dict[str, Any]]:
    parts = manifest["parts"]
    if through is not None and not 1 <= through <= len(parts):
        raise ValueError("capsule is outside the saved run")
    pinned_sources = manifest["php_sources"]
    sources: dict[str, list[dict[str, Any]]] = {}
    for part in parts:
        if through is None or part["index"] <= through:
            for index, source in enumerate(part["sources"], 1):
                if is_php_input(source):
                    sources.setdefault(source["path"], []).append({
                        "start_line": source["start_line"],
                        "end_line": source["end_line"],
                        "capsule": part["index"],
                        "ledger_range_id": f"{part['index']}:{index}",
                        "character_slices": source.get("character_slices", []),
                        **({"symbol": source["symbol"]} if isinstance(source.get("symbol"), str) else {}),
                        "role": ("assigned" if through is None else "current" if part["index"] == through else "prior"),
                    })
    for source in [*manifest.get("shared_sources", []), *manifest.get("reference_sources", [])]:
        if is_php_input(source):
            sources.setdefault(source["path"], []).append({
                "start_line": source["start_line"],
                "end_line": source["end_line"],
                "role": "reference",
                **({"symbol": source["symbol"]} if isinstance(source.get("symbol"), str) else {}),
            })
    return [
        {**pin, "id": f"php-{index:04d}", "ranges": sources.get(pin["path"], [])}
        for index, pin in enumerate(pinned_sources, 1)
    ]


def inspect_context(run_path: Path, through: int | None = None) -> dict[str, Any]:
    capsule, manifest = load_capsule_manifest(run_path)
    verify_pinned_php(capsule, manifest)
    return _inspect_manifest(manifest, through)


def _inspect_manifest(manifest: dict[str, Any], through: int | None) -> dict[str, Any]:
    sources = _catalog(manifest, through)
    index = manifest.get("syntax_boundaries", {})
    future_ends: dict[str, dict[int, int]] = {}
    if through is not None:
        for part in manifest["parts"][through:]:
            for row in part["sources"]:
                for span in row.get("character_slices", []):
                    ends = future_ends.setdefault(row["path"], {})
                    line = span["line"]
                    ends[line] = max(ends.get(line, 0), span["focus_end_column"])
    items = []
    for source in sources:
        structural = index.get(source["path"], {}) if isinstance(index, dict) else {}
        names = structural.get("functions", []) if isinstance(structural, dict) else []
        if not isinstance(names, list):
            names = []
        ends = future_ends.get(source["path"], {})
        sliced = {span["line"] for row in source["ranges"] for span in row.get("character_slices", [])} if ends else set()
        incomplete = sorted(line for line in ends.keys() & sliced
                            if not _whole_line_assigned(source["ranges"], line, ends[line]))
        items.append({
            "id": source["id"], "path": source["path"], "sha256": source["sha256"],
            "encoding": source["encoding"], "ranges": source["ranges"],
            "functions": [
                {**name, "relationship": "lexical-only"}
                for name in names if isinstance(name, dict)
                and isinstance(name.get("name"), str)
                and type(name.get("start_line")) is int and type(name.get("end_line")) is int
                and 0 < name["start_line"] <= name["end_line"]
                and _covering_ranges(source["ranges"], name["start_line"], name["end_line"])
                and bisect_left(incomplete, name["start_line"]) == bisect_left(incomplete, name["end_line"] + 1)
            ],
        })
    return {"version": 1, "unit_id": manifest["unit_id"], "through_capsule": through, "sources": items}


def find_feature(run_path: Path, name: str, through: int | None = None) -> dict[str, Any]:
    query = name.strip().casefold()
    if not query:
        raise ValueError("feature requires a non-empty input symbol or named PHP function")
    capsule, manifest = load_capsule_manifest(run_path)
    result = _inspect_manifest(manifest, through)
    pinned = {source["path"]: source for source in _catalog(manifest, through)}
    matches: list[dict[str, Any]] = []
    for source in result["sources"]:
        declarations = [row for row in source["ranges"] if row.get("symbol", "").casefold() == query]
        lexical = [row for row in source["functions"] if row["name"].casefold() == query]
        if not declarations and not lexical:
            continue
        data = read_pinned_php(capsule, pinned[source["path"]])
        if lexical:
            lexical = _readable_functions(lexical, source["ranges"], data.decode(source["encoding"]))
        if declarations or lexical:
            matches.append({
                "id": source["id"], "path": source["path"], "sha256": source["sha256"],
                "declared_ranges": declarations, "lexical_functions": lexical,
            })
    return {
        "version": result["version"], "unit_id": result["unit_id"], "through_capsule": through,
        "query": name.strip(), "matches": matches, "relationship_status": "not-semantically-verified",
        "unmatched_policy": "Unmatched source remains in the existing evidence catalog and assigned capsules.",
    }


def _whole_line_assigned(ranges: list[dict[str, Any]], line: int, width: int) -> bool:
    focus_ranges = []
    for row in ranges:
        if not row["start_line"] <= line <= row["end_line"]:
            continue
        slices = [item for item in row.get("character_slices", []) if item["line"] == line]
        if not slices:
            return True
        focus_ranges.extend(slices)
    column = 1
    for item in sorted(focus_ranges, key=lambda row: row["focus_start_column"]):
        if item["focus_start_column"] > column:
            return False
        column = max(column, item["focus_end_column"] + 1)
        if column > width:
            return True
    return width == 0 and bool(focus_ranges)


def _readable_functions(
    functions: list[dict[str, Any]], ranges: list[dict[str, Any]], text: str,
) -> list[dict[str, Any]]:
    split_lines = {item["line"] for row in ranges for item in row.get("character_slices", [])}
    widths = {}
    last_line = 0
    limit = max(fn["end_line"] for fn in functions)
    with io.StringIO(text, newline="") as stream:
        for last_line, line in enumerate(stream, 1):
            if last_line in split_lines:
                widths[last_line] = len(line.rstrip("\r\n"))
            if last_line == limit:
                break
    results = []
    for fn in functions:
        start, end = fn["start_line"], fn["end_line"]
        if end > last_line or any(
            start <= number <= end and not _whole_line_assigned(ranges, number, width)
            for number, width in widths.items()
        ):
            continue
        assigned = [row for row in ranges if "ledger_range_id" in row
                    and row["start_line"] <= end and start <= row["end_line"]]
        results.append({**fn, "capsules": sorted({row["capsule"] for row in assigned}),
                        "ledger_range_ids": [row["ledger_range_id"] for row in assigned]})
    return results


def pinned_span_verifier(run_path: Path, through: int | None = None):
    capsule, manifest = load_capsule_manifest(run_path)
    sources = {source["id"]: source for source in _catalog(manifest, through)}
    verified = {}

    def source_sha256(source_id: str, start: int, end: int) -> str | None:
        source = sources.get(source_id)
        if source is None or not 1 <= start <= end or not _covering_ranges(source["ranges"], start, end):
            return None
        if source_id not in verified:
            try:
                data = read_pinned_php(capsule, source)
                with io.StringIO(data.decode(source["encoding"], errors="strict"), newline="") as stream:
                    line_count = 0
                    split_lines = {item["line"] for row in source["ranges"]
                                   for item in row.get("character_slices", [])}
                    restricted = set()
                    for line_count, line in enumerate(stream, 1):
                        if line_count in split_lines and not _whole_line_assigned(
                            source["ranges"], line_count, len(line.rstrip("\r\n")),
                        ):
                            restricted.add(line_count)
            except (OSError, ValueError):
                verified[source_id] = None
            else:
                verified[source_id] = (line_count, frozenset(restricted))
        version = verified[source_id]
        if version is None or end > version[0] or any(start <= line <= end for line in version[1]):
            return None
        return source["sha256"]

    return source_sha256


def read_context(
    run_path: Path, source_id: str, start: int, end: int,
    *, through: int | None = None, offset: int = 0, max_chars: int = _MAX_PAGE,
) -> dict[str, Any]:
    if not 1 <= start <= end or offset < 0 or not 1 <= max_chars <= _MAX_PAGE:
        raise ValueError("invalid evidence range, offset or page size")
    capsule, manifest = load_capsule_manifest(run_path)
    source = next((pin for pin in _catalog(manifest, through) if pin["id"] == source_id), None)
    if source is None or not _covering_ranges(source["ranges"], start, end):
        raise ValueError("unknown evidence ID or range outside the saved source selection")
    data = read_pinned_php(capsule, source)
    split_lines = {item["line"] for row in source["ranges"] for item in row.get("character_slices", [])}
    count = 0
    chunks = []
    number = 0
    with io.StringIO(data.decode(source["encoding"], errors="strict"), newline="") as stream:
        for number, line in enumerate(stream, 1):
            if number >= start:
                if number in split_lines and not _whole_line_assigned(source["ranges"], number, len(line.rstrip("\r\n"))):
                    raise ValueError("line has unassigned character slices in the selected capsule scope")
                begin, finish = max(0, offset - count), min(len(line), offset + max_chars - count)
                if begin < finish:
                    chunks.append(line[begin:finish])
                count += len(line)
            if number == end:
                break
    if number < end:
        raise ValueError("evidence range exceeds the fixed source length")
    if offset > count:
        raise ValueError("offset exceeds the selected evidence range")
    page = "".join(chunks)
    next_offset = offset + max_chars
    return {
        "id": source_id, "path": source["path"], "sha256": source["sha256"], "encoding": source["encoding"],
        "start_line": start, "end_line": end, "offset": offset,
        "chars": len(page), "utf8_bytes": len(page.encode("utf-8")),
        "next_offset": next_offset if next_offset < count else None,
        "text": page,
        "ranges": [row for row in source["ranges"] if row["start_line"] <= end and start <= row["end_line"]],
    }


def boundary_hints(manifest: dict[str, Any], capsule_index: int) -> str:
    syntax = manifest.get("syntax_boundaries")
    if not isinstance(syntax, dict):
        return ""
    hints = []
    for path, entry in syntax.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("functions"), list):
            continue
        for fn in entry["functions"]:
            if not isinstance(fn, dict) or not isinstance(fn.get("capsules"), list):
                continue
            capsules = fn["capsules"]
            if len(capsules) < 2 or capsule_index not in capsules:
                continue
            visible_from = fn.get("name_visible_from")
            if type(visible_from) is not int or not 1 <= visible_from <= capsule_index:
                continue
            if not isinstance(fn.get("name"), str) or not all(
                type(fn.get(key)) is int for key in ("start_line", "end_line")
            ):
                continue
            hint = (
                f"{path!r} {fn['name']!r} lines {fn['start_line']}-{fn['end_line']} "
                f"({sum(type(part) is int and part <= capsule_index for part in capsules)} of {len(capsules)} "
                "assigned excerpts currently available)"
            )
            if sum(map(len, hints)) + len(hint) > 500 or len(hints) == 6:
                break
            hints.append(hint)
    if not hints:
        return ""
    return (
        "Lexical source boundaries (navigation hints only, not verified behavior or extra assignments): "
        + "; ".join(hints)
        + ". Check numbered saved excerpts; do not migrate future ranges or treat hints as verification. "
    )
