from __future__ import annotations

import codecs
import hashlib
import json
import re
import shutil
import subprocess
from collections import Counter
from pathlib import Path
from time import monotonic
from typing import Any

from . import build_receipt, repairs
from .. import config as harness_config, execution
from ..command_paths import build_command_for_worktree
from ..errors import RunInterrupted, format_error
from ..execution import (
    build_process_scope, check_cancelled, current_operation_id, print_progress, report_activity,
)
from ..files import safe_target
from ..junie import junie_cli
from ..runs import run_records
from ..runs.worktree_targets import (
    EDIT_BYTES, finish_pending_targets, read_target, review_target_hash, save_target_edit,
    target_newline, worktree_source_lock,
)
from .diagnostic_log import remember_diagnostic, review_log
from .repairs import repair_stalled
from .roslyn_evidence import roslyn_diagnostic_lines
from .verification_files import checker_provenance


def _build_worktree(
    config: dict[str, Any], result: dict[str, Any], record: dict[str, Any], *, continue_timing: bool = False,
) -> dict[str, Any]:
    worktree = Path(result["worktree_path"])
    run_path = Path(result["run_path"])
    record_path = run_path / "run.json"
    log_path = run_path / "build.log"
    build_project = config["build_project"]
    command = ["dotnet", "build"]

    remember_diagnostic(run_path, record, "build")
    remember_diagnostic(run_path, record, "checker")
    record.update(status="building", build_exit_code=None, build_log_path=str(log_path))
    for key in ("error", "checker_exit_code", "checker_log_path", "review_file_hashes", "checker_review_reason",
                "checker_success_message", "build_operation_id"):
        record.pop(key, None)
    run_records.write_run_record(record_path, record)
    print_progress(f"Building in {worktree} (log: {log_path})")
    report_activity(
        "再ビルドを実行しています" if record.get("build_attempt_seconds") else "ビルドを実行しています",
        kind="phase", detail="コンパイラで構文・型を確認しています。ビルドが成功してからチェッカーへ進みます。",
    )
    try:
        # MSBuildの出力は実行データ側に残し、アプリの差分に混ぜない。
        with log_path.open("wb") as log:
            try:
                command = build_command_for_worktree(worktree, build_project)
                started = monotonic()
                try:
                    with build_process_scope():
                        completed = execution.run_process(command, label="ビルド", cwd=worktree, stdout=log, stderr=subprocess.STDOUT, check=False)
                finally:
                    record.setdefault("build_attempt_seconds", []).append(monotonic() - started)
            except (OSError, ValueError) as exc:
                log.write((format_error(exc, command=command, cwd=worktree) + "\n").encode("utf-8"))
                raise
        record["build_exit_code"] = completed.returncode
        if operation_id := current_operation_id():
            record["build_operation_id"] = operation_id
        if completed.returncode:
            record["error"] = f"dotnet build exited with code {completed.returncode}"
    except (OSError, ValueError) as exc:
        record["error"] = format_error(exc, command=command, cwd=worktree)
    except KeyboardInterrupt:
        record.update(status="build_failed", error="build interrupted by user")
        run_records.write_run_record(record_path, record)
        raise RunInterrupted(record["run_id"]) from None

    record["status"] = "build_failed" if "error" in record else "build_succeeded"
    run_records.write_run_record(record_path, record, timing_stage=4 if continue_timing else None)
    return {
        **result,
        "junie_exit_code": record.get("junie_exit_code"),
        "agent_workspace_path": record.get("agent_workspace_path"),
        "completed_capsules": record["completed_capsules"],
        "status": record["status"],
        "error": record.get("error"),
        "build_exit_code": record["build_exit_code"],
        "build_log_path": str(log_path) if log_path.is_file() else None,
        "verification": record.get("verification"),
    }


