import json
import os
import sys
import tempfile
import textwrap
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest.mock import patch

from migration_harness import junie_cli
from migration_harness.dashboard import Dashboard
from migration_harness.editor_queue import LivePrompts, add_prompt, editor_queue
from migration_harness.execution import OperationCancelled, control_scope


class JunieSteeringTests(unittest.TestCase):
    def test_live_delivery_capability_fallback_completion_races_and_cancellation(self):
        for scenario in ("injected", "new_turn", "new_turn_without_receipt", "exit", "multiple", "unsupported", "invalid_capability",
                         "rejected", "invalid_response", "missing_receipt", "cancelled"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                run_path = Path(directory).resolve()
                root = run_path / "agent-workspace"
                root.mkdir()
                completion = root / "complete.txt"
                target = root / "Page.cs"
                target.write_text("original", encoding="utf-8")
                with editor_queue(run_path) as queue:
                    add_prompt(queue, {"text": "日付を確認してください。"})
                    # 起動時に渡した指示は、途中入力の後始末で書き換えない。
                    queue["prompts"].append({"id": "initial", "text": "initial", "status": "sending"})
                dashboard = Dashboard()
                dashboard.begin_operation()
                dashboard.control.trusted_junie_workspaces.add(str(root))
                received = []

                def activity(entry):
                    if "追加指示を受け取りました" in entry["message"]:
                        received.append(entry)
                        if scenario == "cancelled":
                            dashboard.control.cancelled.set()
                        elif scenario == "multiple" and len(received) == 1:
                            with editor_queue(run_path) as queue:
                                add_prompt(queue, {"text": "時刻も確認してください。"})

                dashboard.control.on_activity = activity
                agent = textwrap.dedent("""
                    import json, os, re, sys, time
                    from pathlib import Path

                    def read():
                        return json.loads(sys.stdin.readline())

                    def send(**message):
                        print(json.dumps(dict(jsonrpc="2.0", **message)), flush=True)

                    assert read()["method"] == "initialize"
                    supported = "true" if scenario == "invalid_capability" else scenario != "unsupported"
                    send(id=1, result={"protocolVersion": 1, "_meta": {"steering": {"supported": supported}}})
                    assert read()["method"] == "session/new"
                    send(id=2, result={"sessionId": "migration"})
                    assert read()["method"] == "session/prompt"
                    if scenario in {"unsupported", "invalid_capability"}:
                        Path("complete.txt").write_text("token")
                        send(id=3, result={"stopReason": "end_turn"})
                        sys.stdin.read()
                        sys.exit(0)
                    for index in range(2 if scenario == "multiple" else 1):
                        request = read()
                        assert request["method"] == "_session/steering"
                        assert request["params"]["sessionId"] == "migration"
                        text = request["params"]["prompt"][0]["text"]
                        assert ("日付" if index == 0 else "時刻") in text
                        Path("received.txt").write_text(str(index + 1))
                        match = re.search(r"write exactly ([a-f0-9]+) to `([^`]+)`", text)
                        assert match
                        if scenario == "rejected":
                            send(id=request["id"], error={"code": -32601, "message": "unsupported"})
                        elif scenario == "invalid_response":
                            send(id=request["id"], result=[])
                        else:
                            if scenario.startswith("new_turn"):
                                send(id=3, result={"stopReason": "end_turn"})
                            send(id=request["id"], result={
                                "outcome": "startedNewTurn" if scenario.startswith("new_turn") else "injected",
                            })
                        if scenario == "cancelled":
                            sys.stdin.read()
                            sys.exit(0)
                        if scenario != "multiple" or index == 1:
                            # 元の完了記録が先にあっても、追加指示の反映前には終了させない。
                            Path("complete.txt").write_text("token")
                        if scenario == "new_turn_without_receipt":
                            event_log = Path(os.environ["JUNIE_HOME"]) / "sessions" / "test" / "events.jsonl"
                            event_log.parent.mkdir(parents=True)
                            def result(task):
                                return {"kind": "SessionA2uxEvent", "taskId": task, "event": {
                                    "state": "COMPLETED", "agentEvent": {"kind": "ResultBlockUpdatedEvent",
                                    "agent": {"kind": "MainAgent"}, "cancelled": False, "result": task},
                                }}
                            rows = [
                                {"kind": "UserPromptEvent", "prompt": "original assignment token"},
                                {"kind": "TaskStartedEvent", "taskId": "original"},
                                result("original"),
                                {"kind": "UserPromptEvent", "prompt": text},
                                {"kind": "TaskStartedEvent", "taskId": "followup"},
                                result("followup"),
                            ]
                            event_log.write_text("".join(json.dumps(row) + "\\n" for row in rows))
                            sys.stdin.read()
                            sys.exit(0)
                        if scenario not in {"rejected", "invalid_response", "missing_receipt"}:
                            if scenario != "exit":
                                time.sleep(0.6)
                            Path("Page.cs").write_text("updated " + str(index + 1))
                            Path(match[2]).write_text(match[1])
                    if scenario != "new_turn":
                        send(id=3, result={"stopReason": "end_turn"})
                    if scenario != "exit":
                        sys.stdin.read()
                """)
                launch = junie_cli._launch_process

                def launch_agent(command, **streams):
                    script = f"import os; os.chdir({str(root)!r})\nscenario = {scenario!r}\n" + agent
                    return launch([sys.executable, "-u", "-c", script], **streams)

                with (
                    patch.object(junie_cli, "_launch_process", side_effect=launch_agent),
                    patch.dict("os.environ", {"JUNIE_HOME": str(run_path / "junie-home")}),
                    control_scope(dashboard.control),
                ):
                    command = junie_cli.build_command(root, "original assignment", None)
                    if scenario == "cancelled":
                        with self.assertRaises(OperationCancelled):
                            junie_cli.run_interactive(command, completion, "token", (target,), prompts=LivePrompts(run_path))
                    elif scenario == "new_turn_without_receipt":
                        def run():
                            with control_scope(dashboard.control):
                                junie_cli.run_interactive(command, completion, "token", (target,), prompts=LivePrompts(run_path))

                        with ThreadPoolExecutor(max_workers=1) as pool:
                            future = pool.submit(run)
                            try:
                                with self.assertRaisesRegex(junie_cli.JunieSessionError, "完了記録を残さず終了"):
                                    future.result(timeout=7)
                            finally:
                                dashboard.control.cancelled.set()
                    else:
                        junie_cli.run_interactive(command, completion, "token", (target,), prompts=LivePrompts(run_path))
                self.assertFalse(dashboard.control.junie_steering)
                successful = scenario in {"injected", "new_turn", "exit", "multiple"}
                with editor_queue(run_path) as queue:
                    for prompt in queue["prompts"]:
                        self.assertEqual(prompt["status"], "sending" if prompt["id"] == "initial"
                                         else "applying" if successful else "queued")
                if successful:
                    self.assertEqual(target.read_text(), "updated 2" if scenario == "multiple" else "updated 1")
                    self.assertEqual(len(received), 2 if scenario == "multiple" else 1)
                if scenario in {"unsupported", "invalid_capability"}:
                    self.assertFalse((root / "received.txt").exists())

    def test_full_acp_pipe_keeps_stop_and_send_deadline_responsive(self):
        agent = textwrap.dedent("""
            import json, sys, time
            from pathlib import Path
            def read():
                return json.loads(sys.stdin.buffer.readline())
            def send(**message):
                print(json.dumps(message), flush=True)
            assert read()["method"] == "initialize"
            send(id=1, result={"protocolVersion": 1, "_meta": {"steering": {"supported": True}}})
            assert read()["method"] == "session/new"
            send(id=2, result={"sessionId": "migration"})
            assert read()["method"] == "session/prompt"
            if scenario in {"slow", "exit"}:
                assert sys.stdin.buffer.read(64)
                Path("partial-read").touch()
            while not Path("release").exists():
                time.sleep(.01)
            if scenario == "exit":
                sys.exit(0)
            sys.stdin.buffer.read()
        """)
        for scenario in ("stop", "deadline", "slow", "exit"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                target = root / "Page.cs"
                target.write_text("class Page {}")
                steering = Event()
                dashboard = Dashboard()
                dashboard.begin_operation()
                dashboard.control.trusted_junie_workspaces.add(str(root))
                dashboard.control.on_activity = lambda entry: (
                    steering.set() if "追加指示を送っています" in entry["message"] else None
                )
                with editor_queue(root) as queue:
                    for _ in range(16):
                        add_prompt(queue, {"text": "あ" * 1333})
                processes = []
                launch = junie_cli._launch_process

                def launch_agent(_command, **streams):
                    script = f"import os; os.chdir({str(root)!r})\nscenario = {scenario!r}\n" + agent
                    process = launch([sys.executable, "-u", "-c", script], **streams)
                    processes.append(process)
                    return process

                def run():
                    with control_scope(dashboard.control):
                        command = junie_cli.build_command(root, "assignment token", None)
                        return junie_cli.run_interactive(
                            command, root / "complete.txt", "token", (target,), prompts=LivePrompts(root),
                        )

                with (
                    ThreadPoolExecutor(max_workers=1) as pool,
                    patch.dict(os.environ, {"JUNIE_HOME": str(root / "home")}),
                    patch.object(junie_cli, "ACP_TIMEOUT", 2 if scenario == "deadline" else 30, create=True),
                    patch.object(junie_cli, "_launch_process", side_effect=launch_agent),
                ):
                    future = pool.submit(run)
                    try:
                        self.assertTrue(steering.wait(5))
                        if scenario in {"slow", "exit"}:
                            deadline = time.monotonic() + 5
                            while not (root / "partial-read").exists() and time.monotonic() < deadline:
                                time.sleep(.01)
                            self.assertTrue((root / "partial-read").exists())
                        if scenario in {"stop", "slow"}:
                            self.assertTrue(dashboard.request_stop({"operation_id": dashboard.state["operation_id"]}))
                            with self.assertRaises(OperationCancelled):
                                future.result(timeout=6)
                        else:
                            if scenario == "exit":
                                (root / "release").touch()
                            with self.assertRaises(junie_cli.JunieSessionError):
                                future.result(timeout=6)
                    finally:
                        dashboard.control.cancelled.set()
                        (root / "release").touch()
                        if not future.done():
                            for process in processes:
                                try:
                                    process.kill()
                                except ProcessLookupError:
                                    pass
                self.assertIsNotNone(processes[0].returncode)
                with editor_queue(root) as queue:
                    self.assertEqual({item["status"] for item in queue["prompts"]}, {"queued"})

    def test_pending_human_edits_defer_delivery_and_crashed_delivery_is_recovered(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with editor_queue(root) as queue:
                add_prompt(queue, {"text": "保存した編集を維持してください。"})
                queue["prompts"][0]["status"] = "steering"
                queue["edits"]["Page.cs"] = {"status": "queued"}
            prompts = LivePrompts(root)
            prompts.recover()
            self.assertEqual(prompts.take(), ([], ""))
            with editor_queue(root) as queue:
                self.assertEqual(queue["prompts"][0]["status"], "queued")
                queue["edits"].clear()
            identifiers, text = prompts.take()
            self.assertEqual(len(identifiers), 1)
            self.assertIn("保存した編集", text)
            with editor_queue(root) as queue:
                with self.assertRaises(ValueError):
                    add_prompt(queue, {"id": identifiers[0], "text": "送信後の変更"})
            prompts.finish(identifiers, False)
            self.assertEqual(prompts.take(), (identifiers, text))


if __name__ == "__main__":
    unittest.main()
