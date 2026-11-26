from __future__ import annotations

from . import run_records, target_snapshots
from ..context.context_handoff import save_junie_memo
from ..errors import CheckerRepairBlocked
from .target_snapshots import (
    restore_snapshot_files as _restore_snapshot_files,
    verified_snapshot_files as _verified_target_snapshot,
)


def archive_stopped_migration(record):
    if draft := record.pop("stopped_migration_draft", None):
        record.setdefault("stopped_migration_history", []).append(draft)


def save_stopped_migration(record_path, record, agent_root, targets, *, workspace_unchanged,
                            memo=None, phase="migration", through=None):
    # Run 記録の書き込みまで成功してから旧下書きの参照を更新する。
    """最新の下書きと旧参照をRun記録へ一緒に保存し、失敗時は再開を保留する。"""
    updated = {**record}
    if "stopped_migration_history" in record:
        updated["stopped_migration_history"] = record["stopped_migration_history"].copy()
    try:
        draft = target_snapshots.save_workspace_snapshot(record_path.parent, agent_root, targets, record["target_hashes"],
                                        workspace_unchanged=workspace_unchanged)
        if workspace_unchanged and memo is not None:
            save_junie_memo(memo, record_path.parent, record, phase=phase, through=through, draft=draft)
        archive_stopped_migration(updated)
        updated["stopped_migration_draft"] = draft
        run_records.write_run_record(record_path, updated)
    except (OSError, ValueError):
        record["stopped_migration_save_error"] = True
        run_records.write_run_record(record_path, record)
        raise
    record.update(updated)


def restore_snapshot_targets(run_path, attempt, agent_files, before, record):
    """合意済みの版をJunie作業領域へ復元し、完了した復元計画を消す。"""
    draft = attempt["draft"]
    try:
        snapshot = _verified_target_snapshot(run_path, draft)
        expected = attempt["restore_from"]
        if draft["base"] != before or snapshot.keys() != agent_files.keys() or expected.keys() != snapshot.keys():
            raise ValueError("停止時の下書きと復元対象が一致しません。")
        _restore_snapshot_files(snapshot, agent_files, expected, draft["targets"])
        attempt.pop("restore_from")
        run_records.write_run_record(run_path / "run.json", record)
    except (OSError, ValueError) as exc:
        raise CheckerRepairBlocked("stopped_draft", f"停止時の下書きを復元できません: {exc}") from exc
