import json
import io
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from fixtures import HarnessCase
from migration_harness import workflow
from migration_harness.command_paths import expand_command_paths
from migration_harness.config import load_config
from migration_harness.settings_form import build_settings_draft


class CommandPathsTests(HarnessCase):
    def setUp(self):
        super().setUp()
        (self.project / "source.php").write_text("<?php echo 1;\n", encoding="utf-8")
        (self.project / "App.csproj").write_text("<Project />", encoding="utf-8")
        (self.project / "First.cs").write_text("class First {}", encoding="utf-8")
        (self.project / "Second.cs").write_text("class Second {}", encoding="utf-8")
        self.commit_project()
        settings = self.config('targets = ["First.cs", "Second.cs"]')
        self.path = Path(settings["config_path"])
        content = self.path.read_text(encoding="utf-8").replace('worktree_root = "trees"', 'worktree_root = "作業 ツリー"')
        arguments = ["{build_project}", "{target}", "{worktree}"]
        self.command = [sys.executable, "-c",
                        "import json,sys,pathlib; pathlib.Path('checker-args.json').write_text(json.dumps(sys.argv[1:]))",
                        *arguments]
        script = [sys.executable, "-c", "import json,sys; print(json.dumps(sys.argv[1:]))", *arguments]
        draft = build_settings_draft(content, {
            "build": {"project": "App.csproj"},
            "checker": {"command": self.command},
            "scripts": [{"phase": "after_build", "command": script}],
        })
        self.path.write_text(draft["content"], encoding="utf-8")
        self.settings = load_config(self.path)

    def prepare_verification(self):
        result = workflow.prepare_run(self.settings, "sample")
        record_path = Path(result["run_path"]) / "run.json"
        record = json.loads(record_path.read_bytes())
        record.update(status="junie_finished", completed_capsules=result["capsule_count"])
        record_path.write_text(json.dumps(record), encoding="utf-8")
        return result

    def verify(self, result):
        execute = workflow.run_process

        def process(command, **kwargs):
            if command[:2] == ["dotnet", "build"]:
                self.assertEqual(command[2], str(Path(result["worktree_path"]) / "App.csproj"))
                return subprocess.CompletedProcess(command, 0)
            return execute(command, **kwargs)

        # 英語設定のWindowsへリダイレクトした標準出力でも、日本語パスで処理を止めない。
        with (
            io.TextIOWrapper(io.BytesIO(), encoding="cp1252") as terminal,
            redirect_stdout(terminal),
            patch.object(workflow, "run_process", side_effect=process),
        ):
            return workflow.resume_with_junie(self.settings, result["run_id"])

    def test_commands_expand_for_each_run_and_resume_without_changing_settings(self):
        for source in ("shared", "unit", "cli"):
            with self.subTest(source=source):
                if source == "unit":
                    self.settings["checker_command"] = ["must-not-run"]
                    self.settings["units"][0]["checker_command"] = self.command
                elif source == "cli":
                    self.settings.update(checker_from_cli=True, checker_command=self.command)
                    self.settings["units"][0]["checker_command"] = ["must-not-run"]
                result = self.prepare_verification()
                root = Path(result["worktree_path"])
                expected = [str(root / "App.csproj"), str(root / "First.cs"), str(root)]
                for _ in range(2):
                    self.assertEqual(self.verify(result)["status"], "checker_succeeded")
                    run_path = Path(result["run_path"])
                    self.assertEqual((run_path / "checker.log").read_bytes(), b"")
                    self.assertEqual(json.loads((root / "checker-args.json").read_text()), expected)
                    script_lines = (run_path / "after_build.log").read_text().splitlines()
                    self.assertEqual(json.loads(script_lines[-1]), expected)
                    self.assertTrue(workflow.migration_ledger(self.settings, result["run_id"])["completion"]["verification_passed"])
                record = json.loads((run_path / "run.json").read_bytes())
                self.assertEqual(record["verification"]["checker_command"], self.command)
        self.assertEqual(load_config(self.path)["checker_command"], self.command)

    def test_expanded_checker_changes_require_explicit_reverification(self):
        result = self.prepare_verification()
        worktree = Path(result["worktree_path"])
        (worktree / ".gitignore").write_text("obj/\n", encoding="utf-8")
        checker = worktree / "obj/check.py"
        checker.parent.mkdir()
        checker.write_text("pass\n", encoding="utf-8")
        self.settings["checker_command"] = [sys.executable, "{worktree}/obj/check.py", "{target}"]
        self.settings["max_repair_attempts"] = 0
        self.verify(result)
        self.assertTrue(workflow.migration_ledger(self.settings, result["run_id"])["completion"]["verification_passed"])
        checker.write_text("raise SystemExit(1)\n", encoding="utf-8")
        completion = workflow.migration_ledger(self.settings, result["run_id"])["completion"]
        self.assertFalse(completion["verification_passed"])
        self.assertEqual(completion["verification_input_status"], "changed")
        self.assertEqual(self.verify(result)["status"], "checker_needs_review")
        self.assertFalse(workflow.migration_ledger(self.settings, result["run_id"])["completion"]["verification_passed"])

    def test_missing_build_setting_records_checker_failure(self):
        self.settings["build_project"] = None
        self.settings["scripts"] = {}
        result = self.prepare_verification()
        record = json.loads((Path(result["run_path"]) / "run.json").read_bytes())
        with patch.object(workflow, "run_process") as execute:
            checked, _, _ = workflow._run_checker(self.command, result, record, None)
        execute.assert_not_called()
        self.assertEqual(checked["status"], "checker_failed")
        self.assertIn("[build].project", checked["error"])
        self.assertIn("[build].project", Path(checked["checker_log_path"]).read_text(encoding="utf-8"))

    def test_before_prepare_rejects_worktree_alias_instead_of_using_original_repository(self):
        self.settings["scripts"] = {"before_prepare": [self.command]}
        with self.assertRaisesRegex(workflow.RunScriptError, "before_prepare"):
            workflow.prepare_run(self.settings, "sample")

    def test_embedded_paths_preserve_argument_boundaries_and_unrelated_braces(self):
        root = self.root / "日本語 {target}"
        command = ["check", "--project={build_project}", '{"value": 1}', "{unknown}", "{target}", ""]
        self.assertEqual(expand_command_paths(command, root, "App.csproj", "First.cs"), [
            "check", f"--project={root / 'App.csproj'}", '{"value": 1}', "{unknown}", str(root / "First.cs"), "",
        ])
        self.assertEqual(command[-2], "{target}")

    def test_alias_cannot_resolve_outside_worktree(self):
        with self.assertRaisesRegex(ValueError, "escapes"):
            expand_command_paths(["check", "{target}"], self.project, "App.csproj", "../Outside.cs")
