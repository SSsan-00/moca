from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from . import editor_queue as editor_queue_store, run_records, worktree_targets as target_files
from ..context import context_handoff
from ..context.context_handoff import junie_memo_request, save_junie_memo
from ..context.context_plan import navigation_plan
from ..context.migration_materials import (
    CSHARP_IDENTIFIERS, STATIC_SOURCE_REVIEW, TARGET_ENCODING, stage_migration_materials,
)
from ..context.source_context import stage_target_context, target_context_was_initialized
from ..errors import RunInterrupted, format_error
from ..execution import check_cancelled, report_activity
from ..files import has_completion_record, safe_target
from ..junie import junie_cli, migration_session
from ..junie.migration_session import task_guidelines, unlisted_workspace_files
from ..rules import detail_selection
from ..verification.script_handoff import script_handoff_context
from .editor_queue import LivePrompts, dispatch_prompts
from .migration_draft import archive_stopped_migration, save_stopped_migration
from .operator_feedback import operator_edit_context, read_operator_review, stage_operator_prompts
from .run_records import run_model_options
from .target_snapshots import save_capsule_checkpoint as _save_capsule_checkpoint
from .worktree_targets import target_hash, worktree_source_lock


def _checkpoint_migration_draft(record_path, record, worktree, agent_root, targets, allowed, other_files, before,
                                *, memo=None, phase="migration", through=None):
    """対象外と基準版が一致するときだけ、移行中の最新下書きを記録する。"""
    with worktree_source_lock(worktree):
        try:
            safe = (
                unlisted_workspace_files(agent_root, allowed) == other_files
                and all(target_hash(safe_target(worktree, path)) == record["target_hashes"][path.as_posix()]
                        for path in targets)
            )
            hashes = {path.as_posix(): target_hash(safe_target(agent_root, path)) for path in targets}
        except (OSError, ValueError):
            record["stopped_migration_save_error"] = True
            run_records.write_run_record(record_path, record)
            raise
        if not safe or hashes != before or (memo is not None and memo.is_file()):
            save_stopped_migration(record_path, record, agent_root, targets, workspace_unchanged=safe,
                                    memo=memo, phase=phase, through=through)


def _run_migration_junie(
    config, record_path, record, phase, guidelines, agent_root, inputs, command, completion, token,
    agent_targets, *, worktree, targets, allowed, other_files, resumed_draft=None, memo=None, through=None, **kwargs,
):
    before = {name.as_posix(): target_hash(file) for name, file in zip(targets, agent_targets, strict=True)}
    if resumed_draft and before != resumed_draft["targets"]:
        raise ValueError("Junie の起動前に停止時の下書きが更新されました。Junie 作業領域を確認してください。")
    closed = False

    def confirm_closed():
        nonlocal closed
        closed = True

    stop_reason = None

    def remember_stop(reason):
        nonlocal stop_reason
        stop_reason = reason

    observer = junie_cli.session_closed_observer.set(confirm_closed)
    stop_observer = junie_cli.acp_stop_observer.set(remember_stop)
    try:
        exit_code = migration_session.run_budgeted_junie(config, record_path, record, phase, guidelines, agent_root, inputs,
                                       command, completion, token, agent_targets, **kwargs)
        if resumed_draft and not closed:
            raise junie_cli.JunieStopError("Junie と子プロセスの終了を確認できませんでした。")
        return exit_code, stop_reason
    except (KeyboardInterrupt, OSError, RuntimeError, ValueError) as exc:
        if junie_cli.unconfirmed_junie_stop(exc) is not None:
            record["migration_stop_unconfirmed"] = True
            run_records.write_run_record(record_path, record)
        elif closed:
            _checkpoint_migration_draft(
                record_path, record, worktree, agent_root, targets, allowed, other_files, before,
                memo=memo, phase=phase, through=through,
            )
        raise
    finally:
        junie_cli.acp_stop_observer.reset(stop_observer)
        junie_cli.session_closed_observer.reset(observer)


