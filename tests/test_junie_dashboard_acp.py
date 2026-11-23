import sys
import tempfile
import textwrap
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from migration_harness.junie import junie_cli
from migration_harness.web.dashboard import Dashboard
from migration_harness.execution import OperationCancelled, control_scope


class JunieDashboardAcpTests(unittest.TestCase):
    def test_steering_budget_measures_final_text_and_requeues_blocked_input(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from migration_harness import workflow

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prompts = Mock()
            prompts.take.return_value = (["id"], "日本語")
            request = Mock()
            session = SimpleNamespace(
                project=root, completion_path=root / "done", token="initial", prompts=prompts,
                steering_supported=True, session_id="session", pending={}, steering_waiting=None,
                prompt_finished=False, next_request_id=4, steering_requests={}, request=request, session_ready=True,
            )
            record_path = root / "run.json"
            record = {}
            with patch.object(junie_cli, "run_interactive", side_effect=lambda: junie_cli._AcpSession.steer(session)):
                workflow._run_budgeted_junie({}, record_path, record, "migration", None, root, [])
                sent = request.call_args.kwargs["prompt"][0]["text"]
                self.assertEqual(record["last_junie_steering_input"]["bytes"], len(sent.encode("utf-8")))
                self.assertFalse(record["last_junie_steering_input"]["blocked"])
                session.steering_waiting = None
                request.reset_mock()
                workflow._run_budgeted_junie({"junie_input_limits": {"max_input_bytes": 1}},
                                            record_path, record, "migration", None, root, [])
                self.assertTrue(record["last_junie_steering_input"]["blocked"])
                request.assert_not_called()
                prompts.finish.assert_called_once_with(["id"], False)

    def test_explicit_lite_is_selected_before_prompt_when_acp_does_not_advertise_models(self):
        from unittest.mock import Mock

        for outcome in ({"result": {}}, {"error": {"code": -32601}}, {"error": {"code": -32602}}):
            with self.subTest(outcome=outcome):
                session = junie_cli._AcpSession.__new__(junie_cli._AcpSession)
                effort = "none" if "result" in outcome else None
                session.command = junie_cli.build_command(Path.cwd(), "prompt", None, model="junie-lite", effort=effort)
                session.request = Mock()
                session.selecting_lite = False
                session.steering_waiting = None
                session._advance_prompt(2, {"result": {"sessionId": "test", "configOptions": [{"id": "mode"}]}})
                request_id, method = session.request.call_args.args
                self.assertEqual(method, "session/set_model")
                self.assertEqual(session.request.call_args.kwargs, {"sessionId": "test", "modelId": "junie-lite"})
                self.assertTrue(session.selecting_lite)
                if outcome.get("error", {}).get("code") == -32602:
                    with self.assertRaises(junie_cli.JunieSessionError):
                        session._receive_message({"id": request_id, **outcome})
                    self.assertEqual(session.request.call_count, 1)
                else:
                    session._receive_message({"id": request_id, **outcome})
                    self.assertEqual(session.request.call_count, 2)
                    self.assertEqual(session.request.call_args.args, (3, "session/prompt"))
                self.assertFalse(session.selecting_lite)

    def test_model_workaround_does_not_override_catalog_default_other_models_or_effort(self):
        from unittest.mock import Mock

        catalog = {
            "models": {"availableModels": [{"modelId": "junie-lite"}]},
        }
        for model, effort, metadata in (
            (None, None, {}),
            ("sonnet", None, {}),
            ("junie-lite", None, catalog),
            ("junie-lite", None, {"configOptions": [{"id": "selector", "category": "model"}]}),
        ):
            with self.subTest(model=model, effort=effort, metadata=metadata):
                session = junie_cli._AcpSession.__new__(junie_cli._AcpSession)
                session.command = junie_cli.build_command(Path.cwd(), "prompt", None, model=model, effort=effort)
                session.request = Mock()
                session._advance_prompt(2, {"result": {"sessionId": "test", **metadata}})
                self.assertEqual(session.request.call_count, 1)
                self.assertEqual(session.request.call_args.args, (3, "session/prompt"))

    def test_missing_acp_model_retries_with_headless_junie_only_after_confirmation(self):
        for approve in (False, True):
            with self.subTest(approve=approve), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                completion = root / "done"
                command = junie_cli.build_command(root, "repair prompt", 7, guidelines=root / "rules.md", model="sonnet", effort="high")
                dashboard = Dashboard()
                dashboard.begin_operation()
                dashboard.control.trusted_junie_workspaces.add(str(root.resolve()))
                acp_agent = textwrap.dedent("""
                    import json, sys
                    for request_id, method, result in (
                        (1, "initialize", {"protocolVersion": 1}),
                        (2, "session/new", {"sessionId": "repair", "configOptions": [
                            {"id": "thought_level", "currentValue": "high"},
                        ]}),
                    ):
                        assert json.loads(sys.stdin.readline())["method"] == method
                        print(json.dumps(dict(jsonrpc="2.0", id=request_id, result=result)), flush=True)
                    request = json.loads(sys.stdin.readline())
                    assert request["method"] == "session/prompt"
                    prompt = request["params"]["prompt"][0]["text"]
                    assert prompt.startswith("repair prompt\\n\\n")
                    assert "進捗と最終回答は日本語で書いてください。" in prompt
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
                        self.assertEqual(commands[0][commands[0].index("--model") + 1], "sonnet")
                        self.assertEqual(commands[0][commands[0].index("--effort") + 1], "high")
                        self.assertTrue(dashboard.answer_junie({
                            "operation_id": operation_id, "request_id": request["id"],
                            "option_id": "headless" if approve else "stop",
                        }))
                        if approve:
                            result.result(timeout=8)
                            self.assertEqual(completion.read_text(), "token")
                            self.assertEqual(commands[1], [
                                "junie", "--project", str(root), "--ide-guidelines", str(root / "rules.md"),
                                "--max-steps", "7", "--model", "sonnet", "--effort", "high", "--task",
                                "repair prompt\n\n" + junie_cli.DASHBOARD_PROGRESS_INSTRUCTION,
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
                    # Windows may spell the same directory with a short path or different casing.
                    assert Path(read()["params"]["cwd"]).samefile(Path.cwd())
                    send(id=2, result={"sessionId": "migration"})
                    prompt = read()["params"]["prompt"][0]["text"]
                    assert prompt.startswith("migration prompt\\n\\n")
                    assert "判断の理由、次に行うことを日本語で" in prompt
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
