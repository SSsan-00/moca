from __future__ import annotations

import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import editor_queue as editor_queue_store, migration_draft, run_records, stage_scripts
from .. import config as harness_config, execution
from ..config import find_unit
from ..context import capsules as capsule_files
from ..context.capsules import prepare_context
from ..errors import RunInterrupted, format_error
from ..execution import check_cancelled
from ..files import safe_target
from .target_snapshots import (
    save_capsule_checkpoint as _save_capsule_checkpoint,
    verified_snapshot_files as _verified_target_snapshot,
)
from .worktree_targets import finish_pending_targets, target_hash, worktree_source_lock


def _validate_inputs(root: Path, unit: dict[str, Any]) -> None:
    """作業場所を作る前に、設定で指定された入力ファイルを確認する。"""
    for source in unit["inputs"]:
        path = (root / source["path"]).resolve()
        if not path.is_file():
            raise ValueError(f"input file not found: {path}")


def _create_worktree(
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


def prepare_run(config: dict[str, Any], unit_id: str, run_id: str) -> dict[str, Any]:
    run_path = Path(config["run_root"]) / run_id
    unit = find_unit(config, unit_id)
    run_path.mkdir(parents=True, exist_ok=False)
    project_root = Path(config["project_root"])
    record = {
        "run_id": run_id, "unit_id": unit_id, "target": unit["target"],
        "targets": [path.as_posix() for path in harness_config.target_paths(unit)],
        "project_root": config["project_root"],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "preparing", "preparation_stage": 0,
    }
    record_path = run_path / "run.json"
    run_records.write_run_record(record_path, record)
    try:
        stage_scripts.run_script_phase(config, "before_prepare", project_root, run_path, record)
        check_cancelled()
        _validate_inputs(project_root, unit)
        record.update(status="preparing", preparation_stage=1)
        run_records.write_run_record(record_path, record)
        context = prepare_context(config, unit, run_path)
        check_cancelled()
        record["preparation_stage"] = 2
        run_records.write_run_record(record_path, record)
        worktree = _create_worktree(config, unit, run_path, context, record)
        check_cancelled()
    except KeyboardInterrupt:
        # Gitが作成を終えていれば、このRunを準備済みとしてそのまま再開できる。
        if record.get("worktree_path"):
            run_records.write_run_record(record_path, record)
        else:
            record.update(status="preparation_interrupted", error="準備を停止しました。実行タブから再度準備してください。")
            run_records.write_run_record(record_path, record)
        raise RunInterrupted(run_id) from None
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        if not record.get("worktree_path"):
            record.update(status="preparation_failed", error=format_error(exc))
            run_records.write_run_record(record_path, record)
        raise
    if config.get("scripts", {}).get("after_prepare"):
        record = run_records.read_run_record(run_path / "run.json")
        stage_scripts.run_script_phase(config, "after_prepare", worktree, run_path, record, once=True)
    return {
        "run_id": run_id,
        "unit_id": unit_id,
        "worktree_path": str(worktree),
        **context,
        "max_context_chars": config["max_context_chars"],
        "run_path": str(run_path),
    }


def prepare_migration_workspace(config, unit, run_path, result, record, *, resuming, followup_only, through_capsule):
    """既存Runを照合し、途中反映の復旧と作業領域の初期化を済ませて進捗を保存する。"""
    worktree_root = Path(result["worktree_path"]).resolve()
    if not worktree_root.is_dir() or not worktree_root.is_relative_to(Path(config["worktree_root"]).resolve()):
        raise ValueError(f"run worktree is missing or outside configured worktree root: {worktree_root}")
    relative_targets = harness_config.target_paths(unit)
    for relative in relative_targets:
        first = relative.parts[0].lower()
        if first in {".migration-context.md", ".migration-materials", ".migration-scratch", ".migration-boundary.md", "migration-read-rules.py"} or first.startswith(("migration-complete-", "migration-rule-reads-", "checker-findings-")):
            raise ValueError("target path conflicts with a reserved file in the agent workspace")
    agent_root = run_path / "agent-workspace"
    if agent_root.is_symlink() or not agent_root.resolve().is_relative_to(run_path.resolve()):
        raise ValueError("agent workspace path escapes the run directory")
    capsule, manifest = capsule_files.load_capsule_manifest(run_path)
    if not resuming:
        capsule_files.verify_pinned_php(capsule, manifest)
    stage_scripts.run_script_phase(config, "after_prepare", worktree_root, run_path, record, once=True)
    # Junieの作業領域を作成済みのRunでは、初期ファイルを再配置しない。
    if not agent_root.exists():
        stage_scripts.run_script_phase(
            config, "before_migration", worktree_root, run_path, record, once=True,
            extra_commands=tuple(unit.get("draft_import_commands", ())),
        )
    record_path = run_path / "run.json"
    resume_attempts = record.get("resume_attempts", 0)
    if resuming and (type(resume_attempts) is not int or resume_attempts < 0):
        raise ValueError("run record contains an invalid resume-attempt count")
    if agent_root.exists() and (not agent_root.is_dir() or not resuming and record["status"] != "prepared"):
        raise ValueError("agent workspace is invalid or already exists for this run")
    if not agent_root.exists():
        if record["status"] != "prepared":
            raise ValueError("agent workspace is missing; this run cannot be resumed safely")
        agent_root.mkdir()

    # 前回の反映が途中で止まっていれば、残りを反映してから再開用のハッシュを比較する。
    finish_pending_targets(agent_root, worktree_root, relative_targets, record, record_path)
    worktree_targets = tuple(safe_target(worktree_root, path) for path in relative_targets)
    agent_targets = tuple(safe_target(agent_root, path) for path in relative_targets)
    hashes = {path.as_posix(): target_hash(file) for path, file in zip(relative_targets, worktree_targets, strict=True)}
    if resuming and not followup_only:
        prior_hashes = record.get("target_hashes", {relative_targets[0].as_posix(): record.get("target_sha256")})
        if hashes != prior_hashes:
            raise ValueError("worktree targets changed since the last recorded Junie result; inspect and reconcile before resuming")
    if record.get("stopped_migration_save_error"):
        raise ValueError("停止時の下書きを保存できませんでした。Junie 作業領域を確認してください。")
    stopped_draft = record.get("stopped_migration_draft")
    if stopped_draft:
        _verified_target_snapshot(run_path, stopped_draft)
        draft_hashes = {path.as_posix(): target_hash(file) for path, file in zip(relative_targets, agent_targets, strict=True)}
        if not stopped_draft["workspace_unchanged"] or stopped_draft["base"] != hashes or stopped_draft["targets"] != draft_hashes:
            raise ValueError("停止時の下書きと現行版が一致しません。"
                             "採用済みの作業ツリーと Junie 作業領域を比較し、対象外の変更や外部編集を確認してください。")
    rewind = record.get("migration_rewind")
    if rewind and "restore_from" in rewind:
        with editor_queue_store.editor_queue(run_path) as queue, worktree_source_lock(worktree_root):
            if queue["edits"] or any(prompt["status"] != "sent" for prompt in queue["prompts"]):
                raise ValueError("巻き戻しと未反映の操作が競合しています。")
            migration_draft.restore_snapshot_targets(
                run_path, rewind, dict(zip(hashes, agent_targets, strict=True)), hashes, record,
            )
    through_capsule = record.get("migration_range_stop", through_capsule)
    # 通常の再開ではJunieの下書きを残し、移行完了後の追加指示ではworktreeの現行版を渡す。
    for relative, worktree_target, agent_target in zip(relative_targets, worktree_targets, agent_targets, strict=True):
        agent_target.parent.mkdir(parents=True, exist_ok=True)
        if not stopped_draft and (followup_only or not rewind and not agent_target.exists()) and worktree_target.is_file():
            shutil.copyfile(worktree_target, agent_target)
        elif followup_only and not stopped_draft:
            agent_target.unlink(missing_ok=True)
        if followup_only and not stopped_draft and target_hash(agent_target) != hashes[relative.as_posix()]:
            raise ValueError("移行先ファイルが更新されました。もう一度再開してください。")

    record["agent_workspace_path"] = str(agent_root)
    if resuming:
        record["resume_attempts"] = resume_attempts + 1
    record["status"] = (
        "editor_checkpoint" if followup_only or record["status"] in {"editor_checkpoint", "editor_followup"} else "junie_running"
    )
    record.setdefault("completed_capsules", 0)
    record.setdefault("current_capsule", record["completed_capsules"] + 1)
    record["target_hashes"] = hashes
    if not resuming:
        record["target_sha256"] = hashes[relative_targets[0].as_posix()]
        record["target_applied"] = False
        record.setdefault("capsule_checkpoints", {})["0"] = _save_capsule_checkpoint(
            run_path, worktree_root, relative_targets, hashes,
        )
    run_records.write_run_record(record_path, record)

    return worktree_root, relative_targets, agent_root, agent_targets, capsule, manifest, through_capsule
