import codecs
import hashlib
import json
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from ..config import target_paths
from ..errors import CheckerRepairBlocked
from ..files import safe_target
from ..verification.csharp_keyword_guard import reject_keyword_changes
from .editor_queue import editor_queue
from .run_records import execution_lock, load_saved_run, write_run_record
from .target_snapshots import (
    restore_snapshot_files, save_target_snapshot, validate_snapshot_metadata, verified_snapshot_files,
)
from .worktree_targets import target_hash, worktree_source_lock


_PREVIEW_BYTES = 64 * 1024


@dataclass(frozen=True)
class _RecoveryPlan:
    run_path: Path
    record: dict[str, Any]
    versions: dict[str, Any]
    worktree: dict[str, Path]
    agent: dict[str, Path]
    stopped: dict[str, Path]


def require_selected_repair_lineage(attempt):
    if attempt is None or ("restore_from" not in attempt and "selection" not in attempt):
        return
    draft = attempt.get("draft")
    if not isinstance(draft, dict) or not draft.get("stopped_snapshot") or not draft.get("base_snapshot"):
        raise ValueError("停止下書きの選択に元の下書き版または作業ツリー版の記録がありません。"
                         "保存済みの下書きと作業ツリーを比較し、必要な編集を新しい Run に引き継いでください。")


def _original_stopped_draft(run_path, record, attempt):
    active = attempt.get("draft", {})
    if not attempt or ("restore_from" not in attempt and "selection" not in attempt):
        return active
    require_selected_repair_lineage(attempt)
    verified_snapshot_files(run_path, active)
    history = record.get("stopped_repair_recovery", [])
    if not isinstance(history, list):
        raise ValueError("停止下書きの Run 復旧履歴を確認してください。")
    source_id = active["stopped_snapshot"]
    candidates = [item for item in history if isinstance(item, dict) and item.get("snapshot") == source_id]
    source = candidates[0] if len(candidates) == 1 else None
    if source:
        validate_snapshot_metadata(source)
    if not source or source["targets"].keys() != active["targets"].keys():
        raise ValueError("元の停止下書きを特定できません。Run の復旧履歴を確認してください。")
    return source


def _build_recovery_plan(run_path, worktree, unit, record, queue):
    attempt = record.get("repair_pending", {})
    draft = _original_stopped_draft(run_path, record, attempt)
    if isinstance(draft, dict) and "snapshot" not in draft:
        raise ValueError("復元できる停止時の下書きがありません。Run の記録を確認してください。")
    validate_snapshot_metadata(draft)
    if not draft["workspace_unchanged"]:
        raise ValueError("復元できる停止時の下書きがありません。Run の記録を確認してください。")
    _require_no_pending_edits(queue, pending_apply=record.get("target_apply_pending"))
    targets = target_paths(unit)
    names = {path.as_posix() for path in targets}
    if set(draft["base"]) != names or not draft["targets"] or not draft["targets"].keys() <= names:
        raise ValueError("停止時の下書きと設定の対象範囲が一致しません。")
    agent = _checked_agent_root(run_path)
    current = {path.as_posix(): safe_target(worktree, path) for path in targets}
    agent_files = {name: safe_target(agent, Path(name)) for name in draft["targets"]}
    snapshot_files = verified_snapshot_files(run_path, draft)
    plan = {
        "snapshot": draft["snapshot"], "base": draft["base"], "draft": draft["targets"],
        "worktree": {name: target_hash(path) for name, path in current.items()},
        "agent": {name: target_hash(path) for name, path in agent_files.items()},
        "edit_revision": queue.get("edit_revision"),
    }
    if draft["snapshot"] != attempt["draft"]["snapshot"]:
        plan["selected_snapshot"] = attempt["draft"]["snapshot"]
    plan["version"] = hashlib.sha256(json.dumps(plan, sort_keys=True).encode("utf-8")).hexdigest()
    return _RecoveryPlan(run_path, record, plan, current, agent_files, snapshot_files)


def _require_no_pending_edits(queue, *, pending_apply=None):
    if pending_apply or queue["edits"] or any(prompt["status"] != "sent" for prompt in queue["prompts"]):
        raise ValueError("未反映の保存操作または Junie 指示があります。")


def _checked_agent_root(run_path):
    agent = run_path / "agent-workspace"
    if agent.is_symlink() or not agent.is_dir() or not agent.resolve().is_relative_to(run_path):
        raise ValueError("Junie 作業領域を確認してください。")
    return agent


@contextmanager
def _locked_recovery_plan(config, run_id):
    run_id = str(uuid.UUID(run_id))
    with execution_lock(config, run_id):
        run_path, worktree, record, unit = load_saved_run(config, run_id)
        with editor_queue(run_path) as queue, worktree_source_lock(worktree):
            yield _build_recovery_plan(run_path, worktree, unit, record, queue)


def plan_stopped_repair(config, run_id):
    with _locked_recovery_plan(config, run_id) as recovery:
        return recovery.versions


