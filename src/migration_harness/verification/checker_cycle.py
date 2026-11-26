from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import build_receipt, checker_output, grounded_evidence, repairs
from .. import execution
from ..command_paths import expand_command_paths
from ..errors import CheckerRepairBlocked, RunInterrupted, format_error
from ..execution import ConfiguredProcessIncomplete, check_cancelled, print_progress, report_activity
from ..files import safe_target
from ..junie import junie_cli
from ..junie.migration_session import advisory_model_options
from ..runs import editor_queue as editor_queue_store, run_records
from ..runs.run_records import edit_run_record, execution_lock, validate_run_id
from ..runs.stage_scripts import UNCONFIRMED_COMMAND_GUIDANCE
from ..runs.worktree_targets import review_target_hash, worktree_source_lock
from .checker_advisory import checker_advisory, matching_evidence
from .checker_output import (
    CHECKER_SUCCESS_MESSAGE, CheckerFinding, accepted_findings, checker_response_status,
    decode_checker_output,
)
from .checker_rule_evidence import rule_checker_exchange
from .diagnostic_log import review_log
from .verification_files import checker_provenance, verification_files


def checker_repair_reason(error: BaseException) -> str:
    if isinstance(error, CheckerRepairBlocked):
        return error.reason
    if isinstance(error, (junie_cli.JunieLaunchError, junie_cli.JunieSessionError, junie_cli.JunieConnectionError)):
        return "junie_unavailable"
    return "repair_error"


