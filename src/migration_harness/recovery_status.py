from .editor_queue import editor_queue
from .run_records import load_dashboard_record


ACTIVE_PHASES = {
    "preparing", "junie_running", "editor_followup", "building", "checking", "repairing", "script_running",
}
RECOVERY_PHASES = {
    "interrupted", "junie_failed", "build_failed", "checker_failed", "checker_needs_review",
    "editor_checkpoint", "script_failed", "preparation_failed", "preparation_interrupted",
}


def recovery_status(config, run_id, *, blocked=False):
    run_path, _ = load_dashboard_record(config, run_id)
    with editor_queue(run_path) as queue:
        _, record = load_dashboard_record(config, run_id)
        completed = record.get("completed_capsules")
        total = record.get("context_capsules")
        attempt = record.get("script_inflight")
        conflict = any(edit.get("status") == "conflict" for edit in queue["edits"].values())
        needs_process_check = blocked or bool(record.get("migration_stop_unconfirmed")) or record["status"] in ACTIVE_PHASES
        draft = ("migration" if record.get("stopped_migration_draft") else
                 "repair" if record.get("repair_pending", {}).get("snapshot") else None)
        phase = record.get("script_phase") or record["status"]
        approval = None if not attempt else {"phase": attempt["phase"], "number": attempt["number"],
                                             "log": attempt["phase"] + ".log"}
        return {
            "run_id": run_id, "stage": phase, "status": record["status"],
            "completed": completed, "total": total,
            "saved_targets": bool(record.get("target_hashes")) or bool(record.get("target_applied")),
            "draft": draft, "conflict": conflict,
            "pending_edits": len(queue["edits"]),
            "pending_prompts": sum(prompt["status"] != "sent" for prompt in queue["prompts"]),
            "needs_process_check": needs_process_check,
            "unknown_script": approval,
            "available": bool(approval or needs_process_check or conflict or draft
                              or record["status"] in RECOVERY_PHASES),
        }
