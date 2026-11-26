from .editor_queue import editor_queue
from .run_records import load_dashboard_record


ACTIVE_PHASES = {
    "preparing", "junie_running", "editor_followup", "building", "checking", "repairing", "script_running",
}
RECOVERY_PHASES = {
    "interrupted", "junie_failed", "build_failed", "checker_failed", "checker_needs_review",
    "editor_checkpoint", "script_failed", "preparation_failed", "preparation_interrupted",
}


def _saved_draft(record):
    if record.get("stopped_migration_draft"):
        return "migration"
    repair = record.get("repair_pending")
    if isinstance(repair, dict):
        draft = repair.get("draft")
        if (isinstance(draft, dict) and draft.get("snapshot")) or repair.get("snapshot"):
            return "repair"
    return None


def recovery_status(config, run_id, *, blocked=False):
    run_path, _ = load_dashboard_record(config, run_id)
    with editor_queue(run_path) as queue:
        _, record = load_dashboard_record(config, run_id)
        completed = record.get("completed_capsules")
        total = record.get("context_capsules")
        command_unconfirmed = record.get("unconfirmed_command")
        attempt = None if command_unconfirmed else record.get("script_inflight")
        conflict = any(edit.get("status") == "conflict" for edit in queue["edits"].values())
        needs_process_check = (blocked or bool(command_unconfirmed or record.get("migration_stop_unconfirmed"))
                               or record["status"] in ACTIVE_PHASES)
        draft = _saved_draft(record)
        repair = record.get("repair_pending")
        phase = record.get("script_phase") or record["status"]
        approval = None if not attempt else {"phase": attempt["phase"], "number": attempt["number"],
                                             "log": attempt["phase"] + ".log"}
        return {
            "run_id": run_id, "stage": phase, "status": record["status"],
            "completed": completed, "total": total,
            "saved_targets": bool(record.get("target_hashes")) or bool(record.get("target_applied")),
            "draft": draft, "draft_selected": isinstance(repair, dict) and "restore_from" in repair,
            "draft_keyword_blocked": draft == "repair" and record.get("checker_review_reason") == "stopped_draft_keyword",
            "conflict": conflict,
            "pending_edits": len(queue["edits"]),
            "pending_prompts": sum(prompt["status"] != "sent" for prompt in queue["prompts"]),
            "needs_process_check": needs_process_check,
            "unknown_script": approval,
            "unconfirmed_command": command_unconfirmed,
            "available": bool(approval or needs_process_check or conflict or draft
                              or record["status"] in RECOVERY_PHASES),
        }
