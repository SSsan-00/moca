from collections import Counter
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
_ROSLYN_LINE_BREAK = re.compile(r"\r\n|[\r\n\x85\u2028\u2029]")


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
    return data.decode("utf-8-sig")


def _scan_version(name, source, report):
    error = ValueError(f"{name}: C#予約語の確認結果がソースと一致しないため反映を止めました。")
    if not isinstance(report, dict) or not isinstance(report.get("declarations"), list) or "uncheckedLine" not in report:
        raise error
    unchecked = report["uncheckedLine"]
    declarations = report["declarations"]
    if unchecked is None and not declarations:
        return [], {}, None
    lines = _ROSLYN_LINE_BREAK.split(source)
    if unchecked is not None and (type(unchecked) is not int or not 1 <= unchecked <= len(lines)):
        raise error
    rows = {}
    for item in declarations:
        if not isinstance(item, dict):
            raise error
        row, column, spelling, value, bindings, retainers = (
            item.get(key) for key in ("line", "column", "text", "value", "bindings", "retainers")
        )
        if (type(row) is not int or not 0 <= row < len(lines) or type(column) is not int or column < 0
                or not isinstance(spelling, str) or not spelling or not isinstance(value, str)
                or value not in _CSHARP_RESERVED or not isinstance(bindings, list) or not bindings
                or any(not isinstance(binding, str) or not binding for binding in bindings)
                or not isinstance(retainers, list) or not retainers or any(not isinstance(key, str) for key in retainers)
                or not lines[row].startswith(spelling, column)):
            raise error
        identity = tuple(sorted(set(bindings)))
        stable = tuple(sorted(set(retainers))) if all(retainers) else ()
        rows.setdefault(row, []).append((column, spelling, value, identity, stable))
    for items in rows.values():
        items.sort()
        if any(left[0] + len(left[1]) > right[0] for left, right in zip(items, items[1:])):
            raise error
    return lines, rows, unchecked


def _diff_tokens(lines, start, end, positions):
    tokens, declarations = [], {}
    for row in range(start, end):
        offset = 0
        line = lines[row]
        for column, spelling, value, bindings, _ in positions.get(row, ()):
            tokens.extend((token[0], None) for token in _DIFF_TOKEN.finditer(line, offset, column))
            declarations[len(tokens)] = (row, spelling)
            tokens.append((value, bindings))
            offset = column + len(spelling)
        tokens.extend((token[0], None) for token in _DIFF_TOKEN.finditer(line, offset))
    return tokens, declarations


def _unique_retainers(positions):
    counts = Counter((value, retainers) for row in positions.values() for _, _, value, _, retainers in row if retainers)
    return {key for key, count in counts.items() if count == 1}


def _without_retained_declarations(positions, retained):
    return {
        row: remaining for row, declarations in positions.items()
        if (remaining := [item for item in declarations if (item[2], item[4]) not in retained])
    }


def _new_escapes(old_lines, new_lines, existing, proposed):
    if not proposed:
        return
    if existing and (retained := _unique_retainers(existing) & _unique_retainers(proposed)):
        existing = _without_retained_declarations(existing, retained)
        proposed = _without_retained_declarations(proposed, retained)
        if not proposed:
            return
    if not existing:
        for row in sorted(proposed):
            for _, spelling, _, _, _ in proposed[row]:
                yield row, spelling
        return
    for tag, old_start, old_end, new_start, new_end in SequenceMatcher(None, old_lines, new_lines).get_opcodes():
        new_rows = range(new_start, new_end)
        if not any(row in proposed for row in new_rows):
            continue
        if tag == "equal":
            for row in new_rows:
                old_row = old_start + row - new_start
                old_positions = {position[:4] for position in existing.get(old_row, ())}
                for position in proposed.get(row, ()):
                    if position[:4] not in old_positions:
                        yield row, position[1]
            continue
        if not any(row in existing for row in range(old_start, old_end)):
            for row in new_rows:
                for _, spelling, _, _, _ in proposed.get(row, ()):
                    yield row, spelling
            continue
        # Edited lines may retain a declaration even when their original line is gone.
        old_tokens, _ = _diff_tokens(old_lines, old_start, old_end, existing)
        new_tokens, declarations = _diff_tokens(new_lines, new_start, new_end, proposed)
        for _, start, size in SequenceMatcher(None, old_tokens, new_tokens).get_matching_blocks():
            for index in range(start, start + size):
                declarations.pop(index, None)
        yield from declarations.values()


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
    names = ", ".join(name for name, _, _ in candidates)
    try:
        reply = _run_editor(_prepare_tool(), "--escaped-keywords", json.dumps(revisions).encode("utf-8"), 6)
        if reply.returncode:
            raise ValueError("compiler exited unsuccessfully")
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        raise ValueError(f"C#予約語の確認を完了できません。.NET 9 SDKを確認してください: {names}") from error
    try:
        reports = json.loads(reply.stdout)
        if not isinstance(reports, list) or len(reports) != len(revisions):
            raise ValueError("unexpected number of reports")
    except ValueError as error:
        raise ValueError(f"{names}: C#予約語の確認結果を読み取れないため反映を止めました。") from error
    for (name, before, after), previous, proposed in zip(candidates, reports[::2], reports[1::2], strict=True):
        old_lines, existing, old_unchecked = _scan_version(name, before, previous)
        new_lines, new_positions, new_unchecked = _scan_version(name, after, proposed)
        if line := new_unchecked or (old_unchecked if new_positions else None):
            raise ValueError(f"{name}:{line}: 条件付きコンパイル内の予約語を確認できないため反映を止めました。"
                             "条件分岐を確認し、変更対象の宣言をエスケープせずに改名してから再開してください。")
        if problem := next(_new_escapes(old_lines, new_lines, existing, new_positions), None):
            row, spelling = problem
            raise ValueError(f"{name}:{row + 1}: 新しく追加した宣言 {spelling} は反映できません。"
                             "エスケープせず、宣言と参照を意味のある未使用名に揃えてから再開してください。")
