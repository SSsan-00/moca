from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import run_records
from .. import execution


def validate_inputs(root: Path, unit: dict[str, Any]) -> None:
    """作業場所を作る前に、設定で指定された入力ファイルを確認する。"""
    for source in unit["inputs"]:
        path = (root / source["path"]).resolve()
        if not path.is_file():
            raise ValueError(f"input file not found: {path}")


def create_worktree(
    config: dict[str, Any], unit: dict[str, Any], run_path: Path, context: dict[str, Any],
    record: dict[str, Any] | None = None,
) -> Path:
    root = Path(config["project_root"])
    run_id = run_path.name
    worktree = Path(config["worktree_root"]) / run_id
    worktree.parent.mkdir(parents=True, exist_ok=True)
    if worktree.exists():
        raise FileExistsError(f"worktree path already exists: {worktree}")
    execution.run_process(
        ["git", "-C", str(root), "worktree", "add", "--detach", str(worktree), config["base_ref"]],
        label="worktree作成", cancellable=False,
        check=True,
        capture_output=True,
        text=True,
    )
    record = {} if record is None else record
    record.update({
        "run_id": run_id,
        "unit_id": unit["id"],
        "status": "prepared",
        "created_at": record.get("created_at", datetime.now(timezone.utc).isoformat()),
        "project_root": config["project_root"],
        "worktree_path": str(worktree),
        "capsule_path": context["capsule_path"],
        "context_chars": context["context_chars"],
        "context_part_chars": context["context_part_chars"],
        "context_capsules": context["capsule_count"],
        "max_context_chars": config["max_context_chars"],
        "junie_max_steps": config["junie_max_steps"],
        "junie_model": config.get("junie_model"),
        "junie_effort": config.get("junie_effort"),
        "resume_attempts": 0,
        "segmentation_policy": "line_with_character_fallback_and_overlap",
        "source_boundary_review_required": context["source_boundary_review_required"],
        "target": unit["target"],
        "completed_capsules": 0,
        "current_capsule": 1,
    })
    if "targets" in unit:
        record["targets"] = unit["targets"]
    if (log_path := run_path / "before_prepare.log").is_file():
        record["script_logs"] = {"before_prepare": str(log_path)}
    run_records.write_run_record(run_path / "run.json", record)
    return worktree
