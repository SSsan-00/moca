import subprocess
import sys
from pathlib import Path

import tempfile
import unittest

from migration_harness.dry_run import plan_dry_run


def make_config(tmp_path):
    root = tmp_path / "アプリ source"
    root.mkdir()
    (root / "source.php").write_text("<?php echo 1;\n", encoding="utf-8")
    (root / "App.csproj").touch()
    (root / "checker.py").write_text("raise Exception('must not run')", encoding="utf-8")
    def git(*args):
        return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()
    git("init", "-q")
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-q", "-m", "fixture")
    return dict(config_path=str(tmp_path / "harness.toml"), project_root=str(root), base_ref="HEAD",
                run_root=str(tmp_path / "runs"), worktree_root=str(tmp_path / "worktrees"),
                max_context_chars=1000, build_project="App.csproj", checker_command=["unselected-command"],
                units=[dict(id="注文", target="Page.cshtml", targets=["Page.cshtml", "Page.cshtml.cs"],
                            checker_command=[sys.executable, "checker.py", "{target}"],
                            inputs=[dict(path="source.php", encoding="auto", start_line=1, end_line=1)])])


class DryRunTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.config = make_config(Path(temp.name))

    def test_plan_uses_unit_checker_and_never_runs_configured_commands(self):
        config = self.config
        root = Path(config["project_root"])
        marker = root / "executed"
        config["scripts"] = {"before_prepare": [[sys.executable, "-c", f"open({str(marker)!r}, 'w')"]]}
        report = plan_dry_run(config, "注文")
        assert report["commit"]
        checks = report["checks"]
        checker = next(item for item in checks if item["phase"] == "checker")
        assert "Page.cshtml" in checker["command"] and "unselected-command" not in checker["command"]
        assert checker["cwd"] == report["worktree_template"]
        assert any(item["phase"] == "prepare" and item.get("characters") for item in checks)
        assert not marker.exists()
        assert not Path(config["run_root"]).exists()
        assert not Path(config["worktree_root"]).exists()


    def test_reports_wrong_alias_and_uncommitted_script(self):
        config = self.config
        root = Path(config["project_root"])
        (root / "local-only").touch()
        config["scripts"] = {"before_prepare": [[sys.executable, "{target}"]], "after_prepare": [["./local-only"]]}
        result = plan_dry_run(config, "注文")
        assert not result["configuration_ok"]
        assert any(item["phase"] == "before_prepare" and item["state"] == "error" for item in result["checks"])
        assert any(item["phase"] == "after_prepare" and "基準コミットに実行ファイルがありません" in item["detail"]
                   for item in result["checks"])


    def test_invalid_base_does_not_hide_independent_input_failure(self):
        config = self.config
        config["base_ref"] = "missing-revision"
        config["units"][0]["inputs"][0]["end_line"] = 2
        report = plan_dry_run(config, "注文")
        assert report["commit"] is None
        assert any(item["phase"] == "prepare" and item["state"] == "error" for item in report["checks"])
        assert any(item["phase"] == "build" and item["state"] == "unchecked" for item in report["checks"])

    def test_control_cancellation_and_timed_out_git_are_not_success(self):
        from unittest.mock import patch

        from migration_harness.execution import ExecutionControl, OperationCancelled, control_scope

        with patch("migration_harness.dry_run._probe_git", side_effect=subprocess.TimeoutExpired("git", 5)):
            plan = plan_dry_run(self.config, "注文")
        self.assertFalse(plan["configuration_ok"])
        self.assertTrue(any(row["phase"] == "git" and row["state"] == "unchecked" for row in plan["checks"]))
        control = ExecutionControl(lambda _: None)
        control.cancelled.set()
        with control_scope(control), self.assertRaises(OperationCancelled):
            plan_dry_run(self.config, "注文")