_BUILD_DIAGNOSTIC = re.compile(
    r"(?P<path>.+?)\((?P<location>\d+(?:,\d+){0,3})\)\s*:\s*"
    r"(?:error|エラー)\s+(?P<code>(?:CS|RZ)\d+)\s*:\s*(?P<message>.+)",
    re.IGNORECASE,
)


_BUILD_ERROR = re.compile(r"(?:^|:\s*)(?:error|エラー)\s+[A-Z]+\d+\s*:", re.IGNORECASE)


_RAZOR_GENERATOR = "Microsoft.NET.Sdk.Razor.SourceGenerators.RazorSourceGenerator"


def _razor_generated_path(page: Path) -> Path:
    # Razorの命名規則に合わせ、区切りを保ち、BMP外の文字はUTF-16の2単位分に置換する。
    name = "".join(
        char if char == "/" or (ord(char) <= 0xFFFF and (char.isalpha() or char.isdecimal()))
        else "__" if ord(char) > 0xFFFF else "_"
        for char in page.as_posix()
    )
    return Path(f"{name}.g.cs")


def build_failure_review(config: dict[str, Any], run_id: str, target: str | None = None) -> dict[str, Any]:
    run_path, worktree, record, unit = run_records.load_saved_run(config, run_id)
    if record["status"] != "build_failed":
        raise ValueError("表示中の Run のビルド結果が変わりました。")
    targets = harness_config.target_paths(unit)
    if target is not None:
        if target not in {path.as_posix() for path in targets}:
            raise ValueError("設定された移行対象ファイルを選んでください。")
        with worktree_source_lock(worktree):
            data, content = read_target(worktree, Path(target))
        if data is None:
            raise ValueError("対象ファイルが見つかりません。作業ツリーで確認してください。")
        return {
            "content": content, "version": hashlib.sha256(data).hexdigest(), "max_bytes": EDIT_BYTES,
            "crlf": target_newline(data) == "\r\n", "bom_bytes": len(codecs.BOM_UTF8),
        }
    preflight = record.get("roslyn_preflight", {})
    if preflight.get("status") in {"blocked", "cancelled"} and record.get("build_exit_code") is None:
        attempts = [item for item in record.get("repair_history", []) if item.get("roslyn_preflight")]
        diffs = "\n".join(item.get("diff", "") for item in attempts if item.get("diff"))
        names = [path.as_posix() for path in targets]
        preferred = next((item["path"] for item in preflight.get("findings", []) if item["path"] in names), None)
        return {"review_kind": "roslyn_preflight", "errors": roslyn_diagnostic_lines(preflight),
                "log": diffs, "truncated": any(item.get("diff_truncated") for item in attempts),
                "preferred_target": preferred, "targets": names}
    log, truncated = review_log(run_path, "build.log")
    errors = []
    preferred_target = None
    for error in _build_error_lines(log):
        match = _BUILD_DIAGNOSTIC.fullmatch(error)
        if match:
            path = (worktree / match["path"]).resolve()
            if path.is_relative_to(worktree):
                relative = path.relative_to(worktree)
                if preferred_target is None and relative in targets:
                    preferred_target = relative.as_posix()
                message = re.sub(r" \[[^\r\n]+\.(?:csproj|vbproj|fsproj)\]$", "", match["message"])
                error = f"{relative.as_posix()}({match['location']}): {match['code']}: {message}"
        errors.append(error)
    return {
        "errors": list(dict.fromkeys(errors))[:30], "log": log, "truncated": truncated, "preferred_target": preferred_target,
        "targets": [target.as_posix() for target in targets],
    }


def _build_error_lines(log: str) -> list[str]:
    lines = (line.strip() for line in log.splitlines())
    return list(dict.fromkeys(line for line in lines if _BUILD_ERROR.search(line)))