def _preview_file(file, expected):
    if expected is None:
        return {"text": None, "truncated": False}
    with file.open("rb") as stream:
        data = stream.read(_PREVIEW_BYTES + 1)
    if target_hash(file) != expected:
        raise ValueError("対象が更新されました。復元計画を取り直してください。")
    return {"text": data[:_PREVIEW_BYTES].decode("utf-8-sig", errors="replace"),
            "truncated": len(data) > _PREVIEW_BYTES}


def preview_stopped_repair(config, run_id, version, target):
    with _locked_recovery_plan(config, run_id) as recovery:
        versions = recovery.versions
        if versions["version"] != version or target not in recovery.stopped:
            raise ValueError("復旧対象が更新されました。復元計画を取り直してください。")
        return {"target": target, "draft": _preview_file(recovery.stopped[target], versions["draft"][target]),
                "worktree": _preview_file(recovery.worktree[target], versions["worktree"][target])}


def resolve_stopped_repair(config, run_id, version, choices):
    with _locked_recovery_plan(config, run_id) as recovery:
        versions = recovery.versions
        if versions["version"] != version:
            raise ValueError("下書きまたは現行版が更新されました。復元計画を取り直してください。")
        if choices.keys() != recovery.stopped.keys() or set(choices.values()) - {"draft", "worktree"}:
            raise ValueError("復元対象すべてに draft または worktree を指定してください。")
        if None in versions["worktree"].values():
            raise ValueError("作業ツリーの欠損を手動で復元してから計画し直してください。")
        _save_recovery_choices(recovery, choices)


def _save_recovery_choices(recovery, choices):
    versions = recovery.versions
    run_path, record = recovery.run_path, recovery.record
    worktree = {name: recovery.worktree[name] for name in recovery.stopped}
    baseline = versions["worktree"]
    _check_reselected_repair(run_path, record, versions, choices)
    sources = {name: stopped if choices[name] == "draft" else worktree[name]
               for name, stopped in recovery.stopped.items()}
    agent_backup = save_target_snapshot(run_path, recovery.agent, baseline, workspace_unchanged=True)
    worktree_backup = save_target_snapshot(run_path, worktree, baseline, workspace_unchanged=True)
    selected = save_target_snapshot(run_path, sources, baseline, workspace_unchanged=True,
                                    stopped_snapshot=versions["snapshot"], base_snapshot=worktree_backup["snapshot"])
    if (agent_backup["targets"] != versions["agent"]
            or worktree_backup["targets"] != {name: baseline[name] for name in worktree}
            or selected["targets"] != {name: versions[choice][name] for name, choice in choices.items()}
            or any(target_hash(path) != baseline[name] for name, path in recovery.worktree.items())):
        raise ValueError("保存中に対象が更新されました。復元計画を取り直してください。")
    attempt = record["repair_pending"]
    record.setdefault("stopped_repair_recovery", []).extend((attempt["draft"], agent_backup, worktree_backup))
    attempt["draft"] = selected
    attempt.pop("selection", None)
    attempt["restore_from"] = versions["agent"]
    attempt.pop("stopped_draft_save_failed", None)
    if record.get("checker_review_reason") == "stopped_draft_keyword":
        record.pop("checker_review_reason")
        record.pop("error", None)
    write_run_record(run_path / "run.json", record)


def _stage_repair_adoption(staging, snapshot, draft):
    sources, adopted, partial = {}, {}, []
    for index, (name, source) in enumerate(snapshot.items()):
        digest = draft["targets"][name]
        if digest == draft["base"][name]:
            continue
        if digest is None:
            partial.append(name)
            continue
        data = source.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("停止時の snapshot の内容が一致しません。")
        try:
            data.decode("utf-8-sig")
        except UnicodeDecodeError:
            partial.append(name)
            continue
        if not data.startswith(codecs.BOM_UTF8):
            source = Path(staging) / str(index)
            data = codecs.BOM_UTF8 + data
            source.write_bytes(data)
            digest = hashlib.sha256(data).hexdigest()
        sources[name] = source
        adopted[name] = digest
    return sources, adopted, partial


def _check_repair_adoption(run_path, record, draft, sources, adopted):
    if not adopted:
        return
    history = record.get("stopped_repair_recovery", ())
    snapshot_id = draft["base_snapshot"]
    baseline = next((saved for saved in reversed(history) if saved["snapshot"] == snapshot_id), {})
    expected = draft["base"]
    if (baseline.get("base") != expected
            or baseline.get("targets") != {name: expected[name] for name in draft["targets"]}):
        raise ValueError("復元前の作業ツリー版を確認できません。進捗画面で復元計画を取り直してください。")
    original = verified_snapshot_files(run_path, baseline)
    versions = {name: {"before": expected[name], "after": digest} for name, digest in adopted.items()}
    try:
        reject_keyword_changes(versions, before_path=original.__getitem__, after_path=sources.__getitem__)
    except ValueError as exc:
        raise CheckerRepairBlocked(
            "stopped_draft_keyword",
            f"停止時の下書きを復元できません: {exc} 進捗画面で作業ツリーを選び直すか、手修正を保存して計画し直してください。",
        ) from exc


