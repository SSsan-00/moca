from __future__ import annotations

import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import build_cycle, build_receipt, checker_cycle, repairs, roslyn_syntax
from .. import config as harness_config
from ..errors import CheckerRepairBlocked, RunInterrupted, RunScriptError, format_error
from ..execution import build_process_scope, check_cancelled, print_progress, report_activity
from ..files import safe_target
from ..junie import junie_cli
from ..runs import (
    editor_queue as editor_queue_store, run_records, stage_scripts, worktree_targets as target_files,
)
from ..runs.resume_plan import BUILD_THROUGH_STAGE, require_saved_checker
from ..runs.stage_scripts import (
    REPAIRABLE_SCRIPT_PHASES, append_script_advisory, review_script_output, script_digest,
    verification_target_basis, verify_saved_script_output,
)
from ..runs.stopped_repair import recover_selected_repair, require_repair_targets
from ..runs.worktree_targets import finish_pending_targets, review_target_hash, worktree_source_lock
from .build_cycle import execute_verification_build
from .checker_advisory import bounded_advisory_text, matching_evidence
from .checker_cycle import (
    apply_checker_pass, checker_repair_inputs, checker_repair_reason, grounded_repair_inputs,
    remaining_checker_findings, repairable_checker_advice, review_free_checker_output,
)
from .checker_output import parse_checker_diagnostics
from .diagnostic_log import remember_diagnostic
from .grounded_evidence import has_environment_diagnostic
from .repairs import finish_repair_attempt, repair_stalled
from .roslyn_evidence import record_roslyn_time, roslyn_diagnostic_lines, roslyn_fingerprints
from .verification_files import verification_files, verification_scripts


def _repair_script_advice(config, run_path, worktree, record, advisory, targets, hashes):
    if advisory.get("phase") not in REPAIRABLE_SCRIPT_PHASES or advisory.get("verdict") != "problem":
        return False
    history = record["repair_history"]
    if sum("instruction" not in entry for entry in history) >= config["max_repair_attempts"]:
        return False
    full_diagnostic = bounded_advisory_text(run_path, advisory)
    if full_diagnostic is None or has_environment_diagnostic(full_diagnostic):
        return False
    evidence = sorted(matching_evidence(run_path, advisory))
    if not evidence or sum(len(line) + 1 for line in evidence) > 8_000:
        return False
    if any(review_target_hash(worktree, Path(name)) != digest for name, digest in hashes.items()):
        return False
    findings, unrecognized = parse_checker_diagnostics(evidence, worktree)
    paths = {Path(finding.path) for finding in findings}
    if not paths.issubset(targets):
        return False
    if unrecognized:
        repair_targets, diagnostic, fingerprints = grounded_repair_inputs(
            config, run_path, worktree, targets, hashes, advisory, evidence, record,
        )
        if not paths.issubset(repair_targets):
            return False
    else:
        repair_targets = tuple(path for path in targets if path in paths)
        diagnostic = "\n".join(evidence)
        fingerprints = {finding.finding_id for finding in findings}
    if not repair_targets or repair_stalled(fingerprints, history, kind="script"):
        return False
    check_cancelled()
    junie_cli.require_interactive_cli()
    advisory["applied"] = "repair_requested"
    details = {"kind": "script", "findings": sorted(fingerprints), "advisory_evidence": dict(advisory)}
    repairs.attempt_repair(config, targets, repair_targets, worktree, run_path, diagnostic, record, details)
    return True