def respond_to_build_failure(
    config: dict[str, Any], targets: tuple[Path, ...], worktree: Path, run_path: Path,
    record: dict[str, Any], response: dict[str, Any],
) -> None:
    record_path = run_path / "run.json"
    finish_pending_targets(run_path / "agent-workspace", worktree, targets, record, record_path)
    check_cancelled()
    if response.get("edit") is not None:
        save_target_edit(worktree, targets, response["edit"])
    instruction = response.get("instruction", "").strip()
    if not instruction:
        return
    preflight = record.get("roslyn_preflight", {})
    is_preflight = preflight.get("status") in {"blocked", "cancelled"} and record.get("build_exit_code") is None
    log = "" if is_preflight else review_log(run_path, "build.log")[0]
    errors = roslyn_diagnostic_lines(preflight) if is_preflight else _build_error_lines(log)
    diagnostic = ("\n".join(errors) or log)[-8_000:]
    findings = f"{record.get('error', '')}\n{diagnostic}".strip() or "The last build failed."
    history = record.setdefault("repair_history", [])
    details = {"kind": "build", "findings": [], "instruction": instruction}
    if is_preflight:
        details["roslyn_preflight"] = True
    if log:
        archive = safe_target(run_path, Path(f"build-before-repair-{len(history) + 1}.log"))
        shutil.copyfile(run_path / "build.log", archive)
        details["build_log_path"] = str(archive)
    try:
        repairs.attempt_repair(config, targets, targets, worktree, run_path, findings, record, details)
    except (OSError, RuntimeError, ValueError) as exc:
        record.update(status="build_failed", error="Junie で修正できませんでした。\n" + format_error(exc))
        run_records.write_run_record(record_path, record)
        raise
    except KeyboardInterrupt:
        record.update(status="build_failed", error="Junie による修正を停止しました。")
        run_records.write_run_record(record_path, record)
        raise RunInterrupted(record["run_id"]) from None