def run_checker(
    command: list[str], result: dict[str, Any], record: dict[str, Any],
    build_project: str | None, *, require_zero_for_success: bool = False, rule_evidence: bool = False,
) -> tuple[dict[str, Any], list[CheckerFinding], bool]:
    run_path = Path(result["run_path"])
    record_path = run_path / "run.json"
    log_path = run_path / "checker.log"
    findings = []
    use_raw_output = False
    record.update(status="checking", checker_exit_code=None, checker_log_path=str(log_path), review_file_hashes={})
    record.pop("error", None)
    record.pop("checker_review_reason", None)
    record.pop("checker_success_message", None)
    record.pop("checker_advisory", None)
    if previous_evidence := record.pop("checker_rule_evidence", None):
        record["previous_checker_rule_evidence"] = previous_evidence
    record.pop("checker_rule_evidence_error", None)
    run_records.write_run_record(record_path, record)
    print_progress(f"Running checker in {result['worktree_path']} (log: {log_path})")
    report_activity("独自チェッカーで確認しています", kind="phase",
                    detail="ビルドは成功しました。設定済みのチェッカーの結果を待っています。")
    try:
        # stdoutとstderrをそのまま保存し、利用者のチェッカー固有の出力形式を保つ。
        with log_path.open("wb") as log:
            try:
                command = expand_command_paths(command, Path(result["worktree_path"]), build_project, record["target"])
                with rule_checker_exchange(run_path, Path(result["worktree_path"]), record,
                                           enabled=rule_evidence) as (checker_options, collect):
                    completed = execution.run_process(
                        command, label="チェッカー", synchronous=True, cwd=result["worktree_path"], stdout=log,
                        stderr=subprocess.STDOUT, check=False, **checker_options,
                    )
                    if collect is not None and completed.returncode == 0:
                        collect()
            except ConfiguredProcessIncomplete:
                raise
            except (OSError, ValueError) as exc:
                log.write((format_error(exc, command=command, cwd=result["worktree_path"]) + "\n").encode("utf-8"))
                raise
        record["checker_exit_code"] = completed.returncode
        record["status"] = "checker_succeeded" if completed.returncode == 0 else "checker_needs_review"
        if completed.returncode >= 0:
            worktree = Path(result["worktree_path"])
            try:
                findings, unrecognized = checker_output.checker_findings(log_path, worktree)
                response_status = checker_response_status(log_path)
                if not findings and response_status == "checker_incomplete":
                    record.update(status="checker_needs_review", checker_review_reason="checker_incomplete",
                                  error="checker reported that no inspection ran; inspect checker.log and rerun")
                elif (not findings and response_status == "checker_succeeded"
                        and (not require_zero_for_success or completed.returncode == 0)):
                    record.update(status="checker_succeeded", checker_success_message=CHECKER_SUCCESS_MESSAGE)
                elif findings or completed.returncode > 0 or response_status == "checker_needs_review":
                    record["status"] = "checker_needs_review"
                    record["review_file_hashes"] = {
                        path: review_target_hash(worktree, Path(path))
                        for path in {finding.path for finding in findings}
                    }
                    use_raw_output = unrecognized
                    if unrecognized:
                        record["error"] = "checker output includes unrecognized text. Inspect checker.log before resuming."
            except (OSError, RuntimeError, ValueError) as exc:
                record.update(status="checker_needs_review", checker_review_reason="output_error", error=format_error(exc))
        elif completed.returncode < 0:
            record.update(checker_review_reason="checker_incomplete", error="checker did not complete normally")
    except ConfiguredProcessIncomplete as exc:
        record["unconfirmed_command"] = {"phase": "checker", "log": log_path.name}
        record.update(status="checker_failed", checker_review_reason="process_completion_unconfirmed",
                      error=format_error(exc, command=command, cwd=result["worktree_path"]) + "\n" + UNCONFIRMED_COMMAND_GUIDANCE)
    except (OSError, ValueError) as exc:
        record.update(status="checker_failed", checker_review_reason="checker_incomplete",
                      error=format_error(exc, command=command, cwd=result["worktree_path"]))
    except KeyboardInterrupt:
        record.update(status="checker_failed", checker_review_reason="checker_interrupted", error="checker interrupted by user")
        run_records.write_run_record(record_path, record)
        raise RunInterrupted(record["run_id"]) from None

    rule_results = record.get("checker_rule_evidence", {}).get("results", {})
    if any(item["kind"] == "failed" for item in rule_results.values()):
        record.update(status="checker_needs_review", checker_review_reason="rule_not_reflected",
                      error="独立チェッカーが未反映のルールを検出しました。チェッカーのログを確認してください。")
        use_raw_output = False
    if rule_results and record["status"] != "checker_succeeded":
        for name in [name for name, item in rule_results.items() if item["kind"] == "passed"]:
            del rule_results[name]
        if not rule_results:
            record.pop("checker_rule_evidence", None)
    run_records.write_run_record(record_path, record, timing_stage=5)
    return {
        **result,
        "status": record["status"],
        "error": record.get("error"),
        "checker_exit_code": record["checker_exit_code"],
        "checker_log_path": str(log_path) if log_path.is_file() else None,
    }, findings, use_raw_output


def review_free_checker_output(config, checked, record):
    if not config.get("checker_review_free_output"):
        return checked
    run_path = Path(checked["run_path"])
    record_path = run_path / "run.json"
    exit_code = record["checker_exit_code"]
    try:
        report_activity("Junie にチェッカー出力の読み取り専用判定を依頼しています。", kind="phase")
        advisory = checker_advisory(
            run_path, exit_code, allow_pass=bool(config.get("junie_apply_output_decisions")),
            model_options=advisory_model_options(config, record, run_path),
        )
        check_cancelled()
    except KeyboardInterrupt:
        record.pop("checker_advisory", None)
        record["checker_review_reason"] = "free_output_advisory_interrupted"
        run_records.write_run_record(record_path, record)
        raise RunInterrupted(record["run_id"]) from None
    record.update(checker_advisory=advisory, checker_review_reason="free_output_advisory",
                  error="Free-form checker output requires operator review; inspect checker.log and run.json.")
    run_records.write_run_record(record_path, record)
    return {**checked, "checker_advisory": advisory, "error": record["error"]}


def remaining_checker_findings(record, checked, findings, use_raw_output):
    if checked["status"] != "checker_needs_review" or (not use_raw_output and (checked["error"] or not findings)):
        return None
    accepted = accepted_findings(record, findings, record.get("review_file_hashes", {}))
    return [finding for finding in findings if finding.finding_id not in accepted]