def _resume_verification_script(config, run_path, worktree, record, targets, *, rebuild_requested, allow_repair):
    pending = record.get("verification_script_pending")
    if pending is None:
        return
    phase = pending.get("phase") if isinstance(pending, dict) else None
    number = pending.get("command_number") if isinstance(pending, dict) else None
    path = run_path / "run.json"
    try:
        if (not isinstance(phase, str) or phase not in REPAIRABLE_SCRIPT_PHASES or type(number) is not int
                or pending.get("log") != f"{phase}.log" or not isinstance(pending.get("target_versions"), dict)
                or "log_sha256" not in pending
                or (pending["log_sha256"] is not None and not re.fullmatch("[0-9a-f]{64}", str(pending["log_sha256"])))
                or type(pending.get("byte_offset")) is not int or pending["byte_offset"] < 0
                or type(pending.get("byte_length")) is not int or pending["byte_length"] <= 0
                or type(pending.get("exit_code")) is not int):
            raise ValueError("Invalid saved verification script response; start a new Run")
        commands = config.get("scripts", {}).get(phase, [])
        if (not config.get("junie_review_script_output") or not config.get("junie_apply_output_decisions")
                or not 1 <= number <= len(commands) or script_digest(commands[number - 1]) != pending.get("command_sha256")):
            raise ValueError("Restore the command and review settings to resolve the saved script response")
        item = {key: value for key, value in pending.items() if key not in {"target_versions", "target_basis", "target_versions_match"}}
        verify_saved_script_output(run_path, item)
        hashes = target_files.recorded_target_hashes(worktree, record)
        if not hashes or not all(hashes.values()) or pending["target_versions"].keys() != hashes.keys():
            raise ValueError("Cannot verify the expected target files; inspect the saved response and targets")
        target_versions_match = pending.get("target_versions_match")
        unverified_target = pending.get("target_basis") != verification_target_basis(phase) or target_versions_match is not True
        target_changed = pending["target_versions"] != hashes
        if not rebuild_requested:
            if target_versions_match is False:
                raise ValueError("The target changed during the saved script; inspect the original log, then explicitly rebuild.")
            if unverified_target:
                raise ValueError("The saved script response has no verified target version; inspect it, then explicitly rebuild.")
            if target_changed:
                raise ValueError("The target changed. Inspect the saved script response, then explicitly rebuild the edited target.")
        history = record.setdefault("script_advisories", [])
        reviewed_failure = rebuild_requested and item["exit_code"] != 0 and any(
            advisory.get("status") == "reported" and all(advisory.get(key) == value for key, value in item.items())
            for advisory in history
        )
        if rebuild_requested and (unverified_target or target_changed or reviewed_failure):
            reason = "rebuild_unverified_target" if unverified_target else (
                "rebuild_edited_target" if target_changed else "rebuild_after_script_failure"
            )
            append_script_advisory(record, {**item, "status": "superseded", "applied": reason})
        else:
            review_script_output(config, run_path, record, item, apply_decisions=True)
            if hashes != target_files.recorded_target_hashes(worktree, record):
                item["applied"] = "review_required"
                raise ValueError("The target changed while reviewing the saved script response")
            if not (item["exit_code"] == 0 and item.get("applied") == "continued"):
                if not allow_repair or not _repair_script_advice(config, run_path, worktree, record, item, targets, hashes):
                    raise ValueError("The saved script response needs operator review; the command was not repeated")
        record.pop("verification_script_pending")
        run_records.write_run_record(path, record)
    except (KeyboardInterrupt, OSError, ValueError) as exc:
        message = f"{phase} #{number}: {exc or 'script review interrupted'}"
        record.update(status="script_failed", script_phase=phase, error=message)
        run_records.write_run_record(path, record)
        if junie_cli.unconfirmed_junie_stop(exc) is not None:
            raise
        raise RunScriptError(message, record["run_id"], interrupted=isinstance(exc, KeyboardInterrupt)) from None


def _verification_script(config, phase, worktree, run_path, record, targets, hashes, request):
    previous = (record.get("script_advisories") or [None])[-1]
    try:
        stage_scripts.run_script_phase(config, phase, worktree, run_path, record, verification_hashes=hashes)
    except RunScriptError as exc:
        advice = (record.get("script_advisories") or [None])[-1]
        pending = record.get("verification_script_pending")
        if (exc.interrupted or request or not pending or pending.get("target_versions_match") is not True or advice is None
                or advice is previous or advice.get("applied") != "review_required"):
            raise
        try:
            if not _repair_script_advice(config, run_path, worktree, record, advice, targets, pending["target_versions"]):
                raise exc from None
            record.pop("verification_script_pending", None)
            run_records.write_run_record(run_path / "run.json", record)
        except KeyboardInterrupt:
            record.update(status="script_failed", error="repair interrupted; inspect Junie's workspace", script_phase=phase)
            run_records.write_run_record(run_path / "run.json", record)
            raise RunInterrupted(record["run_id"]) from None
        return True
    return False


