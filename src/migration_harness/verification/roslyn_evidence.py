from __future__ import annotations

import hashlib
import json
from collections import Counter
from difflib import unified_diff

from . import roslyn_syntax
from ..errors import CheckerRepairBlocked
from ..runs.worktree_targets import read_target, review_target_hash, worktree_source_lock


def roslyn_repair_diff(worktree, agent_root, targets):
    diff = ""
    truncated = False
    for target in targets:
        try:
            _, old = read_target(worktree, target)
            _, new = read_target(agent_root, target)
            if len(old) + len(new) > 512_000:
                truncated = True
                continue
        except (OSError, ValueError):
            truncated = True
            continue
        for line in unified_diff(old.splitlines(), new.splitlines(), f"a/{target.as_posix()}", f"b/{target.as_posix()}", lineterm=""):
            if len(diff) + len(line) >= 8_000:
                return diff, True
            diff += line + "\n"
    return diff, truncated


def roslyn_diagnostic_lines(report):
    return [f"{item['path']}({item['line']},{item['column']}): {item['code']}: {item['message']}"
            for item in report.get("findings", [])]


def roslyn_fingerprints(report):
    counts = Counter((item["path"], item["code"], item["message"]) for item in report.get("findings", []))
    return {hashlib.sha256(json.dumps((key, i)).encode("utf-8")).hexdigest()
            for key, count in counts.items() for i in range(count)}


def refresh_roslyn_repair(run_path, worktree, targets, record, details):
    adopted = record["target_hashes"].copy()

    def verify_adoption():
        if adopted != {path.as_posix(): review_target_hash(worktree, path) for path in targets}:
            raise CheckerRepairBlocked("verification_inputs_changed", "Saved targets changed outside the editor handoff.")

    with worktree_source_lock(worktree):
        verify_adoption()
        report = roslyn_syntax.syntax_diagnostics(worktree, run_path, targets)
        verify_adoption()
    if report["status"] == "blocked":
        raise CheckerRepairBlocked("handoff_unsafe", "The refreshed syntax diagnostic did not stop safely.")
    summary = "\n".join(roslyn_diagnostic_lines(report))
    if report.get("truncated") or len(summary) > 8_000:
        raise CheckerRepairBlocked("handoff_unsafe", "The refreshed syntax diagnostic is too large to send.")
    fingerprints = sorted(roslyn_fingerprints(report))
    details.update(roslyn_versions=adopted, findings=fingerprints)
    # Keep the outer preflight's report object in sync if continuation fails.
    current = record.setdefault("roslyn_preflight", {})
    current.clear()
    current.update(report, status="repairing", target_versions=adopted, fingerprints=fingerprints)
    record_roslyn_time(record, report)
    if summary:
        return summary
    if report["status"] == "clear":
        return "No C# syntax errors remain. Preserve the merged edits and follow the operator's instruction."
    return "Current C# syntax diagnostics are unavailable. Do not use old diagnostics; follow the operator's instruction."


def record_roslyn_time(record, report):
    record["roslyn_preflight_seconds"] = [*record.get("roslyn_preflight_seconds", [])[-4:], report["seconds"]]
