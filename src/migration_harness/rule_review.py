"""保存済みの移行ルールを点検し、利用者の判断と検証根拠を版に結び付ける。"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .capsules import load_capsule_manifest
from .checker_rule_evidence import checker_rule_context
from .config import target_paths
from .files import safe_target
from .run_records import edit_run_record, execution_lock, load_saved_run, validate_run_id
from .source_context import target_context_snapshot
from .worktree_targets import target_hash, worktree_source_lock


def _review_state(config, run_id):
    """固定資料と現在の移行先を照合し、判断を保存するための版を返す。"""
    run_path, worktree, record, unit = load_saved_run(config, run_id)
    capsule, manifest = load_capsule_manifest(run_path)
    rules = manifest.get("rule_files", manifest.get("rule_chunks", []))
    decisions = record.get("detail_applicability", {})
    overrides = record.get("detail_overrides", {})
    evidence = record.get("detail_review_evidence", {})
    measurements = record.get("last_junie_input") or {}
    offered = {item["file"] for item in measurements.get("rules", [])}
    targets = {name.as_posix(): target_hash(safe_target(worktree, name)) for name in target_paths(unit)}
    snapshot = target_context_snapshot(capsule)
    reference_version = target_hash(safe_target(snapshot, Path("manifest.json")))
    history = record.get("rule_review_history", [])
    checker_results = {}
    checker_evidence = record.get("checker_rule_evidence")
    if (checker_evidence and config.get("checker_rule_evidence") and record.get("checker_exit_code") == 0
            and not (record.get("verification") or {}).get("inputs_changed_during_verification")):
        try:
            if checker_evidence["scope"] == checker_rule_context(run_path, worktree, record):
                checker_results = checker_evidence["results"]
        except (OSError, ValueError, KeyError):
            pass
    rows = []
    for rule in rules:
        name = rule["file"]
        if not re.fullmatch(r"rule-\d+\.txt", name):
            raise ValueError("保存されたルールIDが無効です。")
        source = safe_target(capsule, Path("materials") / name)
        data = source.read_bytes()
        if hashlib.sha256(data).hexdigest() != rule["sha256"]:
            raise ValueError(f"saved rule changed: {name}")
        body = data.decode("utf-8")
        model_decision = decisions.get(name)
        decision = overrides.get(name) or model_decision
        entries = [entry for entry in history if entry["rule"] == name and entry["sha256"] == rule["sha256"]]
        applicability = "required" if rule.get("usage", "required") == "required" else (decision or {}).get("applies")
        included = name in offered and not measurements.get("blocked")
        independent = _latest_evidence(entries, targets, decision, reference_version, {"passed", "failed"})
        if independent is None and name in checker_results:
            independent = {**checker_results[name], "source": "checker", "reason": "独立チェッカーがルール全体を検査"}
        if applicability == "required" or applicability is True or (included and name not in overrides):
            verification_state = independent["kind"] if independent else "human_review_needed"
        else:
            verification_state = "not_applicable" if applicability is False else "applicability_pending"
        rows.append({
            **rule, "integrity": "verified", "bytes": len(data), "characters": len(body),
            "saved_file_lines": len(body.splitlines()), "snapshot": f"capsule/materials/{name}",
            "model_decision": model_decision, "operator_decision": overrides.get(name),
            "applicability_evidence": _decision_spans(model_decision, evidence),
            "applicability": applicability, "included_for_last_launch": included,
            "read": _latest_evidence(entries, targets, decision, reference_version, {"read"}),
            "application": _latest_evidence(entries, targets, decision, reference_version, {"applied"}),
            "independent_verification": independent, "verification_state": verification_state,
            "history": entries,
        })
    payload = {"run_id": run_id, "targets": targets, "reference_version": reference_version, "rules": rows}
    payload["version"] = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return run_path, payload


def _decision_spans(decision, evidence):
    if not decision or not evidence:
        return []
    spans = [decision.get("context_evidence"), *decision.get("additional_context_evidence", [])]
    return [{"path": evidence[span["file"]]["path"], "lines": span["lines"]}
            for span in spans if span and span["file"] in evidence]


def _latest_evidence(entries, targets, decision, reference_version, kinds):
    """現在の採否と対象に有効な直近の根拠を探す。読了は資料の版で決まる。"""
    for entry in reversed(entries):
        if entry["kind"] in kinds and (
            entry["kind"] == "read" or (
                entry.get("targets") == targets and entry.get("decision") == decision
                and entry.get("reference_version") == reference_version
            )
        ):
            return entry
    return None


def review_rules(config, run_id, *, rule=None, kind=None, version=None, start=None, end=None, reason=None, reference=None):
    """出典の範囲を照合し、採否または利用者自身が確認した根拠を履歴へ加える。"""
    validate_run_id(run_id)
    with execution_lock(config, run_id), worktree_source_lock(Path(config["worktree_root"]) / run_id):
        run_path, report = _review_state(config, run_id)
        if kind is None:
            if any(value is not None for value in (rule, version, start, end, reason, reference)):
                raise ValueError("記録を追加する場合は --record を指定してください。")
            return report
        if kind not in {"include", "exclude", "read", "applied", "passed", "failed"}:
            raise ValueError("記録の種類を確認してください。")
        if version != report["version"]:
            raise ValueError("確認後に対象版または判定が更新されました。ルールを読み込み直してください。")
        item = next((row for row in report["rules"] if row["file"] == rule), None)
        if not item:
            raise ValueError("保存済みのルールIDを指定してください。")
        if type(start) is not int or type(end) is not int or not 1 <= start <= end <= item["saved_file_lines"]:
            raise ValueError("保存本文内の行範囲を指定してください。")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("記録の理由を指定してください。")
        if kind in {"applied", "passed", "failed"} and (
            not reference or not reference.strip() or None in report["targets"].values()
        ):
            raise ValueError("検証対象がそろっていることを確認し、結果やテストの参照先を指定してください。")
        if kind in {"include", "exclude"} and item.get("usage", "required") != "reference":
            raise ValueError("採否を変更できるのは参照ルールです。")
        entry = {"rule": rule, "sha256": item["sha256"], "kind": kind,
                 "start_line": start, "end_line": end, "reason": reason.strip(),
                 "reference": reference.strip() if reference else None, "targets": report["targets"],
                 "reference_version": report["reference_version"],
                 "decision": item["operator_decision"] or item["model_decision"], "recorded_at": datetime.now(timezone.utc).isoformat()}
        with edit_run_record(run_path / "run.json") as (record, changes):
            changes["rule_review_history"] = [*record.get("rule_review_history", []), entry]
            if kind in {"include", "exclude"}:
                changes["detail_overrides"] = {**record.get("detail_overrides", {}), rule: {
                    "applies": kind == "include", "reason": entry["reason"], "start_line": start, "end_line": end,
                    "sha256": entry["sha256"], "recorded_at": entry["recorded_at"],
                }}
        return _review_state(config, run_id)[1]
