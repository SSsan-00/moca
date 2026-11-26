from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import run_records
from .. import config as harness_config
from ..context import capsules as capsule_files
from ..context.capsules import is_php_input
from ..context.context_handoff import unresolved_junie_memos, unresolved_operator_notes
from ..verification import build_receipt
from ..verification.checker_output import CHECKER_SUCCESS_MESSAGE, accepted_findings, checker_findings
from ..verification.verification_files import (
    configured_verification_checker, has_verification_evidence, verification_files, verification_scripts,
)
from .run_records import edit_run_record
from .worktree_targets import review_target_hash


def migration_ledger(config: dict[str, Any], run_id: str) -> dict[str, Any]:
    run_path, worktree, record, unit = run_records.load_saved_run(config, run_id)
    _, manifest = capsule_files.load_capsule_manifest(run_path)
    if manifest.get("unit_id", unit["id"]) != unit["id"]:
        raise ValueError("run manifest does not match the recorded migration unit")
    mappings = record.get("migration_mappings", {})
    target_names = harness_config.target_paths(unit)
    targets = [path.as_posix() for path in target_names]
    operator_notes = unresolved_operator_notes(run_path, worktree, target_names, record)
    hashes: dict[str, str | None] = {}

    def current_hash(path: str) -> str | None:
        if path not in hashes:
            try:
                hashes[path] = review_target_hash(worktree, Path(path))
            except OSError:
                hashes[path] = None
        return hashes[path]

    capsules, source_count = _ledger_capsules(manifest["parts"], mappings, current_hash)
    verification_passed, input_status = _ledger_verification(
        config, run_path, worktree, record, unit["target"], targets, len(capsules), current_hash,
    )
    return {
        "run_id": run_id,
        "target": unit["target"],
        "targets": targets,
        "capsules": capsules,
        "unresolved_junie_memos": unresolved_junie_memos(
            run_path, worktree, tuple(Path(path) for path in targets), record,
        ),
        "unresolved_operator_notes": operator_notes,
        "completion": {
            "complete": verification_passed and source_count > 0,
            "verification_passed": verification_passed,
            **({"verification_input_status": input_status} if input_status else {}),
            "total": source_count,
        },
    }


def _ledger_capsules(
    parts: list[dict[str, Any]], mappings: dict[str, Any], current_hash: Callable[[str], str | None],
) -> tuple[list[dict[str, Any]], int]:
    capsules = []
    source_count = 0
    for part in parts:
        sources = []
        for index, source in enumerate(part.get("sources", [])):
            if not is_php_input(source):
                continue
            entry = {key: source[key] for key in ("path", "start_line", "end_line", "character_slices", "symbol") if key in source}
            # 表示用IDの範囲番号は1始まり、保存済み辞書のキーは0始まり。
            entry["id"] = f"{part['index']}:{index + 1}"
            key = f"{part['index']}:{index}"
            mapping = mappings.get(key)
            if mapping:
                path = mapping["path"]
                valid = current_hash(path) == mapping["sha256"]
                entry["mapping"] = {
                    **{key: mapping[key] for key in ("path", "start_line", "end_line")},
                    "valid": valid,
                }
                if valid and "candidate_snapshot" in mapping:
                    entry["mapping"].update(snapshot_id=mapping["candidate_snapshot"], anchors=mapping["anchors"])
            sources.append(entry)
        capsules.append({"index": part["index"], "sources": sources})
        source_count += len(sources)
    return capsules, source_count


