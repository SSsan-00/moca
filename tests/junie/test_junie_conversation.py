import hashlib
import io
import json
import subprocess
import threading
import time
import uuid
from contextlib import ExitStack, contextmanager, redirect_stdout
from pathlib import Path
from threading import Event, Thread
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from migration_harness.web import dashboard
from migration_harness.runs import run_manager
from migration_harness import workflow
from migration_harness import execution, files as harness_files
from migration_harness.junie import junie_cli
from migration_harness.runs import (
    editor_queue as editor_queue_store, migration_draft, run_records, stage_scripts,
    worktree_targets as target_files,
)
from migration_harness.verification import build_cycle, build_receipt, repairs
from migration_harness.runs.editor_queue import editor_queue
from migration_harness.execution import check_cancelled, current_operation_id
from migration_harness.junie import junie_conversation
from migration_harness.junie.junie_conversation import _intent
from migration_harness.runs.run_manager import RunManager
from migration_harness.verification import checker_advisory
from tests.support.editor_save_fixtures import pending_editor_edit
from tests.support.build_receipt_fixtures import saved_build_receipt
from tests.support.fixtures import SavedRunFixture
from tests.verification.test_checker_advisory import LITE_EMPTY_MODELS, LITE_SELECTED, acp_reply


class JunieConversationTests(SavedRunFixture):
    def setUp(self):
        super().setUp()
        self.background_errors = []
        self.addCleanup(self.raise_background_error)
        self.enterContext(patch.object(threading, "excepthook", self.capture_background_error))
        self.start_server()

    def capture_background_error(self, error):
        """ワーカーの例外を記録し、Run状態を待つテストを起こす。"""
        with self.manager.condition:
            self.background_errors.append(error.exc_value)
            self.manager.condition.notify_all()

    def raise_background_error(self):
        """ワーカーで発生した例外を元のトレースバック付きで報告する。"""
        if self.background_errors:
            raise self.background_errors.pop(0)

    @contextmanager
    def close_workers_before_unpatching(self, *gates):
        """失敗時にもゲートを開き、差し替えを戻す前にRunと会話を終了する。"""
        try:
            yield
        finally:
            for gate in gates:
                gate.set()
            self.manager.close()
            self.raise_background_error()

    def diagnostics(self, run_id):
        view = self.manager.views.get(run_id)
        state = view.state if view else {}
        thread = self.manager.conversations.thread
        job = self.manager.jobs.get(run_id, {})
        try:
            # A timeout can occur while another thread owns editor.lock.
            persisted = json.loads((Path(self.config["run_root"]) / run_id / "editor.json").read_bytes())
            conversation = [(item.get("id"), item.get("status")) for item in persisted.get("conversation", [])[-4:]]
        except (OSError, ValueError):
            conversation = []
        return (f"Run {run_id}, status={state.get('status')}, operation={state.get('operation_id')}, "
                f"job_running={job.get('running')}, job_conversation={job.get('conversation_id')}, "
                f"conversation_worker_alive={bool(thread and thread.is_alive())}, recent_conversation={conversation}")

    def start_server(self):
        """受理後の永続化と再起動を確認できるRunManagerとHTTPサーバーを開く。"""
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
        started = time.monotonic()
        request = Request(self.url + endpoint, None if body is None else json.dumps(body).encode(), headers)
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, json.load(response)
        except (TimeoutError, URLError) as error:
            if isinstance(error, URLError) and not isinstance(error.reason, TimeoutError):
                raise
            self.raise_background_error()
            raise AssertionError(
                f"{request.get_method()} {endpoint} timed out after {time.monotonic() - started:.3f}s; "
                + self.diagnostics(headers["X-Run-ID"])
            ) from error

    def send(self, text, identifier=None, *, run_id=None):
        run_id = run_id or self.run_id
        identifier = identifier or uuid.uuid4().hex
        status, result = self.request("editor", {
            "kind": "conversation", "run_id": run_id, "id": identifier, "text": text,
        }, run_id=run_id)
        self.assertEqual(status, 202)
        self.assertEqual(result["id"], identifier)
        return identifier, result

    def test_selection_only_reviews_exact_snippet_without_verification_or_source_changes(self):
        original = self.target.read_bytes()
        run_before = self.record_path.read_bytes()
        selection = {"path": self.target.name, "start_line": 2, "end_line": 3,
                     "text": "var scoped = 1;\n// selection only\n"}
        identifier = uuid.uuid4().hex
        payload = {"kind": "conversation", "run_id": self.run_id, "id": identifier,
                   "text": "", "selection": selection}
        with (patch.object(junie_conversation, "run_text_advisory", return_value="選択行だけの説明") as review,
              patch.object(workflow, "resume_with_junie", side_effect=AssertionError("migration must not resume"))):
            status, result = self.request("editor", payload)
            self.assertEqual(status, 202)
            self.assertEqual(result["selection"], selection)
            self.assertEqual(result["text"], "")
            self.assertEqual(self.wait_for(identifier, "done")["message"], "選択行だけの説明")
            self.assertEqual(self.request("editor", payload)[1]["id"], identifier)
            with self.assertRaises(HTTPError):
                self.request("editor", {**payload, "selection": {**selection, "text": "changed\nline"}})
            review.assert_called_once()
        prompt, = review.call_args.args
        self.assertIn("ファイル操作は行わない", prompt)
        self.assertIn("16,384バイト以内", prompt)
        self.assertEqual(json.loads(prompt.splitlines()[-1]), selection)
        self.assertNotIn("class Original", prompt)
        self.assertEqual(self.target.read_bytes(), original)
        self.assertEqual(self.record_path.read_bytes(), run_before)
        with editor_queue(self.run_path) as queue:
            self.assertEqual(queue["prompts"], [])

    def test_selection_uses_saved_lite_model_when_acp_catalog_is_empty(self):
        original = self.target.read_bytes()
        selection = {"path": self.target.name, "start_line": 1, "end_line": 1, "text": "var x = 1;"}
        for effort in (None, "none"):
            self.save_record({**self.record(), "junie_model": "junie-lite", "junie_effort": effort})
            identifier = uuid.uuid4().hex
            with self.subTest(effort=effort), acp_reply(
                "選択範囲だけを説明しました。", model_setup=(LITE_EMPTY_MODELS, LITE_SELECTED),
            ):
                code, _ = self.request("editor", {"kind": "conversation", "run_id": self.run_id, "id": identifier,
                                                  "text": "", "selection": selection})
                self.assertEqual(code, 202)
                self.assertEqual(self.wait_for(identifier, "done")["message"], "選択範囲だけを説明しました。")
        self.assertEqual(self.target.read_bytes(), original)

    def test_selection_persists_only_complete_utf8_replies_within_its_own_budget(self):
        inputs = {path: path.read_bytes() for path in (self.target, self.record_path, self.build_log, self.checker_log)}
        selection = {"path": self.target.name, "start_line": 1, "end_line": 1, "text": "var x = 1;"}
        for size, chunks, metadata, reason, status in (
            (4095, 2, False, "end_turn", "done"), (4096, 2, False, "end_turn", "done"),
            (4097, 253, False, "end_turn", "done"), (4097, 254, False, "end_turn", "done"),
            (4097, 254, True, "end_turn", "done"), (16384, 512, True, "end_turn", "done"),
            (16385, 512, True, "end_turn", "failed"), (4097, 254, False, "cancelled", "failed"),
            (4097, 254, True, "eof", "failed"),
        ):
            reply = "あ" * (size // 3) + "x" * (size % 3)
            identifier = uuid.uuid4().hex
            with self.subTest(size=size, chunks=chunks, metadata=metadata, reason=reason), acp_reply(
                reply, reason, chunks=chunks, metadata=metadata,
            ):
                code, _ = self.request("editor", {"kind": "conversation", "run_id": self.run_id, "id": identifier,
                                                  "text": "", "selection": selection})
                self.assertEqual(code, 202)
                self.wait_for(identifier, status)
                entry = next(e for e in self.request("editor")[1]["conversation"] if e["id"] == identifier)
                if status == "done":
                    self.assertEqual(entry["message"], reply)
                else:
                    self.assertNotIn("あ", entry["message"])
                    if size == 16385:
                        self.assertIn("質問を分けて", entry["message"])
                        self.assertIn("16,384", entry["message"])
                    elif reason == "eof":
                        self.assertIn("ended before a complete reply", entry["message"])
                    else:
                        self.assertIn("did not complete normally", entry["message"])
        for path, original in inputs.items():
            self.assertEqual(path.read_bytes(), original, path.name)

    def test_selection_reports_transport_limits_separately_from_eof_and_answer_size(self):
        selection = {"path": self.target.name, "start_line": 1, "end_line": 1, "text": "var x = 1;"}
        reply = "ここが選択範囲です。" * 8
        for limit, size, chunks, message in (
            ("FRAME_LIMIT", 256, 2, "1フレーム"), ("STREAM_LIMIT", 1024, 32, "累積通信量"),
        ):
            identifier = uuid.uuid4().hex
            with self.subTest(limit=limit), patch.object(checker_advisory, limit, size), acp_reply(reply, chunks=chunks):
                code, _ = self.request("editor", {"kind": "conversation", "run_id": self.run_id, "id": identifier,
                                                  "text": "", "selection": selection})
                self.assertEqual(code, 202)
                error = self.wait_for(identifier, "failed")["message"]
                self.assertIn(message, error)
                self.assertIn(f"{size:,}バイト", error)
                self.assertIn("質問を分けて", error)
                self.assertNotIn("ここが", error)

    def test_selection_with_a_comment_answers_without_running_build_or_migration(self):
        selection = {"path": self.target.name, "start_line": 1, "end_line": 1, "text": "// 停止"}
        comment = "ビルドだけ"
        identifier = uuid.uuid4().hex
        with (patch.object(junie_conversation, "run_text_advisory", return_value="変更せずに回答") as review,
              patch.object(execution, "run_process") as build):
            status, response = self.request("editor", {"kind": "conversation", "run_id": self.run_id,
                                                       "text": comment, "id": identifier, "selection": selection})
            self.assertEqual(status, 202)
            self.assertEqual(response["text"], comment)
            self.assertEqual(response["selection"], selection)
            self.assertEqual(self.wait_for(identifier, "done")["message"], "変更せずに回答")
            build.assert_not_called()
        prompt, = review.call_args.args
        self.assertIn(comment, prompt)
        self.assertEqual(json.loads(prompt.splitlines()[-1]), selection)

    def test_changed_target_fails_before_selection_review(self):
        identifier = uuid.uuid4().hex
        selection = {"path": self.target.name, "start_line": 1, "end_line": 1, "text": "var scoped = 1;"}
        with (patch.object(self.manager.conversations, "notify"),
              patch.object(junie_conversation, "run_text_advisory") as review):
            self.request("editor", {"kind": "conversation", "run_id": self.run_id, "id": identifier,
                                    "text": "", "selection": selection})
            self.target.write_bytes(b"changed by another process")
            self.manager.conversations.advance(self.config, self.run_id)
            self.assertIn("保存版が更新されました", self.wait_for(identifier, "failed")["message"])
            review.assert_not_called()

    def test_invalid_selection_cannot_be_reviewed(self):
        selection = {"path": self.target.name, "start_line": 1, "end_line": 1, "text": "選択"}
        invalid = [
            {"path": "Other.cs"}, {"start_line": True}, {"end_line": 2}, {"end_line": 21},
            {"text": "  "}, {"text": "界" * 667}, {"text": "\x00"}, {"unselected": "must not be accepted"},
        ]
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(HTTPError):
                self.request("editor", {"kind": "conversation", "run_id": self.run_id, "id": uuid.uuid4().hex,
                                        "text": "", "selection": {**selection, **changes}})
        with editor_queue(self.run_path) as queue:
            self.assertFalse(queue.get("conversation"))

    def wait_for(self, identifier, status, *, run_path=None):
        end = time.monotonic() + 6
        while time.monotonic() < end:
            self.raise_background_error()
            with editor_queue(run_path or self.run_path) as queue:
                entry = next((row for row in queue.get("conversation", []) if row["id"] == identifier), {})
            if entry.get("status") == status:
                return entry
            time.sleep(.02)
        self.raise_background_error()
        self.fail(f"expected {status}, last entry: {entry}; {self.diagnostics((run_path or self.run_path).name)}")

    def start_build(self):
        self.request("control", {"action": "resume-run", "revision": 0, "run_id": self.run_id})

    def save_edit(self, text, target="Example.cs"):
        _, editor = self.request("editor?" + urlencode({"target": target}))
        self.request("editor", {"kind": "save", "run_id": self.run_id, "target": target,
                                "base": editor["content"], "content": text, "save_id": uuid.uuid4().hex,
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

        with patch.object(execution, "run_process", side_effect=build), patch.object(
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

    def test_final_migration_checkpoint_admits_conversation_before_dispatch(self):
        self.save_record({**self.record(), "status": "prepared", "completed_capsules": 0,
                          "current_capsule": 1,
                          "target_hashes": {"Example.cs": hashlib.sha256(self.target.read_bytes()).hexdigest()}})
        interpreting, release = Event(), Event()
        interpreter = self.manager.conversations.advance
        instructions = ["Example.cs の既定値を 42 に変更してください", "変更した既定値を保存してください"]
        identifiers = [uuid.uuid4().hex for _ in instructions]
        launches = []

        def delayed(*args):
            interpreting.set()
            release.wait()  # The main thread owns the deadline and always releases this gate.
            return interpreter(*args)

        def junie(command, completion, token, targets, **kwargs):
            launches.append(self.record()["status"])
            if len(launches) == 1:
                self.assertEqual(self.send(instructions[0], identifiers[0])[1]["status"], "received")
                self.assertTrue(interpreting.wait(5))
                self.send(instructions[1], identifiers[1])
                self.send(instructions[0], identifiers[0])
            else:
                with editor_queue(self.run_path) as queue:
                    self.assertEqual([(p["id"], p["status"]) for p in queue["prompts"]],
                                     [(identifier, "sending") for identifier in identifiers])
                targets[0].write_text("class Example { const int Value = 42; }\n", encoding="utf-8")
            completion.write_text(token, encoding="utf-8")
            return 0

        def external(command, **kwargs):
            self.assertIn("Value = 42", self.target.read_text(encoding="utf-8-sig"))
            with editor_queue(self.run_path) as queue:
                self.assertEqual([p["status"] for p in queue["prompts"]], ["sent", "sent"])
            return subprocess.CompletedProcess(command, 0)

        with (
            patch.object(self.manager.conversations, "advance", side_effect=delayed),
            patch.object(junie_cli, "require_interactive_cli"),
            patch.object(junie_cli, "run_interactive", side_effect=junie),
            patch.object(execution, "run_process", side_effect=external) as verify,
            self.close_workers_before_unpatching(release),
        ):
            try:
                self.start_build()
                self.wait_until_idle()
                with editor_queue(self.run_path) as queue:
                    self.assertEqual([item["status"] for item in queue["conversation"]], ["steering", "steering"])
            finally:
                release.set()
            for identifier in identifiers:
                self.wait_for(identifier, "done")
        self.assertEqual(launches, ["junie_running", "editor_followup"])
        self.assertEqual(verify.call_count, 2)

    def test_stop_only_and_nonstopping_diff_during_build_or_migration(self):
        for phase in ("building", "junie_running"):
            with self.subTest(phase=phase):
                view = self.manager.get(self.run_id)
                view.begin_operation()
                self.save_record({**self.record(), "status": phase})
                view.observe(self.record())
                for text in ("止めずに差分だけ確認して", "処理は続けたまま差分だけ確認して"):
                    identifier, _ = self.send(text)
                    self.wait_for(identifier, "done")
                    self.assertFalse(view.control.cancelled.is_set())
                identifier, receipt = self.send("今すぐ止めて")
                self.assertIn("停止を要求", self.wait_for(identifier, "done")["message"])
                self.assertTrue(view.control.cancelled.is_set())
                self.assertNotIn("結果", receipt["message"])

    def test_compound_verification_keeps_prohibitions_and_unknown_conditions(self):
        mocked, calls = self.mock_commands(("dotnet", 0, b"new build result"), ("dotnet", 1, b"new build failure"))
        with mocked, patch.object(build_cycle, "repair_build_failure") as repair:
            report, _ = self.send("ビルドして、結果を教えて")
            self.assertIn("new build result", self.wait_for(report, "done")["logs"][0]["text"])
            self.wait_until_idle()
            prohibited, _ = self.send("コードは修正してはいけません。ビルドだけ実行してください")
            self.assertEqual(self.wait_for(prohibited, "done")["result_status"], "build_failed")
            self.wait_until_idle()
            for text in ("チェッカーでエラーがなければ、ビルドして", "Example.cs の既定値を 0 に直して、ビルドして"):
                ambiguous, _ = self.send(text)
                self.wait_for(ambiguous, "clarification")
                self.send("キャンセル")
                self.wait_for(ambiguous, "cancelled")
            repair.assert_not_called()
        self.assertEqual(len(calls), 2)

    def test_migration_stop_archives_raw_and_missing_drafts_before_adoption(self):
        second = self.worktree / "Second.cs"
        second.write_text("class Accepted {}", encoding="utf-8")
        names = [self.target.name, second.name]
        self.config["units"][0]["targets"] = names
        self.save_record({**self.record(), "targets": names, "status": "prepared",
                          "completed_capsules": 0, "current_capsule": 1})

        def interrupted(_command, _completion, _token, targets, **_kwargs):
            targets[0].write_bytes(b"partial\xff")
            targets[1].unlink()
            junie_cli.session_closed_observer.get()()
            raise KeyboardInterrupt

        with (patch.object(junie_cli, "require_interactive_cli"),
              patch.object(junie_cli, "run_interactive", side_effect=interrupted)):
            with self.assertRaises(workflow.RunInterrupted):
                workflow.resume_with_junie(self.config, self.run_id)
        saved = self.record()["stopped_migration_draft"]
        self.assertEqual((self.run_path / saved["snapshot"] / "0.draft").read_bytes(), b"partial\xff")
        self.assertIsNone(saved["targets"]["Second.cs"])
        self.assertEqual(second.read_bytes(), b"class Accepted {}")
        self.assertIn(b"Original", self.target.read_bytes())

        def finish(_command, completion, token, targets, **_kwargs):
            self.assertEqual(targets[0].read_bytes(), b"partial\xff")
            self.assertFalse(targets[1].exists())
            targets[0].write_bytes(b"class Revised {}")
            targets[1].write_bytes(b"class Recreated {}")
            completion.write_text(token, encoding="ascii")
            return 0

        with (patch.object(junie_cli, "require_interactive_cli"),
              patch.object(junie_cli, "run_interactive", side_effect=finish),
              patch.object(workflow, "_verify_worktree", return_value={})):
            workflow.resume_with_junie(self.config, self.run_id)
        self.assertNotIn("stopped_migration_draft", self.record())
        self.assertEqual(self.record()["stopped_migration_history"], [saved])
        self.assertIn(b"Recreated", second.read_bytes())

    def test_stopped_migration_editor_compares_draft_to_adopted_version(self):
        agent = self.run_path / "agent-workspace"
        agent.mkdir()
        partial = b"class Unverified {}"
        (agent / self.target.name).write_bytes(partial)
        pending = {self.target.name: harness_files.sha256_file(self.target)}
        record = {**self.record(), "status": "interrupted", "completed_capsules": 0, "current_capsule": 1,
                  "target_hashes": pending}
        migration_draft.save_stopped_migration(self.record_path, record, agent, (Path(self.target.name),),
                                        workspace_unchanged=True)
        _, editor = self.request("editor")
        self.assertEqual(editor["content"], partial.decode())
        self.assertEqual(editor["comparison_content"], "class Original {}\n")
        self.assertEqual(editor["comparison_label"], "採用済みとの差分")
        self.assertTrue(editor["stopped_draft"])
        self.target.write_bytes(b"class Manual {}")
        _, new_view = self.request("editor?" + urlencode({"view_version": editor["view_version"],
                                                       "content_version": editor["content_version"]}))
        self.assertEqual(new_view["comparison_content"], "class Manual {}")
        self.assertEqual(new_view["comparison_label"], "作業ツリーとの差分")

    def prepare_interrupted_migration(self, **changes):
        capsule = self.run_path / "capsule"
        manifest_path = capsule / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for index in (2, 3):
            part = capsule / f"context-{index}.txt"
            part.write_text(f"capsule {index}", encoding="utf-8")
            digest = hashlib.sha256(part.read_bytes()).hexdigest()
            manifest["parts"].append({"index": index, "path": str(part), "chars": 9, "sources": [],
                                      "sha256": digest, "segment_sha256": digest})
        manifest["total_context_chars"] = sum(part["chars"] for part in manifest["parts"])
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        agent = self.run_path / "agent-workspace"
        agent.mkdir()
        (agent / self.target.name).write_bytes(self.target.read_bytes())
        self.save_record({**self.record(), "status": "interrupted", "current_capsule": 2, "context_capsules": 3,
                          "target_hashes": {self.target.name: harness_files.sha256_file(self.target)}, **changes})

    def test_default_migration_resume_runs_from_checkpoint_through_last_capsule(self):
        self.prepare_interrupted_migration(migration_range_stop=2)
        _, plan = self.request("resume-plan?" + urlencode({"run_id": self.run_id, "start_stage": "migration"}))
        self.assertEqual(plan["capsule_range"], [2, 3])
        self.assertIsNone(plan["through_capsule"])
        self.assertNotIn("rewind_to", plan)
        visited = []

        def run_agent(_command, completion, token, targets, **_kwargs):
            visited.append(self.record()["current_capsule"])
            if len(visited) == 1:
                self.assertEqual(targets[0].read_text(encoding="utf-8-sig"), "class Original {}\n")
            targets[0].write_bytes(f"class Capsule{visited[-1]} {{}}\n".encode())
            completion.write_text(token, encoding="ascii")
            return 0

        payload = {"action": "resume-run", "run_id": self.run_id, "revision": 0,
                   "start_stage": "migration", "plan_version": plan["version"]}
        with (patch.object(junie_cli, "require_interactive_cli"),
              patch.object(junie_cli, "run_interactive", side_effect=run_agent),
              patch.object(workflow, "_verify_worktree", return_value={"status": "build_succeeded"}) as verify,
              patch.object(execution, "run_process") as external):
            self.assertEqual(self.request("control", payload)[0], 202)
            self.wait_until_idle()
            self.assertEqual(visited, [2, 3])
            verify.assert_called_once()
            external.assert_not_called()
        saved = self.record()
        self.assertEqual(saved["completed_capsules"], 3)
        self.assertNotIn("migration_range_stop", saved)
        self.assertNotIn("rewind_to", saved["resume_operations"][-1])
        self.assertIn("Capsule3", self.target.read_text(encoding="utf-8-sig"))

    def test_migration_stage_plan_and_conversation_share_workflow_across_restart(self):
        self.prepare_interrupted_migration()
        route = "resume-plan?" + urlencode({"run_id": self.run_id, "start_stage": "migration", "through_capsule": 2})
        _, plan = self.request(route)
        self.assertEqual(plan["capsule_range"], [2, 2])
        payload = {"action": "resume-run", "run_id": self.run_id, "revision": 0,
                   "start_stage": "migration", "through_capsule": 2, "plan_version": plan["version"]}

        def run_agent(_command, completion, token, targets, **_kwargs):
            targets[0].write_bytes(b"class Updated {}")
            completion.write_text(token, encoding="ascii")
            return 0

        with (patch.object(junie_cli, "require_interactive_cli"),
              patch.object(junie_cli, "run_interactive", side_effect=run_agent) as junie,
              patch.object(execution, "run_process") as external):
            self.assertEqual(self.request("control", payload)[0], 202)
            self.wait_until_idle()
            self.assertEqual(self.record()["completed_capsules"], 2)
            self.restart_server()
            run = self.manager.get(self.run_id).state
            self.assertEqual(run["current_capsule"], 3)
            identifier, accepted = self.send("移行の続きから区間３まで")
            self.assertEqual(accepted["stage_request"]["capsule_range"], [3, 3])
            self.assertEqual(accepted["stage_request"]["start_stage"], "migration")
            self.wait_for(identifier, "done")
            self.assertEqual(self.record()["completed_capsules"], 3)
            self.assertEqual(junie.call_count, 2)
            external.assert_not_called()

    def get_stage_plan(self, stage):
        status, plan = self.request("resume-plan?" + urlencode({"run_id": self.run_id, "start_stage": stage}))
        self.assertEqual(status, 200)
        return plan

    def start_stage_plan(self, plan):
        return self.request("control", {"action": "resume-run", "run_id": self.run_id, "revision": 0,
                                       "start_stage": plan["start_stage"], "plan_version": plan["version"]})[0]

    def run_build_through(self, responses, expected):
        plan = self.get_stage_plan("build-through")
        self.assertEqual(plan["stages"], ["build", "checker"])
        self.assertNotEqual(plan["version"], self.get_stage_plan("build")["version"])
        commands, calls = self.mock_commands(*responses)
        with (commands, patch.object(junie_cli, "run_interactive") as junie):
            self.assertEqual(self.start_stage_plan(plan), 202)
            self.wait_until_idle()
        junie.assert_not_called()
        record = self.record()
        self.assertEqual(record["status"], expected)
        self.assertEqual(self.manager.get(self.run_id).state["status"], expected)
        self.assertEqual(record["resume_operations"][-1]["start_stage"], "build-through")
        self.assertEqual([command[0] for command in calls], [
            "dotnet" if entry[0] == "dotnet" else "fixture-checker" for entry in responses
        ])
        return record

    def test_dashboard_build_through_runs_the_configured_checker(self):
        record = self.run_build_through([
            ("dotnet", 0, b"build ok"), ("checker", 0, b""),
        ], "checker_succeeded")
        self.assertEqual(record["checker_exit_code"], 0)
        self.assertEqual(record["verification"]["checker_command"], ["fixture-checker"])
        completion = workflow.migration_ledger(self.config, self.run_id)["completion"]
        self.assertTrue(completion["verification_passed"])

    def test_dashboard_build_through_rebuilds_a_saved_edit_after_interrupted_script_review(self):
        hook = ["fixture-after-build"]
        self.config.update(scripts={"after_build": [hook]}, junie_review_script_output=True,
                           junie_apply_output_decisions=True)
        edited = "class SavedCorrection {}\n"
        calls = []

        def run(command, *, stdout, **_kwargs):
            calls.append(command)
            target = self.target.read_text(encoding="utf-8-sig")
            if command == hook:
                stdout.write(b"1 test passed\n" if target == edited else b"Review needed\n")
            elif len(calls) > 2:
                self.assertEqual(target, edited)
            return subprocess.CompletedProcess(command, 0)

        good = '{"verdict":"passed","evidence_lines":[1]}'
        with (patch.object(execution, "run_process", side_effect=run),
              patch.object(checker_advisory, "run_text_advisory", side_effect=[KeyboardInterrupt, good]) as judge):
            self.start_build()
            self.wait_until_idle()
            saved = self.record()["verification_script_pending"]
            original_log = (self.run_path / saved["log"]).read_bytes()
            before_edit = self.get_stage_plan("build-through")

            _, editor = self.request("editor")
            _, response = self.request("editor", {"kind": "save", "run_id": self.run_id, "target": self.target.name,
                                                  "base": editor["content"], "content": edited, "edit_id": None,
                                                  "save_id": uuid.uuid4().hex})
            self.assertFalse(response["queued"])
            with self.assertRaises(HTTPError) as stale:
                self.start_stage_plan(before_edit)
            with stale.exception as reply:
                self.assertEqual(reply.code, 409)

            self.start_build()
            self.wait_until_idle()
            self.assertIn("The target changed", self.record()["error"])
            self.assertEqual(self.record()["verification_script_pending"], saved)
            self.assertEqual(calls, [["dotnet", "build"], hook])

            log = self.run_path / saved["log"]
            log.write_bytes(original_log.replace(b"Review needed", b"Changed text!"))
            self.assertEqual(self.start_stage_plan(self.get_stage_plan("build-through")), 202)
            self.wait_until_idle()
            self.assertIn("missing or changed", self.record()["error"])
            self.assertEqual(self.record()["verification_script_pending"], saved)
            self.assertEqual(calls, [["dotnet", "build"], hook])
            log.write_bytes(original_log)

            plan = self.get_stage_plan("build-through")
            self.assertEqual(self.start_stage_plan(plan), 202)
            self.wait_until_idle()
            self.assertEqual(judge.call_count, 2)
            self.assertIn("1 test passed", judge.call_args.args[0])

        record = self.record()
        self.assertEqual(record["status"], "checker_succeeded")
        self.assertEqual(calls, [["dotnet", "build"], hook, ["dotnet", "build"], hook, ["fixture-checker"]])
        self.assertEqual(record["resume_operations"][-1]["version"], plan["version"])
        self.assertEqual(record["verification"]["target_hashes"][self.target.name], harness_files.sha256_file(self.target))
        self.assertNotIn("verification_script_pending", record)
        superseded, = [item for item in record["script_advisories"] if item["status"] == "superseded"]
        self.assertEqual(superseded["applied"], "rebuild_edited_target")
        self.assertEqual(superseded["log_sha256"], saved["log_sha256"])
        self.assertTrue((self.run_path / saved["log"]).read_bytes().startswith(original_log))

    def test_dashboard_build_through_uses_the_configured_automatic_repair_policy(self):
        self.config["max_repair_attempts"] = 1
        def repair(config, targets, worktree, run_path, record, *, operator_instruction=None):
            self.assertEqual(record["status"], "build_failed")
            self.assertIsNone(operator_instruction)
            return True

        with patch.object(build_cycle, "repair_build_failure", side_effect=repair) as attempt:
            self.run_build_through([
                ("dotnet", 1, b"fixture build failed"), ("dotnet", 0, b"build ok"), ("checker", 0, b""),
            ], "checker_succeeded")
        attempt.assert_called_once()

    def test_dashboard_build_through_preserves_failed_build_and_human_review(self):
        record = self.run_build_through([("dotnet", 1, b"fixture build failed")], "build_failed")
        self.assertIsNone(record.get("checker_exit_code"))
        self.assertFalse(workflow.migration_ledger(self.config, self.run_id)["completion"]["verification_passed"])
        self.run_build_through([
            ("dotnet", 0, b"build ok"), ("checker", 7, b"MIG001 Example.cs:1 warning: human review\n"),
        ], "checker_needs_review")
        self.assertFalse(workflow.migration_ledger(self.config, self.run_id)["completion"]["verification_passed"])
        self.assertTrue(self.manager.get(self.run_id).state["review"]["findings"])

    def test_dashboard_build_through_rechecks_configuration_before_enqueue(self):
        plan = self.get_stage_plan("build-through")
        self.config["checker_command"] = ["changed-fixture-checker"]
        with (patch.object(execution, "run_process") as external, self.assertRaises(HTTPError) as stale):
            self.start_stage_plan(plan)
        with stale.exception as response:
            self.assertEqual(response.code, 409)
            self.assertIn("再開計画を確認", response.read().decode())
        external.assert_not_called()
        updated = self.get_stage_plan("build-through")
        self.assertNotEqual(updated["config_version"], plan["config_version"])
        self.assertNotIn("changed-fixture-checker", json.dumps(updated))

    def test_dashboard_build_through_without_a_configured_checker_finishes_at_build(self):
        self.config["checker_command"] = None
        self.save_record({**self.record(), "status": "junie_finished", "build_exit_code": None,
                          "checker_exit_code": None})
        plan = self.get_stage_plan("build-through")
        self.assertEqual(plan["stages"], ["build"])
        commands, calls = self.mock_commands(("dotnet", 0, b"build ok"))
        with commands:
            self.assertEqual(self.start_stage_plan(plan), 202)
            self.wait_until_idle()
        self.assertEqual(calls, [["dotnet", "build"]])
        self.assertEqual(self.record()["status"], "build_succeeded")

    def test_dashboard_checker_from_uses_saved_build_without_rebuilding(self):
        receipt = saved_build_receipt()
        inputs, outputs = receipt["inputs"], receipt["artifacts"]
        previous = self.record()
        previous["checker_build"] = receipt
        previous["verification"] = {"target_hashes": {
            self.target.name: target_files.review_target_hash(self.worktree, Path(self.target.name)),
        }}
        self.save_record(previous)
        commands, calls = self.mock_commands(("checker", 0, b""))
        with (patch.object(build_receipt, "build_inputs", return_value=inputs),
              patch.object(build_receipt, "build_artifacts", return_value=outputs), commands):
            plan = self.get_stage_plan("checker")
            self.assertEqual(self.start_stage_plan(plan), 202)
            self.wait_until_idle()
        self.assertEqual(calls, [["fixture-checker"]])
        self.assertEqual(self.record()["status"], "checker_succeeded")
        self.assertEqual(self.record()["checker_build"], previous["checker_build"])

    def test_natural_rebuild_only_uses_explicit_plan_without_checker(self):
        self.config["checker_command"] = None
        mocked, calls = self.mock_commands(("dotnet", 0, b"build ok"))
        with mocked, patch.object(junie_cli, "run_interactive") as junie:
            identifier, entry = self.send("手修正したので、ビルドだけ")
            self.assertEqual(entry["stage_request"]["start_stage"], "build")
            self.wait_for(identifier, "done")
            self.wait_until_idle()
            junie.assert_not_called()
        self.assertEqual(calls, [["dotnet", "build"]])

    def test_explicit_build_plan_rejects_a_changed_version_then_builds_only(self):
        self.config["checker_command"] = None
        self.assertFalse(self.manager.get(self.run_id).state["resumable"])
        action = {"action": "resume-run", "run_id": self.run_id, "revision": 0, "start_stage": "build"}
        _, old = self.request("resume-plan?" + urlencode({"run_id": self.run_id}))
        self.save_edit("class SavedVersion {}\n")
        with self.assertRaises(HTTPError) as error:
            self.request("control", {**action, "plan_version": old["version"]})
        error.exception.close()
        self.assertEqual(error.exception.code, 409)
        _, new = self.request("resume-plan?" + urlencode({"run_id": self.run_id}))
        self.assertNotEqual(old["version"], new["version"])
        self.assertFalse(new["repair"])
        mocked, calls = self.mock_commands(("dotnet", 0, b"saved version build"))
        with mocked, patch.object(junie_cli, "run_interactive") as junie:
            self.assertEqual(self.request("control", {**action, "plan_version": new["version"]})[0], 202)
            self.wait_until_idle()
            junie.assert_not_called()
        self.assertEqual(calls, [["dotnet", "build"]])
        record = self.record()
        operation, = record["resume_operations"]
        self.assertEqual(operation["version"], new["version"])
        self.assertEqual(operation["invalidates"], ["build", "checker"])
        self.assertEqual(record["status"], "build_succeeded")
        self.assertEqual(record["completed_capsules"], 1)
        self.assertNotIn("checker_exit_code", record)
        self.assertFalse(workflow.can_resume_run(self.config, record))
        self.assertFalse(workflow.migration_ledger(self.config, self.run_id)["completion"]["verification_passed"])
        with patch.object(execution, "run_process") as external:
            with self.assertRaisesRegex(ValueError, "チェッカーを設定"):
                workflow.resume_with_junie(self.config, self.run_id)
            external.assert_not_called()

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
            pending_editor_edit(queue, "Example.cs", {
                "id": "draft", "status": "queued", "base": "class Original {}\n", "content": "class Draft {}\n",
            })
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
        with patch.object(execution, "run_process") as run, patch.object(self.manager, "enqueue") as enqueue:
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

    def test_mixed_requests_keep_the_complete_instruction_without_running_a_subset(self):
        original = self.target.read_bytes()
        record = self.record()
        requests = (
            "バリデーションを追加して、ビルドしてください。",
            "不要な処理を削除して、ビルドしてください。",
            "変数名を変更して、ビルドしてください。",
            "ビルドログを保存する処理を追加してください。",
            "例外時のフォールバックもお願いします。ビルドして",
            "保存した版でビルドして、その前に入力値を検証するように",
            "前回のビルド結果を教えて。必要な措置もお願いします。",
            "今のビルドが終わったら、メッセージも整えてチェッカーを実行して",
            "設定もお願いします。コードは修正してはいけません。ビルドだけ実行してください",
            "First update Example.cs, then build.",
            "What are you doing? Build, too.",
        )
        with (patch.object(subprocess, "Popen", side_effect=AssertionError("unexpected child process")) as subprocess_start,
              patch.object(execution, "run_process") as run,
              patch.object(junie_cli, "run_interactive") as junie):
            for instruction in requests:
                with self.subTest(instruction=instruction):
                    identifier, receipt = self.send(instruction)
                    self.assertEqual(receipt["status"], "received")
                    self.assertNotIn("query_intent", receipt)
                    entry = self.wait_for(identifier, "clarification")
                    self.assertEqual(entry["text"], instruction)
                    self.assertEqual(entry["targets"], receipt["targets"])
                    self.assertIn("まだ何も実行していません", entry["question"])
                    self.assertIn("編集や他の操作を行わず検証だけ", entry["question"])
                    self.assertNotIn("result_status", entry)
                    self.assertEqual(self.send(instruction, identifier)[1]["status"], "clarification")
                    run.assert_not_called()
                    junie.assert_not_called()
                    self.assertEqual(self.target.read_bytes(), original)
                    self.assertEqual(self.record(), record)
                    with editor_queue(self.run_path) as queue:
                        self.assertEqual(queue["prompts"], [])
                    self.send("キャンセル")
                    self.wait_for(identifier, "cancelled")
            subprocess_start.assert_not_called()

    def test_answer_to_mixed_instruction_completes_only_the_confirmed_verification(self):
        instruction = "バリデーションを追加して、ビルドしてください。"
        original = self.target.read_bytes()
        mocked, calls = self.mock_commands(("dotnet", 0, b"ok"))
        with (mocked, patch.object(junie_cli, "run_interactive") as junie):
            identifier, _ = self.send(instruction)
            self.wait_for(identifier, "clarification")
            self.send("ビルドだけ")
            result = self.wait_for(identifier, "done")
            self.assertEqual(result["text"], instruction)
            self.assertEqual(result["answer"], "ビルドだけ")
            self.assertIn("編集や他の操作は実行していません", result["message"])
            junie.assert_not_called()
        self.assertEqual(calls, [["dotnet", "build"]])
        self.assertEqual(self.target.read_bytes(), original)

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
        with mocked, self.close_workers_before_unpatching():
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

    def assert_checker_answer_replayed(self, answer):
        try:
            _, reply = self.send("チェッカーまで", answer)
        except HTTPError as error:
            with error:
                self.fail(f"Replay returned HTTP {error.code}: {error.read().decode('utf-8', errors='replace')}")
        self.assertEqual(reply["status"], "done")

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
            self.assert_checker_answer_replayed(answer)
            self.assertEqual(self.wait_for(identifier, "done")["result_status"], "checker_succeeded")
            self.wait_until_idle()
        self.assertEqual(calls, [["dotnet", "build"], ["fixture-checker"]])
        self.assertEqual(sum(row["status"] == "steering" for row in self.request("editor")[1]["conversation"]), 15)

    def test_replayed_answer_survives_transient_windows_run_record_read(self):
        identifier = self.fill_conversation()
        self.restart_server()
        self.manager.limit = 0
        answer, _ = self.send("チェッカーまで")
        self.wait_for(answer, "done")
        self.wait_for(identifier, "waiting")
        read_text = Path.read_text
        attempts = []

        def read_record(path, *args, **kwargs):
            if path == self.record_path and not attempts:
                attempts.append(path)
                error = PermissionError("fixture: run record shared during replacement")
                error.winerror = 32
                raise error
            return read_text(path, *args, **kwargs)

        with patch.object(Path, "read_text", read_record):
            self.assert_checker_answer_replayed(answer)
        self.assertEqual(attempts, [self.record_path])

    def test_full_conversation_allows_cancellation_and_frees_one_slot(self):
        identifier = self.fill_conversation()
        self.restart_server()
        with patch.object(execution, "run_process") as run:
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
        with patch.object(execution, "run_process") as run:
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
        with mocked, patch.object(repairs, "attempt_repair") as repair:
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
                self.assertEqual(completion, {"complete": False, "verification_passed": False, "total": 1})

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
                cancelled = self.manager.get(self.run_id).control.cancelled
                self.assertTrue(cancelled.wait(6), "the first build was not stopped")
                check_cancelled()
            kwargs["stdout"].write(b"new successful build\n")
            return subprocess.CompletedProcess(command, 0)

        with patch.object(execution, "run_process", side_effect=work), self.close_workers_before_unpatching():
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

        with patch.object(junie_cli, "require_interactive_cli"), \
                patch.object(junie_cli, "run_interactive", side_effect=interrupt) as junie:
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
        stopped = self.manager.get(self.run_id).state
        self.assertEqual(stopped["last_stop"]["phase"], "editor_followup")
        self.assertIn("追加指示を停止しました", stopped["message"])
        if checker is not None:
            for legacy in (False, True):
                with self.subTest(legacy=legacy):
                    if legacy:
                        record = self.record()
                        del record["verification"]["configured_checker_command"]
                        self.save_record(record)
                    before = self.record()
                    self.assertFalse(workflow.can_resume_run(self.config, before))
                    with patch.object(execution, "run_process") as run, \
                            patch.object(junie_cli, "run_interactive") as junie:
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
        with mocked, patch.object(junie_cli, "require_interactive_cli"), \
                patch.object(junie_cli, "run_interactive", side_effect=complete) as junie:
            self.start_build()
            self.wait_until_idle()
            junie.assert_called_once()
        self.manager.conversations.notify(self.run_id)
        self.wait_for(followup, "done")
        self.assertEqual(calls, [["dotnet", "build"]] + ([checker] if checker is not None else []))
        self.assertEqual(self.record()["status"], "checker_succeeded" if checker else "build_succeeded")
        self.assertTrue(self.request("migration-ledger")[1]["completion"]["complete"])

    def wait_until_idle(self):
        """ワーカージョブの解放または背景例外を待つ。"""
        with self.manager.condition:
            idle = self.manager.condition.wait_for(
                lambda: self.background_errors or self.run_id not in self.manager.jobs, timeout=6,
            )
        self.raise_background_error()
        self.assertTrue(idle, self.diagnostics(self.run_id))

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

        with patch.object(execution, "run_process", side_effect=work):
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
            "変更を保存しました。\n\n　保存した版でビルドして、\n\t結果を教えて": "build",
            "保存した版でビルドして、\n\n　結果を教えて。手元で確認しました。\n処理を追加して": "clarify",
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
        with patch.object(execution, "run_process") as run:
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

        with patch.object(execution, "run_process", side_effect=work):
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
            self.assertTrue(completion["complete"])
        self.assertEqual(calls, [("dotnet", original), ("dotnet", edited), ("fixture-checker", edited)])
        self.assertIsNone(self.request("editor")[1]["edit"])
        self.assertEqual(self.record()["status"], "checker_succeeded")
        self.assertNotEqual(initial["built_targets"], final["built_targets"])
        self.assertEqual(final["built_targets"], self.record()["verification"]["target_hashes"])
        self.assertEqual(self.record()["verification"]["configured_checker_command"], ["fixture-checker"])
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
        manifest["unit_id"] = unit_id
        for pin in manifest["php_sources"]:
            snapshot = path / "capsule" / pin["snapshot"]
            snapshot.parent.mkdir(exist_ok=True)
            snapshot.write_bytes((self.run_path / "capsule" / pin["snapshot"]).read_bytes())
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

        with patch.object(execution, "run_process", side_effect=work):
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
                with patch.object(execution, "run_process", side_effect=lambda command, **_: subprocess.CompletedProcess(command, 0)) as run:
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

        with patch.object(execution, "run_process", side_effect=work):
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
        with patch.object(execution, "run_process") as run:
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

        with patch.object(execution, "run_process", side_effect=work):
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
        with patch.object(execution, "run_process") as run:
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

        with patch.object(execution, "run_process", side_effect=work):
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
        write_record = run_records.write_run_record

        def fail_stop(command, completion, token, targets, **kwargs):
            targets[0].write_text("class Unconfirmed {}\n", encoding="utf-8")
            completion.write_text(token, encoding="utf-8")
            running.set()
            self.assertTrue(release.wait(8))
            raise junie_cli.JunieStopError("Junieの終了を確認できませんでした。")

        def save_record(path, record, **kwargs):
            if fail_diagnostic_save and "終了を確認できません" in record.get("error", ""):
                save_failed.set()
                raise PermissionError("fixture: run.json could not be replaced")
            return write_record(path, record, **kwargs)

        with (patch.object(junie_cli, "require_interactive_cli"),
              patch.object(junie_cli, "run_interactive", side_effect=fail_stop),
              patch.object(run_records, "write_run_record", side_effect=save_record),
              patch.object(execution, "run_process") as run):
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

    def test_wrapped_unconfirmed_stop_blocks_build_repair_followup(self):
        self.check_repair_cleanup_failure("build", unconfirmed=True)

    def test_wrapped_unconfirmed_stop_blocks_checker_repair_followup(self):
        self.check_repair_cleanup_failure("checker", unconfirmed=True)

    def test_ordinary_repair_cleanup_failure_allows_waiting_build(self):
        self.check_repair_cleanup_failure("checker", unconfirmed=False)

    def check_repair_cleanup_failure(self, phase, *, unconfirmed):
        self.config["max_repair_attempts"] = 1
        (self.run_path / "agent-workspace").mkdir()
        entered, release = Event(), Event()
        calls = []
        original = self.target.read_bytes()
        expected = ["dotnet"] if phase == "build" else ["dotnet", "fixture-checker"]

        def external(command, *, stdout, **kwargs):
            calls.append(command[0])
            if len(calls) == len(expected):
                diagnostic = (b"Example.cs(1,1): error CS1002: missing semicolon\n" if phase == "build"
                              else b"Example.cs:1 warning: fixture finding\n")
                stdout.write(diagnostic)
                return subprocess.CompletedProcess(command, 1)
            return subprocess.CompletedProcess(command, 0)

        def repair(command, completion, token, targets, **kwargs):
            targets[0].write_text("class Draft {}\n", encoding="utf-8")
            entered.set()
            if not release.wait(8):
                raise TimeoutError("fixture did not release repair")
            if unconfirmed:
                raise junie_cli.JunieStopError("fixture: Junie termination unconfirmed")
            raise junie_cli.JunieSessionError(command, 1, "fixture: repair failed after confirmed exit")

        def close_prompts(queue, success):
            raise PermissionError("fixture: editor cleanup I/O failed")

        with (patch.object(execution, "run_process", side_effect=external),
              patch.object(junie_cli, "require_interactive_cli"),
              patch.object(junie_cli, "run_interactive", side_effect=repair) as junie,
              patch.object(editor_queue_store, "finish_prompts", side_effect=close_prompts)):
            self.start_build()
            try:
                self.assertTrue(entered.wait(5), self.record())
                build, _ = self.send("ビルドして")
                self.wait_for(build, "waiting")
                checker, _ = self.send("チェッカーを実行して")
                self.wait_for(checker, "waiting")
            finally:
                release.set()
            for identifier in (build, checker):
                entry = self.wait_for(identifier, "failed" if unconfirmed else "done")
                if unconfirmed:
                    self.assertIn("termination unconfirmed", entry["message"])
            self.wait_until_idle()
            junie.assert_called_once()
        self.assertEqual(calls, expected if unconfirmed else [*expected, "dotnet", "dotnet", "fixture-checker"])
        self.assertEqual(self.target.read_bytes(), original)
        if unconfirmed:
            self.assertIn("editor cleanup I/O failed", self.record()["error"])
            self.assertIn("termination unconfirmed", self.record()["error"])

    def test_stop_fence_retries_failed_editor_write_and_keeps_other_run_available(self):
        self.check_stop_fence(restart=False, fail_queue=False)

    def test_stop_fence_survives_restart_from_saved_block(self):
        self.check_stop_fence(restart=True, fail_queue=False)

    def test_stop_fence_survives_restart_from_running_job_when_queue_write_also_fails(self):
        self.check_stop_fence(restart=True, fail_queue=True)

    def test_shared_queue_recovers_in_fifo_order_while_unconfirmed_run_stays_blocked(self):
        self.config["max_repair_attempts"] = 1
        self.manager.limit = 1
        (self.run_path / "agent-workspace").mkdir()
        first, first_path, first_tree = self.add_saved_run("first")
        second, second_path, second_tree = self.add_saved_run("second")
        entered, release_stop = Event(), Event()
        editor_unavailable, finish_failed, recover_finish = Event(), Event(), Event()
        start_failed, recover_start, first_started, release_first = Event(), Event(), Event(), Event()
        calls = []
        replace = Path.replace
        queue_path = self.manager.queue_path

        def replace_record(source, target):
            if editor_unavailable.is_set():
                if target == self.run_path / "editor.json":
                    raise PermissionError("fixture: Run A editor is unavailable")
                if target == queue_path:
                    saved = json.loads(source.read_bytes())
                    if self.run_id in saved["blocked_conversations"]:
                        if not recover_finish.is_set():
                            finish_failed.set()
                            raise PermissionError("fixture: queue completion could not be saved")
                        if not recover_start.is_set() and any(
                            job["run_id"] == first and job.get("running") for job in saved["jobs"]
                        ):
                            start_failed.set()
                            raise PermissionError("fixture: queue start could not be saved")
            return replace(source, target)

        def repair(*args, **kwargs):
            entered.set()
            if not release_stop.wait(8):
                raise TimeoutError("fixture did not release Run A")
            raise junie_cli.JunieStopError("fixture: Junie termination unconfirmed")

        def external(command, *, cwd, **kwargs):
            with self.manager.condition:
                saved = json.loads(queue_path.read_bytes())
                run_id = {first_tree: first, second_tree: second}[cwd]
                self.assertTrue(next(job for job in saved["jobs"] if job["run_id"] == run_id)["running"])
            calls.append((cwd, command[0]))
            if cwd == first_tree:
                first_started.set()
                if not release_first.wait(8):
                    raise TimeoutError("fixture did not release the first build")
            return subprocess.CompletedProcess(command, 0)

        with (patch.object(junie_cli, "require_interactive_cli"),
              patch.object(junie_cli, "run_interactive", side_effect=repair) as junie,
              patch.object(execution, "run_process", side_effect=external),
              patch.object(Path, "replace", replace_record)):
            self.send("この指摘は旧 PHP と同じ既定値にして、チェッカーをもう一度")
            try:
                self.assertTrue(entered.wait(5))
                waiting, _ = self.send("ビルドして")
                self.wait_for(waiting, "waiting")
                build_first, _ = self.send("ビルドして", run_id=first)
                with self.manager.condition:
                    self.assertTrue(self.manager.condition.wait_for(lambda: first in self.manager.pending, timeout=5))
                build_second, _ = self.send("ビルドして", run_id=second)
                with self.manager.condition:
                    self.assertTrue(self.manager.condition.wait_for(lambda: second in self.manager.pending, timeout=5))
                    editor_unavailable.set()
                    release_stop.set()

                self.assertTrue(finish_failed.wait(5))
                with self.manager.condition:
                    saved = json.loads(queue_path.read_bytes())
                    self.assertTrue(next(job for job in saved["jobs"] if job["run_id"] == self.run_id)["running"])
                    self.assertEqual(list(self.manager.pending), [first, second])
                    self.assertFalse(self.manager.workers)
                    self.assertEqual(calls, [])
                recover_finish.set()

                self.assertTrue(start_failed.wait(6), "shared queue did not recover without operator action")
                with self.manager.condition:
                    saved = json.loads(queue_path.read_bytes())
                    self.assertIn(self.run_id, saved["blocked_conversations"])
                    self.assertEqual([job["run_id"] for job in saved["jobs"]], [first, second])
                    self.assertTrue(all(not job.get("running") for job in saved["jobs"]))
                    self.assertEqual(list(self.manager.pending), [first, second])
                    for run_id in (first, second):
                        self.assertFalse(self.manager.jobs[run_id].get("running"))
                        view = self.manager.get(run_id)
                        self.assertTrue(view.state["queued"])
                        self.assertIsNone(view.state["operation_id"])
                        self.assertIsNone(view.control)
                    self.assertFalse(self.manager.workers)
                    self.assertEqual(calls, [])
                recover_start.set()

                self.assertTrue(first_started.wait(6))
                with self.manager.condition:
                    self.assertEqual(list(self.manager.workers), [first])
                    self.assertEqual(list(self.manager.pending), [second])
                    self.assertIn(self.run_id, self.manager.blocked_conversations)
                    self.assertEqual(calls, [(first_tree, "dotnet")])
                self.assertEqual(self.wait_for(waiting, "waiting")["status"], "waiting")
                self.manager.conversations.notify(first)
                self.manager.conversations.notify(second)
                release_first.set()
                self.wait_for(build_first, "done", run_path=first_path)
                self.wait_for(build_second, "done", run_path=second_path)
                with self.manager.condition:
                    self.assertTrue(self.manager.condition.wait_for(lambda: not self.manager.busy, timeout=5))

                self.restart_server()
                with self.manager.condition:
                    self.assertIn(self.run_id, self.manager.blocked_conversations)
                    self.assertFalse(self.manager.busy)
                    self.manager.conversations.start()
                editor_unavailable.clear()
                self.assertIn("termination unconfirmed", self.wait_for(waiting, "failed")["message"])
                with self.manager.condition:
                    self.assertTrue(self.manager.condition.wait_for(
                        lambda: self.run_id not in self.manager.blocked_conversations, timeout=5,
                    ))
                junie.assert_called_once()
            finally:
                release_stop.set()
                release_first.set()
        self.assertEqual(calls, [(first_tree, "dotnet"), (second_tree, "dotnet")])

    def check_stop_fence(self, *, restart, fail_queue):
        self.config["max_repair_attempts"] = 1
        (self.run_path / "agent-workspace").mkdir()
        other, other_path, other_tree = self.add_saved_run("other")
        entered, release = Event(), Event()
        enforce_failure, dispatcher_retry = Event(), Event()
        transient = 2
        editor_attempts = 0
        calls = []
        replace = Path.replace
        queue_path = self.manager.queue_path

        def replace_record(source, target):
            nonlocal editor_attempts, transient
            if target == self.run_path / "editor.json":
                if enforce_failure.is_set():
                    editor_attempts += 1
                    if editor_attempts > 22:
                        dispatcher_retry.set()
                elif dispatcher_retry.is_set() and transient:
                    transient -= 1
                else:
                    return replace(source, target)
            elif not (target == queue_path and fail_queue and enforce_failure.is_set()):
                return replace(source, target)
            error = PermissionError("fixture: Windows sharing violation")
            error.winerror = 32
            raise error

        def repair(*args, **kwargs):
            entered.set()
            if not release.wait(8):
                raise TimeoutError("fixture did not release repair")
            raise junie_cli.JunieStopError("fixture: Junie termination unconfirmed")

        def external(command, *, cwd, **kwargs):
            calls.append((Path(cwd), command[0]))
            return subprocess.CompletedProcess(command, 0)

        with (patch.object(junie_cli, "require_interactive_cli"),
              patch.object(junie_cli, "run_interactive", side_effect=repair) as junie,
              patch.object(execution, "run_process", side_effect=external),
              patch.object(Path, "replace", replace_record)):
            self.send("この指摘は旧 PHP と同じ既定値にして、チェッカーをもう一度")
            try:
                self.assertTrue(entered.wait(5))
                waiting, _ = self.send("ビルドして")
                self.wait_for(waiting, "waiting")
                enforce_failure.set()
            finally:
                release.set()
            self.wait_until_idle()
            self.assertTrue(dispatcher_retry.wait(5), "editor replacement was not retried after the worker exited")
            self.assertEqual(self.wait_for(waiting, "waiting")["status"], "waiting")
            self.assertEqual(calls, [])
            self.assertIn("保存できません", self.manager.get(self.run_id).state["message"])
            with self.assertRaises(HTTPError) as denied:
                self.start_build()
            with denied.exception as response:
                self.assertEqual(response.code, 409)
            saved = json.loads(queue_path.read_bytes())
            if fail_queue:
                self.assertTrue(any(job["run_id"] == self.run_id and job["running"] for job in saved["jobs"]))
            else:
                self.assertIn(self.run_id, saved["blocked_conversations"])
                sibling, _ = self.send("ビルドして", run_id=other)
                self.wait_for(sibling, "done", run_path=other_path)
            if restart:
                # Queue storage is available again. The editor file is still locked.
                if fail_queue:
                    self.server_cleanup.close()
                    enforce_failure.clear()
                    with patch.object(run_manager, "load_dashboard_record", side_effect=PermissionError(
                        "fixture: run record temporarily unavailable during restart",
                    )):
                        self.start_server()
                    enforce_failure.set()
                else:
                    self.restart_server()
                with self.manager.condition:
                    self.manager.conversations.start()
                self.assertIn(self.run_id, self.manager.blocked_conversations)
                self.assertEqual(self.wait_for(waiting, "waiting")["status"], "waiting")
            enforce_failure.clear()
            self.assertEqual(self.wait_for(waiting, "failed")["id"], waiting)
            with self.manager.condition:
                self.assertTrue(self.manager.condition.wait_for(
                    lambda: self.run_id not in self.manager.blocked_conversations, timeout=5,
                ))
            self.restart_server()
            self.assertEqual(self.send("ビルドして", waiting)[1]["status"], "failed")
            junie.assert_called_once()
        self.assertEqual(transient, 0)
        self.assertEqual(calls, [] if fail_queue else [(other_tree, "dotnet")])

    def test_checker_repair_passes_instruction_to_junie_and_respects_run_limit(self):
        self.config["max_repair_attempts"] = 1
        mocked, calls = self.mock_commands(
            ("dotnet", 0, b"ok\n"), ("checker", 1, b"NEW Example.cs:2 warning: unresolved\n"),
        )
        text = "この指摘は旧 PHP と同じ既定値にして、チェッカーをもう一度"
        with mocked, patch.object(repairs, "_repair_target") as junie:
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
        with mocked, patch.object(junie_cli, "require_interactive_cli"), patch.object(repairs, "_repair_target") as repair:
            identifier, _ = self.send("保存した版でビルドして。まだエラーがあれば直して")
            self.assertIn("ビルド成功", self.wait_for(identifier, "done")["message"])
            self.assertIn("Current build diagnostic", repair.call_args.args[5])
            self.assertEqual(repair.call_args.args[6]["repair_history"][0]["operator_instruction"],
                             "保存した版でビルドして。まだエラーがあれば直して")
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

        with patch.object(execution, "run_process", side_effect=wait_build):
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
        from migration_harness.runs.editor_queue import LivePrompts, finish_prompts

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
        script_phase = stage_scripts.run_script_phase
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
            patch.object(stage_scripts, "run_script_phase", side_effect=after_build),
            patch.object(execution, "run_process", side_effect=work),
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
            pending_editor_edit(queue, "Example.cs", {
                "id": "saved", "status": "queued", "base": "class Original {}\n", "content": "class Operator {}\n",
            })
        with patch.object(execution, "run_process") as run:
            identifier, _ = self.send("保存した版でビルドして")
            self.assertIn("Example.cs", self.wait_for(identifier, "failed")["message"])
            run.assert_not_called()
        self.assertEqual(self.target.read_bytes(), b"class Changed {}\n")
        self.assertEqual(self.request("editor")[1]["edit"]["status"], "conflict")

    def test_cancelling_queued_verification_cannot_enqueue_it_again(self):
        self.manager.limit = 0
        with patch.object(execution, "run_process") as run:
            identifier, _ = self.send("チェッカーをもう一度")
            self.wait_for(identifier, "waiting")
            self.request("control", {"action": "cancel-queued", "revision": 0, "run_id": self.run_id})
            self.wait_for(identifier, "cancelled")
            self.manager.limit = 5
            self.manager.start_pending()
            self.assertFalse(self.manager.busy)
            run.assert_not_called()

    def test_first_conversation_queue_write_recovers_after_failure(self):
        failed = Event()
        save = self.manager.persist
        recovered = Event()

        def persist():
            if not recovered.is_set():
                failed.set()
                raise PermissionError("fixture storage failure")
            return save()

        mocked, calls = self.mock_commands(("dotnet", 0, b"build ok"))
        with mocked, patch.object(self.manager, "persist", side_effect=persist):
            identifier, receipt = self.send("ビルドして")
            self.assertEqual(receipt["status"], "received")
            self.assertTrue(failed.wait(5))
            self.wait_for(identifier, "waiting")
            self.assertEqual(calls, [])
            self.assertEqual(json.loads(self.manager.queue_path.read_text())["jobs"], [])
            self.assertTrue(self.manager.row(self.manager.get(self.run_id))["queue_saving"])
            recovered.set()
            self.wait_for(identifier, "done")
            self.wait_until_idle()
        self.assertEqual(calls, [["dotnet", "build"]])

    def test_failed_conversation_queue_cancellation_leaves_job_waiting(self):
        self.manager.limit = 0
        action = {"action": "cancel-queued", "revision": 0, "run_id": self.run_id}
        with patch.object(execution, "run_process") as run:
            identifier, _ = self.send("ビルドして")
            self.wait_for(identifier, "waiting")
            saved = self.manager.queue_path.read_bytes()
            with patch.object(self.manager, "persist", side_effect=PermissionError("fixture failure")):
                with self.assertRaises(HTTPError) as error:
                    self.request("control", action)
                error.exception.close()
                self.assertEqual(error.exception.code, 409)
                self.assertTrue(self.manager.get(self.run_id).state["queued"])
                self.assertIn(self.run_id, self.manager.pending)
                self.assertEqual(saved, self.manager.queue_path.read_bytes())
                self.assertNotIn(self.run_id, self.manager.conversations.cancelled)
            self.assertEqual(self.request("control", action)[0], 202)
            self.wait_for(identifier, "cancelled")
            self.restart_server()
            self.assertFalse(self.manager.jobs)
            run.assert_not_called()

    def test_saved_edits_and_junie_followup_request_the_existing_safe_handoff(self):
        from migration_harness.runs.editor_queue import LivePrompts

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
        with patch.object(execution, "run_process") as run:
            identifier, _ = self.send("残りは触らず、今の差分だけ確認して")
            self.assertEqual(self.wait_for(identifier, "done")["review_targets"], ["Example.cs"])
            run.assert_not_called()