def _roslyn_before_build(config, run_path, worktree, targets, record, *, expected_targets, allow_repair):
    record_path = run_path / "run.json"

    def current_versions():
        return {path.as_posix(): review_target_hash(worktree, path) for path in targets}

    report_activity("Roslyn/Razor の事前診断を実行しています", kind="phase")
    previous = record.get("roslyn_preflight", {})
    report = None
    try:
        with build_process_scope(), worktree_source_lock(worktree):
            hashes = current_versions()
            if expected_targets is not None and hashes != expected_targets:
                raise ValueError("Build inputs changed before the Roslyn diagnostic.")
            report = roslyn_syntax.syntax_diagnostics(worktree, run_path, targets)
            if hashes != current_versions():
                report.update(status="blocked", reason="targets_changed_during_diagnostic")
        report["target_versions"] = hashes
        record["roslyn_preflight"] = report
        record_roslyn_time(record, report)
        run_records.write_run_record(record_path, record, timing_stage=4)
        if report["status"] in {"clear", "unavailable"}:
            return "build"
        if report["status"] == "errors":
            history = record.setdefault("repair_history", [])
            findings = report["findings"]
            fingerprints = roslyn_fingerprints(report)
            prior = next((attempt for attempt in reversed(history)
                          if attempt.get("roslyn_preflight") and "instruction" not in attempt), None)
            summary = "\n".join(roslyn_diagnostic_lines(report))
            report.update(fingerprints=sorted(fingerprints))
            if report["truncated"] or len(summary) > 8_000:
                report["reason"] = "diagnostics_too_large"
            elif (previous.get("status") == "blocked" and previous.get("target_versions") == hashes
                  and previous["reason"] not in {"junie_unavailable", "operator_review"}):
                report["reason"] = previous["reason"]
            elif not allow_repair:
                report["reason"] = "operator_review"
            elif sum("instruction" not in attempt for attempt in history) >= min(2, config["max_repair_attempts"]):
                report["reason"] = "repair_budget"
            elif (prior and not fingerprints < set(prior["findings"])) or repair_stalled(fingerprints, history, kind="build"):
                report["reason"] = "repair_stalled"
            else:
                affected = {item["path"] for item in findings}
                repair_targets = tuple(path for path in targets if path.as_posix() in affected)
                report["status"] = "repairing"
                try:
                    repairs.attempt_repair(config, targets, repair_targets, worktree, run_path, summary, record,
                                    {"kind": "build", "findings": sorted(fingerprints),
                                     "roslyn_preflight": True, "roslyn_versions": hashes})
                except (OSError, RuntimeError, ValueError) as error:
                    if junie_cli.unconfirmed_junie_stop(error) is not None:
                        raise
                    if isinstance(error, (junie_cli.JunieConnectionError, junie_cli.JunieLaunchError, junie_cli.JunieSessionError)):
                        reason = "junie_unavailable"
                    elif isinstance(error, CheckerRepairBlocked) and error.reason == "unchanged":
                        reason = "no_edits"
                    else:
                        reason = "repair_error"
                    report.update(status="errors", reason=reason, repair_error=format_error(error))
                else:
                    if hashes != current_versions():
                        return "retry"
                    report.update(status="errors", reason="no_edits")
        report["status"] = "blocked"
        remember_diagnostic(run_path, record, "build")
        remember_diagnostic(run_path, record, "checker")
        for key in ("build_log_path", "build_operation_id", "checker_build", "checker_log_path", "verification"):
            record.pop(key, None)
        detail = {
            "no_edits": "Junieによる変更がありませんでした。",
            "repair_stalled": "修正後も指摘が減らなかったため、追加の自動修正を行いませんでした。",
            "repair_budget": "自動修正の上限に達しました。",
            "operator_review": "エラーを確認し、移行先を修正してください。",
            "diagnostics_too_large": "指摘が多いため、自動修正を行いませんでした。",
            "repair_error": "Junieによる修正を完了できませんでした。",
            "junie_unavailable": "Junieの起動・通信・認証を確認してから、再開してください。",
            "targets_changed_during_diagnostic": "診断中に対象が変わりました。内容を確認して再開してください。",
            "analyzer_stop_unconfirmed": "解析器の終了を確認できません。プロセスが残っていないか確認してください。",
        }.get(report["reason"], "指摘と保存済みの変更を確認してください。")
        record.update(status="build_failed", build_exit_code=None, checker_exit_code=None,
                      error="事前診断で停止したため、正式なビルドは実行していません。 " + detail)
        run_records.write_run_record(record_path, record)
        return "stop"
    except KeyboardInterrupt:
        record["roslyn_preflight"] = {**(report or {}), "status": "cancelled", "reason": "operator_stop"}
        remember_diagnostic(run_path, record, "build")
        remember_diagnostic(run_path, record, "checker")
        for key in ("build_log_path", "build_operation_id", "checker_build", "checker_log_path", "verification"):
            record.pop(key, None)
        record.update(status="build_failed", build_exit_code=None, checker_exit_code=None,
                      error="事前診断を中断しました。正式なビルドは実行していません。")
        run_records.write_run_record(record_path, record)
        raise RunInterrupted(record["run_id"]) from None


