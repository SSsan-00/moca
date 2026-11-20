from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from migration_harness import checker_advisory as advisory, junie_cli


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
send(id=2, result={'sessionId': '1'})
assert receive('session/prompt', 3)['prompt'][0]['type'] == 'text'
if sys.argv[1] == 'tool':
    update('tool_call', kind='execute')
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
        for mode in ('complete', 'permission', 'tool', 'stop', 'timeout'):
            with self.subTest(mode=mode):
                launched = []

                def launch(command, *, owned, cwd, **streams):
                    self.assertTrue(owned)
                    self.assertEqual(command, ["junie", "--project", str(cwd), "--acp=true"])
                    self.assertNotEqual(cwd, Path.cwd())
                    result = subprocess.Popen([sys.executable, "-c", FAKE_ACP, mode], cwd=cwd, **streams)
                    launched.append(result)
                    return result

                with patch.object(junie_cli, '_launch_process', side_effect=launch):
                    if mode == 'complete':
                        reply = advisory.run_text_advisory("sample only", timeout=5)
                        self.assertEqual(advisory.parse_advisory(reply, 1)["verdict"], "informational")
                    elif mode == 'timeout':
                        with self.assertRaises(TimeoutError):
                            advisory.run_text_advisory("sample only", timeout=.25)
                    else:
                        with self.assertRaises(ValueError):
                            advisory.run_text_advisory("sample only", timeout=5)
                self.assertIsNotNone(launched[0].poll())

    def test_unconfirmed_stop_is_not_converted_to_normal_advisory(self):
        with TemporaryDirectory() as folder:
            run_path = Path(folder)
            (run_path / "checker.log").write_text("diagnostic", encoding="utf-8")
            with patch.object(advisory, 'run_text_advisory', side_effect=junie_cli.JunieStopError("unconfirmed")):
                with self.assertRaises(junie_cli.JunieStopError):
                    advisory.checker_advisory(run_path, 1)
