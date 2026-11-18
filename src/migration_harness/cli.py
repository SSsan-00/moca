from __future__ import annotations

import argparse
import json
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from migration_harness.config import find_unit, load_config, target_paths, validate_checker_command
from migration_harness.errors import format_error
from migration_harness.workflow import (
    VERIFIED_STATUSES, RunScriptError, migration_ledger, plan_stage_resume,
    plan_stopped_repair, prepare_run, resolve_stopped_repair, resume_with_junie, review_checker, review_migration_ranges,
    run_with_junie,
)


def _parse_args() -> argparse.Namespace:
    """CLIの起動方法と、後続をチェッカーへ渡す引数の境界を定義する。"""
    parser = argparse.ArgumentParser(prog="php-to-csharp-harness")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def configured_command(name: str, help_text: str) -> argparse.ArgumentParser:
        """設定ファイルを必須とするサブコマンドを定義する。"""
        command = subparsers.add_parser(name, help=help_text)
        command.add_argument("--config", type=Path, required=True, help="Path to a local TOML config")
        return command

    configured_command("dashboard", "Open the local workspace for configuration and migration runs")
    preview = configured_command("preview-context", "List target reference sizes in the current application checkout")
    preview.add_argument("--unit", required=True, help="Migration unit ID from the config")
    prepare = configured_command("prepare", "Create a worktree and bounded context capsules")
    prepare.add_argument("--unit", required=True, help="Migration unit ID from the config")
    run = configured_command("run", "Migrate, build and run the configured checker")
    run.add_argument("--unit", required=True, help="Migration unit ID from the config")
    resume = configured_command("resume", "Continue migration, build or checker in the saved worktree")
    resume.add_argument("--run-id", required=True, help="Run ID printed by prepare or run")
    resume.add_argument("--from", dest="start_stage", choices=("build", "migration", "checker"), help="Start at an explicit stage")
    resume.add_argument("--through-capsule", type=int, help="With --from migration, stop after this capsule before verification")
    resume.add_argument("--rewind-to", type=int, help="With --from migration, replay a completed capsule from its saved version")
    resume.add_argument("--plan", action="store_true", help="Print the requested stage plan without running it")
    resume.add_argument("--version", help="Require the plan printed by --plan to still match")
    draft = configured_command("repair-draft", "Inspect or select saved input for a stopped repair")
    draft.add_argument("--run-id", required=True)
    draft.add_argument("--version", help="Version from the plan printed without this option")
    draft.add_argument("--choose", action="append", metavar="PATH=SOURCE", help="Select draft or worktree for each path")
    junie_input = configured_command("junie-input", "Show the last measured Junie launch input for a saved run")
    junie_input.add_argument("--run-id", required=True)
    rule_review = configured_command("rule-review", "Inspect saved migration rules and record independent review evidence")
    rule_review.add_argument("--run-id", required=True)
    rule_review.add_argument("--rule")
    rule_review.add_argument("--record", choices=("include", "exclude", "read", "applied", "passed", "failed"))
    rule_review.add_argument("--version")
    rule_review.add_argument("--start-line", type=int)
    rule_review.add_argument("--end-line", type=int)
    rule_review.add_argument("--reason")
    rule_review.add_argument("--reference", help="Identifier or path of the independent test or checker report")
    view = configured_command("view", "Open the dashboard for a saved run")
    view.add_argument("--run-id", required=True, help="Run ID printed by prepare or run")
    review = configured_command("review", "List checker findings or accept one with a reason")
    review.add_argument("--run-id", required=True, help="Run ID awaiting review")
    review.add_argument("--accept", metavar="FINDING_ID", help="Finding ID shown by review")
    review.add_argument("--reason", help="Why the finding can be accepted")
    ledger_review = configured_command("ledger-review", "Record a decision for PHP ranges shown in the migration ledger")
    ledger_review.add_argument("--run-id", required=True, help="Run ID printed by prepare or run")
    ledger_review.add_argument("--range", dest="ranges", action="append", required=True, help="Range ID from the ledger; repeat to record multiple")
    ledger_review.add_argument("--state", choices=("confirmed", "excluded", "needs_review"), required=True)
    ledger_review.add_argument("--reason", required=True)
    ledger_review.add_argument("--target", dest="review_targets", action="append", help="Reviewed target file; repeat for multiple files")
    anchors = configured_command("anchors", "Generate Diff Viewer anchors with Junie")
    anchors.add_argument("--run-id", required=True, help="Run ID with PHP and migration target files")
    anchors.add_argument("--reference", type=Path, action="append", help="Diff Viewer workspace JSON with manual anchors; repeat for multiple files")
    run.add_argument("--dashboard", action="store_true", help="Serve live progress and review on localhost")
    resume.add_argument("--dashboard", action="store_true", help="Open the saved run and resume from the dashboard")
    for command in (run, resume, view):
        command.add_argument(
            "--checker-command", nargs=argparse.REMAINDER, metavar="EXECUTABLE",
            help="Checker executable and its arguments. Put this option last.",
        )
    return parser.parse_args()


