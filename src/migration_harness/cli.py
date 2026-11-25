from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

if sys.platform != "win32":
    raise SystemExit("このハーネスの対応OSはWindowsのみです。")

from migration_harness.config import load_config, target_paths
from migration_harness.errors import format_error


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="php-to-csharp-harness")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def configured_command(name: str, help_text: str) -> argparse.ArgumentParser:
        command = subparsers.add_parser(name, help=help_text)
        command.add_argument("--config", type=Path, required=True, help="Path to a local TOML config")
        return command

    dashboard = configured_command("dashboard", "Open the local workspace for configuration and migration runs")
    dashboard.add_argument("--open-browser", action="store_true", help=argparse.SUPPRESS)
    junie_input = configured_command("junie-input", "Show the last measured Junie launch input for a saved run")
    junie_input.add_argument("--run-id", required=True)
    evidence = configured_command("context-evidence", "Inspect fixed PHP evidence or read a bounded page")
    evidence.add_argument("--run-id", required=True)
    evidence.add_argument("--feature", help="Look up an exact declared symbol or named PHP function")
    evidence.add_argument("--last-plan", action="store_true", help="Show the last migration, resume or repair navigation plan")
    evidence.add_argument("--target-references", action="store_true", help="List pinned C# and Razor reference IDs")
    evidence.add_argument("--related-to", metavar="TARGET_ID", help="Find a pinned Razor companion by exact path")
    evidence.add_argument("--source-id")
    evidence.add_argument("--start-line", type=int)
    evidence.add_argument("--end-line", type=int)
    evidence.add_argument("--through-capsule", type=int)
    evidence.add_argument("--offset", type=int, default=0)
    evidence.add_argument("--max-chars", type=int, default=16384)
    handoff = configured_command("context-handoff", "List or record versioned unresolved source notes")
    handoff.add_argument("--run-id", required=True)
    handoff.add_argument("--note")
    handoff.add_argument("--source-id")
    handoff.add_argument("--start-line", type=int)
    handoff.add_argument("--end-line", type=int)
    handoff.add_argument("--supersedes", help="ID of an older note, kept for history")
    rule_review = configured_command("rule-review", "Inspect saved migration rules and record independent review evidence")
    rule_review.add_argument("--run-id", required=True)
    rule_review.add_argument("--rule")
    rule_review.add_argument("--record", choices=("include", "exclude", "read", "applied", "passed", "failed"))
    rule_review.add_argument("--version")
    rule_review.add_argument("--start-line", type=int)
    rule_review.add_argument("--end-line", type=int)
    rule_review.add_argument("--reason")
    rule_review.add_argument("--reference", help="Identifier or path of the independent test or checker report")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    try:
        try:
            config = load_config(args.config)
        except (OSError, ValueError, RuntimeError, KeyError, TypeError, AttributeError) as exc:
            if args.command != "dashboard":
                raise
            from migration_harness.web.dashboard import run_dashboard

            # 初回のdashboardは設定例から作り始める。既存設定の読み込み失敗は表示する。
            first_setup = isinstance(exc, FileNotFoundError) and not args.config.exists()
            run_dashboard(
                {"config_path": str(args.config.expanduser().resolve())},
                config_error=None if first_setup else format_error(exc),
                open_browser=args.open_browser,
            )
            return
        if args.command == "dashboard":
            from migration_harness.web.dashboard import run_dashboard

            run_dashboard(config, open_browser=args.open_browser)
            return
        if args.command == "context-handoff":
            from migration_harness.context.context_handoff import context_handoff, record_handoff
            from migration_harness.runs.run_records import load_saved_run

            run_path, worktree, record, unit = load_saved_run(config, args.run_id)
            arguments = (run_path, worktree, target_paths(unit), record.get("detail_overrides", {}))
            requested = (args.source_id, args.start_line, args.end_line, args.note)
            if all(value is None for value in requested) and args.supersedes is None:
                result = context_handoff(*arguments)
            elif all(value is not None for value in requested):
                result = record_handoff(*arguments, *requested, supersedes=args.supersedes)
            else:
                raise ValueError("provide --note, --source-id, --start-line and --end-line together")
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return
        if args.command == "context-evidence":
            from migration_harness.context.capsules import load_capsule_manifest
            from migration_harness.context.context_evidence import find_feature, inspect_context, read_context
            from migration_harness.context.target_evidence import read_target_reference, related_target_references, target_references
            from migration_harness.runs.run_records import load_dashboard_record

            run_path, record = load_dashboard_record(config, args.run_id)
            requested = (args.source_id, args.start_line, args.end_line)
            if args.related_to is not None or args.target_references:
                if (args.related_to is not None and args.target_references or args.last_plan or args.feature is not None
                        or args.through_capsule is not None or any(value is not None for value in requested)):
                    raise ValueError("target reference modes cannot be combined with other evidence selection")
                capsule, _ = load_capsule_manifest(run_path)
                if args.target_references:
                    if args.offset < 0:
                        raise ValueError("reference offset cannot be negative")
                    references = list(target_references(capsule).values())
                    end = args.offset + 50
                    result = {"references": references[args.offset:end],
                              "next_offset": end if end < len(references) else None}
                else:
                    result = related_target_references(capsule, args.related_to)
            elif args.last_plan:
                if args.feature is not None or args.through_capsule is not None or any(value is not None for value in requested):
                    raise ValueError("--last-plan cannot be combined with evidence selection")
                result = {"plan": record.get("last_context_plan"), "supplied_rules": record.get("supplied_rules", [])}
            elif args.feature is not None:
                if any(value is not None for value in requested):
                    raise ValueError("--feature cannot be combined with source range selection")
                result = find_feature(run_path, args.feature, args.through_capsule)
            elif all(value is None for value in requested):
                result = inspect_context(run_path, args.through_capsule)
            elif all(value is not None for value in requested) and args.source_id.startswith("target:"):
                if args.through_capsule is not None:
                    raise ValueError("target references do not belong to a migration capsule")
                capsule, _ = load_capsule_manifest(run_path)
                result = read_target_reference(capsule, *requested, offset=args.offset, max_chars=args.max_chars)
            elif all(value is not None for value in requested):
                result = read_context(run_path, *requested, through=args.through_capsule, offset=args.offset,
                                      max_chars=args.max_chars)
            else:
                raise ValueError("provide --source-id, --start-line and --end-line together")
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return
        if args.command == "junie-input":
            from migration_harness.runs.run_records import load_dashboard_record

            _, record = load_dashboard_record(config, args.run_id)
            print(json.dumps({
                "run_id": args.run_id,
                "last_junie_input": record.get("last_junie_input"),
                "last_junie_steering_input": record.get("last_junie_steering_input"),
                "junie_permission_counts": record.get("junie_permission_counts", {}),
            }, ensure_ascii=False, indent=2))
            return
        if args.command == "rule-review":
            from migration_harness.rules.rule_review import review_rules

            report = review_rules(config, args.run_id, rule=args.rule, kind=args.record, version=args.version,
                                  start=args.start_line, end=args.end_line, reason=args.reason, reference=args.reference)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, AttributeError, subprocess.CalledProcessError) as exc:
        print(f"error: {format_error(exc)}", file=sys.stderr)
        raise SystemExit(2) from exc
    except KeyboardInterrupt as exc:
        run_id = getattr(exc, "run_id", None)
        if run_id:
            print(f"Interrupted. Saved run {run_id}; open the dashboard with moca.cmd and resume the saved run.", file=sys.stderr)
        else:
            print("Interrupted.", file=sys.stderr)
        raise SystemExit(130)


if __name__ == "__main__":
    main()
