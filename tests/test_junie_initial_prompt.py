import json
import unittest
from collections import deque
from queue import Queue

from migration_harness.junie import junie_cli


class JunieInitialPromptTests(unittest.TestCase):
    def test_initial_prompt_write_failure_is_reported(self):
        for failure in (OSError("failed pipe"), None):
            with self.subTest(failure=failure):
                session = object.__new__(junie_cli._AcpSession)
                session.session_id = "synthetic-session"
                session.command = ["junie", "--prompt", "ordinary prompt"]
                session.writes = deque()
                session.outgoing = Queue()
                session._on_session_ready()
                data, future = session.outgoing.get_nowait()
                message = json.loads(data)
                self.assertEqual("session/prompt", message["method"])
                self.assertEqual("ordinary prompt", message["params"]["prompt"][0]["text"])
                if failure:
                    future.set_exception(failure)
                    with self.assertRaises(junie_cli.JunieSessionError):
                        session._check_writes()
                else:
                    future.set_result(None)
                    session._check_writes()
                    session._check_writes(closing=True)
