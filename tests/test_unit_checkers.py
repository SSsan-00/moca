import io
import json
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from fixtures import HarnessCase
from migration_harness import cli, dashboard, workflow
from migration_harness.config import checker_command_for_unit, load_config
from migration_harness.settings_form import build_settings_draft


class UnitCheckerTests(HarnessCase):
    def setUp(self):
        super().setUp()
        (self.project / "source.php").write_text("<?php echo 42;\n", encoding="utf-8")
        (self.project / "Page.cs").write_text("class Page {}\n", encoding="utf-8")
        self.commit_project()
        self.settings = self.config()
        self.path = Path(self.settings["config_path"])
        self.content = self.path.read_text(encoding="utf-8")
        self.command = [sys.executable, "-c", "print('unit checker')"]

    def configure(self, command):
        draft = build_settings_draft(self.content, {"units": {"0": {"checker_command": command}}})
        self.path.write_text(draft["content"], encoding="utf-8")
        self.settings = load_config(self.path)
        return draft

    def prepare_verification(self):
        prepared = workflow.prepare_run(self.settings, "sample")
        self.run_id = prepared["run_id"]
        self.record_path = Path(prepared["run_path"]) / "run.json"
        record = json.loads(self.record_path.read_bytes())
        record.update(status="junie_finished", completed_capsules=prepared["capsule_count"])
        self.record_path.write_text(json.dumps(record), encoding="utf-8")

    def build_stub(self):
        run = workflow.run_process

        def process(command, **kwargs):
            if command[:2] == ["dotnet", "build"]:
                return subprocess.CompletedProcess(command, 0)
            return run(command, **kwargs)

        return patch.object(workflow, "run_process", side_effect=process)

    def completion(self):
        return workflow.migration_ledger(self.settings, self.run_id)["completion"]["verification_passed"]

    def test_form_round_trip_preserves_arguments_comments_and_other_units(self):
        self.content = self.content.replace('target = "Page.cs"', 'target = "Page.cs" # keep target')
        self.content += '\n[[units]]\nid = "other"\nsource = "source.php"\ntarget = "Other.cs"\n'
        command = ["py", "-3", "tools/check migration.py", "", "日本語", "--flag"]
        draft = self.configure(command)
        self.assertEqual(draft["form"]["units"][0]["checker_command"], command)
        self.assertEqual(self.settings["units"][0]["checker_command"], command)
        self.assertNotIn("checker_command", self.settings["units"][1])
        self.assertIn('# keep target', draft["content"])
        cleared = build_settings_draft(draft["content"], {"units": {"0": {"checker_command": []}}})
        self.assertEqual(cleared["form"]["units"][0]["checker_command"], [])
        self.assertNotIn("checker_command =", cleared["content"])

    def test_invalid_unit_commands_are_rejected(self):
        for literal in ('[]', '"py check.py"', '[" "]', '["py", 1]', '["py", "\\u0000"]', 'false'):
            with self.subTest(literal=literal):
                self.path.write_text(self.content.replace(
                    'id = "sample"', f'id = "sample"\nchecker_command = {literal}',
                ), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "unit sample: checker command"):
                    load_config(self.path)
        for command in ("py check.py", None, [1]):
            with self.subTest(command=command), self.assertRaisesRegex(ValueError, "チェッカー"):
                build_settings_draft(self.content, {"units": {"0": {"checker_command": command}}})

    def test_precedence_and_fallback_do_not_change_shared_config(self):
        self.configure(self.command)
        unit = self.settings["units"][0]
        self.assertEqual(checker_command_for_unit(self.settings, unit), self.command)
        self.assertIsNone(checker_command_for_unit(self.settings, {}))
        self.settings["checker_command"] = ["shared"]
        self.assertEqual(checker_command_for_unit(self.settings, unit), self.command)
        self.assertEqual(checker_command_for_unit(self.settings, {}), ["shared"])
        self.settings.update(checker_from_cli=True, checker_command=["cli"])
        self.assertEqual(checker_command_for_unit(self.settings, unit), ["cli"])
        self.assertEqual(unit["checker_command"], self.command)

    def test_new_migration_and_resume_execute_unit_checker_and_enable_dashboard_resume(self):
        self.configure(self.command)

        def migrate(_command, completion, token, targets, **_kwargs):
            targets[0].write_text("class Page {}\n", encoding="utf-8")
            completion.write_text(token, encoding="utf-8")
            return 0

        with (
            patch.object(workflow.junie_cli, "require_interactive_cli"),
            patch.object(workflow.junie_cli, "run_interactive", side_effect=migrate),
            self.build_stub(),
        ):
            result = workflow.run_with_junie(self.settings, "sample")
            self.assertEqual(result["status"], "checker_succeeded")
            view = dashboard.Dashboard()
            view.ready(self.settings, result)
            self.assertTrue(view.state["resumable"])
            resumed = workflow.resume_with_junie(self.settings, result["run_id"])
            self.assertEqual(resumed["status"], "checker_succeeded")
            view.ready(self.settings, resumed)
            self.assertTrue(view.state["resumable"])
        record = json.loads((Path(result["run_path"]) / "run.json").read_bytes())
        self.assertEqual(record["verification"]["checker_command"], self.command)
        self.assertEqual(Path(result["checker_log_path"]).read_text().strip(), "unit checker")
        self.assertIsNone(self.settings["checker_command"])

    def test_checker_failure_can_resume_with_only_unit_command(self):
        self.configure([sys.executable, "-c", "print('CHECK Page.cs:1 warning: review'); raise SystemExit(1)"])
        self.settings["max_repair_attempts"] = 0
        self.prepare_verification()
        with self.build_stub():
            failed = workflow.resume_with_junie(self.settings, self.run_id)
            self.assertEqual(failed["status"], "checker_needs_review")
            self.assertTrue(workflow.can_resume_run(self.settings, failed))
            self.settings["units"][0]["checker_command"] = self.command
            self.assertEqual(workflow.resume_with_junie(self.settings, self.run_id)["status"], "checker_succeeded")

    def test_unit_command_changes_expire_verification_but_other_units_do_not(self):
        self.configure(self.command)
        self.prepare_verification()
        with self.build_stub():
            workflow.resume_with_junie(self.settings, self.run_id)
        self.assertTrue(self.completion())
        self.settings["checker_command"] = [sys.executable, "-c", "print('shared')"]
        self.settings["units"].append({"id": "other", "checker_command": ["other"]})
        self.assertTrue(self.completion())
        self.settings["units"][0]["checker_command"] = [sys.executable, "-c", "print('changed')"]
        self.assertFalse(self.completion())
        with self.build_stub():
            workflow.resume_with_junie(self.settings, self.run_id)
        self.assertTrue(self.completion())
        del self.settings["units"][0]["checker_command"]
        self.assertFalse(self.completion())

    def test_cli_override_records_unit_default_for_ledger_checks(self):
        self.configure(self.command)
        self.prepare_verification()
        override = [sys.executable, "-c", "print('CLI checker')"]
        with (
            self.build_stub(), redirect_stdout(io.StringIO()),
            patch.object(sys, "argv", ["harness", "resume", "--config", str(self.path), "--run-id", self.run_id,
                                       "--checker-command", *override]),
        ):
            cli.main()
        record = json.loads(self.record_path.read_bytes())
        self.assertEqual(record["verification"]["checker_command"], override)
        self.assertEqual(record["verification"]["toml_checker_command"], self.command)
        self.assertTrue(self.completion())
        self.settings["checker_command"] = ["new shared checker"]
        self.assertTrue(self.completion())
        self.settings["units"][0]["checker_command"] = ["new unit checker"]
        self.assertFalse(self.completion())

    def test_unit_with_checker_is_not_removed_as_empty(self):
        content = '[[units]]\nid = ""\nsource = ""\ntargets = []\nchecker_command = ["py", "check.py"]\n'
        with self.assertRaisesRegex(ValueError, "入力済み"):
            build_settings_draft(content, {"removeEmptyUnits": [0]})
