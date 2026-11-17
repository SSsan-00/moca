import hashlib
import io
import json
import subprocess
import time
import uuid
from contextlib import redirect_stdout
from threading import Event, Thread
from unittest.mock import patch
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from migration_harness import dashboard, workflow
from migration_harness.editor_queue import editor_queue
from migration_harness.execution import check_cancelled
from migration_harness.run_manager import RunManager
from test_checker_resume_review_dashboard import SavedRunFixture


class JunieConversationTests(SavedRunFixture):
    def setUp(self):
        super().setUp()
        view = dashboard.Dashboard()
        view.publish({"status": "idle", "ready": True})
        self.manager = RunManager(self.config, view)
        view.manager = self.manager
        self.addCleanup(self.manager.close)
        stream = io.StringIO()
        with redirect_stdout(stream):
            server = view.server(self.config, b"", b"")
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        self.url = stream.getvalue().splitlines()[0].removeprefix("Progress dashboard: ")
        origin = urlsplit(self.url)
        self.headers = {"Origin": f"{origin.scheme}://{origin.netloc}", "Content-Type": "application/json"}

    def request(self, endpoint, body=None, run_id=None):
        headers = {**self.headers, "X-Run-ID": run_id or self.run_id}
        with urlopen(Request(self.url + endpoint, None if body is None else json.dumps(body).encode(), headers), timeout=3) as response:
            return response.status, json.load(response)

    def send(self, text, identifier=None):
        identifier = identifier or uuid.uuid4().hex
        status, result = self.request("editor", {
            "kind": "conversation", "run_id": self.run_id, "id": identifier, "text": text,
        })
        self.assertEqual(status, 202)
        self.assertEqual(result["id"], identifier)
        return identifier, result

    def wait_for(self, identifier, status):
        end = time.monotonic() + 6
        while time.monotonic() < end:
            with editor_queue(self.run_path) as queue:
                entry = next((row for row in queue.get("conversation", []) if row["id"] == identifier), {})
            if entry.get("status") == status:
                return entry
            time.sleep(.02)
        self.fail(f"expected {status}, last entry: {entry}")

    def start_build(self):
        self.request("control", {"action": "resume-run", "revision": 0, "run_id": self.run_id})

    def save_edit(self, text):
        _, editor = self.request("editor")
        self.request("editor", {"kind": "save", "run_id": self.run_id, "target": "Example.cs",
                                "base": editor["content"], "content": text,
                                "edit_id": editor["edit"]["id"] if editor["edit"] else None})

    def test_receipt_precedes_interpretation_and_query_does_not_stop_a_build(self):
        running, release = Event(), Event()
        interpreter = self.manager.conversations.advance
        interpreting, interpret = Event(), Event()

        def hold_interpretation(*args):
            interpreting.set()
            self.assertTrue(interpret.wait(5))
            return interpreter(*args)

        def build(command, **kwargs):
            running.set()
            self.assertTrue(release.wait(5))
            check_cancelled()
            kwargs["stdout"].write(b"error CS1002: semicolon expected\n")
            return subprocess.CompletedProcess(command, 1)

        with patch.object(workflow, "run_process", side_effect=build), patch.object(
            self.manager.conversations, "advance", side_effect=hold_interpretation,
        ):
            self.start_build()
            try:
                self.assertTrue(running.wait(5))
                identifier, received = self.send("今何してる？")
                self.assertEqual(received["status"], "received")
                self.assertTrue(interpreting.wait(5))
                self.assertEqual(self.send("今何してる？", identifier)[1]["status"], "received")
                interpret.set()
                self.assertIn("ビルド中", self.wait_for(identifier, "done")["message"])
                self.assertFalse(self.manager.get(self.run_id).control.cancelled.is_set())
            finally:
                interpret.set()
                release.set()
            end = time.monotonic() + 6
            while self.manager.busy and time.monotonic() < end:
                time.sleep(.02)
            self.assertFalse(self.manager.busy)
            reason, _ = self.send("前回のビルドはなぜ落ちた？")
            self.assertIn("CS1002", self.wait_for(reason, "done")["logs"][0]["text"])
            history = self.request("editor")[1]["conversation"]
            self.assertEqual([item["id"] for item in history], [identifier, reason])
            self.assertEqual(self.request("editor", run_id=self.run_id)[1]["conversation"], history)

    def test_clarification_waits_then_runs_build_and_checker(self):
        mocked, calls = self.mock_commands(("dotnet", 0, b"build ok\n"), ("checker", 0, b"check ok\n"))
        with mocked:
            instruction, _ = self.send("もう一回お願い")
            self.assertIn("ビルドだけ", self.wait_for(instruction, "clarification")["message"])
            self.assertEqual(calls, [])
            self.send("チェッカーまで")
            entry = self.wait_for(instruction, "done")
            self.assertIn("チェッカー", entry["message"])
            self.assertEqual([log["kind"] for log in entry["logs"]], ["build", "checker"])
        self.assertEqual(len(calls), 2)

    def test_build_only_failure_does_not_start_checker_or_a_repair(self):
        self.config["max_repair_attempts"] = 2
        mocked, calls = self.mock_commands(("dotnet", 1, b"Example.cs(1,1): error CS1002: missing semicolon\n"))
        with mocked, patch.object(workflow, "_attempt_repair") as repair:
            identifier, _ = self.send("ビルドして")
            entry = self.wait_for(identifier, "done")
            self.assertIn("ビルド失敗", entry["message"])
            repair.assert_not_called()
            self.send("ビルドして", identifier)
        self.assertEqual(calls, [["dotnet", "build"]])

    def test_stop_and_rebuild_uses_saved_edit_once(self):
        running = Event()
        builds = []

        def work(command, **kwargs):
            self.assertEqual(command, ["dotnet", "build"])
            builds.append(self.target.read_text(encoding="utf-8-sig"))
            if len(builds) == 1:
                running.set()
                end = time.monotonic() + 6
                while time.monotonic() < end:
                    check_cancelled()
                    time.sleep(.01)
                self.fail("the first build was not stopped")
            kwargs["stdout"].write(b"new successful build\n")
            return subprocess.CompletedProcess(command, 0)

        with patch.object(workflow, "run_process", side_effect=work):
            self.start_build()
            self.assertTrue(running.wait(5))
            edited = "class Saved {}\n"
            self.save_edit(edited)
            text = "今のビルドを止めて、保存した版でやり直して"
            identifier, _ = self.send(text)
            self.send(text, identifier)
            entry = self.wait_for(identifier, "done")
            self.assertIn("ビルド成功", entry["message"])
            digest = hashlib.sha256(self.target.read_bytes()).hexdigest()
            self.assertEqual(entry["built_targets"], {"Example.cs": digest})
            self.assertEqual(entry["logs"][0]["text"], "new successful build\n")
        self.assertEqual(builds, ["class Original {}\n", edited])

    def test_natural_completion_waits_and_rebuilds_after_checker(self):
        running, release = Event(), Event()
        calls = []

        def work(command, **kwargs):
            calls.append(command[0])
            if len(calls) == 1:
                running.set()
                self.assertTrue(release.wait(5))
            kwargs["stdout"].write(b"ok\n")
            return subprocess.CompletedProcess(command, 0)

        with patch.object(workflow, "run_process", side_effect=work):
            self.start_build()
            try:
                self.assertTrue(running.wait(5))
                self.save_edit("class Saved {}\n")
                identifier, _ = self.send("保存した版で再ビルドして")
                self.assertIn("待ち", self.wait_for(identifier, "waiting").get("message", ""))
                self.assertFalse(self.manager.get(self.run_id).control.cancelled.is_set())
            finally:
                release.set()
            self.wait_for(identifier, "done")
        self.assertEqual(calls, ["dotnet", "fixture-checker", "dotnet"])
        self.assertEqual(self.target.read_text(encoding="utf-8-sig"), "class Saved {}\n")

    def test_restart_recovers_unexecuted_but_never_restarts_started_instructions(self):
        self.manager.limit = 0
        waiting, _ = self.send("ビルドして")
        self.wait_for(waiting, "waiting")
        with editor_queue(self.run_path) as queue:
            queue["conversation"].append({"id": "previous", "text": "ビルドして", "status": "running"})
        self.manager.close()
        manager = RunManager(self.config, dashboard.Dashboard())
        self.addCleanup(manager.close)
        mocked, calls = self.mock_commands(("dotnet", 0, b"build ok\n"))
        with mocked:
            with manager.condition:
                manager.start_pending()
                manager.conversations.start()
            self.wait_for(waiting, "done")
            self.wait_for("previous", "unknown")
        self.assertEqual(calls, [["dotnet", "build"]])

    def test_newer_saved_edit_invalidates_a_waiting_rebuild(self):
        self.manager.limit = 0
        self.save_edit("class First {}\n")
        with patch.object(workflow, "run_process") as run:
            identifier, _ = self.send("保存した版でビルドして")
            self.wait_for(identifier, "waiting")
            self.save_edit("class Second {}\n")
            self.manager.limit = 5
            self.manager.start_pending()
            self.assertIn("更新されました", self.wait_for(identifier, "failed")["message"])
            run.assert_not_called()
        self.assertIn("class Second", self.request("editor")[1]["edit"]["content"])

    def test_checker_repair_passes_instruction_to_junie_and_respects_run_limit(self):
        self.config["max_repair_attempts"] = 1
        mocked, calls = self.mock_commands(
            ("dotnet", 0, b"ok\n"), ("checker", 1, b"NEW Example.cs:2 warning: unresolved\n"),
        )
        text = "この指摘は旧 PHP と同じ既定値にして、チェッカーをもう一度"
        with mocked, patch.object(workflow, "_repair_target") as junie:
            identifier, _ = self.send(text)
            self.wait_for(identifier, "done")
            self.assertIn("previous finding", junie.call_args.args[5])
            self.assertEqual(self.record()["repair_history"][0]["instruction"], text)
            second, _ = self.send(text)
            self.assertIn("上限", self.wait_for(second, "failed")["message"])
        self.assertEqual(len(calls), 2)
        junie.assert_called_once()

    def test_build_with_conditional_repair_uses_the_new_build_diagnostic(self):
        self.config["max_repair_attempts"] = 1
        diagnostic = b"Example.cs(1,1): error CS1002: Current build diagnostic\n"
        mocked, calls = self.mock_commands(("dotnet", 1, diagnostic), ("dotnet", 0, b"ok\n"))
        self.save_edit("class Corrected {}\n")
        with mocked, patch.object(workflow.junie_cli, "require_interactive_cli"), patch.object(workflow, "_repair_target") as repair:
            identifier, _ = self.send("保存した版でビルドして。まだエラーがあれば直して")
            self.assertIn("ビルド成功", self.wait_for(identifier, "done")["message"])
            self.assertIn("Current build diagnostic", repair.call_args.args[5])
        self.assertEqual(calls, [["dotnet", "build"], ["dotnet", "build"]])

    def test_conversation_and_confirmation_are_isolated_between_runs(self):
        other = str(uuid.uuid4())
        path = self.run_path.parent / other
        worktree = self.worktree.parent / other
        path.mkdir()
        worktree.mkdir()
        (path / "run.json").write_text(json.dumps({
            **self.record(), "run_id": other, "worktree_path": str(worktree), "status": "prepared",
        }), encoding="utf-8")
        identifier, _ = self.send("もう一回お願い")
        self.wait_for(identifier, "clarification")
        self.assertEqual(self.request("editor", run_id=other)[1]["conversation"], [])
        self.assertEqual([entry["id"] for entry in self.request("editor")[1]["conversation"]], [identifier])

    def test_operator_answers_junie_with_the_same_input_field(self):
        running, release = Event(), Event()

        def wait_build(command, **kwargs):
            running.set()
            self.assertTrue(release.wait(5))
            return subprocess.CompletedProcess(command, 1)

        with patch.object(workflow, "run_process", side_effect=wait_build):
            self.start_build()
            try:
                self.assertTrue(running.wait(5))
                view = self.manager.get(self.run_id)
                answer = view.show_junie_request({
                    "title": "プロジェクトで続行しますか？", "options": [{"id": "yes", "label": "続行する"}],
                })
                identifier, _ = self.send("続行する")
                self.assertEqual(answer.result(timeout=3), "yes")
                self.assertEqual(self.wait_for(identifier, "done")["question"], "プロジェクトで続行しますか？")
            finally:
                release.set()

    def test_live_repair_instruction_uses_existing_steering_queue(self):
        from migration_harness.editor_queue import LivePrompts, finish_prompts

        view = self.manager.get(self.run_id)
        view.begin_operation()
        view.control.junie_steering = True
        self.save_record({**self.record(), "status": "repairing"})
        view.observe(self.record())
        identifier, _ = self.send("注文日の表示を既存ヘルパーに合わせて")
        self.assertIn("実行中の Junie", self.wait_for(identifier, "steering")["message"])
        live = LivePrompts(self.run_path)
        identifiers, text = live.take()
        self.assertEqual(identifiers, [identifier])
        self.assertIn("既存ヘルパー", text)
        live.finish(identifiers, True)
        with editor_queue(self.run_path) as queue:
            finish_prompts(queue, True)
        self.wait_for(identifier, "done")

    def test_stop_after_natural_build_exit_waits_without_cancelling_checker(self):
        completed, release = Event(), Event()
        script_phase = workflow._run_script_phase
        calls = []

        def after_build(config, phase, *args, **kwargs):
            if phase == "after_build" and calls == ["dotnet"]:
                completed.set()
                self.assertTrue(release.wait(5))
            return script_phase(config, phase, *args, **kwargs)

        def work(command, **kwargs):
            calls.append(command[0])
            return subprocess.CompletedProcess(command, 0)

        with patch.object(workflow, "_run_script_phase", side_effect=after_build), patch.object(
            workflow, "run_process", side_effect=work,
        ):
            self.start_build()
            try:
                self.assertTrue(completed.wait(5))
                identifier, _ = self.send("今のビルドを止めて、保存した版でやり直して")
                self.assertIn("終了を待ちます", self.wait_for(identifier, "waiting")["message"])
                self.assertFalse(self.manager.get(self.run_id).control.cancelled.is_set())
            finally:
                release.set()
            self.wait_for(identifier, "done")
        self.assertEqual(calls, ["dotnet", "fixture-checker", "dotnet"])

    def test_conflicting_saved_edit_is_preserved_and_does_not_build(self):
        self.target.write_bytes(b"class Changed {}\n")
        with editor_queue(self.run_path) as queue:
            queue["edits"]["Example.cs"] = {
                "id": "saved", "status": "queued", "base": "class Original {}\n", "content": "class Operator {}\n",
            }
        with patch.object(workflow, "run_process") as run:
            identifier, _ = self.send("保存した版でビルドして")
            self.assertIn("Example.cs", self.wait_for(identifier, "failed")["message"])
            run.assert_not_called()
        self.assertEqual(self.target.read_bytes(), b"class Changed {}\n")
        self.assertEqual(self.request("editor")[1]["edit"]["status"], "conflict")

    def test_cancelling_queued_verification_cannot_enqueue_it_again(self):
        self.manager.limit = 0
        with patch.object(workflow, "run_process") as run:
            identifier, _ = self.send("チェッカーをもう一度")
            self.wait_for(identifier, "waiting")
            self.request("control", {"action": "cancel-queued", "revision": 0, "run_id": self.run_id})
            self.wait_for(identifier, "cancelled")
            self.manager.limit = 5
            self.manager.start_pending()
            self.assertFalse(self.manager.busy)
            run.assert_not_called()