def apply_checker_pass(run_path, checked, record):
    advisory = record.get("checker_advisory", {})
    if not advisory.get("corroborated") or record["checker_exit_code"] != 0:
        return checked
    if not matching_evidence(run_path, advisory):
        return checked
    advisory["applied"] = "continued"
    record["status"] = "checker_succeeded"
    record.pop("checker_review_reason", None)
    record.pop("error", None)
    return {**checked, "status": record["status"], "error": None}


def repairable_checker_advice(run_path, advisory, remaining, targets):
    if advisory.get("verdict") != "problem" or not remaining:
        return False
    evidence = matching_evidence(run_path, advisory)
    return all(Path(finding.path) in targets and finding.diagnostic in evidence for finding in remaining)


def repair_from_checker_instruction(config, targets, worktree, run_path, record, instruction):
    record_path = run_path / "run.json"
    status = record["status"]
    output, truncated = review_log(run_path, "checker.log")
    diagnostic = (("Only the last part of the checker output is shown.\n" if truncated else "") + output
                  or "The checker produced no output.")
    history = record.setdefault("repair_history", [])
    details = {"findings": [], "instruction": instruction, "raw_checker_output": True}
    if output:
        archive = safe_target(run_path, Path(f"checker-before-repair-{len(history) + 1}.log"))
        shutil.copyfile(run_path / "checker.log", archive)
        details["checker_log_path"] = str(archive)
    try:
        repairs.attempt_repair(config, targets, targets, worktree, run_path, diagnostic, record, details)
    except (OSError, RuntimeError, ValueError) as exc:
        record.update(status=status, checker_review_reason=checker_repair_reason(exc), error=format_error(exc))
        run_records.write_run_record(record_path, record)
        raise
    except KeyboardInterrupt:
        record.update(status=status, checker_review_reason="repair_interrupted", error="Junie による修正を停止しました。")
        run_records.write_run_record(record_path, record)
        raise RunInterrupted(record["run_id"]) from None


def checker_repair_inputs(run_path, remaining, targets, use_raw_output):
    log = safe_target(run_path, Path("checker.log"))
    if log.stat().st_size > 65_536:
        raise CheckerRepairBlocked("output_too_large", "checker output is too large for automatic repair")
    reported_paths = {Path(finding.path) for finding in remaining}
    if not reported_paths.issubset(targets):
        raise CheckerRepairBlocked("outside_targets", "checker reported a finding outside the configured migration targets")
    fingerprints = {finding.finding_id for finding in remaining}
    legacy_fingerprints = {finding.repair_fingerprint for finding in remaining}
    if use_raw_output:
        # 未知の形式を含む場合は、抽出できた指摘だけでなく元の出力全体を判断材料にする。
        with log.open("rb") as stream:
            raw = stream.read(65_537)
        if len(raw) > 65_536:
            raise CheckerRepairBlocked("output_too_large", "checker output is too large for automatic repair")
        try:
            diagnostic = decode_checker_output(raw)
        except UnicodeDecodeError:
            raise CheckerRepairBlocked("output_encoding", "unrecognized checker output encoding; inspect checker.log") from None
        if not diagnostic.strip() or "\0" in diagnostic:
            raise CheckerRepairBlocked("output_unreadable", "unrecognized checker output has no readable diagnostic; inspect checker.log")
        # 混在形式は全文で再発を判定し、減った指摘の一部が残るだけでは止めない。
        fingerprints = {hashlib.sha256(" ".join(diagnostic.split()).encode("utf-8")).hexdigest()}
        legacy_fingerprints = set()
        repair_targets = targets
    else:
        if sum(len(finding.diagnostic) + 1 for finding in remaining) > 8_000:
            raise CheckerRepairBlocked("output_too_large", "checker findings contain too much text for automatic repair")
        diagnostic = "\n".join(finding.diagnostic for finding in remaining)
        repair_targets = tuple(path for path in targets if path in reported_paths)
    return log, repair_targets, diagnostic, fingerprints, legacy_fingerprints


