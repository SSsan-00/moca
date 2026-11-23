"""ルール別の独立チェッカー結果を、今回の検証依頼と照合する。"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .capsules import load_capsule_manifest
from .files import safe_target
from .source_context import target_context_snapshot
from .worktree_targets import target_hash


MAX_RECEIPT_BYTES = 256 * 1024


def checker_rule_context(run_path: Path, worktree: Path, record: dict) -> dict | None:
    """毎回の確認対象を、採否、保存資料、実ファイルと結び付ける。"""
    capsule, manifest = load_capsule_manifest(run_path)
    rules = {}
    decisions = record.get("detail_applicability", {})
    overrides = record.get("detail_overrides", {})
    for rule in manifest.get("rule_files", manifest.get("rule_chunks", [])):
        name = rule["file"]
        decision = overrides.get(name) or decisions.get(name)
        if rule.get("usage", "required") != "required" and (decision or {}).get("applies") is not True:
            continue
        if not re.fullmatch(r"rule-\d+\.txt", name) or name in rules:
            raise ValueError("checker rule context: invalid or repeated rule")
        data = safe_target(capsule, Path("materials") / name).read_bytes()
        if hashlib.sha256(data).hexdigest() != rule["sha256"]:
            raise ValueError("checker rule context: saved rule changed")
        lines = len(data.decode("utf-8").splitlines())
        if lines:
            rules[name] = {"sha256": rule["sha256"], "saved_file_lines": lines, "decision": decision}
    if not rules:
        return None
    names = record.get("targets") or [record["target"]]
    targets = {name: target_hash(safe_target(worktree, Path(name))) for name in names}
    if not all(targets.values()):
        return None
    return {
        "capsule_version": target_hash(safe_target(capsule, Path("manifest.json"))),
        "reference_version": target_hash(safe_target(target_context_snapshot(capsule), Path("manifest.json"))),
        "targets": targets, "rules": rules,
    }


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("checker rule receipt: duplicate JSON key")
        result[key] = value
    return result


def parse_rule_receipt(
    data: bytes, request_id: str, rules: dict[str, dict],
) -> dict[str, dict]:
    """今回の依頼でルール全体を検査した明示結果だけを返す。"""
    if not data or len(data) > MAX_RECEIPT_BYTES:
        raise ValueError("checker rule receipt: invalid size")
    document = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_keys)
    if (not isinstance(document, dict) or set(document) != {"schema", "request_id", "results"}
            or type(document["schema"]) is not int or document["schema"] != 1
            or document["request_id"] != request_id or not isinstance(document["results"], list)
            or len(document["results"]) > len(rules)):
        raise ValueError("checker rule receipt: invalid schema or request")
    covered = {}
    seen = set()
    for result in document["results"]:
        if not isinstance(result, dict) or set(result) != {
            "rule", "sha256", "start_line", "end_line", "status", "evidence",
        }:
            raise ValueError("checker rule receipt: invalid result")
        name = result["rule"]
        if not isinstance(name, str) or name not in rules or name in seen:
            raise ValueError("checker rule receipt: invalid or repeated rule")
        seen.add(name)
        rule = rules[name]
        start, end = result["start_line"], result["end_line"]
        evidence = result["evidence"]
        if (result["sha256"] != rule["sha256"] or type(start) is not int or type(end) is not int
                or not 1 <= start <= end <= rule["saved_file_lines"]
                or result["status"] not in ("passed", "failed", "not_checked")
                or not isinstance(evidence, str) or not 0 < len(evidence.strip()) <= 2000):
            raise ValueError("checker rule receipt: invalid scope or evidence")
        if result["status"] == "not_checked" or (start, end) != (1, rule["saved_file_lines"]):
            continue
        covered[name] = {
            "kind": result["status"], "start_line": start, "end_line": end,
            "reference": evidence.strip(),
        }
    return covered
