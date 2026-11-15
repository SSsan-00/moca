from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Any

from migration_harness.config import load_config, validate_checker_command
from migration_harness.workflow import (
    VERIFIED_STATUSES, RunScriptError, migration_ledger, prepare_run, resume_with_junie, review_checker,
    review_migration_ranges, run_with_junie,
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
    prepare = configured_command("prepare", "Create a worktree and bounded context capsules")
    prepare.add_argument("--unit", required=True, help="Migration unit ID from the config")
    run = configured_command("run", "Migrate, build and run the configured checker")
    run.add_argument("--unit", required=True, help="Migration unit ID from the config")
    resume = configured_command("resume", "Continue migration, build or checker in the saved worktree")
    resume.add_argument("--run-id", required=True, help="Run ID printed by prepare or run")
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
    anchors.add_argument("--reference", type=Path, help="Optional Diff Viewer workspace JSON with manual anchors")
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
        if args.command == "dashboard":
            from migration_harness.dashboard import run_dashboard

            try:
                config = load_config(args.config)
            except (OSError, ValueError):
                config = {"config_path": str(args.config.expanduser().resolve())}
            run_dashboard(config)
            return
        config = load_config(args.config)
        checker_command = getattr(args, "checker_command", None)
        if checker_command is not None:
            validate_checker_command(checker_command)
            config["checker_command"] = checker_command
            config["checker_from_cli"] = True
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
                run_dashboard(config, run_id=args.run_id)
                return
            result = run_dashboard(config, unit_id=args.unit)
            if result is None or "capsule_count" not in result:
                return
        elif args.command == "run":
            result = run_with_junie(config, args.unit)
        else:
            result = resume_with_junie(config, args.run_id)
        completion = migration_ledger(config, result["run_id"])["completion"] if result.get("status") in VERIFIED_STATUSES else None
    except RunScriptError as exc:
        print(f"error: {exc}", file=sys.stderr)
        if exc.run_id is not None:
            print(f"Fix the script and resume Run {exc.run_id}.", file=sys.stderr)
        raise SystemExit(1) from exc
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        details = getattr(exc, "stderr", None)
        print(f"error: {details.strip() if details else exc}", file=sys.stderr)
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