def grounded_repair_inputs(config, run_path, worktree, targets, hashes, advisory, evidence, record):
    if not config.get("junie_ground_output_repairs"):
        return (), "", set()
    grounding = grounded_evidence.grounded_diagnostic(
        run_path, worktree, targets, expected_target_hashes=hashes,
        evidence_lines=evidence, phase=advisory["phase"], through=record.get("completed_capsules") or None,
        model_options=advisory_model_options(config, record, run_path),
    )
    advisory["grounding"] = grounding
    run_records.write_run_record(run_path / "run.json", record)
    check_cancelled()
    if grounding.get("status") != "grounded" or any(review_target_hash(worktree, Path(name)) != digest
                                                  for name, digest in hashes.items()):
        return (), "", set()
    if sorted(matching_evidence(run_path, advisory)) != evidence:
        return (), "", set()
    citations = grounding["citations"]
    if not citations:
        return (), "", set()
    reported = {Path(citation["target"]) for citation in citations}
    if not reported.issubset(targets):
        return (), "", set()
    diagnostic = json.dumps({"verified_citations": citations, "untrusted_diagnostic_lines": evidence}, ensure_ascii=False)
    if len(diagnostic.encode("utf-8")) > 16_384:
        return (), "", set()
    fingerprints = {hashlib.sha256(json.dumps(
        [advisory["phase"], citation["target"], citation["source_id"], " ".join(" ".join(evidence).split())],
        ensure_ascii=False,
    ).encode("utf-8")).hexdigest() for citation in citations}
    return tuple(path for path in targets if path in reported), diagnostic, fingerprints


def review_checker(
    config: dict[str, Any], run_id: str, finding_id: str | None, reason: str | None,
) -> dict[str, Any]:
    """指摘を一覧し、指定された承認は現在のファイル内容を照合して記録する。"""
    if (finding_id is None) != (reason is None) or (reason is not None and not reason.strip()):
        raise ValueError("--accept requires a finding ID and a non-empty --reason")
    if finding_id is None:
        return _review_checker(config, run_id, None, None)
    validate_run_id(run_id)
    with execution_lock(config, run_id):
        return _review_checker(config, run_id, finding_id, reason)


def _review_checker(
    config: dict[str, Any], run_id: str, finding_id: str | None, reason: str | None,
) -> dict[str, Any]:
    run_path, worktree, record, unit = run_records.load_saved_run(config, run_id)
    allowed_status = {"checker_needs_review"} if finding_id is not None else {"checker_needs_review", "checker_accepted"}
    if (record.get("status") not in allowed_status or record.get("checker_exit_code") is None
            or record["checker_exit_code"] < 0):
        raise ValueError("run has no checker findings to review")
    log_path = run_path / "checker.log"
    if log_path.is_symlink() or not log_path.is_file():
        raise ValueError("checker log must be a regular file inside the run directory")
    findings, unrecognized = checker_output.checker_findings(log_path, worktree)
    # 旧版のRunは最後に反映した移行先のハッシュで照合する。
    recorded_hashes = record.get("review_file_hashes", {
        Path(unit["target"]).as_posix(): record.get("review_target_sha256", record.get("target_sha256")),
    })
    hashes = {
        path: recorded_hashes.get(path)
        for path in {finding.path for finding in findings}
        if review_target_hash(worktree, Path(path)) == recorded_hashes.get(path)
    }
    accepted = accepted_findings(record, findings, hashes)
    if finding_id is not None:
        matches = [finding for finding in findings if finding_id in (finding.finding_id, finding.finding_id[:12])]
        if len(matches) != 1:
            raise ValueError("finding ID is missing or ambiguous; list the current findings with review")
        finding = matches[0]
        target_hash = hashes.get(finding.path)
        if not target_hash:
            raise ValueError("the reviewed file changed or was missing; resume to get current findings")
        review = {
            "finding_id": finding.finding_id,
            "diagnostic": finding.diagnostic,
            "target_sha256": target_hash,
            "reason": reason.strip(),
            "accepted_at": datetime.now(timezone.utc).isoformat(),
        }
        with edit_run_record(run_path / "run.json") as (latest, changes):
            changes["checker_reviews"] = [*latest.get("checker_reviews", []), review]
        accepted.add(finding.finding_id)
    return {
        "findings": [
            (finding.finding_id[:12], finding.diagnostic, finding.finding_id in accepted)
            for finding in findings
        ],
        "locations": [{"path": finding.path, "line": finding.line} for finding in findings],
        "unrecognized": unrecognized,
        "log_path": str(log_path),
    }


