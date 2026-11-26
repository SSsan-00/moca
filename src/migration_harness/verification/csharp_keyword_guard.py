from difflib import SequenceMatcher
import hashlib
import json
from pathlib import Path
import re
import subprocess

from ..context.php_literals import _CSHARP_RESERVED
from ..files import safe_target
from .roslyn_syntax import _prepare_tool, _run_editor

_ESCAPE = re.compile(r"@([A-Za-z_][A-Za-z_0-9]*)")
_DIFF_TOKEN = re.compile(r"@\w+|\w+|\S")


def _read_version(path, expected):
    data = path.read_bytes() if expected is not None else b""
    if expected is not None and hashlib.sha256(data).hexdigest() != expected:
        raise ValueError("移行先が更新されたため、C#予約語の確認をやり直してください。")
    return data.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")


def _positions(findings):
    rows = {}
    for item in findings:
        rows.setdefault(item["line"], []).append((item["column"], item["text"]))
    return rows


def _diff_tokens(lines, start, end, positions):
    tokens, declarations = [], {}
    for row in range(start, end):
        for token in _DIFF_TOKEN.finditer(lines[row]):
            spelling = token[0]
            declared = (token.start(), spelling) in positions.get(row, ())
            if declared:
                declarations[len(tokens)] = (row, spelling)
            tokens.append((spelling, declared))
    return tokens, declarations


def _new_escapes(before, after, old_matches, new_matches):
    if not new_matches:
        return
    old_lines, new_lines = before.split("\n"), after.split("\n")
    existing, proposed = _positions(old_matches), _positions(new_matches)
    for tag, a, b, c, d in SequenceMatcher(None, old_lines, new_lines).get_opcodes():
        if not any(row in proposed for row in range(c, d)):
            continue
        if tag == "equal":
            for row in range(c, d):
                for position in proposed.get(row, ()):
                    if position not in existing.get(a + row - c, ()):
                        yield row, position[1]
            continue
        if not any(row in existing for row in range(a, b)):
            for row in range(c, d):
                for _, spelling in proposed.get(row, ()):
                    yield row, spelling
            continue
        # Edits on the same line or formatting over several lines can preserve a declaration.
        old_tokens, _ = _diff_tokens(old_lines, a, b, existing)
        new_tokens, declarations = _diff_tokens(new_lines, c, d, proposed)
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
        if any(match[1] in _CSHARP_RESERVED for match in _ESCAPE.finditer(after)):
            before = _read_version(before_path(name), versions["before"])
            candidates.append((name, before, after))
    if not candidates:
        return
    revisions = [{"Path": name, "Text": text} for name, before, after in candidates for text in (before, after)]
    try:
        reply = _run_editor(_prepare_tool(), "--escaped-keywords", json.dumps(revisions).encode("utf-8"), 6)
        findings = json.loads(reply.stdout)
        if (reply.returncode or not isinstance(findings, list) or len(findings) != len(revisions)
                or any(not isinstance(scan, dict) or not isinstance(scan.get("declarations"), list)
                       or "uncheckedLine" not in scan or (
                           scan["uncheckedLine"] is not None
                           and (type(scan["uncheckedLine"]) is not int or scan["uncheckedLine"] < 1))
                       for scan in findings)):
            raise ValueError("invalid compiler response")
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        names = ", ".join(name for name, _, _ in candidates)
        raise ValueError(f"C#予約語の確認を完了できません。.NET 9 SDKを確認してください: {names}") from error
    for index, (name, before, after) in enumerate(candidates):
        previous, proposed = findings[2 * index:2 * index + 2]
        if line := proposed["uncheckedLine"] or (previous["uncheckedLine"] if proposed["declarations"] else None):
            raise ValueError(f"{name}:{line}: 条件付きコンパイル内の予約語を確認できないため反映を止めました。"
                             "条件分岐を確認し、変更対象の宣言は@を付けずに改名してから再開してください。")
        if problem := next(_new_escapes(before, after, previous["declarations"], proposed["declarations"]), None):
            row, spelling = problem
            raise ValueError(f"{name}:{row + 1}: 新しく追加した宣言 {spelling} は反映できません。"
                             "@を付けず、宣言と参照を意味のある未使用名に揃えてから再開してください。")
