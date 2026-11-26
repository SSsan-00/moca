from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from . import editor_queue as editor_queue_store, run_records
from .. import config as harness_config, files as harness_files
from ..config import find_unit
from ..context import capsules as capsule_files
from ..files import safe_target
from ..verification import build_receipt
from ..verification.verification_files import verification_files, verification_scripts
from .editor_queue import saved_edits
from .worktree_targets import target_versions
from .stage_scripts import block_unknown_script, require_completed_command
from .target_snapshots import (
    save_capsule_checkpoint as _save_capsule_checkpoint, save_target_snapshot as _save_target_snapshot,
    verified_snapshot_files as _verified_target_snapshot,
)
from .worktree_targets import target_hash, worktree_source_lock


_BUILD_RESUME_STATUSES = {"junie_finished", "building", "build_failed", "repairing"}


CHECKER_RESUME_STATUSES = {"checking", "checker_needs_review", "checker_failed"}


VERIFIED_STATUSES = {"build_succeeded", "checker_succeeded", "checker_accepted"}


_JUNIE_RESUME_STATUSES = {"prepared", "junie_running", "junie_failed", "interrupted", "editor_checkpoint", "editor_followup"}


_SCRIPT_RESUME_STATUSES = {"script_running", "script_failed"}


def previously_required_checker(record: dict[str, Any]) -> bool:
    """再開に必要なチェッカー設定が不足しているか判定する。"""
    verification = record.get("verification") or {}
    # 旧形式でCLI由来のnullはコマンドを復元できないが、CLI指定があったことは分かる。
    return (
        record.get("checker_required")
        or record.get("status") in CHECKER_RESUME_STATUSES | {"checker_succeeded", "checker_accepted"}
        or verification.get("checker_from_cli")
        or verification.get("configured_checker_command", verification.get("checker_command")) is not None
    )


def _missing_required_checker(config, record):
    return previously_required_checker(record) and config["checker_command"] is None


def require_saved_checker(config, record):
    if _missing_required_checker(config, record):
        raise ValueError("この Run に必要なチェッカーを設定してください。")


def can_resume_run(config: dict[str, Any], record: dict[str, Any]) -> bool:
    """保存された工程と設定から、再開操作を提供できるか判定する。"""
    status = record.get("status")
    if record.get("script_inflight") or record.get("unconfirmed_command") or record.get("migration_stop_unconfirmed") or status not in (
        _SCRIPT_RESUME_STATUSES | CHECKER_RESUME_STATUSES | VERIFIED_STATUSES | _BUILD_RESUME_STATUSES | _JUNIE_RESUME_STATUSES
    ):
        return False
    if "unit_id" in record:
        find_unit(config, record["unit_id"])
    if _missing_required_checker(config, record):
        return False
    return status not in CHECKER_RESUME_STATUSES or record.get("build_exit_code") == 0


def can_resume_build(record):
    return (record.get("status") in (
        _BUILD_RESUME_STATUSES | CHECKER_RESUME_STATUSES | VERIFIED_STATUSES
        | _SCRIPT_RESUME_STATUSES | {"editor_checkpoint"}
    ) and not record.get("script_inflight") and not record.get("unconfirmed_command") and bool(record.get("completed_capsules"))
        and record["completed_capsules"] == record.get("context_capsules")
        and not record.get("target_apply_pending"))


BUILD_THROUGH_STAGE = "build-through"


def plan_build_resume(config: dict[str, Any], run_id: str, *, through_completion: bool = False) -> dict[str, Any]:
    """指定したビルド再開の範囲を保存版と結び付ける。"""

    run_path, worktree, _, unit = run_records.load_saved_run(config, run_id)
    with editor_queue_store.editor_queue(run_path, recover=(worktree, harness_config.target_paths(unit))) as queue, worktree_source_lock(worktree):
        _, _, record, _ = run_records.load_saved_run(config, run_id)
        require_completed_command(record)
        if not can_resume_build(record):
            raise ValueError("移行が完了してからビルド工程を選んでください。")
        if record.get("target_apply_pending") or record.get("stopped_migration_draft") or any(prompt["status"] != "sent" for prompt in queue["prompts"]):
            raise ValueError("未反映の編集または Junie 指示があります。通常の再開で反映してください。")
        plan = {
            "run_id": run_id, "intent": "build", "start_stage": "build", "capsule_range": None,
            "targets": target_versions(worktree, unit), "edits": saved_edits(queue),
            "edit_revision": queue.get("edit_revision"), "completed_capsules": record["completed_capsules"],
            "repair": False, "invalidates": ["build", "checker"],
        }
        if through_completion:
            require_saved_checker(config, record)
            if record.get("reference_refresh"):
                raise ValueError("参考資料の変更が未反映です。通常の再開で確認してください。")
            checker = config["checker_command"]
            scripts = verification_scripts(config, checker)
            if after_migration := config.get("scripts", {}).get("after_migration"):
                scripts["after_migration"] = after_migration
            limit = config["max_repair_attempts"]
            inputs = {
                "build_project": config["build_project"], "checker_command": checker, "scripts": scripts,
                "files": verification_files(config, worktree, unit["target"], checker, scripts), "repair_limit": limit,
                **{key: config.get(key) for key in ("checker_review_free_output", "checker_rule_evidence",
                                                   "junie_apply_output_decisions", "junie_review_script_output",
                                                   "junie_ground_output_repairs")},
            }
            plan.update(start_stage=BUILD_THROUGH_STAGE, intent="build_through", repair=bool(limit),
                        stages=["build", "checker"] if checker else ["build"],
                        config_version=hashlib.sha256(json.dumps(inputs, sort_keys=True).encode("utf-8")).hexdigest())
    plan["version"] = hashlib.sha256(json.dumps(plan, sort_keys=True).encode("utf-8")).hexdigest()
    return plan


