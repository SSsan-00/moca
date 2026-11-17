import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import tomlkit

from fixtures import HarnessCase
from migration_harness import junie_cli, workflow
from migration_harness.config import load_config
from migration_harness.dashboard import _read_settings, _save_settings
from migration_harness.settings_form import build_settings_draft


class ModelSettingsTests(HarnessCase):
    def setUp(self):
        super().setUp()
        (self.project / "source.php").write_text("<?php echo 'hello';\n")
        self.commit_project()
        self.settings = self.config()
        self.path = Path(self.settings["config_path"])

    def test_save_reload_custom_model_and_clear_preserve_other_settings(self):
        content = self.path.read_text() + '\n[junie]\nmax_steps = 7 # Keep limit\nmodel = "sonnet" # Keep model\n'
        draft = build_settings_draft(content, {"junie": {"model": "custom/my-model", "effort": "high"}})
        _save_settings(self.path, draft["content"], _read_settings(self.path)[1])
        self.assertEqual(load_config(self.path)["junie_model"], "custom/my-model")
        self.assertEqual(load_config(self.path)["junie_effort"], "high")
        self.assertEqual(build_settings_draft(self.path.read_text())["form"]["junie"]["model"], "custom/my-model")
        self.assertIn('# Keep model', draft["content"])
        self.assertEqual(build_settings_draft(self.path.read_text())["form"]["junie"]["effort"], "high")
        cleared = build_settings_draft(draft["content"], {"junie": {"model": "", "effort": ""}})
        _save_settings(self.path, cleared["content"], _read_settings(self.path)[1])
        self.assertIsNone(load_config(self.path)["junie_model"])
        self.assertIsNone(load_config(self.path)["junie_effort"])
        self.assertIn('max_steps = 7 # Keep limit', self.path.read_text())

    def test_invalid_model_rejected_without_overwriting_settings(self):
        original = self.path.read_text()
        for model in (False, 7, [], {}, "two models", "a\nb", "a\0b", "--task"):
            with self.subTest(model=model):
                document = tomlkit.parse(original)
                document["junie"] = {"model": model}
                with self.assertRaisesRegex(ValueError, "モデル"):
                    _save_settings(self.path, tomlkit.dumps(document), _read_settings(self.path)[1])
                self.assertEqual(self.path.read_text(), original)

    def test_effort_validation_requires_a_model_and_preserves_file_on_failure(self):
        original = self.path.read_text()
        for effort in (False, 1, [], {}, "ultra", " high "):
            with self.subTest(effort=effort):
                document = tomlkit.parse(original)
                document["junie"] = {"model": "sonnet", "effort": effort}
                with self.assertRaisesRegex(ValueError, "推論の強さ"):
                    _save_settings(self.path, tomlkit.dumps(document), _read_settings(self.path)[1])
                self.assertEqual(self.path.read_text(), original)
        with self.assertRaisesRegex(ValueError, "モデルも選択"):
            _save_settings(self.path, original + '\n[junie]\neffort = "high"\n', _read_settings(self.path)[1])
        for effort in ("none", "minimal", "low", "medium", "high", "xhigh", "max"):
            content = original + f'\n[junie]\nmodel = "custom"\neffort = "{effort}"\n'
            settings = _save_settings(self.path, content, _read_settings(self.path)[1])
            self.assertEqual(settings["junie_effort"], effort)

    def test_prepared_run_keeps_model_when_settings_change_including_default(self):
        for model in ("sonnet", None):
            with self.subTest(model=model):
                self.settings["junie_model"] = model
                effort = "high" if model else None
                self.settings["junie_effort"] = effort
                prepared = workflow.prepare_run(self.settings, "sample")
                record_path = Path(prepared["run_path"]) / "run.json"
                self.assertEqual(json.loads(record_path.read_text())["junie_model"], model)
                self.assertEqual(json.loads(record_path.read_text())["junie_effort"], effort)
                self.settings["junie_model"] = "opus"
                self.settings["junie_effort"] = "low"
                with patch.object(junie_cli, "require_interactive_cli"), patch.object(junie_cli, "run_interactive", return_value=1) as run:
                    workflow.resume_with_junie(self.settings, prepared["run_id"])
                command = run.call_args.args[0]
                if model:
                    self.assertEqual(command[command.index("--model") + 1], model)
                    self.assertEqual(command[command.index("--effort") + 1], effort)
                else:
                    self.assertNotIn("--model", command)
                    self.assertNotIn("--effort", command)

    def test_old_run_does_not_pick_up_new_effort_setting(self):
        self.settings["junie_model"] = "sonnet"
        prepared = workflow.prepare_run(self.settings, "sample")
        path = Path(prepared["run_path"]) / "run.json"
        record = json.loads(path.read_text())
        record.pop("junie_effort")
        path.write_text(json.dumps(record))
        self.settings["junie_effort"] = "high"
        with patch.object(junie_cli, "require_interactive_cli"), patch.object(junie_cli, "run_interactive", return_value=1) as run:
            workflow.resume_with_junie(self.settings, prepared["run_id"])
        self.assertNotIn("--effort", run.call_args.args[0])


