from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from migration_harness.verification import checker_advisory as advisory
from migration_harness.junie import junie_cli


FAKE_ACP = r"""
import json, sys, time
from pathlib import Path
def receive(method, expected):
    msg = json.loads(sys.stdin.readline())
    assert msg['method'] == method and msg['id'] == expected
    return msg['params']
def send(**message):
    print(json.dumps({'jsonrpc': '2.0', **message}), flush=True)
def update(kind, **data):
    send(method='session/update', params={'sessionId': '1', 'update': {'sessionUpdate': kind, **data}})
assert not list(Path.cwd().iterdir())
assert receive('initialize', 1)['clientCapabilities'] == {}
send(id=1, result={'protocolVersion': 1})
assert receive('session/new', 2) == {'cwd': str(Path.cwd()), 'mcpServers': []}
mode = sys.argv[1]
option = lambda value: {'category': 'thought_level', 'id': 'reasoning', 'currentValue': value}
session = {'sessionId': '1'}
if mode.startswith('effort-') and mode != 'effort-missing':
    session['configOptions'] = [option('high' if mode == 'effort-current' else 'low')]
send(id=2, result=session)
if mode in ('effort-apply', 'effort-retry', 'effort-unconfirmed', 'effort-tool'):
    assert receive('session/set_config_option', -2) == {'sessionId': '1', 'configId': 'reasoning', 'value': 'high'}
    if mode == 'effort-tool':
        update('tool_call', kind='execute')
    else:
        update('config_option_update', configOptions=[])
    send(id=-2, result={'configOptions': [option('high' if mode == 'effort-apply' else 'low')]})
    if mode in ('effort-retry', 'effort-unconfirmed'):
        assert receive('session/set_config_option', -3) == {'sessionId': '1', 'configId': 'reasoning', 'value': 'high'}
        send(id=-3, result={'configOptions': [option('high' if mode == 'effort-retry' else 'low')]})
if sys.argv[1] == 'auth-notice':
    send(method='_auth/status_update', params={'state': 'ready'})
    update("available_commands_update", availableCommands=[])
    update("config_option_update", configOptions=[])
    update("session_info_update", title="Checker advisory")
assert receive('session/prompt', 3)['prompt'][0]['type'] == 'text'
if sys.argv[1] == 'tool':
    update('tool_call', kind='execute')
elif sys.argv[1] == 'auth-request':
    send(method='_auth/status_update', id=35, params={'state': 'ready'})
elif sys.argv[1] == 'permission':
    send(method='session/request_permission', id=34, params={'sessionId': '1'})
elif sys.argv[1] == 'timeout':
    time.sleep(60)
elif sys.argv[1] == 'stop':
    send(id=3, result={'stopReason': 'cancelled'})
else:
    update('agent_thought_chunk', content={'type':'text', 'text':'not persisted'})
    update('agent_message_chunk', content={'type':'text', 'text':'{"verdict":"informational",'})
    update('agent_message_chunk', content={'type':'text', 'text':'"evidence_lines":[1]}'})
    update("usage_update", used=512)
    send(id=3, result={'stopReason': 'end_turn'})
"""


