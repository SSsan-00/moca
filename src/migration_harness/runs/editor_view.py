"""エディタの差分ポーリングに使う版情報と、Runの指示受付時機。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..config import target_paths
from ..files import safe_target
from ..workflow import can_resume_run
from .editor_queue import AGENT_PHASES, EDITOR_TARGET_SUFFIXES, QUEUE_PHASES


def _editor_stamp(path: Path) -> tuple | None:
    """ポーリング時に本文を読まず、差し替えや外部編集を検知する。"""
    try:
        value = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return None
    return value.st_dev, value.st_ino, value.st_mode, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def editor_version(*fields) -> str:
    """個別の取得版を、Runや対象をまたいで再利用できない短い値にする。"""
    return hashlib.sha256(json.dumps(fields, ensure_ascii=False).encode("utf-8")).hexdigest()


def update_history_delta(payload, previous_version, versions):
    """最近の版を基準に、指示と会話の変更だけを返す。古い版には全件を返す。"""
    fields = ("prompts", "conversation")
    current = {key: {entry["id"]: editor_version(entry) for entry in payload.get(key, [])} for key in fields}
    version = editor_version(payload["run_id"], current)
    payload["history_version"] = version
    previous = versions.get((payload["run_id"], previous_version))
    if previous is not None:
        delta = {}
        for key in fields:
            if key not in payload:
                continue
            rows = payload.pop(key)
            changed = [row for row in rows if previous[key].get(row["id"]) != current[key][row["id"]]]
            removed = list(previous[key].keys() - current[key].keys())
            if changed or removed:
                delta[key] = {"changed": changed, "removed": removed}
        if delta:
            payload["history_delta"] = delta
    versions.setdefault((payload["run_id"], version), current)
    if len(versions) > 8:
        del versions[next(iter(versions))]


def prompt_timing(config, state, run_path, record, *, live: bool = False, pending_edits: bool = False) -> str | None:
    if state.get("stop_requested") or state.get("generating_anchors"):
        return None
    phase = record["status"]
    if pending_edits and not state.get("ready") and phase == "repairing":
        return "repair_restart"
    if live and not pending_edits and not state.get("ready") and phase in {"junie_running", "editor_followup", "repairing"}:
        return "immediate"
    if phase == "prepared" or phase in QUEUE_PHASES or (
        phase in {"script_running", "script_failed"}
        and record.get("script_phase") in {"after_prepare", "before_migration"}
    ):
        return "resume" if state.get("ready") else "checkpoint"
    agent = run_path / "agent-workspace"
    if (record.get("completed_capsules", 0) > 0 and record.get("completed_capsules") == record.get("context_capsules")
            and not agent.is_symlink() and agent.is_dir() and can_resume_run(config, record)):
        return "resume"
    return None


def editor_view_versions(view, query, state, run_id, run_path, worktree, record, unit):
    names = [path.as_posix() for path in target_paths(unit) if path.suffix.lower() in EDITOR_TARGET_SUFFIXES]
    name = query.get("target", names[:1] or [None])[0]
    if name is not None and name not in names:
        raise ValueError("設定された移行先ファイルを選んでください。")
    agent = run_path / "agent-workspace"
    phase = record["status"]
    references = record.get("reference_refresh", {})
    resolving_references = bool(references.get("conflict"))
    root = agent if (not resolving_references and phase in AGENT_PHASES
                     and agent.is_dir() and not agent.is_symlink()) else worktree
    try:
        target_stamp = _editor_stamp(safe_target(root, Path(name))) if name else None
        content_version = editor_version(run_id, str(root), name, target_stamp)
    except (OSError, ValueError):
        content_version = None
    live = bool(view.control and view.control.junie_steering)
    adopted_stamp = None
    if name and record.get("stopped_migration_draft") and root == agent:
        adopted_stamp = _editor_stamp(safe_target(worktree, Path(name)))
    view_version = editor_version(
        content_version, adopted_stamp, record.get("stopped_migration_draft"),
        names, phase, record.get("current_capsule"), record.get("operator_question"), record.get("junie_model_revision"),
        record.get("completed_capsules"), record.get("context_capsules"), record.get("script_phase"),
        _editor_stamp(run_path / "editor.json"), resolving_references, bool(references.get("adoption")),
        _editor_stamp(run_path.parent / ".preparation-conversations" / (run_id + ".json")), live,
        *(state.get(key) for key in ("ready", "generating_anchors", "stop_requested")),
    )
    return view_version, content_version, root, name