def resume_checker(config, unit, result, record, plan, request_id):
    """対象版を照合してチェッカーのみを実行し、検証記録を保存する。"""
    run_path, worktree = Path(result["run_path"]), Path(result["worktree_path"])
    record_path = run_path / "run.json"
    command = plan["checker_command"]
    with editor_queue_store.editor_queue(run_path) as queue, worktree_source_lock(worktree):
        if (queue.get("edit_revision") != plan["edit_revision"] or queue["edits"]
                or any(prompt["status"] != "sent" for prompt in queue["prompts"])):
            raise ValueError("計画後に編集または指示が更新されました。")
        if build_receipt.check_build_receipt(config, worktree, record) != plan["build_receipt"]:
            raise ValueError("確認後にビルドの保存版が更新されました。チェッカーの再開計画を確認し直してください。")
        record.setdefault("resume_operations", []).append({
            **plan, "id": request_id, "accepted_at": datetime.now(timezone.utc).isoformat(),
        })
        verification = record["verification"]
        verification.update(configured_checker_command=command, checker_command=command, checker_only=True,
                            scripts={}, scripts_passed=False,
                            input_files=verification_files(config, worktree, unit["target"], command, {}))
        verification.pop("toml_checker_command", None)
        verification.pop("toml_checker_scope", None)
        verification.update(checker_provenance(config))
        record.update(status="checking", checker_exit_code=None)
        run_records.write_run_record(record_path, record)
    checked, findings, use_raw_output = run_checker(
        command, {**result, "request_id": request_id,
                  **{key: record.get(key) for key in ("junie_exit_code", "agent_workspace_path", "completed_capsules",
                                                     "build_exit_code", "build_log_path", "verification")}},
        record, config["build_project"], require_zero_for_success=bool(config.get("checker_review_free_output")),
        rule_evidence=bool(config.get("checker_rule_evidence")),
    )
    if record.get("unconfirmed_command"):
        return checked
    try:
        build_receipt.check_build_receipt(config, worktree, record)
    except ValueError as exc:
        record.update(status="checker_failed", checker_exit_code=None, error=str(exc))
        run_records.write_run_record(record_path, record)
        return {**checked, "status": record["status"], "checker_exit_code": None, "error": str(exc)}
    verification["inputs_changed_during_verification"] = (
        verification["input_files"] != verification_files(config, worktree, unit["target"], command, {})
    )
    verification["scripts_passed"] = True
    if (not use_raw_output and not verification["inputs_changed_during_verification"]
            and remaining_checker_findings(record, checked, findings, use_raw_output) == []):
        record["status"] = "checker_accepted"
        checked = {**checked, "status": record["status"]}
    run_records.write_run_record(record_path, record)
    if use_raw_output:
        checked = review_free_checker_output(config, checked, record)
        if config.get("checker_review_free_output") and config.get("junie_apply_output_decisions"):
            try:
                build_receipt.check_build_receipt(config, worktree, record)
                checked = apply_checker_pass(run_path, checked, record)
            except (OSError, ValueError) as exc:
                record.update(status="checker_failed", checker_exit_code=None, error=str(exc))
                checked = {**checked, "status": record["status"], "checker_exit_code": None, "error": str(exc)}
            run_records.write_run_record(record_path, record)
    return checked
