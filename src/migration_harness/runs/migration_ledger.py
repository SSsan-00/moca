from __future__ import annotations

from pathlib import Path
from typing import Any

from . import run_records
from .. import config as harness_config
from ..context import capsules as capsule_files
from ..context.capsules import is_php_input
from ..context.context_handoff import unresolved_junie_memos, unresolved_operator_notes
from ..verification import build_receipt
from ..verification.checker_output import CHECKER_SUCCESS_MESSAGE
from ..verification.verification_files import (
    configured_verification_checker, verification_files, verification_scripts,
)
from .run_records import edit_run_record
from .worktree_targets import review_target_hash


def migration_ledger(config: dict[str, Any], run_id: str) -> dict[str, Any]:
    """保存した対応を現在の移行先で照合し、閲覧用の状態をまとめる。"""
    run_path, worktree, record, unit = run_records.load_saved_run(config, run_id)
    _, manifest = capsule_files.load_capsule_manifest(run_path)
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

    capsules = []
    total = 0
    for part in manifest["parts"]:
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
                pending = "candidate_snapshot" in mapping
                entry["mapping"] = {
                    **{key: mapping[key] for key in ("path", "start_line", "end_line")},
                    "valid": valid,
                }
                if valid and pending:
                    entry["mapping"].update(snapshot_id=mapping["candidate_snapshot"], anchors=mapping["anchors"])
            sources.append(entry)
            total += 1
        capsules.append({"index": part["index"], "sources": sources})

    verification = record.get("verification") or {}
    saved_checker = verification.get("checker_command")
    saved_configuration = verification.get("configured_checker_command", saved_checker)
    # 旧形式のnullは、未設定とビルドのみで省略した場合の両方で保存されていた。
    ambiguous_checker = saved_checker is None and "configured_checker_command" not in verification
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
    # worktreeは直接編集しない前提。設定と移行対象の版だけを照合し、全体は走査しない。
    verification_passed = (
        not record.get("target_apply_pending")
        and record.get("completed_capsules") == len(capsules)
        and record.get("build_exit_code") == 0
        and verification.get("build_project") == config["build_project"]
        and verification.get("scripts", {}) == scripts
        and verification.get("scripts_passed") is True
        and not ambiguous_checker
        and not (record.get("checker_required") and saved_checker is None)
        and saved_configuration == configured_checker == saved_checker
        and (status == "build_succeeded" if saved_checker is None else checker_passed)
    )
    if verification_passed:
        current_targets = {path: current_hash(path) for path in targets}
        verification_passed = None not in current_targets.values() and current_targets == verification.get("target_hashes")
    input_status = None
    if verification_passed:
        current_inputs = verification_files(config, worktree, unit["target"], configured_checker, scripts)
        saved_inputs = verification.get("input_files")
        if verification.get("inputs_changed_during_verification") or (
            saved_inputs is not None and saved_inputs != current_inputs
        ) or any(digest is None for digest in current_inputs.values()):
            input_status = "changed"
        elif saved_inputs is None and current_inputs:
            input_status = "legacy"
        if input_status:
            verification_passed = False
    if verification_passed and verification.get("checker_only"):
        try:
            build_receipt.check_build_receipt(config, worktree, record)
        except ValueError:
            verification_passed = False

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
            "complete": verification_passed and total > 0,
            "verification_passed": verification_passed,
            **({"verification_input_status": input_status} if input_status else {}),
            "total": total,
        },
    }


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
