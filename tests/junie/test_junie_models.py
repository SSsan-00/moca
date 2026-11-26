import json
import os
import socket
import subprocess
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import tomlkit

from tests.support.fixtures import HarnessCase, prepare_fixture
from migration_harness.junie import junie_cli, junie_launch
from migration_harness import workflow
from migration_harness.config import load_config
from migration_harness.web.dashboard_settings import read_settings, save_settings_file
from migration_harness.web.settings_form import build_settings_draft


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
        save_settings_file(self.path, draft["content"], read_settings(self.path)[1])
        self.assertEqual(load_config(self.path)["junie_model"], "custom/my-model")
        self.assertEqual(load_config(self.path)["junie_effort"], "high")
        self.assertEqual(build_settings_draft(self.path.read_text())["form"]["junie"]["model"], "custom/my-model")
        self.assertIn('# Keep model', draft["content"])
        self.assertEqual(build_settings_draft(self.path.read_text())["form"]["junie"]["effort"], "high")
        cleared = build_settings_draft(draft["content"], {"junie": {"model": "", "effort": ""}})
        save_settings_file(self.path, cleared["content"], read_settings(self.path)[1])
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
                    save_settings_file(self.path, tomlkit.dumps(document), read_settings(self.path)[1])
                self.assertEqual(self.path.read_text(), original)

    def test_effort_validation_requires_a_model_and_preserves_file_on_failure(self):
        original = self.path.read_text()
        for effort in (False, 1, [], {}, "ultra", " high "):
            with self.subTest(effort=effort):
                document = tomlkit.parse(original)
                document["junie"] = {"model": "sonnet", "effort": effort}
                with self.assertRaisesRegex(ValueError, "推論の強さ"):
                    save_settings_file(self.path, tomlkit.dumps(document), read_settings(self.path)[1])
                self.assertEqual(self.path.read_text(), original)
        with self.assertRaisesRegex(ValueError, "モデルも選択"):
            save_settings_file(self.path, original + '\n[junie]\neffort = "high"\n', read_settings(self.path)[1])
        for effort in ("none", "minimal", "low", "medium", "high", "xhigh", "max"):
            content = original + f'\n[junie]\nmodel = "custom"\neffort = "{effort}"\n'
            settings = save_settings_file(self.path, content, read_settings(self.path)[1])
            self.assertEqual(settings["junie_effort"], effort)

    def test_prepared_run_keeps_model_when_settings_change_including_default(self):
        (self.project / "details.md").write_text("Preserve the greeting text.", encoding="utf-8")
        self.path.write_text(self.path.read_text() + '''
[[project.migration_rules]]
path = "details.md"
usage = "reference"
description = "When producing text output"
''', encoding="utf-8")
        self.settings = load_config(self.path)
        choices = [("sonnet", value) for value in (None, "none", "minimal", "low", "medium", "high", "xhigh", "max")]
        for model, effort in [*choices, (None, None)]:
            with self.subTest(model=model, effort=effort):
                self.settings["junie_model"] = model
                self.settings["junie_effort"] = effort
                prepared = prepare_fixture(self.settings, "sample")
                record_path = Path(prepared["run_path"]) / "run.json"
                self.assertEqual(json.loads(record_path.read_text())["junie_model"], model)
                self.assertEqual(json.loads(record_path.read_text())["junie_effort"], effort)
                self.settings["junie_model"] = "opus"
                self.settings["junie_effort"] = "low"
                def junie(command, completion, token, targets, **kwargs):
                    if kwargs.get("accept_result"):
                        kwargs["accept_result"](json.dumps({"review_token": token, "decisions": {
                            "rule-0001.txt": {"applies": True, "reason": "Produces a greeting"},
                        }}))
                    return 1

                with patch.object(junie_cli, "require_interactive_cli"), patch.object(junie_cli, "run_interactive", side_effect=junie) as run:
                    workflow.resume_with_junie(self.settings, prepared["run_id"])
                self.assertEqual(run.call_count, 2)  # Rule review, then migration.
                for call in run.call_args_list:
                    command = call.args[0]
                    for flag, value in (("--model", model), ("--effort", effort)):
                        if value:
                            self.assertEqual(command[command.index(flag) + 1], value)
                        else:
                            self.assertNotIn(flag, command)

    def test_missing_effort_refuses_resume_without_using_current_setting(self):
        self.settings["junie_model"] = "sonnet"
        prepared = prepare_fixture(self.settings, "sample")
        path = Path(prepared["run_path"]) / "run.json"
        record = json.loads(path.read_text())
        record.pop("junie_effort")
        path.write_text(json.dumps(record))
        self.settings["junie_effort"] = "high"
        saved = path.read_bytes()
        with patch.object(junie_cli, "run_interactive") as run, self.assertRaisesRegex(ValueError, "junie_effort"):
            workflow.resume_with_junie(self.settings, prepared["run_id"])
        run.assert_not_called()
        self.assertEqual(saved, path.read_bytes())


