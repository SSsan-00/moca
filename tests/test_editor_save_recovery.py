import io
import json
import subprocess
from contextlib import ExitStack, redirect_stdout
from threading import Thread
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from fixtures import SavedRunFixture
from migration_harness import workflow
from migration_harness.runs import editor_queue, run_records
from migration_harness.web import dashboard


class EditorSaveRecoveryTests(SavedRunFixture):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(subprocess, "Popen", side_effect=AssertionError("unexpected child process")))
        self.server = self.enterContext(ExitStack())
        self.start_server()

    def start_server(self):
        self.server.close()
        view = dashboard.Dashboard()
        view.observe(self.record())
        view.ready(self.config, self.record())
        stream = io.StringIO()
        with redirect_stdout(stream):
            server = view.server(self.config, b"", b"")
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.server.callback(server.server_close)
        self.server.callback(worker.join)
        self.server.callback(server.shutdown)
        self.url = stream.getvalue().splitlines()[0].removeprefix("Progress dashboard: ")
        parsed = urlsplit(self.url)
        self.headers = {"Origin": f"{parsed.scheme}://{parsed.netloc}", "Content-Type": "application/json"}

    def editor(self, action=None):
        request = Request(self.url + "editor", None if action is None else json.dumps(action).encode(), self.headers)
        with urlopen(request, timeout=4) as reply:
            return json.load(reply)

    def http_conflict(self, action=None):
        with self.assertRaises(HTTPError) as failed:
            self.editor(action)
        with failed.exception as reply:
            self.assertEqual(reply.code, 409)
            return reply.read().decode()

    def stage_old_instruction(self):
        identifier = "previous"
        with editor_queue.editor_queue(self.run_path) as queue:
            queue["prompts"].append({"id": identifier, "text": "古い指示", "status": "queued"})
            queue["conversation"] = [{"id": identifier, "text": "古い指示", "status": "steering"}]
            queue["edit_revision"] = "old-revision"

    def save_action(self, text):
        return {"kind": "save", "run_id": self.run_id, "target": self.target.name,
                "base": self.editor()["content"], "content": text, "edit_id": None}

    def fail_history(self, path, queue):
        if path.name == "editor.json":
            raise OSError("fixture editor history is unwritable")
        run_records.replace_run_record(path, queue)

    def test_failed_history_stops_resume_until_recovered_and_does_not_reapply(self):
        self.stage_old_instruction()
        payload = self.save_action("class Manual {}\n")
        history = self.run_path / "editor.json"
        before = history.read_bytes()
        with patch.object(editor_queue, "replace_run_record", side_effect=self.fail_history), \
                patch.object(workflow, "_refresh_target_context_on_resume") as refresh, \
                patch.object(workflow.junie_cli, "run_interactive") as junie, \
                patch.object(workflow, "run_process") as external:
            failure = self.http_conflict(payload)
            self.assertEqual(self.target.read_bytes(), b"\xef\xbb\xbfclass Manual {}\r\n")
            self.assertEqual(history.read_bytes(), before)
            self.assertIn("履歴", self.http_conflict())
            self.assertIn("履歴", failure)
            with self.assertRaisesRegex(OSError, "履歴"):
                workflow.resume_with_junie(self.config, self.run_id)
            refresh.assert_not_called()
            junie.assert_not_called()
            external.assert_not_called()
        self.start_server()
        recovered = self.editor()
        self.assertEqual(recovered["content"], payload["content"])
        self.assertEqual(recovered["prompts"], [])
        self.assertIsNone(recovered["edit"])
        saved = json.loads(history.read_bytes())
        self.assertEqual(saved["conversation"][0]["status"], "failed")
        self.assertNotEqual(saved["edit_revision"], "old-revision")
        self.assertEqual(len(saved["operator_edits"][self.target.name]), 1)
        self.assertIn("+class Manual {}", saved["operator_edits"][self.target.name][0])
        self.assertEqual(self.editor(payload)["content"], payload["content"])
        self.assertEqual(json.loads(history.read_bytes()), saved)
        with patch.object(workflow, "_refresh_target_context_on_resume"), \
                patch.object(workflow, "_verify_worktree", return_value={"status": "build_succeeded"}) as verify, \
                patch.object(workflow.junie_cli, "run_interactive") as junie:
            self.assertEqual(workflow.resume_with_junie(self.config, self.run_id)["status"], "build_succeeded")
            verify.assert_called_once()
            junie.assert_not_called()
        self.assertEqual(json.loads(history.read_bytes()), saved)

    def test_new_file_recovers_without_waiting_prompts(self):
        self.target.unlink()
        payload = self.save_action("class New {}\n")
        with patch.object(editor_queue, "replace_run_record", side_effect=self.fail_history):
            self.assertIn("履歴", self.http_conflict(payload))
        self.assertTrue(self.target.is_file())
        self.start_server()
        recovered = self.editor(payload)
        self.assertIsNone(recovered["edit"])
        self.assertEqual(recovered["content"], payload["content"])
        with editor_queue.editor_queue(self.run_path) as queue:
            self.assertEqual(queue["prompts"], [])
            self.assertEqual(len(queue["operator_edits"][self.target.name]), 1)

    def test_successful_save_retry_and_external_change_do_not_rewrite_history(self):
        self.stage_old_instruction()
        payload = self.save_action("class Saved {}\n")
        self.editor(payload)
        history = self.run_path / "editor.json"
        saved = history.read_bytes()
        self.start_server()
        self.assertIsNone(self.editor(payload)["edit"])
        self.assertEqual(history.read_bytes(), saved)
        external = b"class External {}\r\n"
        self.target.write_bytes(external)
        self.assertIn("更新", self.http_conflict(payload))
        self.assertEqual(self.target.read_bytes(), external)
        self.assertEqual(history.read_bytes(), saved)

    def test_unexpected_file_after_failed_history_blocks_resume_without_overwriting(self):
        payload = self.save_action("class Manual {}\n")
        with patch.object(editor_queue, "replace_run_record", side_effect=self.fail_history):
            self.assertIn("履歴", self.http_conflict(payload))
        external = b"class External {}\r\n"
        self.target.write_bytes(external)
        self.start_server()
        self.assertIn("更新", self.http_conflict())
        self.assertIn("更新", self.http_conflict(payload))
        with patch.object(workflow, "_refresh_target_context_on_resume") as refresh:
            with self.assertRaisesRegex(ValueError, "更新"):
                workflow.resume_with_junie(self.config, self.run_id)
            refresh.assert_not_called()
        self.assertEqual(self.target.read_bytes(), external)

    def test_failure_before_source_replacement_preserves_the_old_state(self):
        self.stage_old_instruction()
        payload = self.save_action("class Manual {}\n")
        body = self.target.read_bytes()
        history = self.run_path / "editor.json"
        before = history.read_bytes()
        with patch.object(dashboard, "save_target_edit", side_effect=OSError("fixture target is unwritable")):
            self.assertIn("履歴", self.http_conflict(payload))
        self.start_server()
        self.assertEqual(self.editor()["content"], payload["base"])
        self.assertEqual(self.target.read_bytes(), body)
        self.assertEqual(history.read_bytes(), before)
        self.editor(payload)
        self.assertEqual(self.editor()["prompts"], [])

    def test_recovery_after_history_was_written_does_not_repeat_it(self):
        self.stage_old_instruction()
        payload = self.save_action("class Manual {}\n")

        def write_then_fail(path, queue):
            run_records.replace_run_record(path, queue)
            if path.name == "editor.json":
                raise OSError("fixture stopped after writing")

        with patch.object(editor_queue, "replace_run_record", side_effect=write_then_fail):
            self.assertIn("履歴", self.http_conflict(payload))
        history = self.run_path / "editor.json"
        before = history.read_bytes()
        self.start_server()
        self.assertEqual(self.editor(payload)["content"], payload["content"])
        self.assertEqual(history.read_bytes(), before)