def _ledger_verification(
    config: dict[str, Any], run_path: Path, worktree: Path, record: dict[str, Any], primary_target: str, targets: list[str],
    capsule_count: int, current_hash: Callable[[str], str | None],
) -> tuple[bool, str | None]:
    verification = record.get("verification") or {}
    saved_checker = verification.get("checker_command")
    configured_checker = configured_verification_checker(config, verification)
    scripts = verification_scripts(config, configured_checker)
    status = record["status"]
    checker_passed = status == "checker_succeeded" and (
        record.get("checker_exit_code") == 0
        or ((record.get("checker_exit_code") or 0) > 0
            and record.get("checker_success_message") == CHECKER_SUCCESS_MESSAGE)
    )
    if status == "checker_accepted":
        approved_file_hashes = record.get("review_file_hashes", {})
        checker_passed = bool(approved_file_hashes) and all(
            digest is not None and current_hash(path) == digest for path, digest in approved_file_hashes.items()
        )
    # 台帳の閲覧で作業ツリー全体を走査しない。
    verification_passed = (
        not record.get("target_apply_pending")
        and record.get("completed_capsules") == capsule_count
        and record.get("build_exit_code") == 0
        and verification.get("build_project") == config["build_project"]
        and verification.get("scripts") == scripts
        and verification.get("scripts_passed") is True
        and "configured_checker_command" in verification and "checker_command" in verification
        and not (record.get("checker_required") and saved_checker is None)
        and verification["configured_checker_command"] == configured_checker == saved_checker
        and (status == "build_succeeded" if saved_checker is None else checker_passed)
    )
    if verification_passed:
        current_targets = {path: current_hash(path) for path in targets}
        verification_passed = None not in current_targets.values() and current_targets == verification.get("target_hashes")
    input_status = None
    if verification_passed:
        if not has_verification_evidence(verification) or "inputs_changed_during_verification" not in verification:
            input_status = "missing"
        else:
            current_inputs = verification_files(config, worktree, primary_target, configured_checker, scripts)
            if (verification["inputs_changed_during_verification"] is not False
                    or verification["input_files"] != current_inputs or None in current_inputs.values()):
                input_status = "changed"
        verification_passed = input_status is None
    if verification_passed and verification.get("checker_only"):
        try:
            build_receipt.check_build_receipt(config, worktree, record)
        except ValueError:
            verification_passed = False

    if verification_passed and status == "checker_accepted":
        log = run_path / "checker.log"
        try:
            if log.is_symlink() or not log.is_file():
                return False, "missing"
            findings, unrecognized = checker_findings(log, worktree)
        except (OSError, ValueError):
            return False, "missing"
        verification_passed = not unrecognized and bool(findings) and (
            {finding.finding_id for finding in findings} == accepted_findings(record, findings, approved_file_hashes)
        )

    return verification_passed, input_status


def record_migration_candidates(
    config: dict[str, Any], run_id: str, snapshot_id: str, workspace: dict[str, Any], target_hashes: dict[str, str],
) -> None:
    run_path, _, _, _ = run_records.load_saved_run(config, run_id)
    _, manifest = capsule_files.load_capsule_manifest(run_path)

    def file_line(segments: list[dict[str, Any]], index: int) -> tuple[str, int]:
        for segment in segments:
            local = index + 2 - segment["startLine"]
            if 1 <= local <= segment["lineCount"]:
                return segment["fileName"], local
        raise ValueError("アンカーがスナップショットのファイル範囲外です。")

    by_source: dict[str, list[tuple[int, str, int]]] = {}
    for anchor in workspace["anchors"]["manualAnchors"]:
        source, line = file_line(workspace["leftSegments"], anchor["leftLineNo"])
        target, target_line = file_line(workspace["rightSegments"], anchor["rightLineNo"])
        by_source.setdefault(source, []).append((line, target, target_line))
    with edit_run_record(run_path / "run.json") as (record, changes):
        if not record.get("completed_capsules") or record.get("status") in {
            "junie_running", "editor_followup", "repairing", "building", "checking",
        }:
            return
        mappings = {
            key: mapping for key, mapping in record.get("migration_mappings", {}).items()
            if "candidate_snapshot" not in mapping
        }
        # アンカーは行単位なので、1行内を文字で分割した原文には候補を自動付与しない。
        for part in manifest["parts"][:record["completed_capsules"]]:
            for index, source in enumerate(part.get("sources", [])):
                key = f"{part['index']}:{index}"
                prior = mappings.get(key)
                if (prior and target_hashes.get(prior["path"]) == prior["sha256"]) or source.get("character_slices"):
                    continue
                matches = [
                    (target, line) for source_line, target, line in by_source.get(Path(source["path"]).as_posix(), [])
                    if source["start_line"] <= source_line <= source["end_line"]
                ]
                # アンカーがない範囲や、対応先が複数ファイルにまたがる範囲は個別の確認に回す。
                if len({target for target, _ in matches}) != 1:
                    continue
                target = matches[0][0]
                lines = [line for _, line in matches]
                mappings[key] = {
                    "path": target, "start_line": min(lines), "end_line": max(lines),
                    "sha256": target_hashes[target], "candidate_snapshot": snapshot_id, "anchors": len(lines),
                }
        if mappings or "migration_mappings" in record:
            changes["migration_mappings"] = mappings
