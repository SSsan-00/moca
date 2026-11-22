from __future__ import annotations

import re
import unicodedata
from collections import Counter, deque
from collections.abc import Iterable
from io import StringIO
from math import log1p

from .execution import check_cancelled, report_activity


_MAX_HINTS = 16
_MAX_REGIONS = 12
_MAX_REGION_CHARS = 6_000
_MAX_QUERY_TERMS = 80
_MAX_RULE_CHARS = 2_000_000
_MAX_PASSAGES = 8_192
_NUMBERED = re.compile(r"^ *[0-9]+ \| ")
_ASCII_WORD = re.compile(r"[a-z][a-z0-9_]{2,}")
_ASCII_TERM = re.compile(r"[a-z][a-z0-9]{2,}")
_JAPANESE = re.compile(r"[一-鿿々ぁ-ゖァ-ヺー]{2,}")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_CAMEL_IN_TEXT = re.compile(r"([a-z0-9])([A-Z])|([A-Z])([A-Z][a-z])")
_PHP_LEX = re.compile(
    r"""(?P<literal>'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*")"""
    r"|(?P<comment>//[^\r\n]*|\#(?!\[)[^\r\n]*|/\*[\s\S]*?\*/)"
    r"|(?P<name>[a-zA-Z_][a-zA-Z0-9_]*)"
)
_PHP_WORDS = frozenset(
    "php function public private protected static return null true false array string int bool void "
    "if else elseif while foreach for switch case break continue default echo print require include "
    "new class extends final const use namespace try catch throw this isset empty count".split()
)


def _terms(text: str) -> Iterable[str]:
    normalized = unicodedata.normalize("NFKC", text)
    split = _CAMEL_IN_TEXT.sub(r"\1\3 \2\4", normalized).replace("_", " ").lower()
    yield from (word for word in _ASCII_TERM.findall(split) if word not in _PHP_WORDS)
    for word in _JAPANESE.findall(normalized):
        yield from (word[i:i + 2] for i in range(len(word) - 1))


def _identifiers(text: str) -> set[str]:
    return set(_ASCII_WORD.findall(unicodedata.normalize("NFKC", text).lower())) - _PHP_WORDS