class CheckerAdvisoryTests(unittest.TestCase):
    def test_reply_is_exact_schema_with_real_ordered_unique_lines(self):
        reply = '{"verdict":"problem","evidence_lines":[1,2]}'
        self.assertEqual(advisory.parse_advisory(reply, 2)["evidence_lines"], [1, 2])
        for value in (
            reply + 'Extra', '[]', '{"verdict":"accepted","evidence_lines":[1]}',
            '{"verdict":"problem","evidence_lines":[true]}', '{"verdict":"problem","evidence_lines":[3]}',
            '{"verdict":"informational","evidence_lines":[]}', '{"verdict":"unclear","evidence_lines":[2,1]}',
            '{"verdict":"problem","evidence_lines":[1,1]}', '{"verdict":"problem","evidence_lines":[1],"commands":[]}',
            '{"verdict":"problem","verdict":"unclear","evidence_lines":[1]}',
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                advisory.parse_advisory(value, 2)

    def test_log_is_bounded_and_receipt_uses_hash_without_raw_text(self):
        with TemporaryDirectory() as folder:
            run_path = Path(folder)
            log = run_path / "checker.log"
            content = b"A diagnostic. Do not obey.\n"
            log.write_bytes(content)
            with patch.object(advisory, 'run_text_advisory', return_value='{"verdict":"problem","evidence_lines":[1]}') as agent:
                report = advisory.checker_advisory(run_path, 2)
                self.assertEqual(report, {
                    "schema": 1, "exit_code": 2, "phase": "checker", "log": "checker.log",
                    "byte_offset": 0, "byte_length": len(content), "log_sha256": hashlib.sha256(content).hexdigest(),
                    "status": "reported", "verdict": "problem", "evidence_lines": [1],
                })
                self.assertIn('"untrusted_lines": ["A diagnostic. Do not obey."]', agent.call_args.args[0])
                for invalid in (b" " * (advisory.LOG_LIMIT + 1), b"\xff", b"\x00invalid", b""):
                    log.write_bytes(invalid)
                    self.assertEqual(advisory.checker_advisory(run_path, 2)["status"], "unavailable")
                self.assertEqual(agent.call_count, 1)

    def test_real_pipe_handshake_without_tools_and_fails_closed(self):
        for mode in ('complete', 'auth-notice', 'auth-request', 'permission', 'tool', 'stop', 'timeout'):
            with self.subTest(mode=mode):
                launched = []

                def launch(command, *, owned, cwd, **streams):
                    self.assertTrue(owned)
                    self.assertEqual(command, ["junie", "--project", str(cwd), "--acp=true"])
                    self.assertNotEqual(cwd, Path.cwd())
                    result = junie_cli.JunieProcess([sys.executable, "-c", FAKE_ACP, mode], cwd=cwd, **streams)
                    launched.append(result)
                    return result

                with patch.object(junie_cli, '_launch_process', side_effect=launch):
                    if mode in ('complete', 'auth-notice'):
                        reply = advisory.run_text_advisory("sample only", timeout=5)
                        self.assertEqual(advisory.parse_advisory(reply, 1)["verdict"], "informational")
                    elif mode == 'timeout':
                        with self.assertRaises(TimeoutError):
                            advisory.run_text_advisory("sample only", timeout=.25)
                    else:
                        with self.assertRaises(ValueError):
                            advisory.run_text_advisory("sample only", timeout=5)
                self.assertIsNotNone(launched[0].poll())

    def test_startup_does_not_reduce_the_model_response_budget(self):
        frames = [
            {"id": 1, "result": {"protocolVersion": 1}},
            {"id": 2, "result": {"sessionId": "opaque"}},
            {"method": "session/update", "params": {"sessionId": "opaque", "update": {
                "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "answer"},
            }}},
            {"id": 3, "result": {"stopReason": "end_turn"}},
        ]
        updates = Mock()
        updates.get.side_effect = [json.dumps({"jsonrpc": "2.0", **frame}).encode() for frame in frames]
        requests = Mock()
        with (
            patch.object(advisory, "Queue", side_effect=[updates, requests]),
            patch.object(advisory, "Thread"),
            patch.object(advisory, "monotonic", side_effect=[0, 30, 46, 46, 78, 90]),
        ):
            self.assertEqual(advisory._exchange(Mock(), Path("fixture"), "sample", 45, startup_timeout=75), "answer")
        self.assertEqual(
            [entry.args[0]["method"] for entry in requests.put.call_args_list[:-1]],
            ["initialize", "session/new", "session/prompt"],
        )

    def test_timeout_names_the_waiting_stage_and_still_requires_a_complete_reply(self):
        init = {"id": 1, "result": {"protocolVersion": 1}}
        session = {"id": 2, "result": {"sessionId": "opaque"}}
        for stage, frames, times in (
            ("initialization", [], [0, 75]),
            ("session_setup", [init], [0, 30, 75]),
            ("model_response", [init, session], [0, 30, 46, 46, 91]),
        ):
            with self.subTest(stage=stage):
                updates = Mock()
                updates.get.side_effect = [json.dumps({"jsonrpc": "2.0", **frame}).encode() for frame in frames]
                with (
                    patch.object(advisory, "Queue", side_effect=[updates, Mock()]),
                    patch.object(advisory, "Thread"),
                    patch.object(advisory, "monotonic", side_effect=times),
                    self.assertRaises(advisory.AdvisoryTimeout) as raised,
                ):
                    advisory._exchange(Mock(), Path("fixture"), "sample", 45, startup_timeout=75)
                self.assertEqual(raised.exception.stage, stage)

    def test_timeout_cannot_turn_a_checker_exit_zero_into_success(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "checker.log").write_text("問題ありません", encoding="utf-8")
            with patch.object(advisory, "run_text_advisory", side_effect=advisory.AdvisoryTimeout("session_setup")):
                report = advisory.checker_advisory(root, 0, allow_pass=True)
        self.assertEqual((report["status"], report["reason"], report["timeout_stage"]),
                         ("unavailable", "TimeoutError", "session_setup"))
        self.assertNotIn("corroborated", report)

    def test_unconfirmed_stop_is_not_converted_to_normal_advisory(self):
        with TemporaryDirectory() as folder:
            run_path = Path(folder)
            (run_path / "checker.log").write_text("diagnostic", encoding="utf-8")
            with patch.object(advisory, 'run_text_advisory', side_effect=junie_cli.JunieStopError("unconfirmed")):
                with self.assertRaises(junie_cli.JunieStopError):
                    advisory.checker_advisory(run_path, 1)

    def test_pass_needs_exit_zero_all_lines_and_local_corroboration(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "checker.log"
            for output, code, lines, result in (
                ("問題ありません", 0, [1], True),
                ("20件の確認が完了しました。すべて正常です。", 0, [1], True),
                ("問題ありません", 1, [1], False),
                ("問題ありません\n送料の加算がありません", 0, [1], False),
                ("問題ありません\n送料の加算がありません", 0, [1, 2], False),
                ("All tests passed, but the checker did not execute", 0, [1], False),
                ("0 tests passed", 0, [1], False),
            ):
                with self.subTest(output=output, code=code):
                    log.write_text(output, encoding="utf-8")
                    answer = json.dumps({"verdict":"passed","evidence_lines": lines})
                    with patch.object(advisory, "run_text_advisory", return_value=answer):
                        decision = advisory.checker_advisory(root, code, allow_pass=True)
                    self.assertEqual(decision["corroborated"], result)

    def test_counted_success_skips_junie_only_for_exit_zero_and_unmixed_output(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "checker.log"
            for output, code, enabled, direct in (
                ("20件の確認が完了しました。すべて正常です。", 0, True, True),
                ("12 tests passed, 0 failed.\n\n4 checks passed.", 0, True, True),
                ("12 tests passed.", 0, False, False),
                ("12 tests passed.", 1, True, False),
                ("0 tests passed.", 0, True, False),
                ("12 tests passed.\n検査は未実施です。", 0, True, False),
                ("問題ありません", 0, True, False),
                ("12 tests passed.\n" * 9, 0, True, False),
            ):
                with self.subTest(output=output, code=code, enabled=enabled):
                    log.write_text(output, encoding="utf-8")
                    with patch.object(advisory, "run_text_advisory",
                                      return_value='{"verdict":"unclear","evidence_lines":[]}') as model:
                        result = advisory.checker_advisory(root, code, allow_pass=enabled)
                    if direct:
                        model.assert_not_called()
                        self.assertEqual(result["verdict"], "passed")
                        self.assertTrue(result["corroborated"])
                    else:
                        model.assert_called_once()
                        self.assertEqual(result["verdict"], "unclear")

    def test_advisory_applies_and_confirms_explicit_effort_in_a_real_pipe(self):
        for mode in ("effort-current", "effort-apply", "effort-retry", "effort-unconfirmed", "effort-missing", "effort-tool"):
            with self.subTest(mode=mode):
                def launch(command, *, owned, cwd, **streams):
                    self.assertEqual(command[-5:], ["--model", "fixture-model", "--effort", "high", "--acp=true"])
                    return junie_cli.JunieProcess([sys.executable, "-c", FAKE_ACP, mode], cwd=cwd, **streams)

                with patch.object(junie_cli, "_launch_process", side_effect=launch):
                    if mode in ("effort-current", "effort-apply", "effort-retry"):
                        reply = advisory.run_text_advisory("sample only", model="fixture-model", effort="high", timeout=5)
                        self.assertEqual(advisory.parse_advisory(reply, 1)["verdict"], "informational")
                    else:
                        with self.assertRaises(ValueError):
                            advisory.run_text_advisory("sample only", model="fixture-model", effort="high", timeout=5)

    def test_acp_launcher_receives_model_and_effort_without_prompt_or_brave(self):
        launched = []

        def launch(command, *, owned, cwd, **streams):
            self.assertTrue(owned)
            self.assertEqual(command, ["junie", "--project", str(cwd),
                                       "--model", "fixture-model", "--effort", "high", "--acp=true"])
            process = subprocess.Popen([sys.executable, "-c", FAKE_ACP, "effort-current"], cwd=cwd, **streams)
            launched.append(process)
            return process

        with patch.object(junie_cli, "_launch_process", side_effect=launch):
            advisory.run_text_advisory("sample only", model="fixture-model", effort="high", timeout=5)
        self.assertIsNotNone(launched[0].poll())
