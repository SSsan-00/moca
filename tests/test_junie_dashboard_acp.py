import sys
import tempfile
import textwrap
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from migration_harness import junie_cli
from migration_harness.dashboard import Dashboard
from migration_harness.execution import OperationCancelled, control_scope


class JunieDashboardAcpTests(unittest.TestCase):
    def test_missing_acp_model_retries_with_headless_junie_only_after_confirmation(self):
        for approve in (False, True):
            with self.subTest(approve=approve), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                completion = root / "done"
                command = junie_cli.build_command(root, "repair prompt", 7, guidelines=root / "rules.md")
                dashboard = Dashboard()
                dashboard.begin_operation()
                dashboard.control.trusted_junie_workspaces.add(str(root.resolve()))
                acp_agent = textwrap.dedent("""
                    import json, sys
                    for request_id, method, result in (
                        (1, "initialize", {"protocolVersion": 1}),
                        (2, "session/new", {"sessionId": "repair"}),
                    ):
                        assert json.loads(sys.stdin.readline())["method"] == method
                        print(json.dumps(dict(jsonrpc="2.0", id=request_id, result=result)), flush=True)
                    request = json.loads(sys.stdin.readline())
                    assert request["method"] == "session/prompt"
                    print(json.dumps(dict(jsonrpc="2.0", id=3, error={
                        "code": -32002, "data": {"reason": "model_not_available"},
                    })), flush=True)
                    sys.stdin.read()
                """)
                launch = junie_cli._launch_process
                commands = []

                def launch_agent(argv, **streams):
                    if argv == ["junie", "--help"]:
                        return launch([sys.executable, "-c", "print('  --task=<text>  Task description')"], **streams)
                    commands.append(argv)
                    if argv[-1] == "--acp=true":
                        return launch([sys.executable, "-u", "-c", acp_agent], **streams)
                    write_token = f"from pathlib import Path; Path({str(completion)!r}).write_text('token')"
                    return launch([sys.executable, "-c", write_token], **streams)

                def run():
                    with control_scope(dashboard.control):
                        return junie_cli.run_interactive(command, completion, "token", ())

                with (
                    patch.object(junie_cli, "_launch_process", side_effect=launch_agent),
                    patch.dict("os.environ", {"JUNIE_HOME": str(root / "junie-home")}),
                    ThreadPoolExecutor(max_workers=1) as executor,
                ):
                    result = executor.submit(run)
                    operation_id = dashboard.state["operation_id"]
                    try:
                        with dashboard.condition:
                            self.assertTrue(dashboard.condition.wait_for(lambda: dashboard.state.get("junie_request"), timeout=8))
                            request = dashboard.state["junie_request"]
                        self.assertEqual(request["title"], "Junie を非対話モードで再試行")
                        self.assertEqual(len(commands), 1)
                        self.assertTrue(dashboard.answer_junie({
                            "operation_id": operation_id, "request_id": request["id"],
                            "option_id": "headless" if approve else "stop",
                        }))
                        if approve:
                            result.result(timeout=8)
                            self.assertEqual(completion.read_text(), "token")
                            self.assertEqual(commands[1], [
                                "junie", "--project", str(root), "--ide-guidelines", str(root / "rules.md"),
                                "--max-steps", "7", "--task", "repair prompt",
                            ])
                        else:
                            with self.assertRaises(junie_cli.JunieAcpModelUnavailable):
                                result.result(timeout=8)
                            self.assertFalse(completion.exists())
                            self.assertEqual(len(commands), 1)
                    finally:
                        dashboard.request_stop({"operation_id": operation_id})

    def test_permission_choices_are_bound_to_the_current_request_and_returned_to_junie(self):
        for stop in (False, True):
            with self.subTest(stop=stop), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                completion = root / "complete.txt"
                target = root / "Page.cs"
                command = junie_cli.build_command(root, "migration prompt", 7)
                dashboard = Dashboard()
                dashboard.begin_operation()
                operation = dashboard.state["operation_id"]
                agent = textwrap.dedent("""
                    import json, sys
                    from pathlib import Path

                    def read():
                        return json.loads(sys.stdin.readline())

                    def send(**message):
                        print(json.dumps(dict(jsonrpc="2.0", **message)), flush=True)

                    assert read()["method"] == "initialize"
                    send(id=1, result={"protocolVersion": 1})
                    assert read()["params"]["cwd"] == str(Path.cwd())
                    send(id=2, result={"sessionId": "migration"})
                    assert read()["params"]["prompt"][0]["text"] == "migration prompt"
                    send(method="session/update", params={"sessionId": "migration", "update": {
                        "sessionUpdate": "tool_call", "toolCallId": "tool", "title": "Inspect file",
                        "rawInput": {"command": "cat migration.txt"},
                    }})
                    options = [
                        {"optionId": "yes-7", "name": "Allow", "kind": "allow_once"},
                        {"optionId": "no-9", "name": "Reject", "kind": "reject_once"},
                    ]
                    for number, choice in ((10, "no-9"), (11, "yes-7")):
                        send(id=number, method="session/request_permission", params={
                            "sessionId": "migration", "toolCall": {"toolCallId": "tool"}, "options": options,
                        })
                        reply = read()
                        assert reply["id"] == number
                        outcome = reply["result"]["outcome"]
                        if outcome["outcome"] == "cancelled":
                            assert read()["method"] == "session/cancel"
                            sys.exit(0)
                        assert outcome == {"outcome": "selected", "optionId": choice}
                    Path("Page.cs").write_text("class Page {}")
                    Path("complete.txt").write_text("token")
                    send(id=3, result={"stopReason": "end_turn"})
                """)
                launch = junie_cli._launch_process

                def launch_agent(arguments, **streams):
                    self.assertEqual(arguments, ["junie", "--project", str(root), "--max-steps", "7", "--acp=true"])
                    script = f"import os; os.chdir({str(root)!r})\n" + agent
                    return launch([sys.executable, "-u", "-c", script], **streams)

                def run():
                    with control_scope(dashboard.control):
                        return junie_cli.run_interactive(command, completion, "token", (target,))

                def request_after(previous=None):
                    def next_request():
                        request = dashboard.state.get("junie_request")
                        return request if request and request["id"] != previous else None

                    with dashboard.condition:
                        self.assertTrue(dashboard.condition.wait_for(next_request, timeout=8))
                        return next_request()

                def answer(request, option):
                    return dashboard.answer_junie({
                        "operation_id": operation, "request_id": request["id"], "option_id": option,
                    })

                with (
                    patch.object(junie_cli, "_launch_process", side_effect=launch_agent),
                    patch.dict("os.environ", {"JUNIE_HOME": str(root / "junie-home")}),
                    ThreadPoolExecutor(max_workers=1) as executor,
                ):
                    result = executor.submit(run)
                    try:
                        trust = request_after()
                        self.assertTrue(answer(trust, "trust"))
                        first = request_after(trust["id"])
                        self.assertIn("cat migration.txt", first["detail"])
                        self.assertFalse(answer(first, "unknown"))
                        self.assertTrue(answer(first, "no-9"))
                        second = request_after(first["id"])
                        self.assertFalse(answer(first, "yes-7"))
                        if stop:
                            self.assertTrue(dashboard.request_stop({"operation_id": operation}))
                            with self.assertRaises(OperationCancelled):
                                result.result(timeout=8)
                            self.assertFalse(answer(second, "yes-7"))
                            self.assertFalse(completion.exists())
                        else:
                            self.assertTrue(answer(second, "yes-7"))
                            result.result(timeout=8)
                            self.assertEqual(completion.read_text(), "token")
                        self.assertNotIn("junie_request", dashboard.state)
                    finally:
                        dashboard.request_stop({"operation_id": operation})
