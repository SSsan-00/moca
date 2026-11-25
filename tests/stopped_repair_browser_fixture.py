import json
import shutil
import subprocess
import uuid
from pathlib import Path
from unittest.mock import patch

from fixtures import stub_syntax_diagnostics
from migration_harness import workflow
from migration_harness.config import target_paths
from migration_harness.execution import OperationCancelled
from migration_harness.junie import junie_cli


def stopped_repair_run(config, template, source_tree, phase):
    run_id = str(uuid.uuid4())
    run = Path(config["run_root"]) / run_id
    worktree = Path(config["worktree_root"]) / run_id
    capsule = run / "capsule"
    capsule.mkdir(parents=True)
    (run / "agent-workspace").mkdir()
    context = capsule / "context.txt"
    context.write_text("<?php echo 1;", encoding="utf-8")
    (capsule / "manifest.json").write_text(json.dumps({
        "parts": [{"index": 1, "path": str(context), "chars": 13,
                   "sources": [{"path": "example.php", "start_line": 1, "end_line": 1, "encoding": "utf-8"}]}],
    }), encoding="utf-8")
    targets = target_paths(config["units"][0])
    for target in targets:
        to = worktree / target
        to.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_tree / target, to)

    record = {key: template[key] for key in ("project_root", "unit_id", "target", "targets")}
    record.update(run_id=run_id, worktree_path=str(worktree), status="build_failed", completed_capsules=1,
                  context_capsules=1, max_context_chars=1000, source_boundary_review_required=False)
    record_path = run / "run.json"
    record_path.write_text(json.dumps(record), encoding="utf-8")
    run_config = dict(config, build_project=None, checker_command=["fixture-checker"], max_repair_attempts=1)
    body = b"class PartlyRepaired {}\n"
    original = (worktree / targets[0]).read_bytes()
    calls = []
    expected = {"build": ["dotnet"], "checker": ["dotnet", "fixture-checker"]}[phase]

    def command(argv, *, stdout, **kwargs):
        name = argv[0]
        if name != expected[len(calls)] or Path(kwargs["cwd"]) != worktree:
            raise AssertionError("unexpected external command")
        calls.append(name)
        if name == "dotnet":
            if phase == "checker":
                return subprocess.CompletedProcess(argv, 0)
            stdout.write(f"{worktree / targets[0]}(1,1): error CS1002: expected\n".encode())
        else:
            stdout.write(f"RULE {targets[0]}:1 error: fixture finding\n".encode())
        return subprocess.CompletedProcess(argv, 1)

    def stop(_argv, _marker, _token, files, **kwargs):
        files[0].write_bytes(body)
        raise OperationCancelled("synthetic operator stop")

    with (patch.object(workflow, "run_process", side_effect=command), stub_syntax_diagnostics(),
          patch.object(junie_cli, "require_interactive_cli"),
          patch.object(junie_cli, "run_interactive", side_effect=stop) as edit):
        try:
            result = workflow.resume_with_junie(run_config, run_id)
        except workflow.RunInterrupted:
            pass
        else:
            raise AssertionError(f"synthetic {phase} repair did not stop: {result.get('status')}, {result.get('error')}")
        edit.assert_called_once()
    assert calls == expected
    saved = json.loads(record_path.read_bytes())
    assert saved["status"] == {"build": "build_failed", "checker": "checker_needs_review"}[phase]
    snapshot = saved["repair_pending"]["draft"]["snapshot"]
    assert (run / snapshot / "0.draft").read_bytes() == body
    assert (worktree / targets[0]).read_bytes() == original
    assert workflow.plan_stopped_repair(run_config, run_id)["snapshot"] == snapshot
    return saved


def resume_after_external_edit(config, run_id):
    run_path, worktree, _, unit = workflow.load_saved_run(config, run_id)
    target = worktree / target_paths(unit)[0]
    target.write_bytes(b"class EditedAfterSelection {}\n")
    with (patch.object(workflow, "run_process") as command,
          patch.object(junie_cli, "run_interactive") as junie):
        result = workflow.resume_with_junie(dict(config, checker_command=["fixture-checker"]), run_id)
        command.assert_not_called()
        junie.assert_not_called()
    assert result["status"] == "build_failed" and "復元計画を取り直して" in result["error"]
    assert target.read_bytes() == b"class EditedAfterSelection {}\n"
    return json.loads((run_path / "run.json").read_bytes())
