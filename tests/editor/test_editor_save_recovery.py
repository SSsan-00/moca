import io
import json
import subprocess
import uuid
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from threading import Thread
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from tests.support.fixtures import SavedRunFixture
from tests.browser.ui_dependencies import node_executable
from migration_harness import workflow
from migration_harness import execution
from migration_harness.junie import junie_cli
from migration_harness.runs import (
    editor_queue as editor_queue_store, operator_feedback, reference_refresh, verification_edits,
)
from migration_harness.files import sha256_file
from migration_harness.junie.junie_conversation import saved_edits, target_versions
from migration_harness.runs import editor_queue, run_records
from migration_harness.web import dashboard, dashboard_editor


class EditorSaveRecoveryTests(SavedRunFixture):
    def setUp(self):
        super().setUp()
        self.popen = subprocess.Popen
        self.enterContext(patch.object(subprocess, "Popen", side_effect=AssertionError("unexpected child process")))
        self.server = self.enterContext(ExitStack())
        self.start_server()

    def start_server(self, *, ready=True, allow_fixture_adoption=False):
        self.server.close()
        view = self.view = dashboard.Dashboard()
        view.observe(self.record())
        view.ready(self.config, self.record())
        if not ready:
            view.publish({**view.state, "ready": False})
        stream = io.StringIO()
        with redirect_stdout(stream):
            server = view.server(self.config, b"", b"")
        if allow_fixture_adoption:
            original_post = server.RequestHandlerClass.do_POST

            def test_post(handler):
                if not handler.path.endswith("/fixture-adopt"):
                    return original_post(handler)
                self.adopt_queued_edits()
                handler.send_body(200, "application/json", b"{}")

            server.RequestHandlerClass.do_POST = test_post
        worker = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        worker.start()
        self.server.callback(server.server_close)
        self.server.callback(worker.join)
        self.server.callback(server.shutdown)
        self.url = stream.getvalue().splitlines()[0].removeprefix("Progress dashboard: ")
        parsed = urlsplit(self.url)
        self.headers = {"Origin": f"{parsed.scheme}://{parsed.netloc}", "Content-Type": "application/json"}

    def editor(self, action=None, *, target=None):
        route = "editor" if target is None else f"editor?target={target}"
        request = Request(self.url + route, None if action is None else json.dumps(action).encode(), self.headers)
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
                "base": self.editor()["content"], "content": text, "edit_id": None, "save_id": uuid.uuid4().hex}

    def fail_history(self, path, queue):
        if path.name == "editor.json":
            raise OSError("fixture editor history is unwritable")
        run_records.replace_run_record(path, queue)

    def adopt_queued_edits(self):
        unit = self.config["units"][0]
        with editor_queue.editor_queue(self.run_path) as queue:
            request = {"edits": saved_edits(queue), "edit_revision": queue["edit_revision"],
                       "targets": target_versions(self.worktree, unit)}
        verification_edits.apply_verification_edits(self.run_path, self.worktree, unit, request)

    def check_build_history_recovery(self, *, partial=False):
        changes = {self.target.name: "class FirstManual {}\n"}
        if partial:
            (self.worktree / "Second.cs").write_bytes(b"class SecondOriginal {}\n")
            self.config["units"][0]["targets"] = [self.target.name, "Second.cs"]
            self.save_record({**self.record(), "targets": list(self.config["units"][0]["targets"])})
            changes["Second.cs"] = "class SecondManual {}\n"
        self.start_server(ready=False)
        for target, draft in changes.items():
            before = self.editor(target=target)
            saved = self.editor({"kind": "save", "run_id": self.run_id, "target": target,
                                 "base": before["content"], "content": draft, "edit_id": None, "save_id": uuid.uuid4().hex})
            self.assertTrue(saved["queued"])
            self.assertEqual(saved["edit"]["content"], draft)
        original_queue = (self.run_path / "editor.json").read_bytes()
        plan = workflow.plan_build_resume(self.config, self.run_id)
        replace = Path.replace

        def fail_second(path, destination):
            if partial and destination == self.worktree / "Second.cs":
                raise PermissionError("fixture target not writable")
            return replace(path, destination)

        def resume(request, request_id):
            return workflow.resume_with_junie(self.config, self.run_id,
                                               verification_request={**request, "id": request_id})

        with (patch.object(workflow, "_verify_worktree", return_value={"status": "build_succeeded"}) as verify,
              patch.object(junie_cli, "run_interactive") as junie):
            with (patch.object(editor_queue, "replace_run_record", side_effect=self.fail_history),
                  patch.object(Path, "replace", new=fail_second),
                  self.assertRaisesRegex(OSError, "保存履歴が未確定") as failure):
                resume(plan, "first")
            self.assertIn("fixture editor history is unwritable", str(failure.exception.__cause__))
            verify.assert_not_called()
            self.assertEqual(self.target.read_text(encoding="utf-8-sig"), changes[self.target.name])
            self.assertEqual((self.run_path / "editor.json").read_bytes(), original_queue)
            if partial:
                self.assertEqual((self.worktree / "Second.cs").read_text(encoding="utf-8-sig"),
                                 "class SecondOriginal {}\n")
            with self.assertRaisesRegex(ValueError, "保存版または実行条件が更新"):
                resume(plan, "stale")
            saved = workflow.plan_build_resume(self.config, self.run_id)
            self.assertEqual(resume(saved, "retry")["status"], "build_succeeded")
            verify.assert_called_once()
            self.assertEqual(resume(workflow.plan_build_resume(self.config, self.run_id),
                                    "another-build")["status"], "build_succeeded")
            self.assertEqual(verify.call_count, 2)
            junie.assert_not_called()

        agent = self.run_path / "agent-workspace"
        agent.mkdir()
        inputs = []
        hint, report = operator_feedback.operator_edit_context(self.run_path, agent, "fixture", inputs, None)
        self.assertIn("Human-edit handoff", hint)
        self.assertIsNotNone(report)
        self.assertEqual(inputs, [Path(".migration-materials/operator-edits.json")])
        written_handoff = json.loads((agent / inputs[0]).read_bytes())
        with editor_queue.editor_queue(self.run_path) as queue:
            self.assertEqual(queue["edits"], {})
            self.assertEqual(queue["operator_edits"], written_handoff)
            self.assertEqual(set(written_handoff), set(changes))
        for target, draft in changes.items():
            self.assertEqual((self.worktree / target).read_text(encoding="utf-8-sig"), draft)
            patch_text, = written_handoff[target]
            self.assertIn("+" + draft, patch_text)

    def test_build_resume_retains_operator_handoff_after_history_write_failure(self):
        self.check_build_history_recovery()

    def test_build_resume_retains_history_after_partial_target_replacement(self):
        self.check_build_history_recovery(partial=True)

    def check_build_recovery_before_resave(self, *, reverse=False, separate_agent_change=False):
        baseline = "class Example {\n    int First = 0;\n" + "".join(
            f"    int Field{i} = {i};\n" for i in range(8)
        ) + "    int Last = 0;\n}\n"
        self.target.write_bytes(baseline.encode("utf-8-sig"))
        self.start_server(ready=False)
        first = baseline.replace("First = 0", "First = 9")
        earlier = self.editor({**self.save_action(first), "save_id": "1" * 32})
        self.assertTrue(earlier["queued"])
        unrelated = "Field3 = 33"
        if separate_agent_change:
            self.target.write_bytes(baseline.replace("Field3 = 3", unrelated).encode("utf-8-sig"))

        def git_only(command, *args, **kwargs):
            self.assertEqual(command[:2], ["git", "merge-file"])
            return self.popen(command, *args, **kwargs)

        def build(request_id):
            plan = workflow.plan_build_resume(self.config, self.run_id)
            return workflow.resume_with_junie(self.config, self.run_id,
                                               verification_request={**plan, "id": request_id})

        with (patch.object(subprocess, "Popen", side_effect=git_only),
              patch.object(workflow, "_verify_worktree", return_value={"status": "build_succeeded"}) as verify):
            with (patch.object(editor_queue, "replace_run_record", side_effect=self.fail_history),
                  self.assertRaises(OSError)):
                build("physical-write-before-history")
            verify.assert_not_called()
            before_resave = first.replace("Field3 = 3", unrelated) if separate_agent_change else first
            self.assertEqual(self.target.read_text(encoding="utf-8-sig"), before_resave)
            self.start_server(ready=False)
            view = self.editor()
            draft = view["content"].replace("Last = 0", "Last = 7")
            if reverse:
                draft = draft.replace("First = 9", "First = 0")
            previous = view["edit"] or {}
            saved = self.editor({
                "kind": "save", "run_id": self.run_id, "target": self.target.name,
                "base": previous.get("base", view["content"]), "content": draft,
                "edit_id": previous.get("id"), "save_id": "2" * 32,
            })
            self.assertTrue(saved["queued"])
            self.assertEqual(build("retry")["status"], "build_succeeded")
            verify.assert_called_once()
            self.assertEqual(build("repeat")["status"], "build_succeeded")
            self.assertEqual(verify.call_count, 2)
        self.assertEqual(self.target.read_text(encoding="utf-8-sig"), draft)
        agent = self.run_path / "agent-workspace"
        agent.mkdir()
        inputs = []
        hint, _ = operator_feedback.operator_edit_context(self.run_path, agent, "fixture", inputs, None)
        self.assertIn("Human-edit handoff", hint)
        operator = json.loads((agent / inputs[0]).read_bytes())
        with editor_queue.editor_queue(self.run_path) as queue:
            self.assertEqual(queue["edits"], {})
            self.assertEqual(queue["operator_edits"], operator)
        history = operator[self.target.name]
        self.assertEqual(len(history), 2)
        self.assertIn("-    int First = 0;", history[0])
        self.assertIn("+    int First = 9;", history[0])
        self.assertIn("+    int Last = 7;", history[1])
        self.assertEqual("+    int First = 0;" in history[1], reverse)
        self.assertNotIn("+    int Field3 = 33;", "".join(history))

    def test_resave_after_build_history_failure_retains_both_operator_changes(self):
        self.check_build_recovery_before_resave()

    def test_resave_can_reverse_the_already_adopted_operator_change(self):
        self.check_build_recovery_before_resave(reverse=True)

    def test_resave_after_build_history_failure_does_not_attribute_agent_changes(self):
        self.check_build_recovery_before_resave(separate_agent_change=True)

    def check_normal_resume_edit_recovery(self, *, boundary=None, reverse=False, separate_agent_change=False, direct=False):
        baseline = "class Example {\n    int First = 0;\n" + "".join(
            f"    int Field{i} = {i};\n" for i in range(8)
        ) + "    int Last = 0;\n}\n"
        self.target.write_bytes(baseline.encode("utf-8-sig"))
        agent = self.run_path / "agent-workspace"
        agent.mkdir()
        paths = [self.target.name, "Second.cs"] if boundary in {"second-target", "second-agent"} else [self.target.name]
        if len(paths) > 1:
            (self.worktree / paths[1]).write_bytes("class Second {}\n".encode("utf-8-sig"))
            self.config["units"][0]["targets"] = paths
        for name in paths:
            (agent / name).write_bytes((self.worktree / name).read_bytes())
        hashes = {name: sha256_file(self.worktree / name) for name in paths}
        self.save_record({**self.record(), "status": "editor_checkpoint", "targets": paths, "junie_exit_code": 0,
                          "target_hashes": hashes, "target_sha256": hashes[self.target.name]})
        self.start_server(ready=False)
        first = baseline.replace("First = 0", "First = 9")
        for index, (name, content) in enumerate(zip(paths, [first, "class SecondManual {}\n"]), 1):
            view = self.editor(target=name)
            saved = self.editor({"kind": "save", "run_id": self.run_id, "target": name, "base": view["content"],
                                 "content": content, "edit_id": None, "save_id": str(index) * 32})
            self.assertTrue(saved["queued"])
        if separate_agent_change:
            (agent / self.target.name).write_bytes(baseline.replace("Field3 = 3", "Field3 = 33").encode("utf-8-sig"))
            first = first.replace("Field3 = 3", "Field3 = 33")
        replace = Path.replace
        fault = {"worktree": self.target, "second-target": self.worktree / "Second.cs",
                 "second-agent": agent / "Second.cs"}.get(boundary)

        def fail_target(path, destination):
            if destination == fault:
                raise OSError("fixture target replace failed")
            return replace(path, destination)

        def git_only(command, *args, **kwargs):
            self.assertEqual(command[:2], ["git", "merge-file"])
            return self.popen(command, *args, **kwargs)

        def resume():
            return workflow.resume_with_junie(self.config, self.run_id)

        with (patch.object(subprocess, "Popen", side_effect=git_only),
              patch.object(workflow, "_verify_worktree", return_value={"status": "build_succeeded"}) as verify,
              patch.object(junie_cli, "require_interactive_cli"),
              patch.object(junie_cli, "run_interactive") as junie):
            gate = (patch.object(editor_queue, "replace_run_record", side_effect=self.fail_history)
                    if fault is None else patch.object(Path, "replace", new=fail_target))
            with gate, self.assertRaises(OSError):
                resume()
            verify.assert_not_called()
            self.assertEqual((agent / self.target.name).read_text(encoding="utf-8-sig"), first)
            if boundary == "worktree":
                self.assertEqual(self.target.read_text(encoding="utf-8-sig"), baseline)
            if boundary in {"second-target", "second-agent"}:
                self.assertEqual((self.worktree / "Second.cs").read_text(encoding="utf-8-sig"), "class Second {}\n")
            if direct:
                self.assertEqual(resume()["status"], "build_succeeded")
                verify.assert_called_once()
            self.start_server(ready=False)
            view = self.editor()
            self.assertEqual(view["content"], first)
            draft = first.replace("Last = 0", "Last = 7")
            if reverse:
                draft = draft.replace("First = 9", "First = 0")
            edit = view["edit"] or {}
            saved = self.editor({"kind": "save", "run_id": self.run_id, "target": self.target.name,
                                 "base": edit.get("base", view["content"]), "content": draft,
                                 "edit_id": edit.get("id"), "save_id": "3" * 32})
            self.assertTrue(saved["queued"])
            self.assertEqual(resume()["status"], "build_succeeded")
            self.assertEqual(resume()["status"], "build_succeeded")
            self.assertEqual(verify.call_count, 3 if direct else 2)
            junie.assert_not_called()

        for directory in (self.worktree, agent):
            self.assertEqual((directory / self.target.name).read_text(encoding="utf-8-sig"), draft)
            if len(paths) > 1:
                self.assertEqual((directory / "Second.cs").read_text(encoding="utf-8-sig"), "class SecondManual {}\n")
        inputs = []
        operator_feedback.operator_edit_context(self.run_path, agent, "fixture-normal", inputs, None)
        operator = json.loads((agent / inputs[0]).read_bytes())
        with editor_queue.editor_queue(self.run_path) as queue:
            self.assertEqual(queue["edits"], {})
            self.assertEqual(queue["operator_edits"], operator)
        self.assertEqual(set(operator), set(paths))
        self.assertEqual(len(operator[self.target.name]), 2)
        older, newer = operator[self.target.name]
        self.assertIn("+    int First = 9;", older)
        self.assertIn("+    int Last = 7;", newer)
        self.assertEqual("+    int First = 0;" in newer, reverse)
        self.assertNotIn("+    int Field3 = 33;", older + newer)
        if len(paths) > 1:
            second, = operator["Second.cs"]
            self.assertIn("+class SecondManual {}", second)

    def test_normal_resume_keeps_operator_history_after_failed_save_and_resave(self):
        self.check_normal_resume_edit_recovery(reverse=True, separate_agent_change=True)

    def test_normal_resume_recovers_between_agent_and_worktree_replacement(self):
        self.check_normal_resume_edit_recovery(boundary="worktree")

    def test_normal_resume_recovers_partial_worktree_targets(self):
        self.check_normal_resume_edit_recovery(boundary="second-target")

    def test_normal_resume_recovers_partial_agent_targets(self):
        self.check_normal_resume_edit_recovery(boundary="second-agent", direct=True)

    def test_model_revision_is_checked_at_resume_acceptance_and_worker_start(self):
        self.save_record({**self.record(), "status": "junie_finished",
                          "junie_model": "old", "junie_effort": "low", "junie_model_revision": "a" * 24})
        self.start_server()
        self.assertTrue(self.view.state["resumable"])

        def control(revision):
            action = {"action": "resume", "run_id": self.run_id, "revision": self.view.revision, "model_revision": revision}
            request = Request(self.url + "control", json.dumps(action).encode(), self.headers)
            with urlopen(request, timeout=4) as response:
                return response.status

        def save(model, effort, revision):
            return self.editor({"kind": "model", "run_id": self.run_id, "model": model, "effort": effort,
                                "model_revision": revision})["junie_model_revision"]

        current = save("new", "high", "a" * 24)
        with self.assertRaises(HTTPError) as stale:
            control("a" * 24)
        with stale.exception as response:
            self.assertEqual(response.code, 409)
            self.assertIn("モデル設定が更新", response.read().decode())
        self.assertTrue(self.view.actions.empty())

        self.assertEqual(control(current), 202)
        accepted = self.view.actions.get_nowait()
        self.assertEqual(accepted["model_revision"], current)
        self.assertEqual(run_records.run_model_options(self.run_path, self.record(), self.config),
                         {"model": "new", "effort": "high"})
        save("next", "medium", current)
        with patch.object(dashboard, "resume_with_junie") as launch:
            dashboard.execute_dashboard_action(self.view, self.config, accepted)
        launch.assert_not_called()
        self.assertTrue(self.view.state["ready"])
        self.assertIn("モデル設定が更新", self.view.state["error_details"])

    def leave_applied_build_edit(self):
        self.start_server(ready=False)
        self.assertTrue(self.editor({**self.save_action("class Manual {}\n"), "save_id": "1" * 32})["queued"])
        plan = workflow.plan_build_resume(self.config, self.run_id)
        with (patch.object(editor_queue, "replace_run_record", side_effect=self.fail_history),
              patch.object(workflow, "_verify_worktree") as verify,
              self.assertRaisesRegex(OSError, "保存履歴が未確定")):
            workflow.resume_with_junie(self.config, self.run_id, verification_request={**plan, "id": "fixture"})
        verify.assert_not_called()
        self.assertEqual(self.target.read_text(encoding="utf-8-sig"), "class Manual {}\n")
        journal = self.run_path / editor_queue.EDITOR_SAVE_PENDING
        self.assertTrue(journal.is_file())
        return journal

    def test_build_plan_recovers_without_opening_editor_and_commits_history_once(self):
        journal = self.leave_applied_build_edit()
        history = self.run_path / "editor.json"
        before = history.read_bytes()
        with (patch.object(editor_queue, "replace_run_record", side_effect=self.fail_history),
              self.assertRaisesRegex(OSError, "保存履歴が未確定")):
            workflow.plan_build_resume(self.config, self.run_id)
        self.assertEqual(history.read_bytes(), before)
        self.assertTrue(journal.is_file())
        unlink = Path.unlink

        def fail_to_remove_journal(path, *args, **kwargs):
            if path == journal:
                raise PermissionError("fixture recovery journal cannot be removed")
            return unlink(path, *args, **kwargs)

        with (patch.object(Path, "unlink", new=fail_to_remove_journal),
              self.assertRaisesRegex(OSError, "保存履歴が未確定")):
            workflow.plan_build_resume(self.config, self.run_id)
        committed = history.read_bytes()
        self.assertTrue(journal.is_file())
        self.assertEqual(json.loads(committed)["edits"], {})
        self.assertEqual(len(json.loads(committed)["operator_edits"][self.target.name]), 1)
        plan = workflow.plan_build_resume(self.config, self.run_id)
        self.assertEqual(plan["edits"], {})
        self.assertFalse(journal.exists())
        self.assertEqual(history.read_bytes(), committed)
        with patch.object(workflow, "_verify_worktree", return_value={"status": "build_succeeded"}) as verify:
            self.assertEqual(workflow.resume_with_junie(
                self.config, self.run_id, verification_request={**plan, "id": "recovered"},
            )["status"], "build_succeeded")
            verify.assert_called_once()
        self.assertEqual(history.read_bytes(), committed)

    def test_build_history_recovery_stops_for_an_unrecognized_target_version(self):
        journal = self.leave_applied_build_edit()
        history = self.run_path / "editor.json"
        before = history.read_bytes()
        applied = self.target.read_bytes()
        self.target.write_bytes(b"class External {}\n")
        with self.assertRaisesRegex(ValueError, "外部で更新"):
            workflow.plan_build_resume(self.config, self.run_id)
        self.assertIn("外部で更新", self.http_conflict())
        self.assertEqual(self.target.read_bytes(), b"class External {}\n")
        self.assertEqual(history.read_bytes(), before)
        self.assertTrue(journal.is_file())
        self.target.write_bytes(applied)
        self.assertEqual(workflow.plan_build_resume(self.config, self.run_id)["edits"], {})
        self.assertFalse(journal.exists())

    def test_failed_history_stops_resume_until_recovered_and_does_not_reapply(self):
        self.stage_old_instruction()
        payload = self.save_action("class Manual {}\n")
        history = self.run_path / "editor.json"
        before = history.read_bytes()
        with patch.object(editor_queue, "replace_run_record", side_effect=self.fail_history), \
                patch.object(reference_refresh, "refresh_target_context_on_resume") as refresh, \
                patch.object(junie_cli, "run_interactive") as junie, \
                patch.object(execution, "run_process") as external:
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
        with patch.object(reference_refresh, "refresh_target_context_on_resume"), \
                patch.object(workflow, "_verify_worktree", return_value={"status": "build_succeeded"}) as verify, \
                patch.object(junie_cli, "run_interactive") as junie:
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

    def test_old_save_without_valid_id_cannot_commit_a_pending_modern_journal(self):
        self.stage_old_instruction()
        payload = self.save_action("class Manual {}\n")
        with patch.object(editor_queue, "replace_run_record", side_effect=self.fail_history):
            self.assertIn("履歴", self.http_conflict(payload))
        files = (self.target, self.run_path / "editor.json", self.run_path / editor_queue.EDITOR_SAVE_PENDING)
        before = tuple(path.read_bytes() for path in files)
        for value in ({}, {"save_id": None}, {"save_id": 123}, {"save_id": "not-a-save-id"}):
            with self.subTest(value=value):
                old = {key: item for key, item in payload.items() if key != "save_id"}
                self.assertIn("保存ID", self.http_conflict({**old, **value}))
                self.assertEqual(tuple(path.read_bytes() for path in files), before)
        self.assertEqual(self.editor(payload)["accepted_save_id"], payload["save_id"])
        self.assertFalse(files[2].exists())

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

    def test_same_change_after_external_restore_requires_a_new_save_id(self):
        original = self.target.read_bytes()
        first = {**self.save_action("class Saved {}\n"), "save_id": "1" * 32}
        self.editor(first)
        history = self.run_path / "editor.json"
        saved = history.read_bytes()

        self.target.write_bytes(original)
        self.start_server()
        self.assertEqual(self.editor()["content"], first["base"])
        self.assertIn("更新", self.http_conflict(first))
        self.assertEqual(self.target.read_bytes(), original)
        self.assertEqual(history.read_bytes(), saved)

        fresh = {**self.save_action(first["content"]), "save_id": "2" * 32}
        self.assertEqual(self.editor(fresh)["content"], first["content"])
        self.assertEqual(len(json.loads(history.read_bytes())["operator_edits"][self.target.name]), 2)
        latest = history.read_bytes()
        self.editor(fresh)
        self.assertIn("同じ保存ID", self.http_conflict({**fresh, "content": "class Other {}\n"}))
        self.assertEqual(history.read_bytes(), latest)
        self.target.write_bytes(original)
        self.assertIn("更新", self.http_conflict(first))
        self.assertEqual(self.target.read_bytes(), original)
        self.assertEqual(history.read_bytes(), latest)

    def test_unexpected_file_after_failed_history_blocks_resume_without_overwriting(self):
        payload = self.save_action("class Manual {}\n")
        with patch.object(editor_queue, "replace_run_record", side_effect=self.fail_history):
            self.assertIn("履歴", self.http_conflict(payload))
        external = b"class External {}\r\n"
        self.target.write_bytes(external)
        self.start_server()
        self.assertIn("更新", self.http_conflict())
        self.assertIn("更新", self.http_conflict(payload))
        with patch.object(reference_refresh, "refresh_target_context_on_resume") as refresh:
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
        with patch.object(dashboard_editor, "save_target_edit", side_effect=OSError("fixture target is unwritable")):
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

    def test_queued_receipts_keep_the_version_and_later_prompts(self):
        original = self.target.read_bytes()
        second_target = self.worktree / "Other.cs"
        names = [self.target.name, second_target.name]
        self.config["units"][0]["targets"] = names
        self.save_record({**self.record(), "targets": names})
        self.start_server()
        immediate = {**self.save_action("class ImmediatelySaved {}\n"), "save_id": "0" * 32}
        self.assertIsNone(self.editor(immediate)["edit"])
        history = self.run_path / "editor.json"
        immediate_history = history.read_bytes()
        self.start_server(ready=False)
        self.assertIsNone(self.editor(immediate)["edit"])
        self.assertEqual(history.read_bytes(), immediate_history)
        first = {**self.save_action("class Queued {}\n"), "save_id": "1" * 32}
        self.assertIn("同じ保存ID", self.http_conflict({**first, "save_id": immediate["save_id"]}))
        accepted = self.editor(first)["edit"]
        self.stage_old_instruction()
        saved = history.read_bytes()
        self.start_server()
        self.assertEqual(self.editor(first)["edit"], accepted)
        self.assertIn("更新", self.http_conflict(immediate))
        self.assertEqual(history.read_bytes(), saved, "retry must not cancel later prompts or change the edit version")
        for changed in [{"content": "class Other {}"}, {"resolve": True},
                        {"edit_id": accepted["id"]}, {"target": second_target.name}]:
            with self.subTest(changed=changed):
                self.assertIn("同じ保存ID", self.http_conflict({**first, **changed}))
        self.assertEqual(history.read_bytes(), saved)
        next_edit = {**first, "content": "class AnotherTab {}\n", "save_id": "2" * 32, "edit_id": accepted["id"]}
        final = self.editor(next_edit)["edit"]
        saved = history.read_bytes()
        self.assertIn("更新", self.http_conflict(first))
        self.assertEqual(self.editor()["edit"], final)
        self.assertEqual(history.read_bytes(), saved)
        self.assertFalse(second_target.exists())
        self.assertNotEqual(self.target.read_bytes(), original)
        self.assertIn(b"ImmediatelySaved", self.target.read_bytes())

    def test_replayed_queued_save_already_adopted_keeps_later_instructions(self):
        self.start_server(ready=False)
        action = {**self.save_action("class Adopted {}\n"), "save_id": "1" * 32}
        self.editor(action)
        self.adopt_queued_edits()
        self.stage_old_instruction()
        history = self.run_path / "editor.json"
        saved = history.read_bytes()
        self.start_server()
        self.assertIsNone(self.editor()["edit"])
        repeated = self.editor(action)
        self.assertIsNone(repeated["edit"])
        self.assertEqual(repeated["accepted_save_id"], action["save_id"])
        self.assertEqual(repeated["content"], action["content"])
        self.assertEqual(history.read_bytes(), saved)
        self.target.write_bytes(b"class ChangedExternally {}\r\n")
        self.assertIn("更新", self.http_conflict(action))
        self.assertEqual(history.read_bytes(), saved)
        self.assertIn(b"ChangedExternally", self.target.read_bytes())

    def test_recovered_merge_receipt_matches_persisted_bytes_before_accepting_next_draft(self):
        baseline = "class File {\n    int First = 1;\n" + "    // context\n" * 7 + "    int Last = 1;\n}\n"
        first = baseline.replace("First = 1", "First = 2")
        merged = first.replace("Last = 1", "Last = 2")
        self.target.write_bytes(baseline.encode("utf-8"))
        agent = self.run_path / "agent-workspace"
        agent.mkdir()
        (agent / self.target.name).write_bytes(baseline.replace("Last = 1", "Last = 2").encode("utf-8"))
        record = {**self.record(), "target_hashes": {self.target.name: sha256_file(self.target)}}
        self.save_record(record)
        self.start_server(ready=False)
        save = {**self.save_action(first), "save_id": "1" * 32}
        queued = self.editor(save)

        def git_only(command, *args, **kwargs):
            self.assertEqual(command[:2], ["git", "merge-file"])
            return self.popen(command, *args, **kwargs)

        with (patch.object(subprocess, "Popen", side_effect=git_only),
              patch.object(editor_queue, "replace_run_record", side_effect=self.fail_history),
              self.assertRaises(OSError)):
            with editor_queue.editor_queue(self.run_path) as queue:
                editor_queue_store.apply_editor_edits(queue, agent, self.worktree, (Path(self.target.name),), record, self.record_path)
        self.assertTrue(editor_queue.editor_save_pending(self.run_path))
        self.assertEqual(self.target.read_text(encoding="utf-8-sig"), merged)
        self.start_server()
        adopted = self.editor()
        self.assertEqual(adopted["accepted_save_id"], save["save_id"])
        self.assertEqual(adopted["accepted_edit_id"], queued["edit"]["id"])
        self.assertEqual(adopted["accepted_version"], sha256_file(self.target))
        self.assertFalse(editor_queue.editor_save_pending(self.run_path))
        self.assertEqual(self.editor(save)["content"], merged, "the original request can still be retried after adoption")

        further = first.replace("First = 2", "First = 3")
        action = {**self.save_action(further), "base": first, "save_id": "2" * 32,
                  "adoption": {"save_id": save["save_id"], "edit_id": adopted["accepted_edit_id"],
                               "version": adopted["accepted_version"]}}
        original = self.target.read_bytes()
        self.target.write_bytes(merged.encode("utf-8"))
        self.assertNotIn("accepted_save_id", self.editor())
        self.assertIn("基準版", self.http_conflict(action))
        self.assertEqual(self.target.read_bytes(), merged.encode("utf-8"))
        self.target.write_bytes(original)
        self.assertIn("基準版", self.http_conflict({**action, "base": merged}))
        saved = self.editor(action)
        self.assertEqual(saved["edit"]["base"], first)
        self.assertEqual(self.editor(action)["edit"], saved["edit"])
        with patch.object(subprocess, "Popen", side_effect=git_only):
            self.adopt_queued_edits()
        self.assertEqual(self.target.read_text(encoding="utf-8-sig"), merged.replace("First = 2", "First = 3"))

    def test_lost_queued_save_response_and_editor_poll_through_actual_http(self):
        node = node_executable(self)
        self.start_server(ready=False, allow_fixture_adoption=True)
        root = Path(__file__).resolve().parents[2]
        web = root / "src/migration_harness/web"
        command = [node, str(root / "tests/browser/editor_run_isolation.cjs"), str(web / "dashboard_editor.js"),
                   str(web / "dashboard.js"), str(web / "dashboard_chat.js"), self.url, self.run_id]
        with patch.object(subprocess, "Popen", self.start_node_only(self.popen, command)):
            result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("live HTTP queued saves recover lost receipts", result.stdout)

    @staticmethod
    def start_node_only(popen, command):
        def start(args, *options, **kwargs):
            if args != command:
                raise AssertionError("unexpected child process")
            return popen(args, *options, **kwargs)
        return start
