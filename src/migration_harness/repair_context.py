from __future__ import annotations

import io
from pathlib import Path
import re

from .capsules import is_php_input, pinned_php_sources, read_pinned_php
from .checker_output import parse_checker_diagnostics
from .files import safe_target


_BUILD_LOCATION = re.compile(r"^(.+\.(?:cs|cshtml|razor))\((\d{1,7})(?:,\d{1,7}){0,3}\):\s*(?:CS|RZ)\d+:", re.IGNORECASE)
_FIELDS = ("path", "start_line", "end_line", "sha256")
_MAX_HINTS = 5
_MAX_PHP_BYTES = 4 * 1024 * 1024


def _diagnostic_locations(findings, worktree, kind):
    rows = findings[:16_384].splitlines()
    if kind != "build":
        parsed, unrecognized = parse_checker_diagnostics(rows, worktree)
        if not unrecognized:
            yield from ((Path(item.path), int(item.line)) for item in parsed if len(item.line) <= 7)
        return
    for row in rows[:64]:
        found = _BUILD_LOCATION.match(row)
        if not found:
            continue
        name, line = found.groups()
        path = Path(name.replace("\\", "/"))
        if path.is_absolute():
            if not path.is_relative_to(worktree):
                continue
            path = path.relative_to(worktree)
        if not path.anchor and ".." not in path.parts:
            yield path, int(line)


def _verified_pins(capsule, pins, ranges):
    matched_paths = {item["path"] for item in ranges}
    by_path = {}
    remaining = _MAX_PHP_BYTES
    for index, pin in enumerate(pins, 1):
        name = pin["path"]
        if name not in matched_paths:
            continue
        if name in by_path:
            by_path[name] = None
            continue
        by_path[name] = None
        try:
            file = safe_target(capsule, Path(pin["snapshot"]))
            size = file.stat().st_size
            if size > remaining:
                continue
            remaining -= size
            data = read_pinned_php(capsule, pin)
            if len(data) != size:
                continue
            with io.StringIO(data.decode(pin["encoding"]), newline="") as stream:
                by_path[name] = (f"php-{index:04d}", pin["sha256"], sum(1 for _ in stream))
        except (OSError, ValueError, LookupError):
            continue
    return by_path


def diagnostic_php_ranges(capsule, manifest, record, worktree, targets, target_hashes, findings, kind):
    pins = pinned_php_sources(manifest)
    mappings = record.get("migration_mappings", {})
    reviews = record.get("migration_reviews", {})
    if not pins or not reviews or len(mappings) + len(reviews) > 50_000:
        return []
    completed = record.get("completed_capsules", 0)
    if type(completed) is not int or not 0 < completed <= len(manifest["parts"]):
        return []
    allowed = set(targets)
    wanted = list(dict.fromkeys(
        (path, line) for path, line in _diagnostic_locations(findings, worktree, kind)
        if path in allowed and line > 0
    ))[:32]
    if not wanted:
        return []
    by_location = {item: [] for item in wanted}
    for key in mappings.keys() | reviews.keys():
        match = re.fullmatch(r"([1-9]\d{0,6}):(\d{1,7})", key)
        if not match:
            continue
        part_index, source_index = map(int, match.groups())
        if part_index > completed:
            continue
        part = manifest["parts"][part_index - 1]
        if part.get("index") != part_index or source_index >= len(part["sources"]):
            continue
        source = part["sources"][source_index]
        if not is_php_input(source):
            continue
        review = reviews.get(key, {})
        saved = review.get("mapping")
        mapping = mappings.get(key, saved)
        if not isinstance(mapping, dict) or not isinstance(mapping.get("path"), str):
            continue
        name = mapping["path"]
        digest = mapping.get("sha256")
        start, end = mapping.get("start_line"), mapping.get("end_line")
        if (not digest or digest != target_hashes.get(name)
                or type(start) is not int or type(end) is not int or not 0 < start <= end):
            continue
        for location, matches in by_location.items():
            if location[0].as_posix() == name and start <= location[1] <= end and len(matches) < 2:
                confirmed = (
                    review.get("state") == "confirmed" and isinstance(saved, dict)
                    and (review.get("target_hashes") or {}).get(name) == digest
                    and all(saved.get(field) == mapping.get(field) for field in _FIELDS)
                    and not source.get("character_slices")
                )
                matches.append((source, part_index, source_index, digest, start, end) if confirmed else None)
    ranges = []
    for (target, line), matches in by_location.items():
        if len(matches) != 1 or matches[0] is None:
            continue
        source, index, source_index, digest, target_start, target_end = matches[0]
        start, end = source.get("start_line"), source.get("end_line")
        if type(start) is not int or type(end) is not int or not 0 < start <= end:
            continue
        ranges.append({
            "path": source["path"], "start_line": start, "end_line": end,
            "range_id": f"{index}:{source_index + 1}", "file": f".migration-materials/source-{index:04d}.md",
            "target": target.as_posix(), "target_line": line, "target_sha256": digest,
            "target_start_line": target_start, "target_end_line": target_end,
            "relationship": "operator-confirmed-mapping-navigation-only",
        })
        if len(ranges) == _MAX_HINTS:
            break
    checked = _verified_pins(capsule, pins, ranges) if ranges else {}
    return [
        {**item, "id": pin[0], "sha256": pin[1]}
        for item in ranges if (pin := checked.get(item["path"])) is not None and item["end_line"] <= pin[2]
    ]
