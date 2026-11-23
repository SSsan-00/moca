import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from fixtures import HarnessCase
from migration_harness.web import dashboard
from migration_harness import workflow
from migration_harness.config import load_config
from migration_harness.web.settings_form import build_settings_draft


class CheckerSettingsTests(HarnessCase):
    def setUp(self):
        super().setUp()
        (self.project / "source.php").write_text("<?php echo 42;\n", encoding="utf-8")
        (self.project / "Page.cs").write_text("class Page {}\n", encoding="utf-8")
        self.commit_project()
        self.settings = self.config()
        self.path = Path(self.settings["config_path"])
        self.content = self.path.read_text(encoding="utf-8")
        self.command = [sys.executable, "-X", "utf8", "-c", "print('特に気になる点はありません。')"]

    def configure(self, command):
        draft = build_settings_draft(self.content, {"checker": {"command": command}})
        self.path.write_text(draft["content"], encoding="utf-8")
        self.settings = load_config(self.path)
        return draft

    def build_stub(self):
        run = workflow.run_process

        def process(command, **kwargs):
            if command[:2] == ["dotnet", "build"]:
                return subprocess.CompletedProcess(command, 0)
            return run(command, **kwargs)

        return patch.object(workflow, "run_process", side_effect=process)

    def test_unit_form_no_longer_accepts_checker_settings(self):
        draft = build_settings_draft(self.content)
        self.assertNotIn("checker_command", draft["form"]["units"][0])
        with self.assertRaisesRegex(ValueError, "移行単位の入力"):
            build_settings_draft(self.content, {"units": {"0": {"checker_command": ["py"]}}})

    def test_common_checker_round_trips_without_unit_setting(self):
        command = ["py", "-3", "tools/check migration.py", "", "日本語", "--flag"]
        draft = self.configure(command)
        self.assertEqual(draft["form"]["checker"]["command"], command)
        self.assertEqual(self.settings["checker_command"], command)
        self.assertNotIn("checker_command", self.settings["units"][0])
        cleared = build_settings_draft(draft["content"], {"checker": {"command": []}})
        self.assertEqual(cleared["form"]["checker"]["command"], [])

    def test_new_migration_and_resume_execute_common_checker_and_enable_dashboard_resume(self):
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
        self.assertEqual(Path(result["checker_log_path"]).read_text(encoding="utf-8").strip(), "特に気になる点はありません。")
        self.assertEqual(self.settings["checker_command"], self.command)
