"""工程の実行時間をRunに保存する。停止・再開の間の待ち時間は加算しない。"""

from __future__ import annotations

import copy
import time
from typing import Any


SCRIPT_STAGES = {
    "before_prepare": 0, "after_prepare": 2, "before_migration": 3, "after_migration": 3,
    "before_build": 4, "after_build": 4, "before_checker": 5, "after_checker": 5,
}
ACTIVE_STAGES = {"junie_running": 3, "repairing": 3, "building": 4, "checking": 5}


def update_timings(record: dict[str, Any], *, now: float | None = None) -> None:
    now = time.time() if now is None else now
    status = record.get("status")
    stage = (record.get("preparation_stage", 0) if status == "preparing" else
             SCRIPT_STAGES.get(record.get("script_phase")) if status == "script_running" else
             ACTIVE_STAGES.get(status))
    previous = record.get("active_stage")
    timings = record.setdefault("stage_timings", {})
    if previous != stage:
        if previous is not None and (entry := timings.get(str(previous))) and entry.get("started_at") is not None:
            entry["elapsed_seconds"] += max(0, now - entry["started_at"])
            entry["started_at"] = None
        if stage is not None:
            entry = timings.setdefault(str(stage), {"elapsed_seconds": 0, "started_at": None})
            entry["started_at"] = now
    record["active_stage"] = stage
    record["timing_updated_at"] = now


def freeze_timings(record: dict[str, Any], *, now: float | None = None) -> dict[str, Any]:
    """実行途中の旧記録を開いた場合も、未観測の時間を実行時間と見なさない。"""
    timings = copy.deepcopy(record.get("stage_timings", {}))
    end = record.get("timing_updated_at", time.time()) if now is None else now
    for entry in timings.values():
        if entry.get("started_at") is not None:
            entry["elapsed_seconds"] += max(0, end - entry["started_at"])
            entry["started_at"] = None
    return timings