def main() -> None:
    """利用者の操作を振り分け、途中停止や入力エラーを終了コードで伝える。"""
    args = _parse_args()
    try:
        try:
            config = load_config(args.config)
        except (OSError, ValueError, RuntimeError, KeyError, TypeError, AttributeError) as exc:
            if args.command not in {"dashboard", "view"} and not getattr(args, "dashboard", False):
                raise
            from migration_harness.dashboard import run_dashboard

            # 初回のdashboardは設定例から作り始める。既存設定の読み込み失敗は表示する。
            first_setup = args.command == "dashboard" and isinstance(exc, FileNotFoundError) and not args.config.exists()
            run_dashboard(
                {"config_path": str(args.config.expanduser().resolve())},
                config_error=None if first_setup else format_error(exc),
            )
            return
        if args.command == "dashboard":
            from migration_harness.dashboard import run_dashboard

            run_dashboard(config)
            return
        checker_command = getattr(args, "checker_command", None)
        if checker_command is not None:
            validate_checker_command(checker_command)
            config["toml_checker_command"] = config["checker_command"]
            config["checker_command"] = checker_command
            config["checker_from_cli"] = True
        if args.command == "preview-context":
            from migration_harness.execution import ExecutionControl, control_scope
            from migration_harness.source_context import preview_target_context

            unit = find_unit(config, args.unit)
            progress = ExecutionControl(on_activity=lambda event: print(event["message"], file=sys.stderr, flush=True))
            with control_scope(progress):
                preview = preview_target_context(Path(config["project_root"]), target_paths(unit), unit.get("target_context"))
            print(json.dumps(preview, ensure_ascii=False, indent=2))
            return
        if args.command == "junie-input":
            from migration_harness.run_records import load_dashboard_record

            _, record = load_dashboard_record(config, args.run_id)
            print(json.dumps({
                "run_id": args.run_id,
                "last_junie_input": record.get("last_junie_input"),
                "last_junie_steering_input": record.get("last_junie_steering_input"),
            }, ensure_ascii=False, indent=2))
            return
        if args.command == "rule-review":
            from migration_harness.rule_review import review_rules

            report = review_rules(config, args.run_id, rule=args.rule, kind=args.record, version=args.version,
                                  start=args.start_line, end=args.end_line, reason=args.reason, reference=args.reference)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return
        if args.command == "repair-draft":
            if args.version is None and not args.choose:
                print(json.dumps(plan_stopped_repair(config, args.run_id), ensure_ascii=False, indent=2))
            else:
                choices = dict(choice.rsplit("=", 1) for choice in args.choose or ())
                if not args.version or len(choices) != len(args.choose or ()):
                    raise ValueError("Supply the plan version and exactly one choice per target.")
                resolve_stopped_repair(config, args.run_id, args.version, choices)
                print("復元する内容を記録しました。通常の再開で反映します。")
            return
        if args.command == "resume":
            if (args.through_capsule is not None or args.rewind_to is not None) and args.start_stage != "migration":
                raise ValueError("--through-capsule and --rewind-to require --from migration.")
            if (args.plan or args.version) and not args.start_stage:
                raise ValueError("--plan and --version require --from.")
        if args.command == "ledger-review":
            review_migration_ranges(
                config, args.run_id, args.ranges, args.state, args.reason, confirmed_targets=args.review_targets,
            )
            print(f"Migration ledger updated for {args.run_id}")
            _print_migration_progress(migration_ledger(config, args.run_id)["completion"])
            return
        if args.command == "anchors":
            from migration_harness.review_anchors import generate_anchors

            output, count = generate_anchors(config, args.run_id, args.reference)
            print(f"Diff Viewer workspace ({count} anchors): {output}")
            return
        if args.command == "review":
            report = review_checker(config, args.run_id, args.accept, args.reason)
            for finding_id, diagnostic, accepted in report["findings"]:
                print(f"{finding_id} {'[accepted]' if accepted else '[pending]'} {diagnostic}")
            print(f"Checker log: {report['log_path']}")
            if report["unrecognized"]:
                print("The log also has unrecognized lines. Review them before continuing.")
            return
        if args.command == "prepare":
            result = prepare_run(config, args.unit)
        elif args.command == "view" or args.dashboard:
            from migration_harness.dashboard import run_dashboard

            if args.command != "run":
                if getattr(args, "start_stage", None):
                    raise ValueError("Use --from without --dashboard.")
                run_dashboard(config, run_id=args.run_id)
                return
            result = run_dashboard(config, unit_id=args.unit)
            if result is None or "capsule_count" not in result:
                return
        elif args.command == "run":
            result = run_with_junie(config, args.unit)
        else:
            if args.start_stage:
                request = plan_stage_resume(config, args.run_id, args.start_stage,
                                            through_capsule=args.through_capsule, rewind_to=args.rewind_to)
                if args.plan:
                    print(json.dumps(request, ensure_ascii=False, indent=2))
                    return
                if args.version and args.version != request["version"]:
                    raise ValueError("計画の版が更新されました。--plan で確認し直してください。")
                if args.rewind_to is not None and not args.version:
                    raise ValueError("巻き戻しは --plan で確認し、--version を指定してください。")
                print(f"保存版 {request['version'][:12]} から {request['start_stage']} を実行します。", flush=True)
                result = resume_with_junie(config, args.run_id, verification_request={**request, "id": uuid.uuid4().hex})
            else:
                result = resume_with_junie(config, args.run_id)
        completion = migration_ledger(config, result["run_id"])["completion"] if result.get("status") in VERIFIED_STATUSES else None
    except RunScriptError as exc:
        print(f"error: {format_error(exc)}", file=sys.stderr)
        if exc.run_id is not None:
            print(f"Fix the script and resume Run {exc.run_id}.", file=sys.stderr)
        raise SystemExit(1) from exc
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, AttributeError, subprocess.CalledProcessError) as exc:
        print(f"error: {format_error(exc)}", file=sys.stderr)
        raise SystemExit(2) from exc
    except KeyboardInterrupt as exc:
        run_id = getattr(exc, "run_id", None)
        if run_id:
            print(f"Interrupted. Saved run {run_id}; resume with: h.cmd s", file=sys.stderr)
        else:
            print("Interrupted.", file=sys.stderr)
        raise SystemExit(130)

    _print_run_result(result, prepared=args.command == "prepare")
    if completion is not None:
        _print_migration_progress(completion)


