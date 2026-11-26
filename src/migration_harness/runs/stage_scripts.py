from __future__ import annotations

import hashlib
import json
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any

from . import run_records, worktree_targets as target_files
from .. import config as harness_config, execution
from ..command_paths import script_command_for_phase
from ..errors import RunScriptError, format_error
from ..execution import ConfiguredProcessIncomplete, check_cancelled, print_progress, report_activity
from ..files import safe_target
from ..junie import junie_cli
from ..junie.migration_session import advisory_model_options
from ..log_text import read_log_tail
from ..verification import checker_advisory
from ..verification.checker_advisory import bounded_advisory_text
from ..verification.grounded_evidence import has_environment_diagnostic
from ..verification.script_handoff import make_script_handoff
from .run_records import execution_lock, validate_run_id
from .worktree_targets import review_target_hash


_HANDOFF_PHASES = frozenset({"before_prepare", "after_prepare", "before_migration"})


REPAIRABLE_SCRIPT_PHASES = frozenset({"before_build", "after_build", "before_checker", "after_checker"})


_RECOVERABLE_ONCE_PHASES = frozenset({"after_prepare", "before_migration", "after_migration"})


_LEGACY_SCRIPT_HASH_BYTES = 32_769


_SAVED_SCRIPT_OUTPUT_CHANGED = "The saved script output is missing or changed; inspect the logs before starting a new Run"


UNCONFIRMED_COMMAND_GUIDANCE = (
    "ログと残存プロセスを確認し、コマンドを同期実行に直してから新しい Run を開始してください。"
)


def script_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode("utf-8")).hexdigest()


def _script_attempt_matches(attempt, phase, number, cwd, invocation):
    return (attempt["phase"], attempt["number"], attempt["command"], attempt["cwd"]) == (
        phase, number, script_digest(invocation), str(cwd.resolve()),
    )


def block_unknown_script(record):
    require_completed_command(record)
    if attempt := record.get("script_inflight"):
        raise ValueError(
            f"自作スクリプトの実行結果不明: {attempt['phase']} #{attempt['number']}。"
            "ログと残存プロセスを確認し、進捗画面で再試行する版を確認してください。"
        )


def require_completed_command(record):
    if command := record.get("unconfirmed_command"):
        raise ValueError(f"{command['phase']} の完了を確認できません（{command['log']}）。" + UNCONFIRMED_COMMAND_GUIDANCE)


def script_recovery(config, run_id, *, retry_version=None):
    validate_run_id(run_id)
    with execution_lock(config, run_id):
        run_path, worktree, record, unit = run_records.load_saved_run(config, run_id)
        require_completed_command(record)
        attempt = record.get("script_inflight")
        if not attempt or attempt["phase"] not in _RECOVERABLE_ONCE_PHASES:
            raise ValueError("実行結果が不明な一度限りのスクリプトはありません。")
        phase, number = attempt["phase"], attempt["number"]
        extras = tuple(unit.get("draft_import_commands", ())) if phase == "before_migration" else ()
        commands = [*config.get("scripts", {}).get(phase, []), *extras]
        invocation = script_command_for_phase(commands[number - 1], phase, worktree,
                                              config.get("build_project"), record.get("target")) if 1 <= number <= len(commands) else ()
        saved_progress = record.get("script_command_progress", {}).get(phase, [])
        current = (_script_attempt_matches(attempt, phase, number, worktree, invocation)
                   and [script_digest(command) for command in commands[:number - 1]] == saved_progress)
        targets = {path.as_posix(): review_target_hash(worktree, path) for path in harness_config.target_paths(unit)}
        version = script_digest([run_id, attempt, commands, invocation, targets])
        if retry_version is None:
            return {"run_id": run_id, "phase": phase, "number": number,
                    "log": str(run_path / f"{phase}.log"), "version": version,
                    "can_retry": current and not record.get("migration_stop_unconfirmed"),
                    "warning": "実行済みの可能性があります。ログ・対象と残存プロセスを確認してください。"}
        if not current or version != retry_version:
            raise ValueError("確認後にコマンド・対象版が変わりました。保存状態とログを確認してください。")
        if record.get("migration_stop_unconfirmed"):
            raise ValueError("前回のプロセス終了を確認できないため、再試行できません。")
        run_script_phase(config, phase, worktree, run_path, record, once=True,
                          extra_commands=extras, retry_attempt=attempt)
        return {"run_id": run_id, "phase": phase, "number": number, "retried": True}