def _region_queries(regions):
    for functions, lines in regions:
        check_cancelled()
        if not functions:
            yield _query(_bounded_lines(lines))
            continue
        count = min(len(functions), _MAX_REGIONS)
        selected = [functions[i * (len(functions) - 1) // max(1, count - 1)] for i in range(count)]
        recent = deque(maxlen=4)
        chunks = []
        size = index = 0
        for number, line in lines:
            check_cancelled()
            while index < count and number > selected[index].end_line:
                yield _query("".join(chunks), selected[index].name)
                index += 1
                chunks, size = [], 0
            if index == count:
                break
            fn = selected[index]
            if number >= fn.start_line:
                if number == fn.start_line:
                    chunks.extend(recent)
                    size = sum(map(len, chunks))
                if size < _MAX_REGION_CHARS:
                    chunks.append(line[:min(_MAX_REGION_CHARS - size, 2_000)])
                    size += len(chunks[-1])
            if line.lstrip().startswith(("//", "#", "/*", "*")):
                recent.append(line[:240])
            else:
                recent.clear()
        if index < count and chunks:
            yield _query("".join(chunks), selected[index].name)


def _bounded_lines(lines):
    remaining = _MAX_REGION_CHARS
    chunks = []
    for _, line in lines:
        check_cancelled()
        chunks.append(line[:min(remaining, 2_000)])
        remaining -= len(chunks[-1])
        if remaining <= 0:
            break
    return "".join(chunks)


def _query(text, function=""):
    comments = {}
    names = dict.fromkeys([function]) if function else {}
    exact = _identifiers(function)
    for match in _PHP_LEX.finditer(text):
        token = match[0]
        if match.lastgroup == "comment" and len(comments) < 4:
            comments[token[:240]] = None
        elif match.lastgroup == "name" and len(names) < 32 and token.lower() not in _PHP_WORDS:
            names[token] = None
            if "_" in token or _CAMEL.search(token):
                exact.add(token.lower())
    terms = dict.fromkeys(_terms(" ".join([*comments, *names])))
    return set(list(terms)[:_MAX_QUERY_TERMS]), exact


def _passages(rendered: str) -> Iterable[tuple[int, int, str]]:
    body = []
    start = end = size = 0
    for number, line in enumerate(StringIO(rendered, newline=""), 1):
        if number % 256 == 0:
            check_cancelled()
        prefix = _NUMBERED.match(line)
        if prefix is None:
            continue
        raw = line[prefix.end():]
        if body and (size + len(raw) > 1_000 or len(body) >= 12 or raw.lstrip().startswith("# ")):
            yield start, end, "".join(body)
            body, size = [], 0
        if not body:
            start = number
        end = number
        body.append(raw)
        size += len(raw)
    if body:
        yield start, end, "".join(body)


def _rank(regions, sources, rule_files):
    pairs = [(item, text, entry) for (item, text), entry in zip(sources, rule_files, strict=True)
             if entry["usage"] == "reference"]
    if not pairs:
        return []
    queries = []
    for query, exact in _region_queries(regions):
        if query or exact:
            if len(queries) >= 3 * _MAX_REGIONS:
                break
            queries.append((query, exact))
    if not queries:
        return []
    if sum(len(text) for _, text, _ in pairs) > _MAX_RULE_CHARS:
        raise ValueError("rule search character limit")

    wanted_terms = set.union(*(query for query, _ in queries))
    wanted_ids = set.union(*(exact for _, exact in queries))
    windows = []
    frequencies = Counter()
    total_length = 0
    for item, text, entry in pairs:
        check_cancelled()
        label = item["description"][:240]
        for start, end, body in _passages(text):
            check_cancelled()
            if len(windows) >= _MAX_PASSAGES:
                raise ValueError("rule search passage limit")
            candidate = f"{label}\n{body}"
            counts = Counter(_terms(candidate))
            length = counts.total()
            identifiers = wanted_ids & _identifiers(candidate) if wanted_ids else set()
            overlap = {word: count for word, count in counts.items() if word in wanted_terms}
            if not length and not identifiers:
                continue
            frequencies.update(overlap.keys())
            total_length += length
            windows.append((entry, start, end, length, identifiers, overlap))

    if not windows:
        return []
    average = max(total_length / len(windows), 1)
    weights = {word: log1p((len(windows) - n + 0.5) / (n + 0.5)) for word, n in frequencies.items()}
    per_query = []
    for query, exact in queries:
        check_cancelled()
        best = {}
        for entry, start, end, length, identifiers, overlap in windows:
            shared = query.intersection(overlap)
            exact_count = len(exact.intersection(identifiers))
            if not shared and not exact_count:
                continue
            denominator = 1.2 * (0.25 + 0.75 * length / average)
            score = sum(weights[word] * (overlap[word] * 2.2) / (overlap[word] + denominator) for word in sorted(shared))
            rank = (-exact_count, -score, entry["file"], start)
            name = entry["file"]
            if name not in best or rank < best[name][0]:
                best[name] = (rank, {"file": name, "sha256": entry["sha256"], "lines": [start, end]})
        per_query.append(sorted(best.values(), key=lambda row: row[0])[:3])

    candidates = {}
    for position in range(3):
        for rank, hint in sorted((rows[position] for rows in per_query if len(rows) > position),
                                 key=lambda row: row[0]):
            if len(candidates) >= _MAX_HINTS:
                break
            candidates.setdefault(hint["file"], (rank, hint))
    return [hint for _, hint in sorted(candidates.values(), key=lambda row: row[0])]


def prepare_rule_hint(regions, sources, rule_files):
    try:
        return _rank(regions, sources, rule_files)
    except (OSError, ValueError):
        report_activity("詳細ルールの検索ヒントを作成できなかったため、通常どおり全件を確認します。")
        return []


def review_hint(manifest, line_counts):
    hints = manifest.get("rule_read_order")
    if not isinstance(hints, list) or not 0 < len(hints) <= _MAX_HINTS:
        return ""
    entries = {entry["file"]: entry["sha256"] for entry in manifest.get("rule_files", ())
               if entry["usage"] == "reference"}
    ranges = []
    for hint in hints:
        if not isinstance(hint, dict):
            return ""
        file, bounds = hint.get("file"), hint.get("lines")
        if (not isinstance(file, str) or file not in line_counts or file not in entries
                or entries[file] != hint.get("sha256")
                or not isinstance(bounds, list) or len(bounds) != 2 or any(type(value) is not int for value in bounds)
                or not 1 <= bounds[0] <= bounds[1] <= line_counts[file]):
            return ""
        ranges.append(f"details/{file} (physical lines {bounds[0]}-{bounds[1]})")
    return (
        " Optional read-order hints from lexical search: " + "; ".join(ranges) + "."
        " They are not evidence of applicability or exclusion. Review the full inventory as instructed. "
    )