class ModelCliTests(unittest.TestCase):
    def test_model_survives_headless_and_windows_launch(self):
        command = junie_cli.build_command(Path("project"), "task", 7, model="sonnet", effort="high")
        self.assertEqual(command[-6:], ["--model", "sonnet", "--effort", "high", "--prompt", "task"])
        self.assertIn("sonnet", junie_cli._task_command(command))
        self.assertIn("high", junie_cli._task_command(command))
        self.assertNotIn("--model", junie_cli.build_command(Path("project"), "task", None))
        with patch.dict(os.environ, {"SystemRoot": r"C:\Windows"}):
            _, _, environment = junie_cli._windows_batch_command([r"C:\Junie\junie.cmd", *command[1:]])
        self.assertEqual(environment["JUNIE_MODEL"], "sonnet")
        self.assertEqual(environment["JUNIE_EFFORT"], "high")
        with self.assertRaisesRegex(ValueError, "モデルも選択"):
            junie_cli.build_command(Path("project"), "task", 7, effort="high")

    def test_model_catalog_validation_and_timeout(self):
        process = Mock(returncode=0)
        process.communicate.return_value = (json.dumps({"models": [{"id": "one", "displayName": "Model One", "efforts": ["low", "high"]}]}).encode(), b"startup log")
        with patch.object(junie_cli, "_launch_process", return_value=process) as launch:
            self.assertEqual(junie_cli.list_models(), [{"id": "one", "name": "Model One", "efforts": ["low", "high"]}])
        self.assertEqual(launch.call_args.args[0], ["junie", "list-models"])
        for output in (b'not json', b'[]', b'{"models": null}', b'{"models": [{"id": "one"}]}',
                       b'{"models": [{"id": "one", "displayName": "One", "efforts": "high"}]}',
                       b'{"models": [{"id": "one", "displayName": "One", "efforts": [1]}]}'):
            process.communicate.return_value = (output, b"")
            with self.subTest(output=output), patch.object(junie_cli, "_launch_process", return_value=process):
                with self.assertRaisesRegex(ValueError, "読み取れません"):
                    junie_cli.list_models()
        process.returncode = 1
        with patch.object(junie_cli, "_launch_process", return_value=process), self.assertRaisesRegex(ValueError, "取得できません"):
            junie_cli.list_models()
        process.communicate.side_effect = [subprocess.TimeoutExpired("junie", 10), (b"", b"")]
        with patch.object(junie_cli, "_launch_process", return_value=process), patch.object(junie_cli, "_close_junie") as close:
            with self.assertRaisesRegex(ValueError, "タイムアウト"):
                junie_cli.list_models()
        close.assert_called_once_with(process)