def run_script_phase(
    config: dict[str, Any], phase: str, cwd: Path, run_path: Path,
    record: dict[str, Any] | None = None, *, once: bool = False,
    extra_commands: tuple[list[str], ...] = (), retry_attempt: dict[str, Any] | None = None,
    verification_hashes: dict[str, str | None] | None = None,
) -> None:
    if record is not None and retry_attempt is None:
        block_unknown_script(record)
    configured = config.get("scripts", {}).get(phase, [])
    commands = [*configured, *extra_commands]
    pending = record.get("script_decisions_pending", {}).get(phase) if once and record is not None else None
    if not pending and (not commands or (record is not None and once and phase in record.get("completed_script_phases", []))):
        return

    run_path.mkdir(parents=True, exist_ok=True)
    log_path = run_path / f"{phase}.log"
    record_path = run_path / "run.json"
    previous_status = record["status"] if record is not None else "preparing"
    previous_stage = record.get("active_stage") if record is not None else None
    if retry_attempt is not None:
        previous_status, previous_stage = retry_attempt["previous_status"], retry_attempt["previous_stage"]
    command_ids = []
    start = 0
    apply_decisions = bool(config.get("junie_apply_output_decisions") and config.get("junie_review_script_output"))
    if record is not None and once:
        command_ids = [script_digest(command) for command in commands]
        completed_ids = record.get("script_command_progress", {}).get(phase, [])
        # 中断後は成功済みの先頭部分だけを飛ばす。コマンドが変わった位置からは再実行する。
        for saved, current in zip(completed_ids, command_ids):
            if saved != current:
                break
            start += 1
    if retry_attempt is not None and retry_attempt["number"] != start + 1:
        raise ValueError("保存済みの実行位置が確認したコマンドと一致しません。")
    if record is not None:
        record.setdefault("script_logs", {})[phase] = str(log_path)
        record.update(status="script_running", script_phase=phase)
        run_records.write_run_record(record_path, record)
    print_progress(f"Running {phase} scripts in {cwd} (log: {log_path})")
    number = start + 1
    invocation = commands[start] if start < len(commands) else []
    try:
        if pending:
            number = pending["command_number"]
            if not apply_decisions:
                raise ValueError("Re-enable script output review and decisions to resolve the saved response; the command was not repeated")
            if not 1 <= number <= start or command_ids[number - 1] != pending["command_sha256"]:
                raise ValueError("The configured script changed while its response was pending")
            _resolve_script_decision(config, run_path, cwd, record, pending)
            if previous_status == "script_failed":
                previous_status = pending["previous_status"]
                previous_stage = pending["previous_stage"]
                record.pop("error", None)
        with log_path.open("ab") as log:
            stop = start + 1 if retry_attempt is not None else len(commands)
            for number, command in enumerate(commands[start:stop], start + 1):
                if record is not None and record.get("script_handoffs"):
                    remaining_receipts = [item for item in record["script_handoffs"]
                                          if (item["phase"], item["command_number"]) != (phase, number)]
                    if len(remaining_receipts) != len(record["script_handoffs"]):
                        record["script_handoffs"] = remaining_receipts
                        run_records.write_run_record(record_path, record)
                retry_label = " (operator retry)" if retry_attempt is not None else ""
                log.write(f"\n[{phase} {number}]{retry_label} {command[0]}\n".encode("utf-8"))
                log.flush()
                invocation = command
                invocation = script_command_for_phase(
                    command, phase, cwd, config.get("build_project"), (record or {}).get("target"),
                )
                review_output = bool(record is not None and config.get("junie_review_script_output")
                                     and number <= len(configured) and not command[0].startswith("@"))
                start_output = os.fstat(log.fileno()).st_size if review_output else 0
                tracked = record is not None and once and phase in _RECOVERABLE_ONCE_PHASES
                if tracked:
                    check_cancelled()
                    record["script_inflight"] = {
                        "id": uuid.uuid4().hex, "phase": phase, "number": number,
                        "command": script_digest(invocation), "cwd": str(cwd.resolve()),
                        "previous_status": previous_status, "previous_stage": previous_stage,
                    }
                    run_records.write_run_record(record_path, record)
                before_command_versions = (
                    target_files.recorded_target_hashes(cwd, record)
                    if not once and apply_decisions and review_output and phase in REPAIRABLE_SCRIPT_PHASES
                    else None
                )
                command_target_versions = before_command_versions if phase == "before_build" else verification_hashes
                completed = execution.run_process(invocation, label=f"スクリプト {phase}", synchronous=True,
                                        cwd=cwd, stdout=log, stderr=subprocess.STDOUT, check=False)
                if tracked and completed.returncode < 0:
                    block_unknown_script(record)
                length = 0
                if review_output and completed.returncode >= 0:
                    log.flush()
                    length = os.fstat(log.fileno()).st_size - start_output
                decision = (_script_decision_item(phase, command, number, completed.returncode, start_output, length)
                            if length > 0 else None)
                settled_once = completed.returncode == 0 and record is not None and once
                if settled_once:
                    if apply_decisions and decision is not None:
                        decision.update(previous_status=previous_status, previous_stage=previous_stage,
                                        target_versions=None, log_sha256=None)
                        record.setdefault("script_decisions_pending", {})[phase] = decision
                    record.setdefault("script_command_progress", {})[phase] = command_ids[:number]
                if tracked:
                    record.pop("script_inflight")

                repeatable_review = decision is not None and apply_decisions and command_target_versions is not None
                if decision is not None:
                    if apply_decisions and settled_once:
                        run_records.write_run_record(record_path, record)
                        decision["log_sha256"] = _script_output_sha256(run_path, decision)
                        decision["log_hash_scope"] = "full_range"
                        decision["target_versions"] = target_files.recorded_target_hashes(cwd, record)
                        run_records.write_run_record(record_path, record)
                        _resolve_script_decision(config, run_path, cwd, record, decision, verify_output=False)
                    else:
                        if tracked:
                            run_records.write_run_record(record_path, record)
                        if repeatable_review:
                            _record_verification_script_output(
                                run_path, cwd, record, decision, command_target_versions, before_command_versions,
                            )
                        review_script_output(config, run_path, record, decision, apply_decisions=apply_decisions)
                        if apply_decisions and completed.returncode == 0:
                            _save_script_handoff(run_path, cwd, record, decision)
                        if apply_decisions and completed.returncode == 0 and decision.get("applied") not in {"continued", "handed_off"}:
                            raise ValueError("The script response needs operator review; see the script log and advisory")
                elif settled_once or tracked:
                    run_records.write_run_record(record_path, record)
                if completed.returncode:
                    raise ValueError(f"終了コード {completed.returncode}")
                if command_target_versions is not None and (phase != "before_build" or repeatable_review):
                    if command_target_versions != target_files.recorded_target_hashes(cwd, record):
                        if repeatable_review:
                            decision["applied"] = "review_required"
                        raise ValueError("The target changed during verification; inspect the script log and explicitly rebuild")
                if repeatable_review and decision.get("applied") == "continued":
                    record.pop("verification_script_pending")
                    run_records.write_run_record(record_path, record)
    except (KeyboardInterrupt, OSError, ValueError) as exc:
        reason = "中断しました" if isinstance(exc, KeyboardInterrupt) else format_error(exc, command=invocation, cwd=cwd)
        message = f"スクリプト {phase} の {number} 番目で停止しました。ログ {log_path}\n{reason}"
        if not isinstance(exc, KeyboardInterrupt):
            try:
                message += "\n" + read_log_tail(log_path)
            except OSError:
                pass
        run_id = None
        if record is not None:
            run_id = record["run_id"]
            if isinstance(exc, ConfiguredProcessIncomplete):
                record["unconfirmed_command"] = {"phase": phase, "log": log_path.name}
                message += "\n" + UNCONFIRMED_COMMAND_GUIDANCE
            record.update(status="script_failed", error=message)
            run_records.write_run_record(record_path, record)
        if junie_cli.unconfirmed_junie_stop(exc) is not None:
            raise
        raise RunScriptError(message, run_id, interrupted=isinstance(exc, KeyboardInterrupt)) from None
    if record is not None:
        if once and (retry_attempt is None or retry_attempt["number"] == len(commands)):
            record.setdefault("completed_script_phases", []).append(phase)
            record.get("script_command_progress", {}).pop(phase, None)
        record["status"] = previous_status
        if retry_attempt is not None:
            record.pop("error", None)
        record.pop("script_phase", None)
        run_records.write_run_record(record_path, record, timing_stage=previous_stage)