def repair_build_failure(
    config: dict[str, Any], targets: tuple[Path, ...], worktree: Path, run_path: Path, record: dict[str, Any],
    *, operator_instruction: str | None = None,
) -> bool:
    """修正先を特定できるコンパイラエラーだけを、Runの残り回数内で修正する。"""
    limit = config["max_repair_attempts"]
    history = record["repair_history"]
    log = run_path / "build.log"
    report_activity("ビルドエラーを確認しています", kind="phase",
                    detail="エラーの内容・修正対象・残りの自動修正回数を調べています。チェッカーはまだ実行しません。")
    # 上限はビルド・チェッカー共通の自動修正回数。利用者が明示した修正指示は数えない。
    automatic_attempts = sum("instruction" not in attempt for attempt in history)
    if automatic_attempts >= limit or not record.get("build_exit_code"):
        return False
    findings = {}
    occurrences = Counter()
    generated_model_errors = set()
    source_model_errors = set()
    # 全文ログは残し、全行を検査する。同じ診断の繰り返しだけを既存のキーでまとめる。
    with log.open(encoding="utf-8-sig", errors="replace") as stream:
        for line in iter(lambda: stream.readline(65_537), ""):
            check_cancelled()
            if len(line) > 65_536:
                return False
            line = line.strip()
            match = _BUILD_DIAGNOSTIC.fullmatch(line)
            if match is None:
                if _BUILD_ERROR.search(line):
                    return False
                continue
            resolved = (worktree / match["path"]).resolve()
            if not resolved.is_relative_to(worktree):
                return False
            path = resolved.relative_to(worktree)
            code = match["code"].upper()
            message = match["message"].rsplit(" [", 1)[0]
            project_suffix = match["message"][len(message):]
            if path not in targets:
                if code != "CS0246" or _RAZOR_GENERATOR not in path.parts:
                    return False
                generator_index = path.parts.index(_RAZOR_GENERATOR)
                if "obj" not in path.parts[:generator_index] or not path.name.lower().endswith("_cshtml.g.cs"):
                    return False
                generated_model_errors.add((Path(*path.parts[generator_index + 1:]), message, project_suffix))
                if sum(len(str(error)) for error in generated_model_errors) > 8_000:
                    return False
                continue
            if code == "CS0246" and path.suffix.lower() == ".cshtml" and project_suffix.lower().endswith(".csproj]"):
                project_dir = (worktree / project_suffix[2:-1]).resolve().parent
                if project_dir.is_relative_to(worktree) and resolved.is_relative_to(project_dir):
                    page = resolved.relative_to(project_dir)
                    generated = _razor_generated_path(page)
                    # 生成名は衝突し得るため、診断が出ていない他ページの名前も照合する。
                    if not any(
                        other != path and other.suffix.lower() == ".cshtml"
                        and (worktree / other).is_relative_to(project_dir)
                        and _razor_generated_path((worktree / other).relative_to(project_dir)) == generated
                        for other in targets
                    ):
                        source_model_errors.add((generated, message, project_suffix))
            key = path.as_posix(), code, message
            findings.setdefault(key, f"{path.as_posix()}({match['location']}): {code}: {message}")
            occurrences[key] += 1
            if sum(len(value) + 1 for value in findings.values()) > 8_000:
                return False
    # 編集可能なRazor原本にも同じ診断がある場合だけ、生成コード側の重複を除外する。
    if not generated_model_errors.issubset(source_model_errors):
        return False
    diagnostics = "\n".join(
        value + (f" ({occurrences[key]} log entries; inspect all occurrences in this target.)"
                 if occurrences[key] > 1 else "")
        for key, value in findings.items()
    )
    if not diagnostics or len(diagnostics) > 8000:
        return False
    fingerprints = {
        hashlib.sha256(json.dumps(key).encode("utf-8")).hexdigest() for key in findings
    }
    if repair_stalled(fingerprints, history, kind="build"):
        return False
    junie_cli.require_interactive_cli()
    saved_log = run_path / f"build-before-repair-{len(history) + 1}.log"
    shutil.copyfile(log, saved_log)
    affected = {key[0] for key in findings}
    repair_targets = tuple(path for path in targets if path.as_posix() in affected)
    repairs.attempt_repair(
        config, targets, repair_targets, worktree, run_path, diagnostics, record,
        {"kind": "build", "findings": sorted(fingerprints), "build_log_path": str(saved_log),
         **({"operator_instruction": operator_instruction} if operator_instruction else {})},
    )
    return True


def execute_verification_build(config, unit, result, record, run_path, worktree, targets, *,
                                expected_targets, previous_receipt, configured_checker, command, scripts, input_files, request):
    """ビルド前の対象版・設定を記録し、ビルドと実行中の対象版を照合する。"""
    hashes = {path.as_posix(): review_target_hash(worktree, path) for path in targets}
    if expected_targets is not None and hashes != expected_targets:
        raise ValueError("ビルドの開始前に移行先が更新されました。エディタで確認して指示を送信し直してください。")
    record["verification"] = {
        "target_hashes": hashes,
        "build_project": config["build_project"],
        "configured_checker_command": configured_checker,
        "checker_command": command,
        **checker_provenance(config),
        "scripts": scripts,
        "scripts_passed": False,
        "input_files": input_files,
    }
    inputs = None if scripts or config.get("scripts", {}).get("after_migration") else build_receipt.build_inputs(
        worktree, config["build_project"],
    )
    if inputs and any((worktree / Path(inputs["project"]).parent / name).exists() for name in ("bin", "obj")):
        if (not previous_receipt or previous_receipt["inputs"] != inputs
                or previous_receipt["artifacts"] != build_receipt.build_artifacts(worktree, inputs)):
            # 既存の生成物が古い場合、MSBuild の増分判定だけから対応する版を証明できない。
            inputs = None
    verified = _build_worktree(config, result, record, continue_timing=True)
    if request and any(review_target_hash(worktree, path) != hashes[path.as_posix()] for path in targets):
        raise ValueError("ビルド中に移行先が更新されました。エディタで確認して指示を送信し直してください。")
    return verified, hashes, inputs