def _run_editor_followup(
    agent_root: Path, worktree: Path, targets: tuple[Path, ...], record: dict[str, Any],
    record_path: Path, config: dict[str, Any], instruction: str,
) -> bool:
    """保留中の指示を、ビルド前に同じ移行先へ適用する。"""
    token = uuid.uuid4().hex
    completion = agent_root / f"migration-complete-{token}.txt"
    memo, memo_request = junie_memo_request(agent_root, token)
    instructions = stage_operator_prompts(agent_root, token, instruction)
    inputs = [Path(instructions)]
    context, report = operator_edit_context(record_path.parent, agent_root, token, inputs, record.get("operator_question"))
    guidelines, supplied = task_guidelines(agent_root, inputs)
    names = ", ".join(target.as_posix() for target in targets)
    operator_input = (
        f"Use the operator's follow-up instructions from the `{instructions}` entry in the guidelines. "
        if supplied else f"Read `{instructions}` for the operator's follow-up instructions. "
    )
    prompt = (
        f"{operator_input}Apply them to the completed migration. "
        f"Read the current versions of the configured targets: {names}. "
        "Consult the staged PHP source and migration rules when needed. "
        "Preserve unrelated edits and the migration rules. Edit only these targets and the optional handoff file. "
        f"{TARGET_ENCODING}{CSHARP_IDENTIFIERS}"
        f"{STATIC_SOURCE_REVIEW}"
        "Do not access files outside this workspace or run builds, tests, or checkers. "
        f"When finished, write exactly {token} to {completion.name} as the final file action."
    )
    handoff = context_handoff.handoff_prompt(record_path.parent, agent_root, targets, record,
                             draft=record.get("stopped_migration_draft"))
    prompt = context + handoff + memo_request + prompt
    command = junie_cli.build_command(agent_root, prompt + supplied, record["junie_max_steps"],
                                      guidelines=guidelines, **run_model_options(record_path.parent, record, config))
    allowed = {*targets, completion.relative_to(agent_root), Path(memo.name)}
    other_files = unlisted_workspace_files(agent_root, allowed)
    prior = record.get("stopped_migration_draft")
    try:
        _run_migration_junie(
            config, record_path, record, "editor_followup", guidelines, agent_root, inputs,
            command, completion, token, tuple(safe_target(agent_root, target) for target in targets),
            worktree=worktree, targets=targets, allowed=allowed, other_files=other_files, resumed_draft=prior, memo=memo,
            report_usage=run_records.junie_usage_recorder(record_path.parent, "migration", record["completed_capsules"]),
            prompts=LivePrompts(record_path.parent),
        )
    except KeyboardInterrupt as exc:
        if junie_cli.unconfirmed_junie_stop(exc) is not None:
            raise
        raise RunInterrupted(record["run_id"]) from None
    if unlisted_workspace_files(agent_root, allowed) != other_files:
        if prior:
            _checkpoint_migration_draft(
                record_path, record, worktree, agent_root, targets, allowed, other_files, prior["targets"],
            )
        raise ValueError("追加指示の実行中に移行対象外のファイルが変更されました。")
    complete = has_completion_record(completion, token) and all(
        safe_target(agent_root, target).is_file() for target in targets
    )
    # 質問も未完了として扱い、同じ照合で下書きを一度だけ保存する。
    try:
        question = read_operator_review(
            report, agent_root, worktree, targets, record, completed=complete, save_stopped_draft=False,
        )
    except (OSError, ValueError, RecursionError):
        if prior:
            # 保存も失敗した場合は、確認レポートの例外をコンテキストに残す。
            _checkpoint_migration_draft(
                record_path, record, worktree, agent_root, targets, allowed, other_files, prior["targets"],
            )
        raise
    if question or not complete:
        if prior:
            _checkpoint_migration_draft(
                record_path, record, worktree, agent_root, targets, allowed, other_files, prior["targets"],
            )
        return False
    record.pop("operator_question", None)
    target_files.apply_agent_targets(agent_root, worktree, targets, record["target_hashes"], record, record_path)
    save_junie_memo(memo, record_path.parent, record, phase="editor_followup")
    archive_stopped_migration(record)
    return True


def prepare_migration_materials(config, unit, run_path, record, worktree_root, relative_targets,
                                 agent_root, capsule, manifest, *, followup_only):
    """ルール適用の判断と移行先周辺の固定資料をJunie作業領域に用意する。"""
    record_path = run_path / "run.json"
    try:
        target_instructions = stage_target_context(
            worktree_root, relative_targets, capsule, agent_root, policy=unit.get("target_context"),
            require_existing=target_context_was_initialized(record),
        )
        if record.get("target_context_initialized") is not True:
            record["target_context_initialized"] = True
            run_records.write_run_record(record_path, record)
        selected = None
        if not followup_only or "detail_applicability" in record:
            selected = detail_selection.review_detail_applicability(
                run_path, capsule, manifest, relative_targets, record,
                record["junie_max_steps"],
                config=config, pinned_context=agent_root,
            )
    except KeyboardInterrupt as exc:
        record.update(status="interrupted", error="detail applicability review was interrupted; resume this Run")
        run_records.write_run_record(record_path, record)
        raise RunInterrupted(record["run_id"]) from exc
    except (OSError, ValueError, RuntimeError) as exc:
        record.update(status="junie_failed", error=format_error(exc))
        run_records.write_run_record(record_path, record)
        raise
    supplied_rules = []
    material_instructions = stage_migration_materials(
        capsule, manifest, agent_root, targets=relative_targets, selected=selected,
        supplied_rules=supplied_rules,
        excluded=frozenset(name for name, value in record.get("detail_overrides", {}).items() if not value["applies"]),
    )
    record["supplied_rules"] = supplied_rules
    return material_instructions + target_instructions