def _print_migration_progress(completion: dict[str, Any]) -> None:
    """コマンドの成功とは別に、現在の移行完了判定を伝える。"""
    status = "complete" if completion["complete"] else "incomplete"
    print(f"Migration: {status} ({completion['reviewed']}/{completion['total']} PHP ranges reviewed)")
    if not completion["verification_passed"]:
        print("Run verification again to check the current build, script and checker inputs.")


def _print_run_result(result: dict[str, Any], *, prepared: bool) -> None:
    """Run ID、成果物の場所と、次の操作に必要な結果を伝える。"""
    print(f"Run:       {result['run_id']}")
    print(f"Worktree:  {result['worktree_path']}")
    print(f"Capsules:  {result['capsule_count']} (max {result['max_context_chars']} characters each)")
    print(f"Context:   {result['capsule_path']} ({result['context_chars']} characters total)")
    if result["source_boundary_review_required"]:
        print("Review:    Check capsule boundaries and character ranges in manifest.json")
    if prepared:
        print("Status:    prepared")
    else:
        print(f"Junie exit code: {result['junie_exit_code']}")
        print(f"Completed capsules: {result['completed_capsules']}/{result['capsule_count']}")
        print(f"Agent workspace: {result['agent_workspace_path']}")
        if "build_exit_code" in result:
            build_status = "succeeded" if result["build_exit_code"] == 0 else "failed"
            print(f"Build:     {build_status} (exit code: {result['build_exit_code']})")
            if result["build_log_path"]:
                print(f"Build log: {result['build_log_path']}")
            if result["status"] == "build_failed":
                print(f"Build failed: {result['error']}")
                print("Fix the cause and resume with the Run ID shown above to retry the build.")
                raise SystemExit(1)
        else:
            print(f"Paused at capsule {result['current_capsule']}: {result['error']}")
            print("Resume with the Run ID shown above.")
            if result.get("requested_range_complete"):
                return
            raise SystemExit(1)
        if "checker_exit_code" in result:
            print(f"Checker:   {result['status']} (exit code: {result['checker_exit_code']})")
            if result["checker_log_path"]:
                print(f"Checker log: {result['checker_log_path']}")
            if result["status"] not in {"checker_succeeded", "checker_accepted"}:
                if result["error"]:
                    print(f"Review reason: {result['error']}")
                print("Use review with the same Run ID to inspect checker findings. Use resume to rerun verification.")
                raise SystemExit(1)
        print("Review the worktree diff.")


if __name__ == "__main__":
    main()
