from __future__ import annotations

from typing import Any

from ..config import find_unit


BUILD_RESUME_STATUSES = {"junie_finished", "building", "build_failed", "repairing"}


CHECKER_RESUME_STATUSES = {"checking", "checker_needs_review", "checker_failed"}


VERIFIED_STATUSES = {"build_succeeded", "checker_succeeded", "checker_accepted"}


_JUNIE_RESUME_STATUSES = {"prepared", "junie_running", "junie_failed", "interrupted", "editor_checkpoint", "editor_followup"}


SCRIPT_RESUME_STATUSES = {"script_running", "script_failed"}


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
        SCRIPT_RESUME_STATUSES | CHECKER_RESUME_STATUSES | VERIFIED_STATUSES | BUILD_RESUME_STATUSES | _JUNIE_RESUME_STATUSES
    ):
        return False
    if "unit_id" in record:
        find_unit(config, record["unit_id"])
    if _missing_required_checker(config, record):
        return False
    return status not in CHECKER_RESUME_STATUSES or record.get("build_exit_code") == 0


def can_resume_build(record):
    return (record.get("status") in (
        BUILD_RESUME_STATUSES | CHECKER_RESUME_STATUSES | VERIFIED_STATUSES
        | SCRIPT_RESUME_STATUSES | {"editor_checkpoint"}
    ) and not record.get("script_inflight") and not record.get("unconfirmed_command") and bool(record.get("completed_capsules"))
        and record["completed_capsules"] == record.get("context_capsules")
        and not record.get("target_apply_pending"))


BUILD_THROUGH_STAGE = "build-through"
