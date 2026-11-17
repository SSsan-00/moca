import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from migration_harness import junie_cli
from migration_harness.dashboard import Dashboard
from migration_harness.editor_queue import LivePrompts, add_prompt, editor_queue
from migration_harness.execution import OperationCancelled, control_scope


class JunieSteeringTests(unittest.TestCase):
    def test_live_delivery_capability_fallback_completion_races_and_cancellation(self):
        for scenario in ("injected", "new_turn", "exit", "multiple", "unsupported", "invalid_capability",
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
                    import json, re, sys, time
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
                            if scenario == "new_turn":
                                send(id=3, result={"stopReason": "end_turn"})
                            send(id=request["id"], result={
                                "outcome": "startedNewTurn" if scenario == "new_turn" else "injected",
                            })
                        if scenario == "cancelled":
                            sys.stdin.read()
                            sys.exit(0)
                        if scenario != "multiple" or index == 1:
                            # 元の完了記録が先にあっても、追加指示の反映前には終了させない。
                            Path("complete.txt").write_text("token")
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
