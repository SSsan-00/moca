from dataclasses import dataclass
from difflib import SequenceMatcher
from itertools import groupby
import re


_HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@\n(.*?)(?=^@@ |\Z)", re.MULTILINE | re.DOTALL)


@dataclass
class _Change:
    start: int
    end: int
    removed: set[str]
    added: set[str]


def _meaningful(lines):
    return {line.removesuffix("\n") for line in lines if line.strip()}


def _recorded_changes(hunk):
    offset = 0
    for changed, group in groupby(hunk, key=lambda row: row.startswith(("+", "-"))):
        rows = list(group)
        after = [row[1:] for row in rows if not row.startswith("-")]
        if changed:
            yield _Change(offset, offset + len(after),
                          _meaningful(row[1:] for row in rows if row.startswith("-")), _meaningful(after))
        offset += len(after)


def _unique_hunk_start(checkpoint, hunk):
    after = [row[1:] for row in hunk if row.startswith((" ", "+"))]
    if not after:
        return None
    anchor = "\n" + "\n".join(after) + "\n"
    start = checkpoint.find(anchor)
    if start < 0 or checkpoint.find(anchor, start + 1) >= 0:
        return None
    return checkpoint.count("\n", 0, start)


def _overlaps(saved, proposed, anchor):
    removes_manual_line = not saved.added.isdisjoint(proposed.removed)
    restores_deleted_line = not saved.removed.isdisjoint(proposed.added)
    if anchor is None:
        return removes_manual_line or restores_deleted_line
    start, end = anchor + saved.start, anchor + saved.end
    return ((removes_manual_line and proposed.start < end and start < proposed.end)
            or (restores_deleted_line and proposed.start <= end and start <= proposed.end))


def recorded_reference_edit_overlap(old, new, operator_edits):
    if not operator_edits:
        return False
    before, after = old.splitlines(keepends=True), new.splitlines(keepends=True)
    changes = [_Change(i, j, _meaningful(before[i:j]), _meaningful(after[k:m]))
               for op, i, j, k, m in SequenceMatcher(None, before, after).get_opcodes() if op != "equal"]
    checkpoint = "\n" + "\n".join(old.splitlines()) + "\n"
    for patch in operator_edits:
        for match in _HUNK.finditer(patch):
            old_count, new_count, body = match.groups()
            hunk = [row for row in body.splitlines() if row != r"\ No newline at end of file"]
            if (sum(row.startswith((" ", "-")) for row in hunk) != int(old_count or 1) or
                    sum(row.startswith((" ", "+")) for row in hunk) != int(new_count or 1)):
                # 旧履歴では末尾に改行のない差分行が連結され、編集位置を復元できないことがある。
                return True
            anchor = _unique_hunk_start(checkpoint, hunk)
            if any(_overlaps(saved, proposal, anchor) for saved in _recorded_changes(hunk) for proposal in changes):
                return True
    return False