class ModelCliTests(unittest.TestCase):
    def test_model_survives_headless_and_windows_launch(self):
        command = junie_cli.build_command(Path("project"), "task", 7, model="sonnet", effort="high")
        self.assertEqual(command[-6:], ["--model", "sonnet", "--effort", "high", "--prompt", "task"])
        self.assertIn("sonnet", junie_cli._task_command(command))
        self.assertIn("high", junie_cli._task_command(command))
        self.assertNotIn("--model", junie_cli.build_command(Path("project"), "task", None))
        with patch.dict(os.environ, {"SystemRoot": r"C:\Windows"}):
            _, _, environment = junie_launch._windows_batch_command([r"C:\Junie\junie.cmd", *command[1:]])
        self.assertEqual(environment["JUNIE_MODEL"], "sonnet")
        self.assertEqual(environment["JUNIE_EFFORT"], "high")
        with self.assertRaisesRegex(ValueError, "モデルも選択"):
            junie_cli.build_command(Path("project"), "task", 7, effort="high")

    def test_model_catalog_validation_and_timeout(self):
        process = Mock(returncode=0)
        output = json.dumps({"models": [{"id": "one", "displayName": "Model One", "efforts": ["low", "high"]}]}).encode()

        def launch_model(_command, **streams):
            streams["stdout"].write(output)
            return process

        with patch.object(junie_cli, "_launch_process", side_effect=launch_model) as launch, patch.object(junie_cli, "_close_junie"):
            self.assertEqual(junie_cli.list_models(), [{"id": "one", "name": "Model One", "efforts": ["low", "high"]}])
        self.assertEqual(launch.call_args.args[0], ["junie", "list-models"])
        for output in (b'not json', b'[]', b'{"models": null}', b'{"models": [{"id": "one"}]}',
                       b'{"models": [{"id": "one", "displayName": "One", "efforts": "high"}]}',
                       b'{"models": [{"id": "one", "displayName": "One", "efforts": [1]}]}'):
            with self.subTest(output=output), patch.object(junie_cli, "_launch_process", side_effect=launch_model), patch.object(junie_cli, "_close_junie"):
                with self.assertRaisesRegex(ValueError, "読み取れません"):
                    junie_cli.list_models()
        process.returncode = 1
        with patch.object(junie_cli, "_launch_process", side_effect=launch_model), patch.object(junie_cli, "_close_junie"), self.assertRaisesRegex(ValueError, "取得できません"):
            junie_cli.list_models()
        process.wait.side_effect = subprocess.TimeoutExpired("junie", 10)
        with patch.object(junie_cli, "_launch_process", return_value=process), patch.object(junie_cli, "_close_junie") as close:
            with self.assertRaisesRegex(ValueError, "タイムアウト"):
                junie_cli.list_models()
        close.assert_called_once_with(process)

    def test_model_catalog_closes_inheriting_child_after_exit_or_timeout(self):
        child = """
import socket, sys
with socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=10) as connection:
    connection.settimeout(None)
    connection.sendall(b"r")
    connection.recv(1)
"""
        launcher = """
import json, subprocess, sys, time
from pathlib import Path
port, child, release = sys.argv[1:]
subprocess.Popen([sys.executable, "-c", child, port])
print(json.dumps({"models": [{"id": "one", "displayName": "Model One"}]}), flush=True)
while not Path(release).exists():
    time.sleep(.01)
"""
        own_launch = junie_cli._launch_process
        for held in (False, True):
            with self.subTest(held=held), socket.socket() as listener, TemporaryDirectory() as directory, ExitStack() as sockets:
                release = Path(directory) / "release"
                listener.bind(("127.0.0.1", 0))
                listener.listen()
                listener.settimeout(20)
                port = listener.getsockname()[1]
                connection = None

                def launch(_command, **streams):
                    nonlocal connection
                    process = own_launch([sys.executable, "-c", launcher, str(port), child, str(release)], **streams)
                    try:
                        accepted, _ = listener.accept()
                        connection = sockets.enter_context(accepted)
                        connection.settimeout(20)
                        self.assertEqual(connection.recv(1), b"r")
                        # プロセスの終了待ちを計測する。テスト用の子プロセスの起動時間は含めない。
                        if not held:
                            release.touch()
                    except BaseException:
                        process.stop()
                        raise
                    return process

                try:
                    with patch.object(junie_cli, "_launch_process", side_effect=launch), patch.object(
                        junie_cli, "MODEL_LIST_TIMEOUT", .25 if held else 10,
                    ):
                        if held:
                            with self.assertRaisesRegex(ValueError, "タイムアウト"):
                                junie_cli.list_models()
                        else:
                            self.assertEqual(junie_cli.list_models(), [{"id": "one", "name": "Model One"}])
                    try:
                        self.assertEqual(connection.recv(1), b"")
                    except ConnectionResetError:
                        pass
                finally:
                    release.touch()