def plan_migration_resume(
    config: dict[str, Any], run_id: str, through_capsule: int | None, *, rewind_to: int | None = None,
) -> dict[str, Any]:

    run_path, worktree, _, unit = run_records.load_saved_run(config, run_id)
    with editor_queue_store.editor_queue(run_path) as queue, worktree_source_lock(worktree):
        _, _, record, _ = run_records.load_saved_run(config, run_id)
        require_completed_command(record)
        agent = run_path / "agent-workspace"
        capsule, manifest = capsule_files.load_capsule_manifest(run_path)
        statuses = {"junie_running", "junie_failed", "interrupted"}
        if rewind_to is not None:
            statuses |= VERIFIED_STATUSES | _BUILD_RESUME_STATUSES | CHECKER_RESUME_STATUSES | {"editor_checkpoint"}
            statuses.discard("junie_running")
        if (record["status"] not in statuses
                or agent.is_symlink() or not agent.is_dir() or not agent.resolve().is_relative_to(run_path)
                or record.get("target_apply_pending") or record.get("migration_rewind")
                or rewind_to is not None and (record.get("repair_pending") or record.get("stopped_migration_draft"))
                or record.get("operator_question") or queue["edits"]
                or any(prompt["status"] != "sent" for prompt in queue["prompts"])):
            raise ValueError("未処理の操作がない中断済みの移行から、区間を指定して再開できます。")
        completed = record["completed_capsules"]
        start = completed + 1 if rewind_to is None else rewind_to
        end = len(manifest["parts"]) if through_capsule is None else through_capsule
        if type(start) is not int or type(end) is not int or not 1 <= start <= end <= len(manifest["parts"]):
            raise ValueError("終了区間は、次の未完了区間から最後の区間までの番号を指定してください。")
        if rewind_to is None and start != record.get("current_capsule", start):
            raise ValueError("保存された移行進捗が一致しません。")
        hashes = []
        for part in manifest["parts"][:end]:
            context = Path(part["path"])
            if not context.is_absolute() or not context.is_relative_to(capsule):
                raise ValueError("保存された移行区間のパスが無効です。")
            digest = harness_files.sha256_file(safe_target(capsule, context.relative_to(capsule)))
            if part.get("sha256", digest) != digest:
                raise ValueError("保存された移行区間の内容が一致しません。")
            hashes.append(digest)
        plan = {
            "run_id": run_id, "intent": "migration", "start_stage": "migration",
            "capsule_range": [start, end], "through_capsule": through_capsule,
            "targets": target_versions(worktree, unit), "draft_targets": target_versions(agent, unit),
            "edit_revision": queue.get("edit_revision"), "status": record["status"],
            "manifest_sha256": harness_files.sha256_file(safe_target(capsule, Path("manifest.json"))),
            "capsule_hashes": hashes,
            "repair": through_capsule is None, "invalidates": ["build", "checker"],
        }
        if rewind_to is not None:
            checkpoints = record.get("capsule_checkpoints", {})
            origin = checkpoints.get(str(start - 1))
            latest = checkpoints.get(str(completed))
            if not origin or not latest or start > completed:
                raise ValueError("処理済み区間の開始前と現在の完了地点に保存版が必要です。")
            _verified_target_snapshot(run_path, origin)
            if plan["targets"] != latest["targets"] or plan["draft_targets"] != latest["targets"]:
                raise ValueError("保存後の編集を検出しました。現行版を保持したまま巻き戻しを中止します。")
            plan.update(rewind_to=start, rewind_checkpoint=origin, completed_capsules=completed)
            plan["invalidates"].extend(("later_capsules", "reviews"))
    plan["version"] = hashlib.sha256(json.dumps(plan, sort_keys=True).encode("utf-8")).hexdigest()
    return plan


