"""移行と再開の主工程を進める。"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config as harness_config
from .config import find_unit
from .context.migration_materials import stage_migration_capsule
from .errors import RunInterrupted, RunScriptError, format_error
from .junie import junie_cli
from .runs import editor_queue as editor_queue_store, reference_refresh, resume_plan, run_records
from .runs.editor_queue import editor_save_pending
from .runs.migration_capsules import (
    execute_migration_capsule, finish_migration_followups, prepare_migration_materials,
)
from .runs.migration_ledger import migration_ledger, record_migration_candidates
from .runs.migration_workspace import prepare_migration_workspace, prepare_run
from .runs.resume_plan import (
    BUILD_THROUGH_STAGE, CHECKER_RESUME_STATUSES, VERIFIED_STATUSES, can_resume_build, can_resume_run,
    plan_build_resume, plan_checker_resume, plan_migration_resume, plan_stage_resume,
    previously_required_checker, stage_migration_rewind, validate_resume_plan,
)
from .runs.run_records import execution_lock
from .runs.stage_scripts import block_unknown_script, script_recovery
from .runs.stopped_repair import (
    plan_stopped_repair, preview_stopped_repair, require_selected_repair_lineage, resolve_stopped_repair,
)
from .runs.timings import freeze_timings
from .runs.verification_edits import apply_verification_edits
from .runs.worktree_targets import finish_pending_targets, review_target_hash
from .verification.build_cycle import build_failure_review, respond_to_build_failure
from .verification.checker_cycle import repair_from_checker_instruction, resume_checker, review_checker
from .verification.diagnostic_log import remember_diagnostic
from .verification.pipeline import run_verification


def _run_prepared_with_junie(
    config: dict[str, Any],
    unit: dict[str, Any],
    run_path: Path,
    result: dict[str, Any],
    record: dict[str, Any],
    *,
    resuming: bool,
    followup_only: bool = False,
    through_capsule: int | None = None,
    expected_capsule_hashes: list[str] | None = None,
) -> dict[str, Any]:
    worktree_root, relative_targets, agent_root, agent_targets, capsule, manifest, through_capsule = (
        prepare_migration_workspace(config, unit, run_path, result, record, resuming=resuming,
                                     followup_only=followup_only, through_capsule=through_capsule)
    )
    material_instructions = prepare_migration_materials(
        config, unit, run_path, record, worktree_root, relative_targets, agent_root, capsule, manifest,
        followup_only=followup_only,
    )
    exit_code = record.get("junie_exit_code")
    with editor_queue_store.editor_queue(run_path) as queue:
        editor_queue_store.finish_prompts(queue, False)
    # 未完了のカプセルから再開する。保存済みの下書きがあっても完了数は先に進めない。
    start_index = record["completed_capsules"] + 1
    for index, context_path in enumerate(result["context_paths"][start_index - 1 : through_capsule], start=start_index):
        prompt, completion_path, completion_token, boundary_digest, verified_sources = stage_migration_capsule(
            unit, capsule, manifest, agent_root, relative_targets, index, context_path, result["capsule_count"],
            material_instructions, resume_first_capsule=resuming and index == start_index,
            expected_capsule_hashes=expected_capsule_hashes,
        )
        exit_code, completed = execute_migration_capsule(
            config, run_path, record, worktree_root, agent_root, relative_targets, agent_targets,
            index, result["capsule_count"], through_capsule, prompt, completion_path, completion_token,
            boundary_digest, exit_code, manifest=manifest, verified_sources=verified_sources,
            resuming=resuming and index == start_index,
        )
        if not completed:
            break

    if through_capsule is None:
        finish_migration_followups(config, run_path, record, worktree_root, agent_root,
                                    relative_targets, result["capsule_count"])

    if exit_code is None and record["completed_capsules"] != result["capsule_count"] and record["status"] != "editor_checkpoint":
        raise RuntimeError("context preparation produced no capsules")
    result = {
        **result,
        "junie_exit_code": exit_code,
        "agent_workspace_path": str(agent_root),
        "completed_capsules": record["completed_capsules"],
        "status": record["status"],
        "current_capsule": record["current_capsule"],
        "error": record.get("error"),
    }
    if through_capsule is not None and record["completed_capsules"] == through_capsule:
        result["requested_range_complete"] = True
    if record["status"] == "junie_finished":
        return _verify_worktree(config, unit, result, record)
    return result


def _verify_worktree(
    config: dict[str, Any], unit: dict[str, Any], result: dict[str, Any], record: dict[str, Any],
    *, request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    run_path = Path(result["run_path"])
    record_path = run_path / "run.json"

    def process_queued_edits():
        return _run_prepared_with_junie(config, unit, run_path, result, record, resuming=True, followup_only=True)

    try:
        return run_verification(config, unit, result, record, process_queued_edits=process_queued_edits, request=request)
    finally:
        if record.get("active_stage") is not None:
            run_records.write_run_record(record_path, record, timing_stage=None)


def run_with_junie(config: dict[str, Any], unit_id: str, *, run_id: str | None = None) -> dict[str, Any]:
    junie_cli.require_interactive_cli()
    run_id = str(uuid.UUID(run_id)) if run_id else str(uuid.uuid4())
    with execution_lock(config, run_id):
        with execution_lock(config, "prepare", wait=True):
            result = prepare_run(config, unit_id, run_id)
        unit = find_unit(config, unit_id)
        record_path = Path(result["run_path"]) / "run.json"
        record = json.loads(record_path.read_text(encoding="utf-8"))
        return _run_prepared_with_junie(config, unit, Path(result["run_path"]), result, record, resuming=False)


def resume_with_junie(
    config: dict[str, Any], run_id: str, *, build_response: dict[str, Any] | None = None,
    checker_instruction: str | None = None, verification_request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """既存Runの整合性を検証して保存地点から再開する。"""
    run_id = str(uuid.UUID(run_id))
    with execution_lock(config, run_id):
        return _resume_with_junie(config, run_id, build_response=build_response, checker_instruction=checker_instruction,
                                  verification_request=verification_request)


def _resume_with_junie(
    config: dict[str, Any], run_id: str, *, build_response: dict[str, Any] | None = None,
    checker_instruction: str | None = None, verification_request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Runとカプセルを照合し、移行またはビルドから再開する。"""
    run_path, worktree_path, record, unit = run_records.load_saved_run(config, run_id)
    block_unknown_script(record)
    if record.get("migration_stop_unconfirmed"):
        raise ValueError("前回の Junie と子プロセスの終了を確認できませんでした。"
                         "残存プロセスを終了し、保存版と作業領域を比較して必要な編集を新しい Run に引き継いでください。")
    require_selected_repair_lineage(record.get("repair_pending", {}))
    if editor_save_pending(run_path):
        with editor_queue_store.editor_queue(run_path, recover=(worktree_path, harness_config.target_paths(unit))):
            pass
        _, _, record, _ = run_records.load_saved_run(config, run_id)
    if "stage_timings" in record:
        record["stage_timings"] = freeze_timings(record)
        record["active_stage"] = None
    status = record.get("status")
    if build_response is not None and status != "build_failed":
        raise ValueError("表示中の Run のビルド結果が変わりました。")
    if checker_instruction is not None and status not in {"checker_needs_review", "checker_failed"}:
        raise ValueError("表示中の Run のチェッカー結果が変わりました。")
    stage = verification_request.get("start_stage") if verification_request else None
    plan = validate_resume_plan(config, run_id, record, verification_request, stage)
    if not verification_request and not resume_plan.can_resume_run(config, record):
        if status in CHECKER_RESUME_STATUSES:
            raise ValueError("resuming the checker requires a successful build and a checker command")
        raise ValueError(f"run is not resumable from status {status!r}")
    history = record.get("repair_history", [])
    if (
        record.get("error") == "repair interrupted; inspect Junie's workspace"
        and "repair_pending" not in record and history
        and "junie_exit_code" not in history[-1]
        and not record.get("target_apply_pending")
    ):
        # 旧版はJunieを起動した時点で試行を履歴に追加していた。
        history.pop()
        record.pop("error", None)
        run_records.write_run_record(run_path / "run.json", record)

    result, migration_complete, waiting_at_final_checkpoint, script_retry = resume_plan.load_resume_context(
        config, run_id, run_path, worktree_path, record, unit, status,
    )
    pinned_only = (script_retry or build_response or checker_instruction or
                   verification_request and (stage != "migration" or plan.get("rewind_to")))
    if pinned_only and record.get("reference_refresh"):
        raise ValueError("参考資料の変更が未反映です。通常の再開で確認してください。")
    if not pinned_only:
        try:
            reference_refresh.refresh_target_context_on_resume(config, unit, run_path, worktree_path, record,
                                              migration_complete=migration_complete or waiting_at_final_checkpoint)
        except (OSError, ValueError, RuntimeError) as exc:
            record["error"] = format_error(exc)
            run_records.write_run_record(run_path / "run.json", record)
            raise
    record.pop("last_stop", None)
    if stage == "migration":
        result["request_id"] = verification_request["id"]
        junie_cli.require_interactive_cli()
        if previously_required_checker(record):
            record["checker_required"] = True
        operation = {
            **plan, "id": verification_request["id"], "accepted_at": datetime.now(timezone.utc).isoformat(),
        }
        if "rewind_to" in plan:
            remember_diagnostic(run_path, record, "build")
            remember_diagnostic(run_path, record, "checker")
            stage_migration_rewind(run_path, worktree_path, unit, record, plan, operation)
            record.pop("build_log_path", None)
            record.pop("checker_log_path", None)
        if plan["through_capsule"] is None:
            record.pop("migration_range_stop", None)
        else:
            record["migration_range_stop"] = plan["through_capsule"]
        record.setdefault("resume_operations", []).append(operation)
        for key in ("verification", "build_exit_code", "checker_exit_code", "build_operation_id", "checker_build", "error"):
            record.pop(key, None)
        run_records.write_run_record(run_path / "run.json", record, invalidate_migration_mappings="rewind_to" in plan)
        return _run_prepared_with_junie(
            config, unit, run_path, result, record, resuming=True, through_capsule=plan["through_capsule"],
            expected_capsule_hashes=plan["capsule_hashes"],
        )
    if stage == "checker":
        return resume_checker(config, unit, result, record, plan, verification_request["id"])
    if verification_request:
        if not (migration_complete or waiting_at_final_checkpoint):
            raise ValueError("移行が完了してから、ビルドまたはチェッカーを実行してください。")
        targets = harness_config.target_paths(unit)
        record_path = run_path / "run.json"
        finish_pending_targets(run_path / "agent-workspace", worktree_path, targets, record, record_path)
        result["request_id"] = verification_request["id"]
        if verification_request["intent"] == "checker_repair":
            if status not in {"checker_needs_review", "checker_failed"}:
                raise ValueError("チェッカー指摘が更新されました。最新の結果を確認してください。")
            if len(history) >= config["max_repair_attempts"]:
                raise ValueError("この Run の修正回数が上限に達しました。残った指摘はエディタで修正してください。")
        verification_request["build_targets"] = apply_verification_edits(run_path, worktree_path, unit, verification_request)
        if stage in {"build", BUILD_THROUGH_STAGE}:
            if previously_required_checker(record):
                record["checker_required"] = True
            operation = {
                **verification_request, "build_targets": verification_request["build_targets"].copy(),
                "accepted_at": datetime.now(timezone.utc).isoformat(),
            }
            record.setdefault("resume_operations", []).append(operation)
            for key in ("verification", "build_exit_code", "checker_exit_code", "build_operation_id", "error"):
                record.pop(key, None)
            record["status"] = "junie_finished"
            run_records.write_run_record(record_path, record)
        if verification_request["intent"] == "checker_repair":
            repair_from_checker_instruction(config, targets, worktree_path, run_path, record, verification_request["text"])
            verification_request["build_targets"] = {
                target.as_posix(): review_target_hash(worktree_path, target) for target in targets
            }
        return _verify_worktree(config, unit, result, record, request=verification_request)
    if migration_complete:
        if build_response is not None:
            respond_to_build_failure(config, harness_config.target_paths(unit), worktree_path, run_path, record, build_response)
        if checker_instruction is not None:
            targets = harness_config.target_paths(unit)
            record_path = run_path / "run.json"
            finish_pending_targets(run_path / "agent-workspace", worktree_path, targets, record, record_path)
            repair_from_checker_instruction(config, targets, worktree_path, run_path, record, checker_instruction)
        if script_retry:
            record.update(status="junie_finished")
            record.pop("error", None)
            record.pop("script_phase", None)
        with editor_queue_store.editor_queue(run_path) as queue:
            pending = any(prompt["status"] != "sent" for prompt in queue["prompts"])
            pending_edits = bool(queue["edits"])
        if pending or pending_edits:
            if "restore_from" in record.get("repair_pending", {}):
                raise ValueError("下書きの復元計画と未反映の操作が競合しています。")
            if pending:
                junie_cli.require_interactive_cli()
            return _run_prepared_with_junie(config, unit, run_path, result, record, resuming=True, followup_only=True)
        # 検証済みRunでも、再開時は現行ファイルをビルドから確認する。
        return _verify_worktree(config, unit, result, record)
    junie_cli.require_interactive_cli()
    record.pop("error", None)
    if script_retry:
        record["status"] = "prepared"
    return _run_prepared_with_junie(
        config, unit, run_path, result, record, resuming=record["status"] != "prepared",
    )
