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
from migration_harness.runs import stage_scripts
from migration_harness.verification import checker_advisory, checker_cycle


FAKE_ACP = r"""
import json, sys, time
from pathlib import Path
def receive(method, expected):
    msg = json.loads(sys.stdin.readline())
    if sys.argv[1] == 'effort-missing' and len(sys.argv) > 2 and msg.get('method') == 'session/prompt':
        Path(sys.argv[2]).touch()
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
selection_frames = None
if mode == 'text' and len(sys.argv) > 6:
    metadata, selection_frames = json.loads(sys.argv[6])
    session.update(metadata)
if mode.startswith('effort-') and mode != 'effort-missing':
    session['configOptions'] = [option('high' if mode == 'effort-current' else 'low')]
if mode == 'brave':
    session['configOptions'] = [{'id': 'brave_mode', 'type': 'boolean', 'currentValue': True}]
send(id=2, result=session)
if selection_frames is not None:
    assert receive('session/set_model', -1) == {'sessionId': '1', 'modelId': 'junie-lite'}
    for frame in selection_frames:
        send(**frame)
if mode == 'brave':
    assert not sys.stdin.read()
    sys.exit()
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
params = receive('session/prompt', 3)
assert params['prompt'][0]['type'] == 'text'
if mode == 'text' and len(sys.argv) > 7:
    Path(sys.argv[7]).write_text(json.dumps(params), encoding='utf-8')
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
elif mode == 'text':
    reply, stop_reason, count, metadata = sys.argv[2:6]
    count = int(count)
    for index in range(count):
        chunk = reply[len(reply) * index // count:len(reply) * (index + 1) // count]
        if metadata == 'mixed' and index % 32 == 0:
            update('agent_thought_chunk', content={'type': 'text', 'text': 'checking'})
            update('usage_update', used=index)
            update('config_option_update', configOptions=[])
        update('agent_message_chunk', content={'type': 'text', 'text': chunk})
    if stop_reason != 'eof':
        send(id=3, result={'stopReason': stop_reason})
else:
    update('agent_thought_chunk', content={'type':'text', 'text':'not persisted'})
    update('agent_message_chunk', content={'type':'text', 'text':'{"verdict":"informational",'})
    update('agent_message_chunk', content={'type':'text', 'text':'"evidence_lines":[1]}'})
    update("usage_update", used=512)
    send(id=3, result={'stopReason': 'end_turn'})
"""


LITE_EMPTY_MODELS = {"models": {"availableModels": []}}
LITE_SELECTED = [{"id": -1, "result": {}}]


def acp_reply(text, stop_reason="end_turn", *, chunks=2, metadata=False, model_setup=None, prompt_path=None):
    def launch(command, *, owned, cwd, **streams):
        command = [sys.executable, "-c", FAKE_ACP, "text", text, stop_reason, str(chunks),
                   "mixed" if metadata else "plain"]
        if model_setup is not None or prompt_path is not None:
            command.append(json.dumps(model_setup or ({}, None)))
        if prompt_path is not None:
            command.append(str(prompt_path))
        return junie_cli.JunieProcess(command, cwd=cwd, **streams)
    return patch.object(junie_cli, "_launch_process", side_effect=launch)