def verification_target_basis(phase):
    return "before_build_command" if phase == "before_build" else "verification"


def _script_decision_item(phase, command, number, exit_code, offset, length):
    return {
        "phase": phase, "command_number": number, "exit_code": exit_code, "log": f"{phase}.log",
        "byte_offset": offset, "byte_length": length,
        "command_sha256": hashlib.sha256(json.dumps(command).encode("utf-8")).hexdigest(),
    }


def _script_output_sha256(run_path: Path, item: dict[str, Any]) -> str:
    digest = hashlib.sha256()
    remaining = item["byte_length"]
    try:
        with safe_target(run_path, Path(item["log"])).open("rb") as stream:
            stream.seek(item["byte_offset"])
            while remaining:
                block = stream.read(min(remaining, 64 * 1024))
                if not block:
                    raise EOFError
                digest.update(block)
                remaining -= len(block)
    except (OSError, ValueError, OverflowError, EOFError) as exc:
        raise ValueError(_SAVED_SCRIPT_OUTPUT_CHANGED) from exc
    return digest.hexdigest()


def _record_verification_script_output(run_path, cwd, record, item, expected, before):
    path = run_path / "run.json"
    pending = record["verification_script_pending"] = {
        **item, "target_versions": expected.copy(), "target_basis": verification_target_basis(item["phase"]),
        "target_versions_match": None, "log_sha256": None,
    }
    run_records.write_run_record(path, record)
    reason = "output_unverified"
    try:
        digest = _script_output_sha256(run_path, item)
        item.update(log_sha256=digest, log_hash_scope="full_range")
        pending.update(item)
        run_records.write_run_record(path, record)

        reason = "target_unverified"
        try:
            after = target_files.recorded_target_hashes(cwd, record)
        except OSError as exc:
            reason = "target_unreadable"
            item["target_read_error"] = pending["target_read_error"] = type(exc).__name__
            raise ValueError("The script response has no verified target version; target read failed after execution") from exc
        if not all(snapshot and all(snapshot.values()) for snapshot in (expected, before, after)):
            raise ValueError("The script response has no verified target version; inspect the log and explicitly rebuild")
        pending["target_versions_match"] = expected == before == after
        if not pending["target_versions_match"]:
            reason = "target_changed"
            raise ValueError("The target changed during verification; inspect the script log and explicitly rebuild")
    except (KeyboardInterrupt, ValueError) as exc:
        interrupted = isinstance(exc, KeyboardInterrupt)
        append_script_advisory(record, {
            **item, "log_sha256": pending["log_sha256"], "applied": "review_required",
            "reason": "verification_interrupted" if interrupted else reason,
            "status": "interrupted" if interrupted else "blocked",
        })
        raise