def stage_migration_rewind(run_path, worktree, unit, record, plan, operation):
    with editor_queue_store.editor_queue(run_path) as queue, worktree_source_lock(worktree):
        if (queue.get("edit_revision") != plan["edit_revision"] or queue["edits"]
                or any(prompt["status"] != "sent" for prompt in queue["prompts"])):
            raise ValueError("巻き戻しと未反映の操作が競合しています。")
        targets = harness_config.target_paths(unit)
        agent = run_path / "agent-workspace"
        if any(target_hash(safe_target(agent, path)) != plan["draft_targets"][path.as_posix()] for path in targets):
            raise ValueError("巻き戻し前に Junie 作業領域が更新されました。")
        backup = _save_capsule_checkpoint(run_path, worktree, targets, plan["targets"])
        checkpoint = plan["rewind_checkpoint"]
        sources = _verified_target_snapshot(run_path, checkpoint)
        draft = _save_target_snapshot(run_path, sources, plan["targets"], workspace_unchanged=True)
        if draft["targets"] != checkpoint["targets"]:
            raise ValueError("巻き戻しの保存版が更新されました。")
        start = plan["rewind_to"]
        operation["rewind_backup"] = backup
        operation["superseded_checkpoints"] = {
            index: record["capsule_checkpoints"].pop(index)
            for index in list(record["capsule_checkpoints"]) if int(index) >= start
        }
        record.update(
            completed_capsules=start - 1, current_capsule=start, status="interrupted",
            target_hashes=plan["targets"].copy(),
            migration_rewind={"draft": draft, "restore_from": plan["draft_targets"]},
        )
        for key in ("checker_reviews", "migration_mappings", "review_file_hashes", "checker_success_message",
                    "checker_review_reason", "repair_history"):
            if key in record:
                operation.setdefault("superseded_reviews", {})[key] = record.pop(key)
        if "after_migration" in record.get("completed_script_phases", []):
            record["completed_script_phases"].remove("after_migration")
        record.get("script_command_progress", {}).pop("after_migration", None)


def plan_stage_resume(config, run_id, stage, *, through_capsule=None, rewind_to=None):
    block_unknown_script(run_records.load_saved_run(config, run_id)[2])
    if stage == "migration":
        return plan_migration_resume(config, run_id, through_capsule, rewind_to=rewind_to)
    if through_capsule is not None or rewind_to is not None:
        raise ValueError("区間は移行工程を選んだ場合に指定してください。")
    if stage in {"build", BUILD_THROUGH_STAGE}:
        return plan_build_resume(config, run_id, through_completion=stage == BUILD_THROUGH_STAGE)
    if stage == "checker":
        return plan_checker_resume(config, run_id)
    raise ValueError("再開する工程を選び直してください。")


def plan_checker_resume(config, run_id):
    """ビルド記録、対象版、チェッカー設定に対して単独再開の計画を返す。"""
    run_path, worktree, _, _ = run_records.load_saved_run(config, run_id)
    command = config["checker_command"]
    if command is None:
        raise ValueError("チェッカーを設定してから再開してください。")
    if verification_scripts(config, command) or config.get("scripts", {}).get("after_migration"):
        raise ValueError("ビルドまたはチェッカーの前後スクリプトがある Run は通常の再開を使用してください。")
    with editor_queue_store.editor_queue(run_path) as queue, worktree_source_lock(worktree):
        _, _, record, _ = run_records.load_saved_run(config, run_id)
        require_completed_command(record)
        if record.get("script_decisions_pending"):
            raise ValueError("Resolve the saved one-time script response before running the checker alone")
        if record.get("verification_script_pending") is not None:
            raise ValueError("Resolve the saved verification script response before running the checker alone")
        if (record["status"] not in VERIFIED_STATUSES | CHECKER_RESUME_STATUSES
                or not record.get("completed_capsules")
                or record["completed_capsules"] != record.get("context_capsules")
                or record.get("target_apply_pending") or record.get("migration_rewind")
                or record.get("repair_pending") or record.get("stopped_migration_draft") or queue["edits"]
                or any(prompt["status"] != "sent" for prompt in queue["prompts"])):
            raise ValueError("移行を完了し、保留中の編集・追加指示・修正を解決してから再開してください。")
        plan = {
            "run_id": run_id, "start_stage": "checker", "intent": "checker", "repair": False,
            "invalidates": ["checker"], "checker_command": command, "completed_capsules": record["completed_capsules"],
            "edit_revision": queue.get("edit_revision"), "build_receipt": build_receipt.check_build_receipt(config, worktree, record),
        }
    plan["version"] = hashlib.sha256(json.dumps(plan, sort_keys=True).encode("utf-8")).hexdigest()
    return plan