def execute_migration_capsule(config, run_path, record, worktree_root, agent_root, relative_targets,
                               agent_targets, index, capsule_count, through_capsule,
                               prompt, completion_path, completion_token, boundary_digest, exit_code, *, manifest,
                               verified_sources, resuming=False):
    record_path = run_path / "run.json"
    agent_context = agent_root / ".migration-context.md"
    boundary = agent_root / ".migration-boundary.md"
    max_steps = record["junie_max_steps"]
    memo, memo_request = junie_memo_request(agent_root, completion_token)
    allowed = {*relative_targets, completion_path.relative_to(agent_root), Path(memo.name)}
    complete = False
    targets_adopted = False
    with editor_queue_store.editor_queue(run_path) as queue:
        # Junieが停止している区間の境目で、画面からの編集と追加指示を取り込む。
        if not editor_queue_store.apply_editor_edits(queue, agent_root, worktree_root, relative_targets, record, record_path):
            return exit_code, False
        extra = dispatch_prompts(queue)
        if not extra and record.get("operator_question"):
            record.update(status="editor_checkpoint", error=record["operator_question"])
            run_records.write_run_record(record_path, record, timing_stage=None)
            return exit_code, False
        record.update(current_capsule=index, status="junie_running")
        record.pop("error", None)
        run_records.write_run_record(record_path, record)
    inputs = [Path(agent_context.name)]
    if boundary_digest:
        inputs.append(Path(boundary.name))
    if extra:
        instructions = stage_operator_prompts(agent_root, completion_token, extra)
        inputs.append(Path(instructions))
        prompt = (
            "Operator follow-up for this migration. Re-read the current target files and preserve"
            " the operator's edits. Adjust previously migrated C# if the follow-up requests it."
            f" Keep the PHP source range for this segment unchanged. Read `{instructions}`.\n\n{prompt}"
        )
    context, report = operator_edit_context(run_path, agent_root, completion_token, inputs, record.get("operator_question"))
    guidelines, supplied = task_guidelines(agent_root, inputs)
    draft = record.get("stopped_migration_draft")
    plan, plan_hint = navigation_plan(
        manifest, record, "resume" if resuming else "migration", relative_targets, current=index,
        staged_versions=draft["targets"] if draft else record.get("target_hashes"),
    )
    record["last_context_plan"] = plan
    scripted_handoff = ""
    if index == 1 and config.get("junie_apply_output_decisions") and config.get("junie_review_script_output") and record.get("script_handoffs"):
        scripted_handoff = script_handoff_context(
            run_path, record, config.get("scripts", {}), target_files.recorded_target_hashes(worktree_root, record),
        )
    handoff = context_handoff.handoff_prompt(run_path, agent_root, relative_targets, record, through=index,
                             draft=draft)
    prompt = context + plan_hint + scripted_handoff + handoff + memo_request + prompt
    other_files = unlisted_workspace_files(agent_root, allowed, verified_sources)
    command = junie_cli.build_command(agent_root, prompt + supplied, max_steps, guidelines=guidelines,
                                      **run_model_options(run_path, record, config))
    try:
        try:
            exit_code, stop_reason = _run_migration_junie(
                config, record_path, record, "migration", guidelines, agent_root, inputs,
                command, completion_path, completion_token, agent_targets,
                worktree=worktree_root, targets=relative_targets, allowed=allowed, other_files=other_files,
                memo=memo, through=index,
                report_usage=run_records.junie_usage_recorder(run_path, "migration", index),
                prompts=LivePrompts(run_path),
            )
        except KeyboardInterrupt as exc:
            if junie_cli.unconfirmed_junie_stop(exc) is not None:
                raise
            record.update(status="interrupted", error="interrupted by user; current capsule can be resumed")
            run_records.write_run_record(record_path, record)
            raise RunInterrupted(record["run_id"]) from None
        current_files = unlisted_workspace_files(agent_root, allowed)
        if current_files != other_files:
            changed = sorted(
                path.as_posix() for path in other_files.keys() | current_files.keys()
                if other_files.get(path) != current_files.get(path)
            )
            paths = ", ".join(repr(path) for path in changed[:5])
            if len(changed) > 5:
                paths += f" (+{len(changed) - 5} more)"
            raise ValueError(f"Junie changed files outside the configured targets: {paths}")
        completion_recorded = has_completion_record(completion_path, completion_token)
        missing = [path.as_posix() for path, file in zip(relative_targets, agent_targets, strict=True) if not file.is_file()]
        complete = completion_recorded and not missing
        if not complete and (prior := record.get("stopped_migration_draft")):
            _checkpoint_migration_draft(
                record_path, record, worktree_root, agent_root, relative_targets, allowed, other_files,
                prior["targets"],
            )
        question = read_operator_review(
            report, agent_root, worktree_root, relative_targets, record, completed=completion_recorded,
        )
        if question:
            complete = False
            record.update(status="editor_checkpoint", error=question, junie_exit_code=exit_code)
            run_records.write_run_record(record_path, record, timing_stage=None)
            return exit_code, False
        if completion_recorded:
            record.pop("operator_question", None)
        # 旧経路の途中編集は保持するが、完了トークンと全対象がそろうまで進捗は進めない。
        if complete or not record.get("stopped_migration_draft"):
            target_files.apply_agent_targets(agent_root, worktree_root, relative_targets, record["target_hashes"], record, record_path)
            targets_adopted = True
            archive_stopped_migration(record)
    except (OSError, ValueError) as exc:
        record.update(status="junie_failed", error=format_error(exc, command=command))
        run_records.write_run_record(record_path, record)
        raise
    finally:
        with editor_queue_store.editor_queue(run_path) as queue:
            editor_queue_store.finish_prompts(queue, complete and targets_adopted)
    record["junie_exit_code"] = exit_code
    if not complete:
        record["status"] = "interrupted" if len(missing) < len(agent_targets) else "junie_failed"
        record["error"] = (
            f"Junie did not produce configured targets: {', '.join(missing)}" if missing
            else "Junie did not leave a valid completion record for this capsule"
        )
        if stop_reason:
            record["error"] += f"\nJunie ACP stopped: {stop_reason}."
        record["error"] += f"\nExit code: {exit_code}\n" + junie_cli.launch_diagnostics(command)
        run_records.write_run_record(record_path, record)
        return exit_code, False

    save_junie_memo(memo, run_path, record, phase="migration", through=index)
    record["completed_capsules"] = index
    record["current_capsule"] = min(index + 1, capsule_count)
    with worktree_source_lock(worktree_root):
        record.setdefault("capsule_checkpoints", {})[str(index)] = _save_capsule_checkpoint(
            run_path, worktree_root, relative_targets, record["target_hashes"],
        )
    record.pop("migration_rewind", None)
    if index == through_capsule:
        record.pop("migration_range_stop", None)
        record["status"] = "editor_checkpoint" if index == capsule_count else "interrupted"
        record["error"] = "指定区間を完了しました。ビルドとチェッカーは通常の再開で実行できます。"
    else:
        record["status"] = "editor_checkpoint" if index == capsule_count else "junie_running"
        record.pop("error", None)
    run_records.write_run_record(record_path, record, timing_stage=3)
    try:
        completion_path.unlink(missing_ok=True)
    except OSError:
        report_activity("保存済み区間の一時完了マーカーを削除できませんでした。区間の完了は保持されています。")
    return exit_code, True


