from difflib import SequenceMatcher
import hashlib
import json
from pathlib import Path
import re
import subprocess
import unicodedata

from ..context.php_literals import _CSHARP_RESERVED
from ..files import safe_target
from .roslyn_syntax import _prepare_tool, _run_editor

_ESCAPE = re.compile(r"@([A-Za-z_][A-Za-z_0-9]*)")
_UNICODE_ESCAPE = re.compile(r"\\(?:u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8})")
_DIFF_TOKEN = re.compile(r"@\w+|\w+|\S")


def _could_contain_keyword_escape(text):
    if any(match[1] in _CSHARP_RESERVED for match in _ESCAPE.finditer(text)):
        return True
    if "\\" in text:
        for match in _UNICODE_ESCAPE.finditer(text):
            codepoint = int(match[0][2:], 16)
            if codepoint <= 0x10FFFF:
                char = chr(codepoint)
                if "a" <= char <= "z" or char == "_" or unicodedata.category(char) == "Cf":
                    return True
    return not text.isascii() and any(unicodedata.category(char) == "Cf" for char in set(text))


def _read_version(path, expected):
    data = path.read_bytes() if expected is not None else b""
    if expected is not None and hashlib.sha256(data).hexdigest() != expected:
        raise ValueError("移行先が更新されたため、C#予約語の確認をやり直してください。")
    return data.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")


def _positions(findings):
    rows = {}
    for item in findings:
        rows.setdefault(item["line"], []).append((item["column"], item["text"], item["value"], item["kind"]))
    return {row: sorted(items) for row, items in rows.items()}


def _diff_tokens(lines, start, end, positions):
    tokens, declarations = [], {}
    for row in range(start, end):
        offset = 0
        line = lines[row]
        for column, spelling, value, kind in positions.get(row, ()):
            tokens.extend((token[0], None) for token in _DIFF_TOKEN.finditer(line, offset, column))
            declarations[len(tokens)] = (row, spelling)
            tokens.append((value, kind))
            offset = column + len(spelling)
        tokens.extend((token[0], None) for token in _DIFF_TOKEN.finditer(line, offset))
    return tokens, declarations


def _new_escapes(before, after, old_matches, new_matches):
    if not new_matches:
        return
    old_lines, new_lines = before.split("\n"), after.split("\n")
    existing, proposed = _positions(old_matches), _positions(new_matches)
    for tag, old_start, old_end, new_start, new_end in SequenceMatcher(None, old_lines, new_lines).get_opcodes():
        new_rows = range(new_start, new_end)
        if not any(row in proposed for row in new_rows):
            continue
        if tag == "equal":
            for row in new_rows:
                old_row = old_start + row - new_start
                for position in proposed.get(row, ()):
                    if position not in existing.get(old_row, ()):
                        yield row, position[1]
            continue
        if not any(row in existing for row in range(old_start, old_end)):
            for row in new_rows:
                for _, spelling, _, _ in proposed.get(row, ()):
                    yield row, spelling
            continue
        # Edited lines may retain a declaration even when their original line is gone.
        old_tokens, _ = _diff_tokens(old_lines, old_start, old_end, existing)
        new_tokens, declarations = _diff_tokens(new_lines, new_start, new_end, proposed)
        for _, start, size in SequenceMatcher(None, old_tokens, new_tokens).get_matching_blocks():
            for index in range(start, start + size):
                declarations.pop(index, None)
        yield from declarations.values()


def _parse_scan_results(reply, revision_count):
    reports = json.loads(reply.stdout)
    if reply.returncode or not isinstance(reports, list) or len(reports) != revision_count:
        raise ValueError("invalid compiler response")
    for report in reports:
        if not isinstance(report, dict) or not isinstance(report.get("declarations"), list) or "uncheckedLine" not in report:
            raise ValueError("invalid compiler response")
        line = report["uncheckedLine"]
        if line is not None and (type(line) is not int or line < 1):
            raise ValueError("invalid compiler response")
    return reports


def reject_new_keyword_escapes(agent_root: Path, worktree: Path, pending: dict) -> None:
    reject_keyword_changes(
        pending, before_path=lambda name: safe_target(worktree, Path(name)),
        after_path=lambda name: safe_target(agent_root, Path(name)),
    )


def reject_keyword_changes(pending, *, before_path, after_path):
    candidates = []
    for name, versions in pending.items():
        if Path(name).suffix.lower() not in {".cs", ".cshtml"}:
            continue
        after = _read_version(after_path(name), versions["after"])
        if _could_contain_keyword_escape(after):
            before = _read_version(before_path(name), versions["before"])
            candidates.append((name, before, after))
    if not candidates:
        return
    revisions = [{"Path": name, "Text": text} for name, before, after in candidates for text in (before, after)]
    try:
        reply = _run_editor(_prepare_tool(), "--escaped-keywords", json.dumps(revisions).encode("utf-8"), 6)
        reports = _parse_scan_results(reply, len(revisions))
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        names = ", ".join(name for name, _, _ in candidates)
        raise ValueError(f"C#予約語の確認を完了できません。.NET 9 SDKを確認してください: {names}") from error
    for (name, before, after), previous, proposed in zip(candidates, reports[::2], reports[1::2], strict=True):
        if line := proposed["uncheckedLine"] or (previous["uncheckedLine"] if proposed["declarations"] else None):
            raise ValueError(f"{name}:{line}: 条件付きコンパイル内の予約語を確認できないため反映を止めました。"
                             "条件分岐を確認し、変更対象の宣言をエスケープせずに改名してから再開してください。")
        if problem := next(_new_escapes(before, after, previous["declarations"], proposed["declarations"]), None):
            row, spelling = problem
            raise ValueError(f"{name}:{row + 1}: 新しく追加した宣言 {spelling} は反映できません。"
                             "エスケープせず、宣言と参照を意味のある未使用名に揃えてから再開してください。")