def verify_saved_script_output(run_path, item):
    legacy_complete = "log_hash_scope" not in item and item["byte_length"] <= _LEGACY_SCRIPT_HASH_BYTES
    if item.get("log_sha256") is None or (not legacy_complete and item.get("log_hash_scope") != "full_range"):
        raise ValueError("The saved script output has no verifiable full-range hash; inspect the logs and start a new Run")
    if _script_output_sha256(run_path, item) != item["log_sha256"]:
        raise ValueError(_SAVED_SCRIPT_OUTPUT_CHANGED)


def _resolve_script_decision(config, run_path, cwd, record, decision, *, verify_output=True):
    if verify_output:
        verify_saved_script_output(run_path, decision)
    expected = decision.get("target_versions")
    if expected is None:
        raise ValueError("The saved script response has no verified target version; inspect the logs and start a new Run")
    if expected != target_files.recorded_target_hashes(cwd, record):
        raise ValueError("Target files changed since the script response was recorded")
    item = {key: value for key, value in decision.items() if key not in {"target_versions", "previous_status", "previous_stage"}}
    review_script_output(config, run_path, record, item, apply_decisions=True)
    if expected == target_files.recorded_target_hashes(cwd, record):
        _save_script_handoff(run_path, cwd, record, item)
    if item.get("applied") not in {"continued", "handed_off"} or expected != target_files.recorded_target_hashes(cwd, record):
        raise ValueError("The saved script response needs operator review; the command was not repeated")
    record["script_decisions_pending"].pop(decision["phase"])
    run_records.write_run_record(run_path / "run.json", record)


