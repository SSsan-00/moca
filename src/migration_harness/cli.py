from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from migration_harness.config import load_config
from migration_harness.workflow import prepare_run, resume_with_junie, run_with_junie


def main() -> None:
    parser = argparse.ArgumentParser(prog="php-to-csharp-harness")
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare", help="Create a worktree and bounded context capsules")
    prepare.add_argument("--config", type=Path, required=True, help="Path to a local TOML config")
    prepare.add_argument("--unit", required=True, help="Migration unit ID from the config")
    run = subparsers.add_parser("run", help="Migrate the selected unit with Junie and build the worktree")
    run.add_argument("--config", type=Path, required=True, help="Path to a local TOML config")
    run.add_argument("--unit", required=True, help="Migration unit ID from the config")
    resume = subparsers.add_parser("resume", help="Continue migration or retry the build in the saved worktree")
    resume.add_argument("--config", type=Path, required=True, help="Path to a local TOML config")
    resume.add_argument("--run-id", required=True, help="Run ID printed by prepare or run")
    args = parser.parse_args()

    try:
        config = load_config(args.config)
        if args.command == "prepare":
            result = prepare_run(config, args.unit)
        elif args.command == "run":
            result = run_with_junie(config, args.unit)
        else:
            result = resume_with_junie(config, args.run_id)
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

    print(f"Run:       {result['run_id']}")
    print(f"Worktree:  {result['worktree_path']}")
    print(f"Capsules:  {result['capsule_count']} (max {result['max_context_chars']} characters each)")
    print(f"Context:   {result['capsule_path']} ({result['context_chars']} characters total)")
    if result["source_boundary_review_required"]:
        print("Review:    Check capsule boundaries and character ranges in manifest.json")
    if args.command == "prepare":
        print("Status:    prepared (no source files changed)")
    else:
        print(f"Junie exit code: {result['junie_exit_code']}")
        print(f"Completed capsules: {result['completed_capsules']}/{result['capsule_count']}")
        print(f"Agent workspace: {result['agent_workspace_path']}")
        if "build_exit_code" in result:
            print(f"Build:     {result['status']} (exit code: {result['build_exit_code']})")
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
        print("Review the worktree diff before running your checker.")


if __name__ == "__main__":
    main()