def run_verification(
    config: dict[str, Any], unit: dict[str, Any], result: dict[str, Any], record: dict[str, Any],
    *, process_queued_edits: Callable[[], dict[str, Any]], request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """毎回ビルドを先に実行し、指摘の状態に応じて修正かレビューに進む。"""
    start_stage = request.get("start_stage") if request else None
    build_only = start_stage == "build"
    rebuild_requested = build_only or start_stage == BUILD_THROUGH_STAGE
    automatic = not request or start_stage == BUILD_THROUGH_STAGE
    manual_request = None if automatic else request
    if not build_only:
        require_saved_checker(config, record)
    run_path = Path(result["run_path"])
    worktree = Path(result["worktree_path"])
    targets = harness_config.target_paths(unit)
    record_path = run_path / "run.json"
    finish_pending_targets(run_path / "agent-workspace", worktree, targets, record, record_path)
    original_versions = record.get("target_hashes", {}).copy()
    try:
        if not build_only:
            recover_selected_repair(run_path, worktree, targets, record)
    except CheckerRepairBlocked as exc:
        error = str(exc) if exc.reason == "stopped_draft_keyword" else format_error(exc)
        record.update(error=error, checker_review_reason=exc.reason)
        run_records.write_run_record(record_path, record)
        return {**result, "status": record["status"], "error": error}
    _resume_verification_script(
        config, run_path, worktree, record, targets, rebuild_requested=rebuild_requested, allow_repair=not build_only,
    )
    stage_scripts.run_script_phase(config, "after_migration", worktree, run_path, record, once=True)
    mode = request["intent"].removeprefix("stop_") if request else None
    configured_checker = config["checker_command"]
    command = None if mode in {"build", "build_repair"} else configured_checker
    scripts = verification_scripts(config, command)
    limit = config["max_repair_attempts"]
    history = record.setdefault("repair_history", [])
    expected_targets = request.get("build_targets") if request else None
    if expected_targets is not None and automatic:
        updated_versions = record.get("target_hashes", {})
        expected_targets = {name: updated_versions.get(name) if name in updated_versions
                            and updated_versions[name] != original_versions.get(name) else digest
                            for name, digest in expected_targets.items()}

    def save_verification() -> None:
        verification = record["verification"]
        current = verification_files(config, worktree, unit["target"], command, scripts)
        verification["inputs_changed_during_verification"] = verification["input_files"] != current
        verification["scripts_passed"] = True
        run_records.write_run_record(record_path, record, timing_stage=record.get("active_stage"))

    # 修正に成功したらここへ戻り、変更後のファイルを必ずビルドから検証し直す。
    while True:
        previous_receipt = record.get("checker_build")
        with editor_queue_store.editor_queue(run_path) as queue:
            pending_changes = automatic and bool(queue["edits"] and any(prompt["status"] == "queued" for prompt in queue["prompts"]))
            if not pending_changes:
                # 指示の受付と工程変更を直列化し、修正終了間際の送信もビルド前に扱う。
                if record["status"] == "repairing":
                    record["status"] = "building"
                # フック中に中断しても、前回のビルドは今回の待機条件を満たさない。
                record.pop("build_operation_id", None)
                record.pop("checker_build", None)
                run_records.write_run_record(record_path, record, timing_stage=4)
        if pending_changes:
            try:
                check_cancelled()
            except KeyboardInterrupt:
                raise RunInterrupted(record["run_id"]) from None
            return process_queued_edits()
        input_files = verification_files(config, worktree, unit["target"], command, scripts)
        if _verification_script(config, "before_build", worktree, run_path, record, targets, None, manual_request):
            expected_targets = None
            continue
        preflight = _roslyn_before_build(
            config, run_path, worktree, targets, record, expected_targets=expected_targets,
            allow_repair=automatic or mode == "build_repair",
        )
        if preflight == "retry":
            expected_targets = None
            continue
        if preflight == "stop":
            return {**result, "status": record["status"], "error": record["error"], "build_exit_code": None,
                    "build_log_path": None, "roslyn_preflight": record["roslyn_preflight"]}
        verified, hashes, inputs = execute_verification_build(
            config, unit, result, record, run_path, worktree, targets, expected_targets=expected_targets,
            previous_receipt=previous_receipt, configured_checker=configured_checker, command=command,
            scripts=scripts, input_files=input_files, request=request,
        )
        expected_targets = None
        if record["status"] == "build_failed":
            try:
                if (automatic or mode == "build_repair" and len(history) < limit) and build_cycle.repair_build_failure(
                    config, targets, worktree, run_path, record,
                    **({"operator_instruction": request["text"]} if mode == "build_repair" else {}),
                ):
                    continue
            except (OSError, RuntimeError, ValueError) as exc:
                record.update(status="build_failed", error=format_error(exc))
                run_records.write_run_record(record_path, record)
                if junie_cli.unconfirmed_junie_stop(exc) is not None:
                    raise
                return {**verified, "error": record["error"]}
            except KeyboardInterrupt:
                record.update(status="build_failed", error="repair interrupted; inspect Junie's workspace")
                run_records.write_run_record(record_path, record)
                raise RunInterrupted(record["run_id"]) from None
            return verified
        check_cancelled()
        if _verification_script(config, "after_build", worktree, run_path, record, targets, hashes, manual_request):
            continue
        if inputs and inputs == build_receipt.build_inputs(worktree, config["build_project"]):
            if artifacts := build_receipt.build_artifacts(worktree, inputs):
                record["checker_build"] = {"inputs": inputs, "artifacts": artifacts}
        if command is None:
            if not build_only:
                try:
                    require_repair_targets(record, ())
                except CheckerRepairBlocked as exc:
                    record.update(status="build_failed", error=format_error(exc))
                    run_records.write_run_record(record_path, record)
                    return {**verified, "status": record["status"], "error": record["error"]}
                finish_repair_attempt(record)
            save_verification()
            return verified
        run_records.write_run_record(record_path, record, timing_stage=5)
        if _verification_script(config, "before_checker", worktree, run_path, record, targets, hashes, manual_request):
            continue
        if request and any(review_target_hash(worktree, path) != hashes[path.as_posix()] for path in targets):
            raise ValueError("チェッカーの開始前に移行先が更新されました。エディタで確認して指示を送信し直してください。")
        review_free_output = bool(config.get("checker_review_free_output"))
        checked, findings, use_raw_output = checker_cycle.run_checker(
            command, verified, record, config["build_project"], require_zero_for_success=review_free_output,
            rule_evidence=bool(config.get("checker_rule_evidence")),
        )
        if record.get("unconfirmed_command"):
            return checked
        check_cancelled()
        if request and any(review_target_hash(worktree, path) != hashes[path.as_posix()] for path in targets):
            raise ValueError("チェッカー実行中に移行先が更新されました。エディタで確認して指示を送信し直してください。")
        if use_raw_output and review_free_output:
            checked = review_free_checker_output(config, checked, record)
        if (record.get("checker_exit_code") is not None
                and _verification_script(config, "after_checker", worktree, run_path, record, targets, hashes, manual_request)):
            continue
        try:
            apply_decision = use_raw_output and review_free_output and config.get("junie_apply_output_decisions")
            if apply_decision:
                if any(review_target_hash(worktree, Path(name)) != digest for name, digest in hashes.items()):
                    raise CheckerRepairBlocked("verification_inputs_changed", "Migration targets changed after the build; verify again")
                checked = apply_checker_pass(run_path, checked, record)
            if checked["status"] == "checker_succeeded":
                require_repair_targets(record, ())
                finish_repair_attempt(record)
            save_verification()
            remaining = remaining_checker_findings(record, checked, findings, use_raw_output)
            if remaining is None:
                return checked
            if not remaining and not use_raw_output:
                require_repair_targets(record, ())
                record["status"] = "checker_accepted"
                finish_repair_attempt(record)
                save_verification()
                return {**checked, "status": record["status"]}
            if not automatic:
                return checked
            advisory_path = use_raw_output and review_free_output
            if advisory_path and (not apply_decision or record["checker_advisory"].get("verdict") != "problem"):
                return checked
            advised_repair = False
            grounded = False
            if not limit:
                record["checker_review_reason"] = "repair_disabled"
                run_records.write_run_record(record_path, record, timing_stage=5)
                return checked
            if sum("instruction" not in attempt for attempt in history) >= limit:
                raise CheckerRepairBlocked("repair_limit", f"automatic repair limit reached ({limit})")
            if advisory_path:
                advisory = record["checker_advisory"]
                full_diagnostic = bounded_advisory_text(run_path, advisory)
                if full_diagnostic is None or has_environment_diagnostic(full_diagnostic):
                    return checked
                advised_repair = True
                use_raw_output = False
                if not repairable_checker_advice(run_path, advisory, remaining, targets):
                    if any(Path(finding.path) not in targets for finding in remaining):
                        return checked
                    evidence = sorted(matching_evidence(run_path, advisory))
                    if not evidence or sum(len(line) + 1 for line in evidence) > 8_000:
                        return checked
                    repair_targets, diagnostic, fingerprints = grounded_repair_inputs(
                        config, run_path, worktree, targets, hashes, advisory, evidence, record,
                    )
                    if not repair_targets or any(Path(finding.path) not in repair_targets for finding in remaining):
                        return checked
                    log = safe_target(run_path, Path("checker.log"))
                    grounded = True
            if not grounded:
                log, repair_targets, diagnostic, fingerprints = checker_repair_inputs(
                    run_path, remaining, targets, use_raw_output,
                )
            if repair_stalled(fingerprints, history, kind="checker", raw=use_raw_output):
                raise CheckerRepairBlocked("finding_repeated", "a previous checker finding is still present")
            junie_cli.require_interactive_cli()
            attempt = len(history) + 1
            checker_log = safe_target(run_path, Path(f"checker-before-repair-{attempt}.log"))
            shutil.copyfile(log, checker_log)
            print_progress(f"Junie repair {attempt}/{limit} for {', '.join(path.as_posix() for path in repair_targets)}")
            details = {"findings": sorted(fingerprints), "checker_log_path": str(checker_log)}
            if advised_repair:
                record["checker_advisory"]["applied"] = "repair_requested"
                details["advisory_evidence"] = dict(record["checker_advisory"])
            if use_raw_output:
                details["raw_checker_output"] = True
            repairs.attempt_repair(config, targets, repair_targets, worktree, run_path, diagnostic, record, details)
        except (OSError, RuntimeError, ValueError) as exc:
            record.update(status="checker_needs_review", checker_review_reason=checker_repair_reason(exc),
                          error=format_error(exc))
            run_records.write_run_record(record_path, record)
            if junie_cli.unconfirmed_junie_stop(exc) is not None:
                raise
            return {**checked, "status": record["status"], "error": record["error"]}
        except KeyboardInterrupt:
            record.update(status="checker_needs_review", checker_review_reason="repair_interrupted",
                          error="repair interrupted; inspect Junie's workspace")
            run_records.write_run_record(record_path, record)
            raise RunInterrupted(record["run_id"]) from None