def append_script_advisory(record, item):
    history = record.setdefault("script_advisories", [])
    history.append(item)
    del history[:-100]


def review_script_output(config, run_path, record, item, *, apply_decisions):
    item["status"] = "pending"
    append_script_advisory(record, item)
    path = run_path / "run.json"
    run_records.write_run_record(path, record)
    phase, number = item["phase"], item["command_number"]
    try:
        check_cancelled()
        report_activity(f"Junie に {phase} の {number} 番目の出力の参考判定を依頼しています。", kind="phase")
        advisory = checker_advisory.output_advisory(
            run_path, item["log"], item["exit_code"], phase=phase, byte_offset=item["byte_offset"],
            byte_length=item["byte_length"], expected_hash=item.get("log_sha256"), allow_pass=apply_decisions,
            allow_handoff=apply_decisions and item["exit_code"] == 0 and phase in _HANDOFF_PHASES,
            model_options=advisory_model_options(config, None if phase == "before_prepare" else record, run_path),
        )
        check_cancelled()
        item.update(advisory)
        if apply_decisions:
            item["applied"] = "continued" if advisory.get("corroborated") else "review_required"
            if advisory.get("reason") == "input_too_large":
                raise ValueError("出力が Junie の参考判定上限（32 KiB）を超えたため送信しませんでした"
                                 f"（終了コード {item['exit_code']}）。保存済みログを確認してください。")
    except KeyboardInterrupt:
        item["status"] = "interrupted"
        raise
    finally:
        run_records.write_run_record(path, record)


def _save_script_handoff(run_path, cwd, record, item):
    if item["phase"] not in _HANDOFF_PHASES or item.get("verdict") != "handoff":
        return
    captured = bounded_advisory_text(run_path, item)
    if captured is None or has_environment_diagnostic(captured):
        return
    versions = target_files.recorded_target_hashes(cwd, record)
    receipt = make_script_handoff(run_path, record, item, versions)
    if receipt is None:
        return
    existing = record.setdefault("script_handoffs", [])
    if receipt not in existing:
        if len(existing) >= 8:
            return
        existing.append(receipt)
    item["applied"] = "handed_off"
    run_records.write_run_record(run_path / "run.json", record)
