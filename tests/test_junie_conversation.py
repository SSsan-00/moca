import hashlib
import io
import json
import subprocess
import time
import uuid
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from threading import Event, Thread
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from migration_harness import dashboard, workflow
from migration_harness.editor_queue import editor_queue
from migration_harness.execution import check_cancelled, current_operation_id
from migration_harness.junie_conversation import _intent
from migration_harness.run_manager import RunManager
from test_checker_resume_review_dashboard import SavedRunFixture


class JunieConversationTests(SavedRunFixture):
    def setUp(self):
        super().setUp()
        self.start_server()

    def start_server(self):
        view = dashboard.Dashboard()
        view.publish({"status": "idle", "ready": True})
        self.manager = RunManager(self.config, view)
        view.manager = self.manager
        stream = io.StringIO()
        with redirect_stdout(stream):
            server = view.server(self.config, b"", b"")
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.server_cleanup = ExitStack()
        self.addCleanup(self.server_cleanup.close)
        self.server_cleanup.callback(self.manager.close)
        self.server_cleanup.callback(server.server_close)
        self.server_cleanup.callback(thread.join)
        self.server_cleanup.callback(server.shutdown)
        self.url = stream.getvalue().splitlines()[0].removeprefix("Progress dashboard: ")
        origin = urlsplit(self.url)
        self.headers = {"Origin": f"{origin.scheme}://{origin.netloc}", "Content-Type": "application/json"}

    def restart_server(self, **settings):
        self.server_cleanup.close()
        self.config = {**self.config, **settings}
        self.start_server()

    def request(self, endpoint, body=None, run_id=None):
        headers = {**self.headers, "X-Run-ID": run_id or self.run_id}
        with urlopen(Request(self.url + endpoint, None if body is None else json.dumps(body).encode(), headers), timeout=3) as response:
            return response.status, json.load(response)

    def send(self, text, identifier=None, *, run_id=None):
        run_id = run_id or self.run_id
        identifier = identifier or uuid.uuid4().hex
        status, result = self.request("editor", {
            "kind": "conversation", "run_id": run_id, "id": identifier, "text": text,
        }, run_id=run_id)
        self.assertEqual(status, 202)
        self.assertEqual(result["id"], identifier)
        return identifier, result

    def wait_for(self, identifier, status, *, run_path=None):
        end = time.monotonic() + 6
        while time.monotonic() < end:
            with editor_queue(run_path or self.run_path) as queue:
                entry = next((row for row in queue.get("conversation", []) if row["id"] == identifier), {})
            if entry.get("status") == status:
                return entry
            time.sleep(.02)
        self.fail(f"expected {status}, last entry: {entry}")

    def start_build(self):
        self.request("control", {"action": "resume-run", "revision": 0, "run_id": self.run_id})

    def save_edit(self, text, target="Example.cs"):
        _, editor = self.request("editor?" + urlencode({"target": target}))
        self.request("editor", {"kind": "save", "run_id": self.run_id, "target": target,
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

    def test_verification_status_questions_only_read_saved_information(self):
        self.save_record({**self.record(), "status": "build_failed", "build_exit_code": 1,
                          "verification": {"target_hashes": {"Example.cs": "previous"}}})
        self.build_log.write_text("error CS1002: missing semicolon\n", encoding="utf-8")
        with editor_queue(self.run_path) as queue:
            queue["edits"]["Example.cs"] = {
                "id": "draft", "status": "queued", "base": "class Original {}\n", "content": "class Draft {}\n",
            }
        original_record = self.record()
        original_target = self.target.read_bytes()
        original_edit = self.request("editor")[1]["edit"]
        self.assertEqual(original_edit["status"], "queued")
        queries = {
            "なぜビルドが失敗したの？": "CS1002",
            "ビルドは終わった？": "CS1002",
            "ビルドした？": "CS1002",
            "前回のビルドの結果を教えて": "CS1002",
            "チェッカーは成功した？": "previous finding",
            "チェッカー回った？": "previous finding",
            "チェッカーはもう実行したのですか": "previous finding",
            "Why did the checker fail?": "previous finding",
        }
        with patch.object(workflow, "run_process") as run, patch.object(self.manager, "enqueue") as enqueue:
            for query, detail in queries.items():
                with self.subTest(query=query):
                    identifier, _ = self.send(query)
                    self.assertIn(detail, self.wait_for(identifier, "done")["logs"][0]["text"])
            run.assert_not_called()
            enqueue.assert_not_called()
        self.assertEqual(self.record(), original_record)
        self.assertEqual(self.target.read_bytes(), original_target)
        self.assertEqual(self.request("editor")[1]["edit"], original_edit)
        with editor_queue(self.run_path) as queue:
            self.assertEqual(queue["prompts"], [])

    def test_an_ambiguous_build_mention_waits_for_an_explicit_answer(self):
        mocked, calls = self.mock_commands(("dotnet", 0, b"ok\n"))
        with mocked:
            identifier, _ = self.send("ビルドについて")
            self.wait_for(identifier, "clarification")
            self.assertEqual(calls, [])
            self.save_edit("class ReviewedAfterQuestion {}\n")
            self.send("ビルドだけ")
            self.assertEqual(self.wait_for(identifier, "done")["result_status"], "build_succeeded")
        self.assertEqual(calls, [["dotnet", "build"]])
        self.assertEqual(self.target.read_text(encoding="utf-8-sig"), "class ReviewedAfterQuestion {}\n")

    def fill_conversation(self):
        (self.run_path / "agent-workspace").mkdir()
        for index in range(15):
            identifier, _ = self.send(f"注文日の表示を既存ヘルパーに合わせて {index}")
            self.wait_for(identifier, "steering")
        with patch.object(self.manager.conversations, "advance", return_value=True):
            query, _ = self.send("今何してる？")
            identifier, _ = self.send("もう一回お願い")
        self.wait_for(query, "done")
        self.wait_for(identifier, "clarification")
        return identifier

    def assert_conversation_rejected(self, text, detail, identifier=None):
        with self.assertRaises(HTTPError) as rejected:
            self.send(text, identifier)
        with rejected.exception as response:
            self.assertEqual(response.code, 409)
            self.assertIn(detail, response.read().decode())

    def test_full_conversation_allows_status_and_existing_answer(self):
        identifier = self.fill_conversation()
        self.restart_server()
        self.assertEqual(self.send("もう一回お願い", identifier)[1]["status"], "clarification")
        self.assert_conversation_rejected("ビルドだけ", "同じ指示 ID", identifier)
        self.assert_conversation_rejected("ビルドだけ。注文日の表示も直して", "最大件数")
        query, _ = self.send("チェッカーは成功した？")
        self.assertIn("previous finding", self.wait_for(query, "done")["logs"][0]["text"])
        self.wait_for(identifier, "clarification")

        self.manager.limit = 0
        mocked, calls = self.mock_commands(("dotnet", 0, b"ok\n"))
        with mocked:
            answer, _ = self.send("ビルドだけ")
            self.wait_for(answer, "done")
            self.wait_for(identifier, "waiting")
            self.assertEqual(self.send("ビルドだけ", answer)[1]["status"], "done")
            self.assert_conversation_rejected("チェッカーまで", "同じ指示 ID", answer)
            self.assert_conversation_rejected("チェッカーをもう一度", "最大件数")
            self.manager.limit = 5
            self.manager.start_pending()
            self.assertEqual(self.wait_for(identifier, "done")["result_status"], "build_succeeded")
        self.assertEqual(calls, [["dotnet", "build"]])

    def test_full_conversation_runs_checker_once_after_reload_and_answer(self):
        identifier = self.fill_conversation()
        self.restart_server()
        history = self.request("editor")[1]["conversation"]
        self.assertEqual([row["id"] for row in history if row["status"] == "clarification"], [identifier])
        self.assertEqual(sum(row["status"] == "steering" for row in history), 15)
        self.assert_conversation_rejected("新規作業を追加して", "最大件数")
        query, _ = self.send("チェッカーは成功した？")
        self.wait_for(query, "done")
        mocked, calls = self.mock_commands(("dotnet", 0, b""), ("checker", 0, b""))
        with mocked:
            answer, _ = self.send("チェッカーまで")
            self.wait_for(answer, "done")
            self.assertEqual(self.send("チェッカーまで", answer)[1]["status"], "done")
            self.assertEqual(self.wait_for(identifier, "done")["result_status"], "checker_succeeded")
            self.wait_until_idle()
        self.assertEqual(calls, [["dotnet", "build"], ["fixture-checker"]])
        self.assertEqual(sum(row["status"] == "steering" for row in self.request("editor")[1]["conversation"]), 15)

    def test_full_conversation_allows_cancellation_and_frees_one_slot(self):
        identifier = self.fill_conversation()
        self.restart_server()
        with patch.object(workflow, "run_process") as run:
            query, _ = self.send("今何してる？")
            self.wait_for(query, "done")
            cancellation, _ = self.send("キャンセル")
            self.wait_for(cancellation, "done")
            self.wait_for(identifier, "cancelled")
            self.restart_server()
            self.assertEqual(self.send("もう一回お願い", identifier)[1]["status"], "cancelled")
            replacement, _ = self.send("ビルドについて")
            self.wait_for(replacement, "clarification")
            self.assert_conversation_rejected("新規作業を追加して", "最大件数")
            view = self.manager.get(self.run_id)
            view.begin_operation()
            reply = view.show_junie_request({
                "title": "作業を続けますか？", "options": [{"id": "yes", "label": "続ける"}],
            })
            response, _ = self.send("続ける")
            self.wait_for(response, "done")
            self.assertEqual(reply.result(timeout=3), "yes")
            self.wait_for(replacement, "clarification")
            run.assert_not_called()

    def test_an_answer_cannot_be_applied_to_a_replacement_question(self):
        original, _ = self.send("もう一回お願い")
        self.wait_for(original, "clarification")
        replacement = uuid.uuid4().hex
        with patch.object(workflow, "run_process") as run:
            with patch.object(self.manager.conversations, "advance", return_value=True):
                answer, _ = self.send("ビルドだけ")
                with editor_queue(self.run_path) as queue:
                    question = queue["conversation"][0]
                    queue["conversation"].append({**question, "id": replacement})
                    question["status"] = "cancelled"
            self.assertIn("更新されました", self.wait_for(answer, "failed")["message"])
            self.wait_for(replacement, "clarification")
            run.assert_not_called()

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
        self.assertFalse(workflow.migration_ledger(self.config, self.run_id)["completion"]["verification_passed"])

    def test_build_only_does_not_restore_a_null_cli_checker(self):
        default = self.config["checker_command"]
        toml = self.config.copy()
        self.config.update(checker_from_cli=True, toml_checker_command=default, checker_command=["cli-checker"])
        workflow.review_migration_ranges(self.config, self.run_id, ["1:1"], "confirmed", "Reviewed",
                                         confirmed_targets=[self.target.name])
        mocked, calls = self.mock_commands(("dotnet", 0, b"ok\n"), ("dotnet", 0, b"ok\n"), ("checker", 0, b""))
        with mocked:
            identifier, _ = self.send("ビルドして")
            self.wait_for(identifier, "done")
            for settings in (self.config, toml):
                completion = workflow.migration_ledger(settings, self.run_id)["completion"]
                self.assertFalse(completion["verification_passed"])
                self.assertFalse(completion["complete"])
            record = self.record()
            verification = record["verification"]
            self.assertEqual(verification["configured_checker_command"], ["cli-checker"])
            self.assertIsNone(verification["checker_command"])
            self.assertFalse(workflow.can_resume_run({**toml, "checker_command": None}, record))
            del verification["configured_checker_command"]
            self.save_record(record)
            self.assertFalse(workflow.migration_ledger(toml, self.run_id)["completion"]["verification_passed"])
            identifier, _ = self.send("チェッカーをもう一度")
            self.wait_for(identifier, "done")
        self.assertEqual(calls, [["dotnet", "build"], ["dotnet", "build"], ["cli-checker"]])
        for settings in (self.config, toml):
            completion = workflow.migration_ledger(settings, self.run_id)["completion"]
            self.assertTrue(completion["complete"], completion)
        self.target.write_text("class Changed {}\n", encoding="utf-8")
        self.assertFalse(workflow.migration_ledger(toml, self.run_id)["completion"]["verification_passed"])

    def test_restarted_conversation_cannot_drop_the_cli_checker_requirement(self):
        override = ["cli-checker"]
        self.config.update(checker_from_cli=True, toml_checker_command=None, checker_command=override)
        workflow.review_migration_ranges(self.config, self.run_id, ["1:1"], "confirmed", "Reviewed",
                                         confirmed_targets=[self.target.name])
        mocked, calls = self.mock_commands(("dotnet", 0, b"ok"))
        with mocked:
            first, _ = self.send("ビルドして")
            self.wait_for(first, "done")
        self.assertEqual(calls, [["dotnet", "build"]])
        saved_record = self.record()
        self.assertEqual(saved_record["verification"]["configured_checker_command"], override)
        self.assertIsNone(saved_record["verification"]["checker_command"])

        self.restart_server(checker_from_cli=False, checker_command=None)
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                if legacy:
                    record = self.record()
                    del record["verification"]["configured_checker_command"]
                    self.save_record(record)
                before = self.record()
                self.assertFalse(workflow.can_resume_run(self.config, before))
                mocked, calls = self.mock_commands(("dotnet", 0, b"ok"))
                with mocked:
                    second, _ = self.send("ビルドして")
                    self.assertIn("チェッカー", self.wait_for(second, "failed")["message"])
                self.assertEqual(calls, [])
                self.assertEqual(self.record(), before)
                completion = self.request("migration-ledger")[1]["completion"]
                self.assertEqual(completion, {"complete": False, "verification_passed": False,
                                              "reviewed": 1, "total": 1})

        self.restart_server(checker_from_cli=True, checker_command=override)
        mocked, calls = self.mock_commands(("dotnet", 0, b"ok"), ("checker", 0, b""))
        with mocked:
            identifier, _ = self.send("チェッカーをもう一度")
            self.assertEqual(self.wait_for(identifier, "done")["result_status"], "checker_succeeded")
        self.assertEqual(calls, [["dotnet", "build"], override])
        self.assertTrue(self.request("migration-ledger")[1]["completion"]["complete"])

    def test_build_only_verifies_a_run_without_a_checker(self):
        record = self.record()
        record.update(status="junie_finished", build_exit_code=None)
        record.pop("checker_exit_code", None)
        self.save_record(record)
        self.config["checker_command"] = None
        mocked, _ = self.mock_commands(("dotnet", 0, b"ok\n"), ("dotnet", 0, b"ok\n"))
        with mocked:
            identifier, _ = self.send("ビルドして")
            self.wait_for(identifier, "done")
            self.assertTrue(workflow.migration_ledger(self.config, self.run_id)["completion"]["verification_passed"])
            record = self.record()
            record["verification"].pop("configured_checker_command")
            self.save_record(record)
            self.assertFalse(workflow.migration_ledger(self.config, self.run_id)["completion"]["verification_passed"])
            identifier, _ = self.send("ビルドして")
            self.wait_for(identifier, "done")
        self.assertTrue(workflow.migration_ledger(self.config, self.run_id)["completion"]["verification_passed"])

    def test_stop_and_rebuild_uses_saved_edit_once(self):
        toml = self.config.copy()
        self.config.update(checker_from_cli=True, toml_checker_command=toml["checker_command"],
                           checker_command=["cli-checker"])
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
            self.wait_until_idle()
            self.restart_server()
            self.assertEqual(self.send(text, identifier)[1]["built_targets"], entry["built_targets"])
        self.assertEqual(builds, ["class Original {}\n", edited])
        self.assertEqual(self.record()["verification"]["configured_checker_command"], ["cli-checker"])
        self.assertIsNone(self.record()["verification"]["checker_command"])
        self.assertFalse(workflow.migration_ledger(toml, self.run_id)["completion"]["verification_passed"])

    def test_interrupted_followup_keeps_required_checker_across_restart(self):
        self._check_interrupted_followup(["cli-checker"])

    def test_interrupted_followup_can_resume_when_no_checker_was_configured(self):
        self._check_interrupted_followup(None)

    def _check_interrupted_followup(self, checker):
        self.target.write_text("class Original {}\n", encoding="utf-8-sig")
        record = self.record()
        record.update(status="junie_finished", build_exit_code=None)
        record.pop("checker_exit_code", None)
        self.save_record(record)
        (self.run_path / "agent-workspace").mkdir()
        self.config.update(checker_from_cli=checker is not None, toml_checker_command=None, checker_command=checker)
        workflow.review_migration_ranges(self.config, self.run_id, ["1:1"], "confirmed", "Reviewed",
                                         confirmed_targets=[self.target.name])
        mocked, calls = self.mock_commands(("dotnet", 0, b"ok"))
        with mocked:
            first, _ = self.send("ビルドして")
            self.wait_for(first, "done")
        self.wait_until_idle()
        self.assertEqual(calls, [["dotnet", "build"]])

        followup, _ = self.send("既存の実装をそのまま維持してください")
        self.wait_for(followup, "steering")
        running, release = Event(), Event()

        def interrupt(*args, **kwargs):
            running.set()
            self.assertTrue(release.wait(5))
            check_cancelled()

        with patch.object(workflow.junie_cli, "require_interactive_cli"), \
                patch.object(workflow.junie_cli, "run_interactive", side_effect=interrupt) as junie:
            self.start_build()
            try:
                self.assertTrue(running.wait(5))
                self.assertEqual(self.record()["status"], "editor_followup")
                self.stop_operation()
            finally:
                release.set()
            self.wait_until_idle()
            junie.assert_called_once()
        self.assertEqual(self.record()["status"], "editor_checkpoint")
        self.assertEqual(self.record()["verification"]["configured_checker_command"], checker)
        self.assertEqual(self.request("prompts")[1]["prompts"][0]["status"], "queued")

        self.restart_server(checker_from_cli=False, checker_command=None)
        if checker is not None:
            for legacy in (False, True):
                with self.subTest(legacy=legacy):
                    if legacy:
                        record = self.record()
                        del record["verification"]["configured_checker_command"]
                        self.save_record(record)
                    before = self.record()
                    self.assertFalse(workflow.can_resume_run(self.config, before))
                    with patch.object(workflow, "run_process") as run, \
                            patch.object(workflow.junie_cli, "run_interactive") as junie:
                        with self.assertRaises(HTTPError) as rejected:
                            self.start_build()
                        with rejected.exception as response:
                            self.assertEqual(response.code, 409)
                        self.assertFalse(self.manager.get(self.run_id).state["resumable"])
                        with self.assertRaisesRegex(ValueError, "必要なチェッカー"):
                            workflow.resume_with_junie(self.config, self.run_id)
                        requested, _ = self.send("ビルドして")
                        self.assertIn("チェッカー", self.wait_for(requested, "failed")["message"])
                        self.wait_until_idle()
                        run.assert_not_called()
                        junie.assert_not_called()
                    self.assertEqual(self.record(), before)
                    completion = self.request("migration-ledger")[1]["completion"]
                    self.assertFalse(completion["complete"])
                    self.assertFalse(completion["verification_passed"])
            self.restart_server(checker_from_cli=True, checker_command=checker)

        def complete(command, completion, token, targets, **kwargs):
            completion.write_text(token, encoding="utf-8")
            return 0

        responses = [("dotnet", 0, b"ok")] if checker is None else [("dotnet", 0, b"ok"), ("checker", 0, b"")]
        mocked, calls = self.mock_commands(*responses)
        with mocked, patch.object(workflow.junie_cli, "require_interactive_cli"), \
                patch.object(workflow.junie_cli, "run_interactive", side_effect=complete) as junie:
            self.start_build()
            self.wait_until_idle()
            junie.assert_called_once()
        self.manager.conversations.notify(self.run_id)
        self.wait_for(followup, "done")
        self.assertEqual(calls, [["dotnet", "build"]] + ([checker] if checker is not None else []))
        self.assertEqual(self.record()["status"], "checker_succeeded" if checker else "build_succeeded")
        self.assertTrue(self.request("migration-ledger")[1]["completion"]["complete"])

    def wait_until_idle(self):
        with self.manager.condition:
            self.assertTrue(self.manager.condition.wait_for(lambda: self.run_id not in self.manager.jobs, timeout=6))

    def stop_operation(self):
        operation = self.manager.get(self.run_id).state["operation_id"]
        status, _ = self.request("control", {"action": "stop", "run_id": self.run_id,
                                             "operation_id": operation, "revision": 0})
        self.assertEqual(status, 202)

    def test_prefaced_unresolved_condition_requires_confirmation(self):
        self.save_edit("class Saved {}\n")
        saved = self.target.read_bytes()
        mocked, calls = self.mock_commands(("dotnet", 0, b"ok"), ("checker", 0, b""))
        with mocked:
            condition, _ = self.send("変更を保存しました。環境が整ったらチェッカーを実行して")
            self.assertIn("実行条件を確定できません", self.wait_for(condition, "clarification")["message"])
        self.assertEqual(calls, [])
        self.assertEqual(self.target.read_bytes(), saved)
        self.assertEqual(self.record()["status"], "checker_needs_review")

    def test_stopping_before_build_does_not_use_the_preceding_operations_exit_code(self):
        self.config["scripts"] = {"before_build": [["before-build"]]}
        hook, release = Event(), Event()
        calls = []
        original = self.target.read_bytes()

        def work(command, **kwargs):
            calls.append(command[0])
            if calls == ["before-build", "dotnet", "before-build"]:
                hook.set()
                self.assertTrue(release.wait(5))
                check_cancelled()
            return subprocess.CompletedProcess(command, 0)

        with patch.object(workflow, "run_process", side_effect=work):
            previous, _ = self.send("ビルドして")
            self.assertEqual(self.wait_for(previous, "done")["result_status"], "build_succeeded")
            self.wait_until_idle()
            active, _ = self.send("ビルドして")
            try:
                self.assertTrue(hook.wait(5))
                self.assertEqual(self.record()["script_phase"], "before_build")
                self.assertEqual(self.record()["build_exit_code"], 0)
                self.save_edit("class Saved {}\n")
                pending, _ = self.send("今のビルドが終わったら、チェッカーを実行して")
                self.wait_for(pending, "waiting")
                self.stop_operation()
            finally:
                release.set()
            self.wait_for(active, "failed")
            self.assertIn("終了を確認", self.wait_for(pending, "failed")["message"])
            self.wait_until_idle()
        self.assertEqual(calls, ["before-build", "dotnet", "before-build"])
        self.assertEqual(self.target.read_bytes(), original)
        self.assertEqual(self.request("editor")[1]["edit"]["status"], "queued")
        self.assertEqual(self.record()["status"], "script_failed")

    def test_conditional_verification_intents(self):
        cases = {
            "今のビルドが終わったら、保存した版でチェッカーを実行して": "after_build_checker",
            "ビルドの終了後に保存版でビルドしてください": "after_build_build",
            "変更を保存しました。今のビルドが終わったら、保存した版でチェッカーを実行して": "after_build_checker",
            "準備しました。\n環境が整ったらチェッカーを実行して": "clarify",
            "手元で確認しました。\nなぜビルドが失敗したの？": "build_status",
            "ビルドが完了したらチェッカーを実行してもらえますか？": "after_build_checker",
            "ビルドが終わったらチェッカーを実行するの？": "clarify",
            "ビルドが通ったらチェッカーを実行して": "clarify",
            "環境が整ったらチェッカーを実行して": "clarify",
            "ビルドが終わったら差分だけ確認して": "clarify",
            "なぜビルドが失敗したの？": "build_status",
            "ビルドは終わった？": "build_status",
            "チェッカーは成功した？": "checker_status",
        }
        for message, expected in cases.items():
            with self.subTest(message=message):
                self.assertEqual(_intent(message, "building"), expected)

    def test_preupgrade_query_receipt_cannot_start_a_conditional_checker_without_a_saved_version(self):
        identifier = uuid.uuid4().hex
        with editor_queue(self.run_path) as queue:
            queue["conversation"] = [{
                "id": identifier, "text": "ビルドが終わったら、チェッカーを実行して",
                "status": "received", "query_intent": "checker_status",
            }]
        with patch.object(workflow, "run_process") as run:
            self.manager.conversations.notify(self.run_id)
            self.assertIn("送信し直してください", self.wait_for(identifier, "failed")["message"])
            run.assert_not_called()

    def test_conditional_checker_waits_for_build_and_verifies_saved_edit(self):
        for first_exit in (0, 1, None):
            with self.subTest(first_exit=first_exit):
                self._check_conditional_checker(first_exit)

    def _check_conditional_checker(self, first_exit):
        running, release = Event(), Event()
        checking, release_checker = Event(), Event()
        calls = []
        original = self.target.read_text(encoding="utf-8-sig")
        edited = f"class Saved{first_exit} {{}}\n"

        def work(command, **kwargs):
            calls.append((command[0], self.target.read_text(encoding="utf-8-sig")))
            if len(calls) == 1:
                running.set()
                self.assertTrue(release.wait(5))
                check_cancelled()
                return subprocess.CompletedProcess(command, first_exit)
            if command[0] == "fixture-checker":
                checking.set()
                self.assertTrue(release_checker.wait(5))
            return subprocess.CompletedProcess(command, 0)

        with patch.object(workflow, "run_process", side_effect=work):
            first, _ = self.send("ビルドして")
            try:
                self.assertTrue(running.wait(5))
                self.save_edit(edited)
                self.assertEqual(self.request("editor")[1]["edit"]["status"], "queued")
                query, _ = self.send("ビルドは終わった？")
                self.assertIn("ビルド中", self.wait_for(query, "done")["message"])
                prefix = {0: "変更を保存しました。", 1: "編集が終わりました。\n", None: ""}[first_exit]
                message = prefix + "今のビルドが終わったら、保存した版でチェッカーを実行して"
                second, received = self.send(message)
                self.assertNotIn("query_intent", received)
                self.assertIn("Example.cs", received["edits"])
                self.assertIn("待ち", self.wait_for(second, "waiting")["message"])
                self.assertEqual(self.send(message, second)[1]["status"], "waiting")
                view = self.manager.get(self.run_id)
                self.assertFalse(view.control.cancelled.is_set())
                self.assertEqual(calls, [("dotnet", original)])
                if first_exit is None:
                    self.request("control", {"action": "stop", "run_id": self.run_id,
                                             "operation_id": view.state["operation_id"], "revision": 0})
            finally:
                release.set()
            if first_exit is None:
                self.wait_for(first, "cancelled")
                self.assertIn("終了を確認", self.wait_for(second, "failed")["message"])
                self.assertEqual(calls, [("dotnet", original)])
                self.assertEqual(self.request("editor")[1]["edit"]["status"], "queued")
                self.assertEqual(self.target.read_text(encoding="utf-8-sig"), original)
                self.assertEqual(self.record()["status"], "build_failed")
                return
            try:
                initial = self.wait_for(first, "done")
                self.assertEqual(initial["result_status"], "build_succeeded" if first_exit == 0 else "build_failed")
                self.assertTrue(checking.wait(5))
                self.assertFalse(self.request("migration-ledger")[1]["completion"]["verification_passed"])
            finally:
                release_checker.set()
            final = self.wait_for(second, "done")
            self.assertEqual(final["result_status"], "checker_succeeded")
            self.wait_until_idle()
            self.restart_server()
            self.assertEqual(self.send(message, second)[1]["status"], "done")
            completion = self.request("migration-ledger")[1]["completion"]
            self.assertTrue(completion["verification_passed"])
            self.assertFalse(completion["complete"])
        self.assertEqual(calls, [("dotnet", original), ("dotnet", edited), ("fixture-checker", edited)])
        self.assertIsNone(self.request("editor")[1]["edit"])
        self.assertEqual(self.record()["status"], "checker_succeeded")
        self.assertNotEqual(initial["built_targets"], final["built_targets"])
        self.assertEqual(final["built_targets"], self.record()["verification"]["target_hashes"])
        self.assertEqual(self.record()["verification"]["configured_checker_command"], ["fixture-checker"])
        workflow.review_migration_ranges(self.config, self.run_id, ["1:1"], "confirmed", "Reviewed",
                                         confirmed_targets=[self.target.name])
        self.assertTrue(self.request("migration-ledger")[1]["completion"]["complete"])

    def add_saved_run(self, unit_id):
        run_id = str(uuid.uuid4())
        path = self.run_path.parent / run_id
        worktree = self.worktree.parent / run_id
        (path / "capsule").mkdir(parents=True)
        capsule = path / "capsule" / "context.txt"
        capsule.write_bytes((self.run_path / "capsule" / "context.txt").read_bytes())
        manifest = json.loads((self.run_path / "capsule" / "manifest.json").read_bytes())
        manifest["parts"][0]["path"] = str(capsule)
        (path / "capsule" / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        worktree.mkdir()
        (worktree / self.target.name).write_bytes(self.target.read_bytes())
        (path / "run.json").write_text(json.dumps({
            **self.record(), "run_id": run_id, "unit_id": unit_id, "worktree_path": str(worktree),
        }), encoding="utf-8")
        self.config["units"].append({"id": unit_id, "target": self.target.name})
        return run_id, path, worktree

    def test_conditional_checker_binds_to_build_waiting_for_a_real_worker_slot(self):
        blockers = [(*self.add_saved_run(f"blocker-{index}"), Event()) for index in range(5)]

        started = {worktree: event for _, _, worktree, event in blockers}
        release = Event()
        calls = []
        operations = []

        def work(command, *, cwd, **kwargs):
            if event := started.get(cwd):
                event.set()
                if not release.wait(15):
                    raise TimeoutError("blocked build was not released")
            else:
                calls.append(command[0])
                operations.append(current_operation_id())
            return subprocess.CompletedProcess(command, 0)

        with patch.object(workflow, "run_process", side_effect=work):
            threads = []
            try:
                for run_id, path, _, event in blockers:
                    self.send("ビルドして", run_id=run_id)
                    if not event.wait(5):
                        with editor_queue(path) as queue:
                            self.fail(str(queue["conversation"]))
                    with self.manager.condition:
                        threads.append(self.manager.workers[run_id])
                self.assertEqual(len(threads), self.manager.limit)
                first, _ = self.send("ビルドして")
                self.assertIn("実行枠", self.wait_for(first, "waiting")["message"])
                view = self.manager.get(self.run_id)
                self.assertTrue(view.state["queued"])
                self.assertIsNone(view.state["operation_id"])
                second, receipt = self.send("今のビルドが終わったら、チェッカーを実行して")
                self.wait_for(second, "waiting")
                self.assertEqual(calls, [])
            finally:
                release.set()
                for thread in threads:
                    thread.join(timeout=5)
                    self.assertFalse(thread.is_alive())
            self.assertEqual(self.wait_for(first, "done")["result_status"], "build_succeeded")
            self.assertEqual(self.wait_for(second, "done")["result_status"], "checker_succeeded")
            self.assertTrue(receipt["predecessor_operation_id"])
            self.assertEqual(operations[0], receipt["predecessor_operation_id"])
            self.assertNotEqual(operations[0], operations[1])
            self.wait_until_idle()
        self.assertEqual(calls, ["dotnet", "dotnet", "fixture-checker"])

    def test_queued_predecessor_identity_survives_restart_or_fails_on_cancellation(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                self.manager.limit = 0
                first, _ = self.send("ビルドして")
                self.assertIn("実行枠", self.wait_for(first, "waiting")["message"])
                second, _ = self.send("今のビルドが終わったら、チェッカーを実行して")
                self.wait_for(second, "waiting")
                if cancel:
                    self.request("control", {"action": "cancel-queued", "revision": 0, "run_id": self.run_id})
                with patch.object(workflow, "run_process", side_effect=lambda command, **_: subprocess.CompletedProcess(command, 0)) as run:
                    self.restart_server()
                    with self.manager.condition:
                        self.manager.conversations.start()
                    if cancel:
                        self.wait_for(first, "cancelled")
                        replacement, _ = self.send("ビルドして")
                        self.wait_for(replacement, "done")
                        self.assertIn("終了を確認", self.wait_for(second, "failed")["message"])
                    else:
                        self.manager.start_pending()
                        self.wait_for(first, "done")
                        self.assertEqual(self.wait_for(second, "done")["result_status"], "checker_succeeded")
                    self.wait_until_idle()
                self.assertEqual([call.args[0][0] for call in run.call_args_list],
                                 ["dotnet"] if cancel else ["dotnet", "dotnet", "fixture-checker"])

    def test_conditional_checker_can_resume_waiting_after_a_confirmed_build(self):
        building, release = Event(), Event()
        calls = []

        def work(command, **kwargs):
            calls.append(command[0])
            if len(calls) == 1:
                building.set()
                self.assertTrue(release.wait(5))
            return subprocess.CompletedProcess(command, 0)

        with patch.object(workflow, "run_process", side_effect=work):
            first, _ = self.send("ビルドして")
            try:
                self.assertTrue(building.wait(5))
                pending, receipt = self.send("今のビルドが終わったら、チェッカーを実行して")
                self.wait_for(pending, "waiting")
                with patch.object(self.manager.conversations, "advance", return_value=True):
                    release.set()
                    self.wait_for(first, "done")
                    self.wait_until_idle()
                    self.assertEqual(self.record()["build_operation_id"], receipt["predecessor_operation_id"])
                    self.assertEqual(self.wait_for(pending, "waiting")["predecessor_operation_id"],
                                     receipt["predecessor_operation_id"])
                    self.restart_server()
            finally:
                release.set()
            self.manager.conversations.notify(self.run_id)
            self.assertEqual(self.wait_for(pending, "done")["result_status"], "checker_succeeded")
        self.assertEqual(calls, ["dotnet", "dotnet", "fixture-checker"])

    def test_restart_rejects_legacy_queued_conditional_checker_without_a_predecessor(self):
        identifier = uuid.uuid4().hex
        with editor_queue(self.run_path) as queue:
            queue["conversation"] = [{
                "id": identifier, "text": "ビルドが終わったら、チェッカーを実行して",
                "status": "waiting", "intent": "after_build_checker",
            }]
        self.manager.limit = 0
        with self.manager.condition:
            self.manager.enqueue(self.manager.get(self.run_id), {"action": "conversation", "conversation_id": identifier})
            self.manager.persist()
        self.restart_server()
        with patch.object(workflow, "run_process") as run:
            self.manager.start_pending()
            self.assertIn("終了を確認", self.wait_for(identifier, "failed")["message"])
            run.assert_not_called()

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

    def test_razor_pair_verifies_both_saved_targets_while_another_run_builds(self):
        sibling, sibling_path, sibling_tree = self.add_saved_run("other")
        names = ["Pages/Example.cshtml", "Pages/Example.cshtml.cs"]
        originals = {names[0]: "@page\n", names[1]: "class Original {}\n"}
        self.config["units"][0].update(target=names[0], targets=names)
        self.save_record({**self.record(), "target": names[0], "targets": names})
        (self.worktree / "Pages").mkdir()
        for name, content in originals.items():
            (self.worktree / name).write_text(content, encoding="utf-8")

        sibling_started, release_sibling = Event(), Event()
        building, release_build = Event(), Event()
        calls = []

        def work(command, *, cwd, **kwargs):
            cwd = Path(cwd)
            if cwd == sibling_tree:
                self.assertEqual(command[0], "dotnet")
                sibling_started.set()
                self.assertTrue(release_sibling.wait(12))
                check_cancelled()
            else:
                calls.append((command[0], {name: (cwd / name).read_text(encoding="utf-8-sig") for name in names}))
                if len(calls) == 1:
                    building.set()
                    self.assertTrue(release_build.wait(8))
            return subprocess.CompletedProcess(command, 0)

        with patch.object(workflow, "run_process", side_effect=work):
            other, _ = self.send("ビルドして", run_id=sibling)
            try:
                self.assertTrue(sibling_started.wait(5))
                self.send("ビルドして")
                self.assertTrue(building.wait(5))
                saved = {names[0]: "@page\n<p>Edited</p>\n", names[1]: "class Edited {}\n"}
                for name, content in saved.items():
                    self.save_edit(content, name)
                stale, _ = self.send("今のビルドが終わったら、チェッカーを実行して")
                self.wait_for(stale, "waiting")
                saved[names[1]] = "class Latest {}\n"
                self.save_edit(saved[names[1]], names[1])
                fresh, _ = self.send("今のビルドが終わったら、チェッカーを実行して")
                self.wait_for(fresh, "waiting")
                release_build.set()
                self.assertIn("更新されました", self.wait_for(stale, "failed")["message"])
                result = self.wait_for(fresh, "done")
                self.assertEqual(result["result_status"], "checker_succeeded")
                self.assertEqual(result["built_targets"], {
                    name: hashlib.sha256((self.worktree / name).read_bytes()).hexdigest() for name in names
                })
                sibling_view = self.manager.get(sibling)
                self.assertFalse(sibling_view.control.cancelled.is_set())
                self.assertEqual(self.request("editor", run_id=sibling)[1]["conversation"][0]["id"], other)
                self.assertEqual(self.request("editor", run_id=sibling)[1]["content"], "class Original {}\n")
            finally:
                release_build.set()
                release_sibling.set()
            self.assertEqual(self.wait_for(other, "done", run_path=sibling_path)["result_status"], "build_succeeded")
            self.wait_until_idle()
        self.assertEqual(calls, [("dotnet", originals), ("dotnet", saved), ("fixture-checker", saved)])
        self.restart_server()
        self.assertTrue(self.request("migration-ledger")[1]["completion"]["verification_passed"])
        self.assertFalse(self.request("migration-ledger", run_id=sibling)[1]["completion"]["verification_passed"])

    def test_unconfirmed_junie_stop_does_not_continue_with_a_waiting_build(self):
        self.check_unconfirmed_junie_stop()

    def test_unconfirmed_junie_stop_blocks_waiting_build_even_when_saving_diagnostics_fails(self):
        self.check_unconfirmed_junie_stop(fail_diagnostic_save=True)

    def check_unconfirmed_junie_stop(self, *, fail_diagnostic_save=False):
        self.config["max_repair_attempts"] = 1
        (self.run_path / "agent-workspace").mkdir()
        running, release = Event(), Event()
        save_failed = Event()
        original = self.target.read_bytes()
        write_record = workflow.write_run_record

        def fail_stop(command, completion, token, targets, **kwargs):
            targets[0].write_text("class Unconfirmed {}\n", encoding="utf-8")
            completion.write_text(token, encoding="utf-8")
            running.set()
            self.assertTrue(release.wait(8))
            raise workflow.junie_cli.JunieStopError("Junieの終了を確認できませんでした。")

        def save_record(path, record, **kwargs):
            if fail_diagnostic_save and "終了を確認できません" in record.get("error", ""):
                save_failed.set()
                raise PermissionError("fixture: run.json could not be replaced")
            return write_record(path, record, **kwargs)

        with (patch.object(workflow.junie_cli, "require_interactive_cli"),
              patch.object(workflow.junie_cli, "run_interactive", side_effect=fail_stop),
              patch.object(workflow, "write_run_record", side_effect=save_record),
              patch.object(workflow, "run_process") as run):
            repair, _ = self.send("この指摘は旧 PHP と同じ既定値にして、チェッカーをもう一度")
            try:
                if not running.wait(5):
                    self.fail(str(self.request("editor")[1]["conversation"]))
                build, _ = self.send("ビルドして")
                self.wait_for(build, "waiting")
                self.stop_operation()
            finally:
                release.set()
            self.assertIn("終了を確認できません", self.wait_for(repair, "failed")["message"])
            self.assertIn("終了を確認できません", self.wait_for(build, "failed")["message"])
            self.wait_until_idle()
            self.assertEqual(save_failed.is_set(), fail_diagnostic_save)
            self.assertIn("終了を確認できません", self.manager.get(self.run_id).state["error_details"])
            self.assertEqual(self.target.read_bytes(), original)
            run.assert_not_called()

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
        toml = self.config.copy()
        self.config.update(checker_from_cli=True, toml_checker_command=toml["checker_command"],
                           checker_command=["cli-checker"])
        diagnostic = b"Example.cs(1,1): error CS1002: Current build diagnostic\n"
        mocked, calls = self.mock_commands(("dotnet", 1, diagnostic), ("dotnet", 0, b"ok\n"))
        self.save_edit("class Corrected {}\n")
        with mocked, patch.object(workflow.junie_cli, "require_interactive_cli"), patch.object(workflow, "_repair_target") as repair:
            identifier, _ = self.send("保存した版でビルドして。まだエラーがあれば直して")
            self.assertIn("ビルド成功", self.wait_for(identifier, "done")["message"])
            self.assertIn("Current build diagnostic", repair.call_args.args[5])
        self.assertEqual(calls, [["dotnet", "build"], ["dotnet", "build"]])
        verification = self.record()["verification"]
        self.assertEqual(verification["configured_checker_command"], ["cli-checker"])
        self.assertIsNone(verification["checker_command"])
        for settings in (self.config, toml, {**toml, "checker_command": None}):
            self.assertFalse(workflow.migration_ledger(settings, self.run_id)["completion"]["verification_passed"])

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
        other_question, _ = self.send("もう一回お願い", run_id=other)
        self.wait_for(other_question, "clarification", run_path=path)
        self.send("キャンセル", run_id=other)
        self.wait_for(other_question, "cancelled", run_path=path)
        self.wait_for(identifier, "clarification")

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
        replace = Path.replace
        sharing_violation = Event()
        calls = []
        text = "今のビルドを止めて、保存した版でやり直して"

        def after_build(config, phase, *args, **kwargs):
            # Pause before an optional hook changes the phase away from build_succeeded.
            if phase == "after_build" and calls == ["dotnet"]:
                completed.set()
                self.assertTrue(release.wait(5))
            return script_phase(config, phase, *args, **kwargs)

        def work(command, **kwargs):
            calls.append(command[0])
            return subprocess.CompletedProcess(command, 0)

        def replace_record(source, target):
            if target == self.record_path and completed.is_set() and not sharing_violation.is_set():
                self.assertEqual(self.record()["status"], "build_succeeded")
                sharing_violation.set()
                error = PermissionError("fixture: destination held open by a reader")
                error.winerror = 32
                raise error
            return replace(source, target)

        with (
            patch.object(workflow, "_run_script_phase", side_effect=after_build),
            patch.object(workflow, "run_process", side_effect=work),
            patch.object(Path, "replace", replace_record),
        ):
            self.start_build()
            try:
                self.assertTrue(completed.wait(5))
                identifier, _ = self.send(text)
                self.assertIn("終了を待ちます", self.wait_for(identifier, "waiting")["message"])
                self.assertEqual(self.send(text, identifier)[1]["status"], "waiting")
                self.assertFalse(self.manager.get(self.run_id).control.cancelled.is_set())
            finally:
                release.set()
            self.wait_for(identifier, "done")
            self.wait_until_idle()
            self.restart_server()
            self.assertEqual(self.send(text, identifier)[1]["status"], "done")
        self.assertTrue(sharing_violation.is_set())
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

    def test_saved_edits_and_junie_followup_request_the_existing_safe_handoff(self):
        from migration_harness.editor_queue import LivePrompts

        view = self.manager.get(self.run_id)
        view.begin_operation()
        self.save_record({**self.record(), "status": "repairing"})
        view.observe(self.record())
        self.save_edit("class Saved {}\n")
        identifier, _ = self.send("保存した。これを踏まえて続けて")
        self.assertIn("保存した編集と指示を合流", self.wait_for(identifier, "steering")["message"])
        live = LivePrompts(self.run_path, interrupt_for_edits=True)
        self.assertTrue(live.needs_handoff())
        self.assertEqual(live.take(), ([], ""))
        self.save_edit("class Revised {}\n")
        self.assertIn("保存版が更新されました", self.wait_for(identifier, "failed")["message"])
        self.assertFalse(live.needs_handoff())

    def test_scope_change_only_reviews_the_edited_targets(self):
        self.save_record({**self.record(), "verification": {
            "target_hashes": {"Example.cs": hashlib.sha256(self.target.read_bytes()).hexdigest()},
        }})
        self.save_edit("class Draft {}\n")
        with patch.object(workflow, "run_process") as run:
            identifier, _ = self.send("残りは触らず、今の差分だけ確認して")
            self.assertEqual(self.wait_for(identifier, "done")["review_targets"], ["Example.cs"])
            run.assert_not_called()