def validate_resume_plan(config, run_id, record, verification_request, stage):
    if stage in {"build", BUILD_THROUGH_STAGE}:
        plan = plan_build_resume(config, run_id, through_completion=stage == BUILD_THROUGH_STAGE)
        if any(verification_request.get(key) != value for key, value in plan.items()):
            raise ValueError("確認後に保存版または実行条件が更新されました。再開する版を確認し直してください。")
    elif stage == "migration":
        plan = plan_migration_resume(
            config, run_id, verification_request.get("through_capsule"), rewind_to=verification_request.get("rewind_to"),
        )
        if any(verification_request.get(key) != value for key, value in plan.items()):
            raise ValueError("確認後に保存版または移行区間が更新されました。")
        if plan["through_capsule"] is None:
            require_saved_checker(config, record)
    elif stage == "checker":
        plan = plan_checker_resume(config, run_id)
        if any(verification_request.get(key) != value for key, value in plan.items()):
            raise ValueError("確認後にビルド成果物またはチェッカー条件が更新されました。")
    else:
        require_saved_checker(config, record)
        return None
    return plan


def load_resume_context(config, run_id, run_path, worktree_path, record, unit, status):
    """保存カプセルと進捗を検証し、再開時の移行・検証の入力を復元する。"""
    max_steps = record.get("junie_max_steps", config["junie_max_steps"])
    if max_steps is not None and (isinstance(max_steps, bool) or not isinstance(max_steps, int) or max_steps < 1):
        raise ValueError("run record contains an invalid Junie max-steps value")
    resume_attempts = record.get("resume_attempts", 0)
    if not isinstance(resume_attempts, int) or isinstance(resume_attempts, bool) or resume_attempts < 0:
        raise ValueError("run record contains an invalid resume-attempt count")
    capsule_path, manifest = capsule_files.load_capsule_manifest(run_path)
    parts = manifest["parts"]
    context_paths = []
    for part in parts:
        part_path = part.get("path")
        if not isinstance(part_path, str):
            raise ValueError("run manifest contains an invalid context path")
        context_path = Path(part_path).resolve()
        if not isinstance(part.get("chars"), int) or part["chars"] < 0:
            raise ValueError("run manifest contains an invalid capsule character count")
        if not context_path.is_relative_to(capsule_path) or not context_path.is_file():
            raise ValueError("run manifest points to a missing or out-of-run context capsule")
        context_paths.append(str(context_path))

    completed_capsules = record.get("completed_capsules", 0)
    script_retry = status in _SCRIPT_RESUME_STATUSES
    # 移行後の失敗ならカプセル処理を繰り返さない。フックの失敗は保存済みの実行段階で分ける。
    migration_complete = (
        status in _BUILD_RESUME_STATUSES or status in CHECKER_RESUME_STATUSES or status in VERIFIED_STATUSES
        or (script_retry and record.get("script_phase") not in {"after_prepare", "before_migration"})
    )
    waiting_at_final_checkpoint = status in {"editor_checkpoint", "editor_followup"} and completed_capsules == len(context_paths)
    if (
        type(completed_capsules) is not int
        or (completed_capsules != len(context_paths) if migration_complete or waiting_at_final_checkpoint
            else not 0 <= completed_capsules < len(context_paths))
    ):
        raise ValueError("run record has inconsistent capsule completion")
    if not migration_complete and not waiting_at_final_checkpoint and record.get("current_capsule", completed_capsules + 1) != completed_capsules + 1:
        raise ValueError("run record has inconsistent capsule progress; inspect it before resuming")

    if record.get("context_capsules", len(context_paths)) != len(context_paths):
        raise ValueError("run record and manifest have inconsistent capsule counts")
    boundary = record.get("source_boundary_review_required")
    max_context = record.get("max_context_chars")
    if type(boundary) is not bool or type(max_context) is not int or max_context < 1:
        raise ValueError("run record is missing valid context preparation settings")
    result = {
        "run_id": run_id,
        "unit_id": unit["id"],
        "run_path": str(run_path),
        "worktree_path": str(worktree_path),
        "context_paths": context_paths,
        "context_path": context_paths[0],
        "context_chars": manifest.get("total_context_chars", sum(part["chars"] for part in parts)),
        "context_part_chars": [part["chars"] for part in parts],
        "capsule_path": str(capsule_path),
        "capsule_count": len(context_paths),
        "source_boundary_review_required": boundary,
        "max_context_chars": max_context,
    }
    return result, migration_complete, waiting_at_final_checkpoint, script_retry
