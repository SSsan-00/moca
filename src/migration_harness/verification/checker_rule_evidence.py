"""ルール別の独立チェッカー結果を、今回の検証依頼と照合する。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory

from ..context.capsules import load_capsule_manifest
from ..files import safe_target
from ..context.source_context import target_context_snapshot
from ..runs.worktree_targets import target_hash
from .verification_files import configured_verification_checker, has_verification_evidence, verification_files, verification_scripts


MAX_RECEIPT_BYTES = 256 * 1024


def _saved_verification_context(verification):
    if not isinstance(verification, dict) or not {
        "build_project", "checker_command", "scripts", "input_files",
    }.issubset(verification):
        return None
    command, scripts, inputs = (verification[key] for key in ("checker_command", "scripts", "input_files"))
    if (not isinstance(command, list) or not command or not isinstance(scripts, dict) or not isinstance(inputs, dict)
            or not all(isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest) for digest in inputs.values())):
        return None
    return deepcopy({
        "build_project": verification["build_project"], "checker_command": command,
        "scripts": scripts, "input_files": inputs,
    })


def current_checker_rule_results(config, run_path, worktree, record, target):
    evidence = record.get("checker_rule_evidence")
    verification = record.get("verification")
    if not (
        config.get("checker_rule_evidence") and isinstance(evidence, dict) and has_verification_evidence(verification)
        and record.get("checker_exit_code") == 0 and verification.get("scripts_passed") is True
        and verification.get("inputs_changed_during_verification") is False
    ):
        return {}
    saved = _saved_verification_context(verification)
    if saved is None or evidence.get("verification_context") != saved:
        return {}
    try:
        command = configured_verification_checker(config, verification)
        scripts = verification_scripts(config, command)
        if (command != saved["checker_command"] or scripts != saved["scripts"]
                or config["build_project"] != saved["build_project"]
                or verification_files(config, worktree, target, command, scripts) != saved["input_files"]):
            return {}
        scope = checker_rule_context(run_path, worktree, record)
        if scope is None or scope != evidence.get("scope"):
            return {}
        results = evidence["results"]
        return results if isinstance(results, dict) else {}
    except (OSError, ValueError, KeyError):
        return {}


def checker_rule_context(run_path: Path, worktree: Path, record: dict) -> dict | None:
    """毎回の確認対象を、採否、保存資料、実ファイルと結び付ける。"""
    capsule, manifest = load_capsule_manifest(run_path)
    rules = {}
    decisions = record.get("detail_applicability", {})
    overrides = record.get("detail_overrides", {})
    for rule in manifest["rule_files"]:
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


@contextmanager
def rule_checker_exchange(run_path: Path, worktree: Path, record: dict, *, enabled: bool):
    if not enabled:
        yield {}, None
        return
    try:
        scope = checker_rule_context(run_path, worktree, record)
    except (OSError, ValueError, KeyError):
        record["checker_rule_evidence_error"] = "verification context unavailable"
        scope = None
    if not scope:
        yield {}, None
        return
    verification_context = _saved_verification_context(record.get("verification"))
    with TemporaryDirectory(prefix="rule-check-", dir=run_path) as temporary:
        directory = Path(temporary)
        request_id = uuid.uuid4().hex
        request = directory / "request.json"
        response = directory / "receipt.json"
        request.write_text(json.dumps({
            "schema": 1, "request_id": request_id, **scope,
            "rules": {name: {field: item[field] for field in ("sha256", "saved_file_lines")}
                      for name, item in scope["rules"].items()},
        }, ensure_ascii=False), encoding="utf-8")

        def collect():
            if not response.exists():
                return
            try:
                if not stat.S_ISREG(response.lstat().st_mode):
                    raise ValueError("checker rule receipt: not a regular file")
                with response.open("rb") as stream:
                    data = stream.read(MAX_RECEIPT_BYTES + 1)
                results = parse_rule_receipt(data, request_id, scope["rules"])
                if checker_rule_context(run_path, worktree, record) != scope:
                    raise ValueError("checker rule receipt: input changed")
            except (OSError, ValueError, KeyError, RecursionError):
                record["checker_rule_evidence_error"] = "receipt invalid or verification inputs changed"
                return
            if results:
                record["checker_rule_evidence"] = {
                    "request_id": request_id, "scope": scope, "results": results,
                    "verification_context": verification_context,
                }

        yield {"env": {
            **os.environ, "MIGRATION_RULE_CHECK_REQUEST": str(request), "MIGRATION_RULE_CHECK_RECEIPT": str(response),
        }}, collect