def finish_migration_followups(config, run_path, record, worktree_root, agent_root, relative_targets, capsule_count):
    record_path = run_path / "run.json"
    while record["completed_capsules"] == capsule_count and record["status"] == "editor_checkpoint":
        with editor_queue_store.editor_queue(run_path) as queue:
            check_cancelled()
            if not editor_queue_store.apply_editor_edits(queue, agent_root, worktree_root, relative_targets, record, record_path):
                break
            extra = dispatch_prompts(queue)
            if not extra and record.get("operator_question"):
                record["error"] = record["operator_question"]
                run_records.write_run_record(record_path, record, timing_stage=None)
                break
            if not extra:
                record["status"] = "junie_finished"
                record.pop("error", None)
                run_records.write_run_record(record_path, record, timing_stage=3)
                break
            record["status"] = "editor_followup"
            run_records.write_run_record(record_path, record)
        completed = False
        try:
            completed = _run_editor_followup(
                agent_root, worktree_root, relative_targets, record, record_path, config, extra,
            )
        finally:
            with editor_queue_store.editor_queue(run_path) as queue:
                editor_queue_store.finish_prompts(queue, completed)
                record["status"] = "editor_checkpoint"
                run_records.write_run_record(record_path, record, timing_stage=3)
        if not completed:
            record["error"] = record.get("operator_question") or "Junie が追加指示を完了できませんでした。実行を再開するともう一度試します。"
            run_records.write_run_record(record_path, record, timing_stage=None)
            break