class CheckerAdvisoryTests(unittest.TestCase):
    def test_checker_and_script_advisories_select_lite_before_sending_output(self):
        with TemporaryDirectory() as folder:
            run = Path(folder)
            for log, phase, effort in (
                ("checker.log", "checker", None),
                ("checker.log", "checker", "none"),
                ("before_migration.log", "before_migration", "none"),
            ):
                (run / log).write_text("The checker could not inspect the fixture.", encoding="utf-8")
                with self.subTest(log=log, effort=effort), acp_reply(
                    '{"verdict":"informational","evidence_lines":[1]}', model_setup=(LITE_EMPTY_MODELS, LITE_SELECTED),
                ):
                    result = advisory.output_advisory(
                        run, log, 1, phase=phase, model_options={"model": "junie-lite", "effort": effort},
                    )
                    self.assertEqual((result["status"], result["verdict"], result["evidence_lines"]),
                                     ("reported", "informational", [1]))

    def test_lite_fallback_does_not_override_models_or_a_different_effort(self):
        for model, effort, session in (
            (None, None, LITE_EMPTY_MODELS),
            ("other", None, LITE_EMPTY_MODELS),
            ("junie-lite", "high", {**LITE_EMPTY_MODELS, "configOptions": [
                {"id": "thought_level", "currentValue": "high"},
            ]}),
            ("junie-lite", "none", {"models": {"availableModels": [{"modelId": "other"}]}}),
            ("junie-lite", "none", {"models": {"availableModels": [], "currentModelId": "other"}}),
            ("junie-lite", None, {"configOptions": [{"id": "model"}]}),
            ("junie-lite", "none", {"configOptions": [{"id": "selection", "category": "model"}]}),
        ):
            with self.subTest(model=model, effort=effort, session=session), acp_reply(
                "answer", model_setup=(session, None),
            ):
                self.assertEqual(advisory.run_text_advisory("sample", model=model, effort=effort, timeout=5), "answer")

    def test_model_selection_rejects_invalid_replies_tools_or_brave_and_allows_legacy_server(self):
        brave = [{"id": "brave_mode", "type": "boolean", "currentValue": True}]
        for frame, expected in (
            ({"id": -1, "result": None}, None),
            ({"id": -1, "error": {"code": -32601}}, None),
            ({"id": -1, "error": {"code": -32602}}, "could not select"),
            ({"id": -1, "result": {}, "error": "broken"}, "could not select"),
            ({"id": -2, "result": {}}, "Unexpected Junie advisory model selection reply"),
            ({"id": -1, "result": {"configOptions": brave}}, "cannot use Brave mode"),
            ({"id": 8, "method": "session/request_permission", "params": {"sessionId": "1"}},
             "unsupported operation"),
            ({"method": "session/update", "params": {"sessionId": "1", "update": {
                "sessionUpdate": "config_option_update", "configOptions": brave,
            }}}, "cannot use Brave mode"),
            ({"method": "session/update", "params": {"sessionId": "1", "update": {
                "sessionUpdate": "tool_call", "kind": "execute",
            }}}, "attempted a non-text operation"),
        ):
            with self.subTest(frame=frame), acp_reply("answer", model_setup=(LITE_EMPTY_MODELS, [frame])):
                if expected is None:
                    self.assertEqual(advisory.run_text_advisory("sample", model="junie-lite", timeout=5), "answer")
                else:
                    with self.assertRaisesRegex(ValueError, expected):
                        advisory.run_text_advisory("sample", model="junie-lite", timeout=5)

    def test_structured_checker_retains_4kib_boundary(self):
        answer = '{"verdict":"problem","evidence_lines":[1]}'
        with TemporaryDirectory() as folder:
            run = Path(folder)
            (run / "checker.log").write_text("Issue on line 1.", encoding="utf-8")
            for size in (4096, 4097):
                with self.subTest(size=size), acp_reply(answer.ljust(size), chunks=254, metadata=True):
                    result = advisory.checker_advisory(run, 1)
                    if size == 4096:
                        self.assertEqual((result["status"], result["verdict"]), ("reported", "problem"))
                    else:
                        self.assertEqual(result["status"], "unavailable")
                        self.assertNotIn("verdict", result)

    def test_free_output_advisory_uses_explicit_saved_settings_and_rejects_older_runs(self):
        with TemporaryDirectory() as folder:
            run = Path(folder)
            (run / "checker.log").write_text("One diagnostic.\n", encoding="utf-8")
            config = {"junie_model": "current-model", "checker_review_free_output": True}
            for current, saved, model, effort in (
                ("high", {"junie_model": "saved-model", "junie_effort": None}, "saved-model", None),
                ("low", {"junie_model": "saved-model", "junie_effort": "high"}, "saved-model", "high"),
            ):
                with self.subTest(current=current, saved=saved):
                    config["junie_effort"] = current
                    (run / "run.json").write_text(json.dumps(saved), encoding="utf-8")
                    commands = []
                    prompt_received = run / "prompt-received"
                    prompt_received.unlink(missing_ok=True)

                    def launch(command, *, owned, cwd, **streams):
                        commands.append(command[3:-1])
                        return junie_cli.JunieProcess(
                            [sys.executable, "-c", FAKE_ACP, "effort-missing", str(prompt_received)], cwd=cwd, **streams,
                        )

                    with patch.object(junie_cli, "_launch_process", side_effect=launch):
                        checked = checker_cycle.review_free_checker_output(
                            config, {"run_path": str(run)}, {"checker_exit_code": 0, **saved},
                        )["checker_advisory"]
                    self.assertEqual(commands, [["--model", model] + (["--effort", effort] if effort else [])])
                    self.assertEqual(effort is None, prompt_received.exists())
                    if effort:
                        self.assertEqual(("unavailable", "AdvisoryEffortUnavailable"),
                                         (checked["status"], checked["reason"]))
                    else:
                        self.assertEqual(("reported", "informational"), (checked["status"], checked["verdict"]))

            for old in ({}, {"junie_model": "saved-model"}, {"junie_effort": None}):
                with self.subTest(old=old), patch.object(junie_cli, "_launch_process") as launch:
                    path = run / "run.json"
                    content = json.dumps(old).encode()
                    path.write_bytes(content)
                    with self.assertRaisesRegex(ValueError, "設定・進捗"):
                        checker_cycle.review_free_checker_output(
                            config, {"run_path": str(run)}, {"checker_exit_code": 0, **old},
                        )
                    self.assertEqual(content, path.read_bytes())
                    launch.assert_not_called()

    def test_before_prepare_advisory_keeps_configured_effort(self):
        with TemporaryDirectory() as folder:
            run = Path(folder)
            config = {"junie_model": "current-model", "junie_effort": "high"}
            item = {"phase": "before_prepare", "command_number": 1, "exit_code": 0,
                    "log": "before_prepare.log", "byte_offset": 0, "byte_length": 8}
            with patch.object(checker_advisory, "output_advisory", return_value={"status": "reported"}) as review:
                stage_scripts.review_script_output(config, run, {}, item, apply_decisions=False)
            self.assertEqual({"model": "current-model", "effort": "high"}, review.call_args.kwargs["model_options"])

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
        for mode in ('complete', 'auth-notice', 'auth-request', 'permission', 'tool', 'brave', 'stop', 'timeout'):
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
        for stage, frames, times, model in (
            ("initialization", [], [0, 75], None),
            ("session_setup", [init], [0, 30, 75], None),
            ("model_selection", [init, session], [0, 30, 46, 75], "junie-lite"),
            ("model_response", [init, session], [0, 30, 46, 46, 91], None),
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
                    advisory._exchange(Mock(), Path("fixture"), "sample", 45, startup_timeout=75, model=model)
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
