from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from migration_harness.config import load_config
from migration_harness.workflow import prepare_run, run_with_junie


def main() -> None:
    parser = argparse.ArgumentParser(prog="php-to-csharp-harness")
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare", help="Create a worktree and bounded context capsules")
    prepare.add_argument("--config", type=Path, required=True, help="Path to a local TOML config")
    prepare.add_argument("--unit", required=True, help="Migration unit ID from the config")
    run = subparsers.add_parser("run", help="Prepare a worktree and run Junie for each context capsule")
    run.add_argument("--config", type=Path, required=True, help="Path to a local TOML config")
    run.add_argument("--unit", required=True, help="Migration unit ID from the config")
    args = parser.parse_args()

    try:
        config = load_config(args.config)
        result = prepare_run(config, args.unit) if args.command == "prepare" else run_with_junie(config, args.unit)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        details = getattr(exc, "stderr", None)
        print(f"error: {details.strip() if details else exc}", file=sys.stderr)
        raise SystemExit(2) from exc

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
        print("Review the worktree diff before continuing to build/checker steps.")


if __name__ == "__main__":
    main()
