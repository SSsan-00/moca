import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from migration_harness.execution import ExecutionControl, control_scope
from migration_harness.junie import acp_session, junie_cli


BRAVE_ON = {"id": "brave_mode", "type": "select", "currentValue": "on",
            "options": [{"value": value} for value in ("brave-auto", "on", "off")]}


class JunieBraveAcpTests(unittest.TestCase):
    def session(self, *, required=True, effort=None):
        command = junie_cli.build_command(Path.cwd(), "synthetic instruction", None,
                                          model="gpt-6-luna", effort=effort)
        with patch.object(acp_session, "Thread"):
            session = acp_session.AcpSession(
                Mock(), command, Path.cwd(), Mock(), Path("complete"), "fixture-token", None,
                require_brave=required,
            )
        session.request = Mock()
        session.initialize()
        return session

    def announce(self, session, options):
        session._receive_message({"id": 2, "result": {"sessionId": "fixture", "configOptions": options,
                                                       "models": {"currentModelId": "gpt-6-luna"}}})

    def test_native_confirmation_does_not_send_a_setter_or_an_approval(self):
        for option in (BRAVE_ON, {"id": "brave_mode", "type": "boolean", "currentValue": True}):
            with self.subTest(option=option):
                session = self.session()
                activity = Mock()
                with control_scope(ExecutionControl(activity)):
                    self.announce(session, [option])
                self.assertTrue(session.session_ready)
                initialize, prompt = session.request.call_args_list
                self.assertEqual(initialize.kwargs["clientCapabilities"]["session"], {"configOptions": {"boolean": {}}})
                self.assertEqual(prompt.args, (3, "session/prompt"))
                session.show_request.assert_not_called()
                self.assertIn("Brave", activity.call_args.args[0]["message"])

    def test_missing_wrong_or_conflicting_modes_never_prompt(self):
        for options in (
            [], [dict(BRAVE_ON, currentValue="off")], [dict(BRAVE_ON, currentValue="brave-auto")],
            [dict(BRAVE_ON, options=[])], [BRAVE_ON, dict(BRAVE_ON, currentValue="off")],
            [dict(BRAVE_ON, id="another_mode")], [{"id": "brave_mode", "type": "boolean", "currentValue": "true"}],
        ):
            with self.subTest(options=options):
                session = self.session()
                with self.assertRaisesRegex(junie_cli.JunieSessionError, "指示を送信していません"):
                    self.announce(session, options)
                self.assertFalse(session.session_ready)
                self.assertEqual([call.args[1] for call in session.request.call_args_list], ["initialize"])
                session.show_request.assert_not_called()

    def test_effort_acknowledgment_cannot_supersede_brave_verification(self):
        session = self.session(effort="medium")
        effort = {"id": "reasoning", "category": "thought_level", "currentValue": "low"}
        self.announce(session, [BRAVE_ON, effort])
        self.assertFalse(session.session_ready)
        request = session.request.call_args
        self.assertEqual(request.kwargs["configId"], "reasoning")
        with self.assertRaisesRegex(junie_cli.JunieSessionError, "指示を送信していません"):
            session._receive_message({"id": request.args[0], "result": {
                "configOptions": [dict(BRAVE_ON, currentValue="off"), dict(effort, currentValue="medium")],
            }})
        self.assertEqual([call.args[1] for call in session.request.call_args_list],
                         ["initialize", "session/set_config_option"])

    def test_default_does_not_require_a_capability_and_checks_no_brave_value(self):
        session = self.session(required=False)
        self.announce(session, [])
        self.assertEqual(session.request.call_args_list[0].kwargs["clientCapabilities"], {})
        self.assertEqual(session.request.call_args.args, (3, "session/prompt"))

    def test_override_is_process_local_and_removed_after_success_or_launch_failure(self):
        for selected, raises in ((False, False), (True, False), (True, True)):
            with self.subTest(selected=selected, raises=raises), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                done = root / "done"
                process = Mock()
                process.wait.side_effect = subprocess.TimeoutExpired("junie", 0.25)
                events = Mock(result=None, usage=[], read_new_events=Mock(return_value=False))
                config_paths = []
                control = ExecutionControl(Mock(), on_junie_request=Mock(), junie_brave=selected,
                                           trusted_junie_workspaces={str(root.resolve())})

                def launch(command, **kwargs):
                    self.assertIn("--acp=true", command)
                    self.assertNotIn("--task", command)
                    self.assertNotIn("--brave", command)
                    self.assertNotIn("synthetic instruction", command)
                    self.assertEqual("--config-location" in command, selected)
                    self.assertEqual(kwargs.get("kill_on_close", False), selected)
                    if selected:
                        config = Path(command[command.index("--config-location") + 1])
                        self.assertFalse(config.is_relative_to(root))
                        self.assertEqual(json.loads(config.read_bytes()), {"brave": True})
                        self.assertEqual(list(config.parent.iterdir()), [config])
                        config_paths.append(config)
                    if raises:
                        raise OSError("fixture launch failure")
                    return process

                with (
                    control_scope(control), patch.object(junie_cli, "_launch_process", side_effect=launch),
                    patch.object(junie_cli, "SessionEvents", return_value=events),
                    patch.object(junie_cli, "_close_junie", return_value=0), patch.object(junie_cli, "AcpSession") as acp,
                ):
                    acp.return_value.advance.return_value = True
                    command = ["junie", "--brave", "--prompt", "synthetic instruction"]
                    if raises:
                        with self.assertRaisesRegex(OSError, "fixture launch failure"):
                            junie_cli._run_session(command, done, "fixture-token", ())
                        acp.assert_not_called()
                    else:
                        self.assertEqual(junie_cli._run_session(command, done, "fixture-token", ()), 0)
                        self.assertEqual(acp.call_args.kwargs["require_brave"], selected)
                self.assertEqual(len(config_paths), int(selected))
                self.assertTrue(all(not path.parent.exists() for path in config_paths))

    def test_opted_in_operation_never_falls_back_to_headless_on_model_error(self):
        control = ExecutionControl(Mock(), on_junie_request=Mock(), junie_brave=True)
        command = ["junie", "--brave", "--prompt", "synthetic instruction"]
        unavailable = junie_cli.JunieAcpModelUnavailable(command, 1, "fixture model unavailable")
        with (
            control_scope(control), patch.object(junie_cli, "_run_session", side_effect=unavailable) as run,
            patch.object(junie_cli, "_supports_task") as headless,
        ):
            with self.assertRaisesRegex(junie_cli.JunieSessionError, "非対話モードには切り替えていません"):
                junie_cli.run_interactive(command, Path("done"), "fixture-token", (), prefer_task=True)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0][-2], "--prompt")
        headless.assert_not_called()