def _check_reselected_repair(run_path, record, plan, choices):
    attempt = record["repair_pending"]
    if "restore_from" not in attempt and not attempt.get("selection"):
        return
    previous = attempt["draft"]
    # An earlier release could have written this exact draft before stopping. Selecting it
    # again must not turn it into a manual edit or make it the baseline for its own check.
    candidates = [name for name in previous["targets"] if plan["worktree"][name] != previous["base"][name]
                  and (choices[name] == "worktree" or plan["draft"][name] == previous["targets"][name])]
    if not candidates:
        return
    snapshot = verified_snapshot_files(run_path, previous)
    with TemporaryDirectory(dir=run_path) as staging:
        sources, adopted, _ = _stage_repair_adoption(staging, {name: snapshot[name] for name in candidates}, previous)
        still_applied = {name: digest for name, digest in adopted.items() if digest == plan["worktree"][name]}
        _check_repair_adoption(run_path, record, previous, sources, still_applied)


def recover_selected_repair(run_path, worktree, targets, record):
    attempt = record.get("repair_pending", {})
    if "restore_from" not in attempt and not attempt.get("selection"):
        return
    try:
        with editor_queue(run_path) as queue, worktree_source_lock(worktree):
            _require_no_pending_edits(queue)
            _restore_selected_repair(run_path, worktree, targets, record, attempt)
    except CheckerRepairBlocked:
        raise
    except (OSError, ValueError) as exc:
        raise CheckerRepairBlocked("stopped_draft", f"停止時の下書きを復元できません: {exc}") from exc


def _restore_selected_repair(run_path, worktree, targets, record, attempt):
    require_selected_repair_lineage(attempt)
    draft = attempt.get("draft")
    validate_snapshot_metadata(draft)
    baseline, selected_hashes = draft["base"], draft["targets"]
    restoring = "restore_from" in attempt
    names = {path.as_posix() for path in targets}
    if (baseline.keys() != names or not selected_hashes or not selected_hashes.keys() <= names
            or not draft["workspace_unchanged"]
            or restoring and (not isinstance(attempt["restore_from"], dict)
                              or attempt["restore_from"].keys() != selected_hashes.keys())):
        raise ValueError("停止時の下書きと設定の対象範囲が一致しません。")
    agent_root = _checked_agent_root(run_path)
    snapshot = verified_snapshot_files(run_path, draft)
    agent_files = {name: safe_target(agent_root, Path(name)) for name in snapshot}
    worktree_files = {name: safe_target(worktree, Path(name)) for name in baseline}
    if not restoring:
        selection = attempt["selection"]["base"]
        if (any(target_hash(file) != selection[name] for name, file in worktree_files.items())
                or any(target_hash(file) != selected_hashes[name] for name, file in agent_files.items())):
            raise ValueError("現行版が更新されました。進捗画面で復元計画を取り直してください。")

    # Missing or partial UTF-8 snapshots are kept as Junie input, not adopted to the worktree.
    with TemporaryDirectory(dir=run_path) as staging:
        sources, adopted, partial = _stage_repair_adoption(staging, snapshot, draft)
        if restoring and any(target_hash(file) not in (baseline[name], adopted.get(name, baseline[name]))
                             for name, file in worktree_files.items()):
            raise ValueError("作業ツリーが更新されました。進捗画面で復元計画を取り直してください。")
        _check_repair_adoption(run_path, record, draft, sources, adopted)
        if not restoring:
            return
        restore_snapshot_files(snapshot, agent_files, attempt["restore_from"], selected_hashes)
        restore_snapshot_files(sources, worktree_files, baseline, adopted, preserve_mode=True)
    _save_restored_selection(run_path, record, attempt, baseline, adopted, partial)


def _save_restored_selection(run_path, record, attempt, baseline, adopted, partial):
    restored_hashes = {**baseline, **adopted}
    attempt["selection"] = {"base": restored_hashes, "partial": partial}
    if adopted:
        record["target_hashes"] = restored_hashes
    # A failed commit leaves the saved plan retryable against the original hashes.
    previous = attempt.pop("restore_from")
    try:
        write_run_record(run_path / "run.json", record)
    except OSError:
        attempt["restore_from"] = previous
        attempt.pop("selection")
        raise


def require_repair_targets(record, repair_targets):
    selection = record.get("repair_pending", {}).get("selection")
    if selection:
        unresolved = set(selection["partial"]) - {path.as_posix() for path in repair_targets}
        if unresolved:
            raise CheckerRepairBlocked(
                "stopped_draft",
                "今回の修正対象外に復元した欠損または UTF-8 にできない下書きがあります。"
                "進捗画面で作業ツリーを選び直すか、手修正を保存して計画し直してください。"
                f" 対象: {', '.join(sorted(unresolved))}",
            )
