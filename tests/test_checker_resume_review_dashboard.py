from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
import unittest
import uuid
from hashlib import sha256
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from fixtures import SavedRunFixture
from migration_harness.verification import build_receipt
from migration_harness import cli, workflow
from migration_harness.web import dashboard
from migration_harness.junie import junie_cli
from migration_harness.runs import run_records
from migration_harness.runs.editor_queue import add_prompt, editor_queue, record_operator_edit
from migration_harness.execution import ExecutionControl, OperationCancelled, control_scope
from migration_harness.web.settings_form import build_settings_draft


class JunieConnectionTests(unittest.TestCase):
    def test_matching_junie_session_counts_complete_usage_events_once(self):
        def event(task, **usage):
            return {"kind": "SessionA2uxEvent", "taskId": task, "event": {"agentEvent": {
                "kind": "LlmResponseMetadataEvent", "modelUsage": [usage],
            }}}

        def line(entry):
            return json.dumps(entry).encode("utf-8") + b"\n"

        with TemporaryDirectory() as directory, patch.dict("os.environ", {"JUNIE_HOME": directory}):
            events = junie_cli._JunieSessionEvents("this-launch")
            sessions = Path(directory) / "sessions"
            for name in ("current", "unrelated"):
                (sessions / name).mkdir(parents=True)
            task = {"kind": "TaskStartedEvent", "taskId": "task"}
            metadata = event("task", inputTokens=10, outputTokens=4, cacheInputTokens=5, cacheCreateTokens=3)
            (sessions / "unrelated/events.jsonl").write_bytes(
                line({"kind": "UserPromptEvent", "prompt": "unrelated"}) + line(task) + line(metadata),
            )
            current = sessions / "current/events.jsonl"
            current.write_bytes(
                line({"kind": "UserPromptEvent", "prompt": "this-launch"}) + line(task)
                + line(event("another-task", inputTokens=100, outputTokens=100))
                + line(event("task", inputTokens=None, outputTokens=100)) + line(metadata)[:-1],
            )
            self.assertFalse(events.read_new_events())
            self.assertEqual(events.usage, [0, 0, 0, 0, 0])
            with current.open("ab") as stream:
                stream.write(b"\n")
            for _ in range(2):
                self.assertFalse(events.read_new_events())
                self.assertEqual(events.usage, [18, 4, 5, 3, 1])
            with current.open("ab") as stream:
                stream.write(line({**task, "taskId": "continue"}) + line(event("continue", inputTokens=2, outputTokens=1)))
            self.assertFalse(events.read_new_events())
            self.assertEqual(events.usage, [20, 5, 5, 3, 2])

    def test_usage_persists_across_launches_without_resetting_dashboard_actions(self):
        with TemporaryDirectory() as directory:
            run_path = Path(directory)
            view = dashboard.Dashboard()
            handle = run_records.run_observer.set(view.observe)
            record = {"run_id": "run", "status": "checker_succeeded"}
            try:
                run_records.write_run_record(run_path / "run.json", record)
                view.publish({**view.state, "ready": False, "generating_anchors": True})
                for delta in ([18, 4, 5, 3, 1], [2, 1, 0, 0, 1]):
                    report = run_records.junie_usage_recorder(run_path, "anchors")
                    report([0, 0, 0, 0, 0])
                    report(delta)
                    self.assertTrue(view.state["generating_anchors"])
                loaded = json.loads((run_path / "run.json").read_bytes())
                self.assertEqual(loaded["token_usage"], [{
                    "phase": "anchors", "capsule": None, "input": 20, "output": 5,
                    "cache_read": 5, "cache_write": 3, "responses": 2,
                }])
                self.assertEqual(view.state["token_usage"], loaded["token_usage"])
            finally:
                run_records.run_observer.reset(handle)

    def test_only_current_tasks_terminal_connection_failure_retries(self):
        failure = {"kind": "SessionA2uxEvent", "taskId": "our-task", "event": {
            "state": "FAILED", "agentEvent": {
                "kind": "AgentFailureEvent", "errorCode": "ConnectionFailed", "agent": {"kind": "MainAgent"},
            },
        }}
        with TemporaryDirectory() as directory, patch.dict("os.environ", {"JUNIE_HOME": directory}):
            events = junie_cli._JunieSessionEvents("current-completion-token")
            sessions = Path(directory) / "sessions"
            for name in ("ours", "unrelated"):
                (sessions / name).mkdir(parents=True)
            (sessions / "unrelated/events.jsonl").write_text(json.dumps(failure) + "\n")
            current = sessions / "ours/events.jsonl"
            prefix = [
                {"kind": "UserPromptEvent", "prompt": "Write current-completion-token"},
                {"kind": "TaskStartedEvent", "taskId": "our-task"},
                {**failure, "taskId": "another-task"},
                {**failure, "event": {**failure["event"], "state": "IN_PROGRESS"}},
                {**failure, "event": {**failure["event"], "agentEvent": {
                    **failure["event"]["agentEvent"], "errorCode": "AuthenticationFailed",
                }}},
            ]
            current.write_text("\n".join(map(json.dumps, prefix)) + "\n")
            self.assertFalse(events.read_new_events())
            with current.open("a") as stream:
                stream.write(json.dumps(failure) + "\n")
            self.assertTrue(events.read_new_events())

    def test_connection_failure_relaunches_at_most_twice(self):
        command = ["junie", "--prompt", "task"]
        path = Path("fixture")
        error = junie_cli.JunieConnectionError("temporary")
        usage_updates = []
        report_usage = usage_updates.append
        with (
            patch.object(junie_cli, "_run_session", side_effect=[error, error, 0]) as launch,
            patch.object(junie_cli.time, "sleep") as delay,
        ):
            self.assertEqual(junie_cli.run_interactive(command, path, "token", (path,), report_usage=report_usage), 0)
            self.assertEqual(launch.call_count, 3)
            self.assertTrue(all(call.kwargs["report_usage"] is report_usage for call in launch.call_args_list))
            self.assertIn("without duplicating completed changes", launch.call_args.args[0][-1])
            self.assertEqual([call.args[0] for call in delay.call_args_list], [1, 3])
        with (
            patch.object(junie_cli, "_run_session", side_effect=error) as launch,
            patch.object(junie_cli.time, "sleep") as delay,
            io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict") as stdout,
            redirect_stdout(stdout),
            self.assertRaisesRegex(junie_cli.JunieConnectionError, "after 2 retries"),
        ):
            junie_cli.run_interactive(command, path, "token", (path,))
        self.assertEqual(launch.call_count, 3)
        self.assertEqual([call.args[0] for call in delay.call_args_list], [1, 3])

    def test_connection_retry_notice_with_cp1252_stdout(self):
        path = Path("fixture")
        with (
            io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict") as stdout,
            redirect_stdout(stdout),
            patch.object(junie_cli, "_run_session", side_effect=[junie_cli.JunieConnectionError("temporary"), 0]) as launch,
            patch.object(junie_cli.time, "sleep") as delay,
        ):
            self.assertEqual(junie_cli.run_interactive(["junie", "--prompt", "task"], path, "token", (path,)), 0)
            self.assertIn(b"Retrying Junie", stdout.buffer.getvalue())
        self.assertEqual(launch.call_count, 2)
        delay.assert_called_once_with(1)


class MigrationFailureRecoveryTests(SavedRunFixture):
    def test_resumed_migration_snapshots_only_edits_from_a_confirmed_session(self):
        second = self.worktree / "Second.cs"
        second.write_bytes(b"class SecondOriginal {}")
        names = (self.target.name, second.name)
        self.config["units"][0]["targets"] = list(names)
        self.save_record({**self.record(), "targets": list(names), "status": "prepared",
                          "completed_capsules": 0, "current_capsule": 1})
        original = self.target.read_bytes()
        agent = self.run_path / "agent-workspace"

        def first_stop(_command, _completion, _token, targets, **kwargs):
            targets[0].write_bytes(b"class FirstPartial {}")
            junie_cli.session_closed_observer.get()()
            raise OperationCancelled("stopped after saving")

        with (
            patch.object(junie_cli, "require_interactive_cli"),
            patch.object(junie_cli, "run_interactive", side_effect=first_stop),
            self.assertRaises(workflow.RunInterrupted),
        ):
            workflow.resume_with_junie(self.config, self.run_id)
        baseline = self.record()
        first = baseline["stopped_migration_draft"]

        for outcome in ("connection", "session", "incomplete", "missing", "prelaunch", "unchanged",
                        "outside", "external", "unconfirmed", "wrapped_stop", "snapshot_failure", "record_failure"):
            with self.subTest(outcome=outcome):
                self.save_record(baseline)
                self.target.write_bytes(original)
                second.write_bytes(b"class SecondOriginal {}")
                (agent / names[0]).write_bytes(b"class FirstPartial {}")
                (agent / names[1]).write_bytes(second.read_bytes())
                outside = agent / "outside.txt"
                outside.unlink(missing_ok=True)

                def failed(command, _completion, _token, targets, **kwargs):
                    if outcome == "prelaunch":
                        raise junie_cli.JunieLaunchError(command, None, None, OSError("fixture launch"))
                    if outcome != "unchanged":
                        targets[0].write_bytes(b"class SecondPartial {}")
                    if outcome == "missing":
                        targets[1].unlink()
                    if outcome == "outside":
                        outside.write_text("fixture", encoding="utf-8")
                    if outcome == "external":
                        self.target.write_bytes(b"class OperatorEdit {}")
                    if outcome == "unconfirmed":
                        raise junie_cli.JunieStopError("fixture unconfirmed")
                    if outcome == "wrapped_stop":
                        junie_cli.session_closed_observer.get()()  # The earlier retry ended safely.
                        try:
                            raise junie_cli.JunieStopError("fixture unconfirmed")
                        except OSError as exc:
                            raise junie_cli.JunieSessionError(command, 1, "fixture wrapped stop") from exc
                    junie_cli.session_closed_observer.get()()
                    if outcome == "session":
                        raise junie_cli.JunieSessionError(command, 1, "fixture session")
                    if outcome == "incomplete":
                        return 1
                    raise junie_cli.JunieConnectionError("fixture connection")

                def save_record(path, record, **kwargs):
                    if record.get("stopped_migration_draft") != first and outcome == "record_failure":
                        raise OSError("fixture record save failed")
                    return write_record(path, record, **kwargs)

                write_record = workflow.write_run_record
                save_snapshot = workflow._snapshot_stopped_repair
                with (
                    patch.object(junie_cli, "require_interactive_cli"),
                    patch.object(junie_cli, "run_interactive", side_effect=failed),
                    patch.object(workflow, "write_run_record", side_effect=save_record),
                    patch.object(workflow, "_snapshot_stopped_repair",
                                 side_effect=OSError("fixture snapshot failed") if outcome == "snapshot_failure" else save_snapshot),
                ):
                    if outcome == "incomplete":
                        self.assertEqual(workflow.resume_with_junie(self.config, self.run_id)["status"], "interrupted")
                    else:
                        with self.assertRaises(OSError):
                            workflow.resume_with_junie(self.config, self.run_id)
                saved = self.record()
                self.assertEqual(saved["completed_capsules"], 0)
                self.assertEqual(self.target.read_bytes(), b"class OperatorEdit {}" if outcome == "external" else original)
                self.assertEqual(second.read_bytes(), b"class SecondOriginal {}")
                draft = saved["stopped_migration_draft"]
                if outcome in {"prelaunch", "unchanged", "unconfirmed", "wrapped_stop", "snapshot_failure", "record_failure"}:
                    self.assertEqual(draft, first)
                    self.assertEqual(saved.get("stopped_migration_history", []), [])
                else:
                    self.assertEqual(saved["stopped_migration_history"], [first])
                    self.assertEqual((self.run_path / draft["snapshot"] / "0.draft").read_bytes(), b"class SecondPartial {}")
                    self.assertEqual(draft["workspace_unchanged"], outcome not in {"outside", "external"})
                    if outcome == "missing":
                        self.assertIsNone(draft["targets"][names[1]])
                        self.assertFalse((self.run_path / draft["snapshot"] / "1.draft").exists())

                def next_junie(_command, completion, token, targets, **kwargs):
                    expected = b"class FirstPartial {}" if outcome in {"prelaunch", "unchanged"} else b"class SecondPartial {}"
                    self.assertEqual(targets[0].read_bytes(), expected)
                    self.assertEqual(targets[1].exists(), outcome != "missing")
                    targets[1].write_bytes(b"class Recreated {}")
                    completion.write_text(token, encoding="ascii")
                    return 0

                with (
                    patch.object(junie_cli, "require_interactive_cli"),
                    patch.object(junie_cli, "run_interactive", side_effect=next_junie) as junie,
                    patch.object(workflow, "_verify_worktree", return_value={}),
                ):
                    blocked = {
                        "unconfirmed": "終了を確認できません",
                        "wrapped_stop": "終了を確認できません",
                        "outside": "一致しません",
                        "external": "worktree targets changed",
                        "snapshot_failure": "保存できません",
                        "record_failure": "保存できません",
                    }.get(outcome)
                    if blocked:
                        with self.assertRaisesRegex(ValueError, blocked):
                            workflow.resume_with_junie(self.config, self.run_id)
                        junie.assert_not_called()
                    else:
                        workflow.resume_with_junie(self.config, self.run_id)
                        junie.assert_called_once()
                        self.assertEqual(self.record()["completed_capsules"], 1)


class FollowupDraftRecoveryTests(SavedRunFixture):
    instruction = "移行済みの C# を更新してください"
    first_edit = b"class FirstPartial {}\n"
    latest_edit = b"class SecondPartial {}\n"

    def setUp(self):
        super().setUp()
        self.second = self.worktree / "Second.cs"
        self.second.write_bytes(b"class SecondOriginal {}\n")
        names = [self.target.name, self.second.name]
        self.config["units"][0]["targets"] = names
        self.save_record({**self.record(), "targets": names})
        self.original = self.target.read_bytes()
        self.second_original = self.second.read_bytes()
        self.agent = self.run_path / "agent-workspace"
        self.agent.mkdir()
        with editor_queue(self.run_path) as queue:
            add_prompt(queue, {"text": self.instruction})
            self.prompt_id = queue["prompts"][0]["id"]
        cli_check = patch.object(junie_cli, "require_interactive_cli")
        cli_check.start()
        self.addCleanup(cli_check.stop)

    def assert_prompt(self, status):
        with editor_queue(self.run_path) as queue:
            self.assertEqual(queue["prompts"], [{"id": self.prompt_id, "text": self.instruction, "status": status}])

    def resume_with(self, junie):
        with patch.object(junie_cli, "run_interactive", side_effect=junie):
            return self.resume()

    def stop_followup(self):
        def stop(_command, _completion, _token, targets, **kwargs):
            targets[0].write_bytes(self.first_edit)
            junie_cli.session_closed_observer.get()()
            raise OperationCancelled("stopped after saving")

        with self.assertRaises(workflow.RunInterrupted):
            self.resume_with(stop)
        self.assert_prompt("queued")
        return self.record()

    def assert_pending(self, expected_first=None):
        self.assert_prompt("queued")
        self.assertEqual(self.record()["status"], "editor_checkpoint")
        self.assertEqual(self.record()["completed_capsules"], 1)
        self.assertEqual(self.target.read_bytes(), expected_first or self.original)
        self.assertEqual(self.second.read_bytes(), self.second_original)

    def test_incomplete_followup_passes_latest_edit_and_missing_target_to_normal_resume(self):
        baseline = self.stop_followup()
        first = baseline["stopped_migration_draft"]
        for outcome in ("zero", "one", "missing", "wrong_token", "unchanged"):
            with self.subTest(outcome=outcome):
                self.save_record(baseline)
                (self.agent / self.target.name).write_bytes(self.first_edit)
                (self.agent / self.second.name).write_bytes(self.second_original)

                def incomplete(_command, completion, token, targets, **kwargs):
                    self.assertEqual(targets[0].read_bytes(), self.first_edit)
                    if outcome != "unchanged":
                        targets[0].write_bytes(self.latest_edit)
                    if outcome == "missing":
                        targets[1].unlink()
                        completion.write_text(token, encoding="ascii")
                    if outcome == "wrong_token":
                        completion.write_text("wrong token", encoding="ascii")
                    junie_cli.session_closed_observer.get()()
                    return 1 if outcome == "one" else 0

                self.assertEqual(self.resume_with(incomplete)["status"], "editor_checkpoint")
                self.assert_pending()
                saved = self.record()
                draft = saved["stopped_migration_draft"]
                expected = self.first_edit if outcome == "unchanged" else self.latest_edit
                self.assertEqual(saved.get("stopped_migration_history", []), [] if outcome == "unchanged" else [first])
                self.assertEqual((self.run_path / first["snapshot"] / "0.draft").read_bytes(), self.first_edit)
                self.assertEqual((self.run_path / draft["snapshot"] / "0.draft").read_bytes(), expected)
                if outcome == "missing":
                    self.assertIsNone(draft["targets"][self.second.name])
                    self.assertFalse((self.run_path / draft["snapshot"] / "1.draft").exists())

                def next_junie(_command, completion, token, targets, **kwargs):
                    instructions = self.agent / f"checker-findings-{token}-operator.txt"
                    self.assertIn(self.instruction, instructions.read_text(encoding="utf-8"))
                    self.assertEqual(targets[0].read_bytes(), expected)
                    self.assertEqual(targets[1].exists(), outcome != "missing")
                    junie_cli.session_closed_observer.get()()
                    return 0

                with patch.object(junie_cli, "run_interactive", side_effect=next_junie) as junie:
                    self.resume()
                    junie.assert_called_once()
                self.assert_pending()
                self.assertEqual(self.record()["stopped_migration_draft"], draft)
                self.assertEqual(self.record().get("stopped_migration_history", []), saved.get("stopped_migration_history", []))

    def test_incomplete_followup_blocks_resume_after_unsafe_changes_or_save_failures(self):
        baseline = self.stop_followup()
        first = baseline["stopped_migration_draft"]
        external = b"class External {}\n"
        write_record = workflow.write_run_record
        save_snapshot = workflow._snapshot_stopped_repair
        cases = {"outside": "一致しません", "worktree": "worktree targets changed", "agent": "一致しません",
                 "unconfirmed": "終了を確認できません", "snapshot": "保存できません", "record": "保存できません"}
        for outcome, blocked in cases.items():
            with self.subTest(outcome=outcome):
                self.save_record(baseline)
                self.target.write_bytes(self.original)
                (self.agent / self.target.name).write_bytes(self.first_edit)
                outside = self.agent / "outside.txt"
                outside.unlink(missing_ok=True)

                def incomplete(_command, _completion, _token, targets, **kwargs):
                    if outcome == "outside":
                        outside.write_bytes(b"outside")
                    else:
                        targets[0].write_bytes(self.latest_edit)
                    if outcome == "worktree":
                        self.target.write_bytes(external)
                    if outcome != "unconfirmed":
                        junie_cli.session_closed_observer.get()()
                    return 0

                def save_record(path, record, **kwargs):
                    if outcome == "record" and record.get("stopped_migration_draft") != first:
                        raise OSError("fixture record save failed")
                    return write_record(path, record, **kwargs)

                with (
                    patch.object(workflow, "write_run_record", side_effect=save_record),
                    patch.object(workflow, "_snapshot_stopped_repair",
                                 side_effect=OSError("fixture snapshot failed") if outcome == "snapshot" else save_snapshot),
                ):
                    if outcome in {"worktree", "agent"}:
                        self.resume_with(incomplete)
                    else:
                        error = ValueError if outcome == "outside" else OSError
                        with self.assertRaises(error):
                            self.resume_with(incomplete)
                if outcome == "agent":
                    (self.agent / self.target.name).write_bytes(external)
                self.assert_pending(external if outcome == "worktree" else self.original)
                saved = self.record()
                if outcome in {"unconfirmed", "snapshot", "record"}:
                    self.assertEqual(saved["stopped_migration_draft"], first)
                    self.assertEqual(saved.get("stopped_migration_history", []), [])
                self.assertEqual((self.run_path / first["snapshot"] / "0.draft").read_bytes(), self.first_edit)
                with patch.object(junie_cli, "run_interactive") as junie:
                    with self.assertRaisesRegex(ValueError, blocked):
                        self.resume()
                    junie.assert_not_called()

    def test_invalid_followup_review_preserves_the_latest_draft_for_normal_resume(self):
        baseline = self.stop_followup()
        first = baseline["stopped_migration_draft"]
        with editor_queue(self.run_path) as queue:
            record_operator_edit(queue, self.target.name, "class Previous {}\n", self.original.decode())

        cases = {
            "json": (b'{"question": ', json.JSONDecodeError),
            "type": (b'{"question": false}', ValueError),
            "size": (json.dumps({"question": "x" * 8200}).encode(), ValueError),
            "utf8": (b'{"question": "\xff"}', UnicodeDecodeError),
            "read": (b'{"question": "valid"}', PermissionError),
        }
        open_path = Path.open
        for outcome, (contents, error) in cases.items():
            with self.subTest(outcome=outcome):
                self.save_record(baseline)
                (self.agent / self.target.name).write_bytes(self.first_edit)

                def invalid(_command, completion, token, targets, **kwargs):
                    self.assertEqual(targets[0].read_bytes(), self.first_edit)
                    targets[0].write_bytes(self.latest_edit)
                    report = completion.parent / ".migration-scratch" / f"operator-review-{token}.json"
                    report.write_bytes(contents)
                    if outcome == "type":
                        completion.write_text(token, encoding="ascii")
                    junie_cli.session_closed_observer.get()()
                    return 0

                def open_report(path, mode="r", *args, **kwargs):
                    if outcome == "read" and mode == "rb" and path.name.startswith("operator-review-"):
                        raise PermissionError("fixture report read failed")
                    return open_path(path, mode, *args, **kwargs)

                with patch.object(Path, "open", open_report):
                    with self.assertRaises(error) as raised:
                        self.resume_with(invalid)
                self.assertIs(type(raised.exception), error)
                self.assert_pending()
                saved = self.record()
                self.assertNotIn("operator_question", saved)
                draft = saved["stopped_migration_draft"]
                self.assertEqual(saved["stopped_migration_history"], [first])
                self.assertEqual((self.run_path / first["snapshot"] / "0.draft").read_bytes(), self.first_edit)
                self.assertEqual((self.run_path / draft["snapshot"] / "0.draft").read_bytes(), self.latest_edit)

                def next_junie(_command, _completion, token, targets, **kwargs):
                    instructions = self.agent / f"checker-findings-{token}-operator.txt"
                    self.assertIn(self.instruction, instructions.read_text(encoding="utf-8"))
                    self.assertEqual(targets[0].read_bytes(), self.latest_edit)
                    junie_cli.session_closed_observer.get()()
                    return 0

                with patch.object(junie_cli, "run_interactive", side_effect=next_junie) as junie:
                    self.resume()
                    junie.assert_called_once()
                self.assert_pending()
                self.assertEqual(self.record()["stopped_migration_draft"], draft)
                self.assertEqual(self.record()["stopped_migration_history"], [first])

    def test_invalid_followup_review_retains_both_errors_and_prior_snapshot_on_save_failure(self):
        baseline = self.stop_followup()
        first = baseline["stopped_migration_draft"]
        with editor_queue(self.run_path) as queue:
            record_operator_edit(queue, self.target.name, "class Previous {}\n", self.original.decode())

        def invalid(_command, completion, token, targets, **kwargs):
            targets[0].write_bytes(self.latest_edit)
            report = completion.parent / ".migration-scratch" / f"operator-review-{token}.json"
            report.write_bytes(b'{"question": ')
            junie_cli.session_closed_observer.get()()
            return 0

        write_record = workflow.write_run_record
        for outcome in ("snapshot", "record"):
            with self.subTest(outcome=outcome):
                self.save_record(baseline)
                (self.agent / self.target.name).write_bytes(self.first_edit)
                failure = OSError(f"fixture {outcome} save failed")

                def save_record(path, record, **kwargs):
                    if record.get("stopped_migration_draft") != first:
                        raise failure
                    return write_record(path, record, **kwargs)

                failed_write = (
                    patch.object(workflow, "_snapshot_stopped_repair", side_effect=failure)
                    if outcome == "snapshot" else patch.object(workflow, "write_run_record", side_effect=save_record)
                )
                with failed_write:
                    with self.assertRaises(OSError) as raised:
                        self.resume_with(invalid)
                self.assertIs(raised.exception, failure)
                self.assertIsInstance(raised.exception.__context__, json.JSONDecodeError)
                details = workflow.format_error(raised.exception)
                self.assertIn("JSONDecodeError", details)
                self.assertIn(str(failure), details)
                self.assert_pending()
                saved = self.record()
                self.assertTrue(saved["stopped_migration_save_error"])
                self.assertEqual(saved["stopped_migration_draft"], first)
                self.assertNotIn("stopped_migration_history", saved)
                self.assertNotIn("operator_question", saved)
                self.assertEqual((self.run_path / first["snapshot"] / "0.draft").read_bytes(), self.first_edit)
                self.assertEqual((self.agent / self.target.name).read_bytes(), self.latest_edit)
                with patch.object(junie_cli, "run_interactive") as junie:
                    with self.assertRaisesRegex(ValueError, "保存できません"):
                        self.resume()
                    junie.assert_not_called()

    def test_followup_question_retains_the_question_and_saves_one_snapshot(self):
        baseline = self.stop_followup()
        first = baseline["stopped_migration_draft"]
        question = "Example.cs の更新方針を選んでください。"
        with editor_queue(self.run_path) as queue:
            record_operator_edit(queue, self.target.name, "class Previous {}\n", self.original.decode())

        def ask(_command, completion, token, targets, **kwargs):
            targets[0].write_bytes(self.latest_edit)
            report = completion.parent / ".migration-scratch" / f"operator-review-{token}.json"
            report.write_text(json.dumps({"question": question}), encoding="utf-8")
            junie_cli.session_closed_observer.get()()
            return 0

        with patch.object(workflow, "_snapshot_stopped_repair", side_effect=OSError("fixture snapshot failed")):
            with self.assertRaises(OSError):
                self.resume_with(ask)
        self.assert_pending()
        failed = self.record()
        self.assertEqual(failed["operator_question"], question)
        self.assertEqual(failed["stopped_migration_draft"], first)
        self.assertNotIn("stopped_migration_history", failed)

        self.save_record(baseline)
        (self.agent / self.target.name).write_bytes(self.first_edit)
        result = self.resume_with(ask)
        self.assert_pending()
        self.assertEqual(result["error"], question)
        saved = self.record()
        self.assertEqual(saved["operator_question"], question)
        self.assertEqual(saved["stopped_migration_history"], [first])
        self.assertEqual((self.run_path / saved["stopped_migration_draft"]["snapshot"] / "0.draft").read_bytes(), self.latest_edit)

        response = "Example.cs は現在の編集を採用してください。"
        with editor_queue(self.run_path) as queue:
            add_prompt(queue, {"text": response})

        def answer(command, completion, token, targets, **kwargs):
            self.assertIn(question, command[-1])
            instructions = self.agent / f"checker-findings-{token}-operator.txt"
            self.assertIn(response, instructions.read_text(encoding="utf-8"))
            self.assertEqual(targets[0].read_bytes(), self.latest_edit)
            completion.write_text(token, encoding="ascii")
            junie_cli.session_closed_observer.get()()
            return 0

        with patch.object(workflow, "_verify_worktree", side_effect=lambda config, unit, result, record: result):
            self.assertEqual(self.resume_with(answer)["status"], "junie_finished")
        with editor_queue(self.run_path) as queue:
            self.assertEqual([(item["text"], item["status"]) for item in queue["prompts"]],
                             [(self.instruction, "sent"), (response, "sent")])
        self.assertEqual(self.target.read_text(encoding="utf-8-sig"), self.latest_edit.decode())
        self.assertNotIn("operator_question", self.record())
        self.assertEqual(self.record()["stopped_migration_history"], [first, saved["stopped_migration_draft"]])

    def test_incomplete_followup_without_a_prior_stop_does_not_create_a_draft(self):
        def incomplete(_command, _completion, _token, targets, **kwargs):
            targets[0].write_bytes(self.latest_edit)
            junie_cli.session_closed_observer.get()()
            return 0

        self.assertEqual(self.resume_with(incomplete)["status"], "editor_checkpoint")
        self.assert_pending()
        self.assertNotIn("stopped_migration_draft", self.record())


class CheckerResumeAndReviewTests(SavedRunFixture):
    def test_junie_input_cli_preserves_unmeasured_and_measured_states(self):
        args = ["php-to-csharp-harness", "junie-input", "--config", "fixture.toml", "--run-id", self.run_id]
        for report in (None, {"phase": "rule_review", "bytes": 125, "blocked": True}):
            self.save_record({**self.record(), "last_junie_input": report})
            output = io.StringIO()
            with (patch.object(sys, "argv", args), patch.object(cli, "load_config", return_value=self.config),
                  redirect_stdout(output)):
                cli.main()
            self.assertEqual(json.loads(output.getvalue()), {"run_id": self.run_id, "last_junie_input": report,
                                                             "last_junie_steering_input": None, "junie_permission_counts": {}})

    def test_checker_only_requires_intact_build_inputs_and_artifacts(self):
        project = self.worktree / "src/App.csproj"
        project.parent.mkdir()
        project.write_text('<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup>'
                           '<TargetFramework>net8.0</TargetFramework><Nullable>enable</Nullable></PropertyGroup></Project>', encoding="utf-8")
        self.config["build_project"] = project.relative_to(self.worktree).as_posix()
        toolchain = self.worktree.parent / "sdk"
        dotnet = toolchain / "dotnet"
        packs = toolchain / "packs" / "Microsoft.NETCore.App.Ref"
        packs.mkdir(parents=True)
        dotnet.write_bytes(b"fixture dotnet")
        reference = packs / "reference.dll"
        reference.write_bytes(b"fixture reference")
        dll = project.parent / "bin/Debug/net8.0/App.dll"
        assets = project.parent / "obj/project.assets.json"
        helper = self.worktree / "Helper.cs"
        helper.write_bytes(b"class Dependency {}")
        calls = []

        def run(command, **_kwargs):
            calls.append(command)
            if command[0] == "dotnet":
                dll.parent.mkdir(parents=True, exist_ok=True)
                dll.write_bytes(b"compiled")
                assets.parent.mkdir(exist_ok=True)
                assets.write_bytes(b'{"libraries": {}}')
            return subprocess.CompletedProcess(command, 0)

        with (patch("migration_harness.verification.build_receipt.shutil.which", return_value=str(dotnet)),
              patch.object(workflow, "run_process", side_effect=run),
              patch.object(junie_cli, "run_interactive") as junie, redirect_stdout(io.StringIO())):
            control = ExecutionControl(lambda _activity: None)
            original_hash = build_receipt.sha256_file

            def interrupt_artifact(path, *, check=None):
                if path == dll:
                    control.cancelled.set()
                return original_hash(path, check=check)

            build = workflow.plan_build_resume(self.config, self.run_id)
            with (
                control_scope(control), patch.object(build_receipt, "sha256_file", side_effect=interrupt_artifact),
                self.assertRaises(OperationCancelled),
            ):
                workflow.resume_with_junie(self.config, self.run_id, verification_request={**build, "id": "stop"})
            self.assertNotIn("checker_build", self.record())
            with self.assertRaisesRegex(ValueError, "照合できるビルド成果物がありません"):
                workflow.plan_checker_resume(self.config, self.run_id)
            shutil.rmtree(project.parent / "bin")
            shutil.rmtree(project.parent / "obj")
            calls.clear()

            build = workflow.plan_build_resume(self.config, self.run_id)
            workflow.resume_with_junie(self.config, self.run_id, verification_request={**build, "id": "build"})
            plan = workflow.plan_checker_resume(self.config, self.run_id)
            request = {**plan, "id": "checker"}
            for path in (helper, dll, reference):
                with self.subTest(path=path.name):
                    original = path.read_bytes()
                    path.write_bytes(b"changed")
                    with self.assertRaisesRegex(ValueError, "再ビルド"):
                        workflow.resume_with_junie(self.config, self.run_id, verification_request=request)
                    path.write_bytes(original)
            result = workflow.resume_with_junie(self.config, self.run_id, verification_request=request)
            self.assertEqual(result["status"], "checker_succeeded")
            self.assertEqual([command[0] for command in calls], ["dotnet", "fixture-checker"])
            self.assertEqual(self.record()["resume_operations"][-1]["start_stage"], "checker")
            self.assertTrue(workflow.migration_ledger(self.config, self.run_id)["completion"]["verification_passed"])
            helper.write_bytes(b"class EditedAfterChecker {}")
            self.assertFalse(workflow.migration_ledger(self.config, self.run_id)["completion"]["verification_passed"])
            junie.assert_not_called()

    def test_concurrent_checker_approval_is_rejected_and_can_be_retried(self):
        self.checker_log.write_bytes(
            b"A Example.cs:1 warning: first finding\nB Example.cs:2 warning: second finding\n",
        )
        record = self.record()
        record["review_file_hashes"] = {"Example.cs": workflow.review_target_hash(self.worktree, Path("Example.cs"))}
        self.save_record(record)
        parse = workflow.checker_findings
        findings, _ = parse(self.checker_log, self.worktree)
        parsed, release = Event(), Event()

        def hold_first_parse(*args):
            result = parse(*args)
            if not parsed.is_set():
                parsed.set()
                self.assertTrue(release.wait(5))
            return result

        def approve(index):
            return workflow.review_checker(self.config, self.run_id, findings[index].finding_id, f"reason {index}")

        with patch.object(workflow, "checker_findings", side_effect=hold_first_parse), ThreadPoolExecutor() as pool:
            first = pool.submit(approve, 0)
            try:
                self.assertTrue(parsed.wait(5))
                with self.assertRaisesRegex(RuntimeError, "使用中"):
                    approve(1)
            finally:
                release.set()
            first.result(timeout=5)
        approve(1)
        self.assertEqual([row["reason"] for row in self.record()["checker_reviews"]], ["reason 0", "reason 1"])

    """再開時の検証順序とチェッカー指摘の判定を検証する。"""

    build = ("dotnet", 0, b"rebuilt edited target\n")
    diagnostic = b"MIG101 Example.cs:1 warning: use SharedUtility\n"

    def test_migration_requires_current_verification_without_range_decisions(self):
        def completion():
            return workflow.migration_ledger(self.config, self.run_id)["completion"]

        subprocess.run(["git", "init", "-q", str(self.worktree)], check=True)
        self.config["checker_command"] = [sys.executable]
        secondary = self.worktree / "View.cshtml"
        secondary.write_text("<p>View</p>", encoding="utf-8")
        targets = [self.target.name, secondary.name]
        self.config["units"][0]["targets"] = targets
        self.save_record({**self.record(), "targets": targets})
        path = self.run_path / "capsule" / "manifest.json"
        manifest = json.loads(path.read_bytes())
        ranges = manifest["parts"][0]["sources"]
        for number in (2, 3):
            ranges.append({**ranges[0], "start_line": number, "end_line": number})
        path.write_text(json.dumps(manifest), encoding="utf-8")
        check_passed = ("fixture-checker", 0, b"")
        self.resume(self.build, check_passed)
        self.assertEqual(completion(), {"complete": True, "verification_passed": True, "total": 3})

        self.target.write_text("class Edited {}", encoding="utf-8")
        ledger = workflow.migration_ledger(self.config, self.run_id)
        self.assertFalse(ledger["completion"]["complete"])
        self.assertFalse(completion()["verification_passed"])
        self.resume(self.build, ("fixture-checker", 1, self.diagnostic))
        self.assertFalse(completion()["complete"])
        self.resume(("dotnet", 1, b"build error\n"))
        self.assertFalse(completion()["complete"])
        self.resume(self.build, check_passed)
        self.assertTrue(completion()["complete"])
        self.config["checker_command"] = ["different-checker"]
        self.assertFalse(completion()["complete"])

    # 承認や修正履歴がない手編集でも、再ビルドが先に走ることを検証する。
    def test_manual_edit_rebuilds_before_rechecking_without_history_or_approvals(self):
        self.target.write_text("class ManuallyEdited {}\n", encoding="utf-8")
        result = self.resume(self.build, ("fixture-checker", 0, b""))
        self.assertEqual(result["status"], "checker_succeeded")
        saved = self.record()
        self.assertEqual(saved["status"], "checker_succeeded")
        self.assertEqual(saved["build_exit_code"], 0)
        self.assertEqual(saved["build_log_path"], str(self.build_log))
        self.assertEqual(self.build_log.read_bytes(), self.build[2])
        self.assertEqual(self.checker_log.read_bytes(), b"")

    # 新しいビルドが失敗した場合に、以前の成功を再利用しないことを検証する。
    def test_failed_rebuild_stops_before_checker_and_records_current_failure(self):
        self.target.write_text("class Example { int Value = MissingValue; }", encoding="utf-8")
        old_checker_log = self.checker_log.read_bytes()
        result = self.resume(("dotnet", 99, b"edited target does not build\n"))
        self.assertEqual(result["status"], "build_failed")
        saved = self.record()
        self.assertEqual(saved["status"], "build_failed")
        self.assertEqual(saved["build_exit_code"], 99)
        self.assertNotIn("checker_exit_code", saved)
        self.assertEqual(self.build_log.read_bytes(), b"edited target does not build\n")
        self.assertEqual(self.checker_log.read_bytes(), old_checker_log)

    def test_build_response_passes_saved_edit_and_instruction_to_junie_before_verification(self):
        diagnostic = f"{self.target}(1,30): error CS0103: The name MissingValue is not defined\n".encode()
        self.target.write_bytes(b"\xef\xbb\xbfclass Example { int Value = MissingValue; }\r\n")
        self.resume(("dotnet", 1, diagnostic * 2))
        (self.run_path / "agent-workspace").mkdir()
        summary = workflow.build_failure_review(self.config, self.run_id)
        self.assertEqual(summary["errors"], ["Example.cs(1,30): CS0103: The name MissingValue is not defined"])
        self.assertEqual(summary["preferred_target"], self.target.name)
        source = workflow.build_failure_review(self.config, self.run_id, self.target.name)
        edited = b"\xef\xbb\xbf// human edit\r\nclass Example { int Value = MissingValue; }\r\n"
        instruction = "値を 42 にしてください。"

        def repair(command, completion, token, targets, **kwargs):
            instructions = next(completion.parent.glob("checker-findings-*-instruction.txt"))
            self.assertIn(instructions.name, command[-1])
            self.assertEqual(instructions.read_text(encoding="utf-8").strip(), instruction)
            self.assertEqual(targets[0].read_bytes(), edited)
            targets[0].write_bytes(edited.replace(b"= MissingValue;", b"= 42;"))
            completion.write_text(token, encoding="utf-8")
            return 0

        commands, calls = self.mock_commands(self.build, ("fixture-checker", 0, b""))
        with (
            commands, redirect_stdout(io.StringIO()),
            patch.object(junie_cli, "require_interactive_cli"),
            patch.object(junie_cli, "run_interactive", side_effect=repair) as junie,
        ):
            result = workflow.resume_with_junie(self.config, self.run_id, build_response={
                "instruction": instruction,
                "edit": {"target": self.target.name, "version": source["version"],
                         "content": "// human edit\nclass Example { int Value = MissingValue; }\n"},
            })
        junie.assert_called_once()
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["status"], "checker_succeeded")
        self.assertEqual(self.target.read_bytes(), edited.replace(b"= MissingValue;", b"= 42;"))

    def test_build_response_rejects_a_stale_edit_before_starting_any_command(self):
        self.resume(("dotnet", 1, b"failed\n"))
        source = workflow.build_failure_review(self.config, self.run_id, self.target.name)
        self.target.write_text("class ChangedExternally {}", encoding="utf-8")
        with patch.object(workflow, "run_process") as process:
            with self.assertRaisesRegex(ValueError, "対象ファイルが更新されました"):
                workflow.resume_with_junie(self.config, self.run_id, build_response={
                    "edit": {"target": self.target.name, "version": source["version"], "content": "class Stale {}"},
                })
        process.assert_not_called()
        self.assertEqual(self.target.read_text(encoding="utf-8"), "class ChangedExternally {}")
        self.assertEqual(self.record()["status"], "build_failed")

    def test_build_diagnostic_repairs_target_and_stops_if_error_repeats(self):
        self.config["max_repair_attempts"] = 2
        error = f"{self.target}(5,3): error CS1002: ; expected\n".encode()
        with (
            patch.object(junie_cli, "require_interactive_cli"),
            patch.object(workflow, "_repair_target") as repair,
        ):
            result = self.resume(("dotnet", 1, error), ("dotnet", 1, error.replace(b"(5,3)", b"(6,3)")))
            self.assertEqual(result["status"], "build_failed")
            repair.assert_called_once()
            self.assertEqual(repair.call_args.args[2], (Path(self.target.name),))
            self.assertEqual((self.run_path / "build-before-repair-1.log").read_bytes(), error)
            repair.reset_mock()
            error = error.replace(b"CS1002: ; expected", b"CS1513: } expected")
            result = self.resume(("dotnet", 1, error), self.build, ("fixture-checker", 0, b""))
            self.assertEqual(result["status"], "checker_succeeded")
            repair.assert_called_once()
            self.assertEqual([entry["kind"] for entry in self.record()["repair_history"]], ["build", "build"])

    def test_build_recovery_reports_each_step_before_checker_runs(self):
        self.config["max_repair_attempts"] = 1
        (self.run_path / "agent-workspace").mkdir()
        updates = []

        def repair(command, completion, token, targets, **kwargs):
            self.assertEqual(updates[-1]["message"], "Junieがビルドエラーを修正しています")
            self.assertIn("自動修正 1/1回目", updates[-1]["detail"])
            self.assertIn(self.target.name, updates[-1]["detail"])
            self.assertNotIn("独自チェッカーで確認しています", [item["message"] for item in updates])
            targets[0].write_text("class Fixed {}\n", encoding="utf-8")
            completion.write_text(token, encoding="utf-8")
            return 0

        with (
            control_scope(ExecutionControl(updates.append)),
            patch.object(junie_cli, "require_interactive_cli"),
            patch.object(junie_cli, "run_interactive", side_effect=repair),
        ):
            result = self.resume(
                ("dotnet", 1, f"{self.target}(1,1): error CS1002: ; expected\n".encode()),
                self.build, ("fixture-checker", 0, b""),
            )
        self.assertEqual(result["status"], "checker_succeeded")
        self.assertEqual([item["message"] for item in updates if item["kind"] == "phase"], [
            "Roslyn/Razor の事前診断を実行しています",
            "ビルドを実行しています", "ビルドエラーを確認しています",
            "修正に必要な資料を準備しています", "Junieがビルドエラーを修正しています",
            "Junieの修正結果を確認しています",
            "Roslyn/Razor の事前診断を実行しています",
            "再ビルドを実行しています", "独自チェッカーで確認しています",
        ])
        self.assertEqual(len(self.record()["build_attempt_seconds"]), 2)

    def test_interrupted_repair_can_restart_with_a_one_attempt_limit(self):
        self.config["max_repair_attempts"] = 1
        build_failure = ("dotnet", 1, f"{self.target}(5,3): error CS1002: ; expected\n".encode())
        check_failure = ("fixture-checker", 1, self.diagnostic)
        success = (self.build, ("fixture-checker", 0, b""))
        original_record = self.record()
        for responses in ((build_failure,), (self.build, check_failure)):
            with self.subTest(phase=responses[-1][0]):
                self.save_record(original_record)
                with (
                    patch.object(junie_cli, "require_interactive_cli"),
                    patch.object(workflow, "_repair_target", side_effect=[KeyboardInterrupt, None]) as repair,
                ):
                    with self.assertRaises(workflow.RunInterrupted):
                        self.resume(*responses)
                    saved = self.record()
                    self.assertEqual(saved["repair_history"], [])
                    self.assertIn("repair_pending", saved)
                    self.assertTrue(workflow.can_resume_run(self.config, saved))
                    result = self.resume(*responses, *success)
                    self.assertEqual(result["status"], "checker_succeeded")
                    self.assertEqual(repair.call_count, 2)
                    self.assertEqual(len(self.record()["repair_history"]), 1)
                    self.assertNotIn("repair_pending", self.record())

    def test_stop_preserves_missing_and_partial_repair_targets_and_detects_manual_conflict(self):
        names = ["Example.cs", "Second.cs"]
        self.config["units"][0]["targets"] = names
        self.config["max_repair_attempts"] = 1
        (self.worktree / names[1]).write_text("class SecondOriginal {}\n", encoding="utf-8")
        self.save_record({**self.record(), "targets": names})
        agent = self.run_path / "agent-workspace"
        agent.mkdir()
        partial = b"class Partial {\xff"
        responses = (
            ("dotnet", 0, b"build passed\n"),
            ("fixture-checker", 1, b"A Example.cs:1 error: first\nB Second.cs:1 error: second\n"),
        )

        def interrupt(_command, _completion, _token, targets, **kwargs):
            targets[0].write_bytes(partial)
            targets[1].unlink()
            raise OperationCancelled("test stop")

        def verify_input(_command, _completion, _token, targets, **kwargs):
            self.assertEqual(targets[0].read_bytes(), partial)
            self.assertFalse(targets[1].exists())
            raise OperationCancelled("test stop after inspecting")

        with patch.object(junie_cli, "require_interactive_cli"):
            with patch.object(junie_cli, "run_interactive", side_effect=interrupt):
                with self.assertRaises(workflow.RunInterrupted):
                    self.resume(*responses)
            draft = self.record()["repair_pending"]["draft"]
            snapshot = self.run_path / draft["snapshot"]
            self.assertEqual((snapshot / "0.draft").read_bytes(), partial)
            self.assertEqual(json.loads((snapshot / "draft.json").read_text())["targets"][names[1]], None)
            self.assertFalse((snapshot / "1.draft").exists())
            self.assertEqual(self.record()["repair_history"], [])
            for error in (
                junie_cli.JunieLaunchError(["junie"], None, None, OSError("fixture")),
                junie_cli.JunieConnectionError("fixture"),
                junie_cli.JunieSessionError(["junie"], 1, "fixture"),
            ):
                with patch.object(junie_cli, "run_interactive", side_effect=error) as failed:
                    self.resume(*responses)
                    failed.assert_called_once()
                self.assertEqual(self.record()["repair_pending"]["draft"], draft)
            with patch.object(junie_cli, "run_interactive", side_effect=verify_input) as junie:
                with self.assertRaises(workflow.RunInterrupted):
                    self.resume(*responses)
                junie.assert_called_once()
            self.assertIn(draft, self.record()["stopped_repair_recovery"])
            manual = b"class SavedByOperator {}\n"
            self.target.write_bytes(manual)
            with patch.object(junie_cli, "run_interactive") as junie:
                self.resume(*responses)
                junie.assert_not_called()
        self.assertEqual(self.target.read_bytes(), manual)
        self.assertEqual((agent / names[0]).read_bytes(), partial)
        self.assertEqual((snapshot / "0.draft").read_bytes(), partial)
        self.assertIn("停止時の下書き", self.record()["error"])
        self.assertEqual(self.record()["repair_history"], [])

        plan = workflow.plan_stopped_repair(self.config, self.run_id)
        choices = {names[0]: "draft", names[1]: "worktree"}
        (agent / names[0]).write_bytes(b"class AgentManual {}")
        with self.assertRaisesRegex(ValueError, "更新"):
            workflow.resolve_stopped_repair(self.config, self.run_id, plan["version"], choices)
        plan = workflow.plan_stopped_repair(self.config, self.run_id)
        active = self.run_path / plan["snapshot"] / "0.draft"
        active.write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "snapshot"):
            workflow.resolve_stopped_repair(self.config, self.run_id, plan["version"], choices)
        active.write_bytes(partial)
        with patch.object(workflow, "write_run_record", side_effect=OSError("injected plan save failure")):
            with self.assertRaisesRegex(OSError, "save failure"):
                workflow.resolve_stopped_repair(self.config, self.run_id, plan["version"], choices)
        self.assertEqual(self.record()["repair_pending"]["draft"]["snapshot"], plan["snapshot"])
        workflow.resolve_stopped_repair(self.config, self.run_id, plan["version"], choices)
        self.assertEqual((agent / names[0]).read_bytes(), b"class AgentManual {}")

        def recovered(_command, _completion, _token, targets, **kwargs):
            self.assertEqual(targets[0].read_bytes(), partial)
            self.assertEqual(targets[1].read_bytes(), (self.worktree / names[1]).read_bytes())
            self.assertEqual(self.target.read_bytes(), manual)
            raise OperationCancelled("stop after restored input")

        with patch.object(junie_cli, "require_interactive_cli"):
            copyfile = shutil.copyfile

            def fail_second_restore(source, destination, **kwargs):
                if Path(source).name == "1.draft":
                    raise OSError("injected restore write failure")
                return copyfile(source, destination, **kwargs)

            with (
                patch.object(shutil, "copyfile", side_effect=fail_second_restore),
                patch.object(junie_cli, "run_interactive") as junie,
            ):
                self.resume()
                junie.assert_not_called()
            self.assertEqual((agent / names[0]).read_bytes(), partial)
            self.assertFalse((agent / names[1]).exists())
            self.assertIn("restore_from", self.record()["repair_pending"])
            self.assertEqual(self.record()["repair_history"], [])
            with patch.object(junie_cli, "run_interactive", side_effect=recovered) as junie:
                with self.assertRaises(workflow.RunInterrupted):
                    self.resume(*responses)
                junie.assert_called_once()
        self.assertEqual(self.target.read_bytes(), manual)
        backups = self.record()["stopped_repair_recovery"]
        saved_bytes = [(self.run_path / item["snapshot"] / "0.draft").read_bytes() for item in backups]
        self.assertIn(b"class AgentManual {}", saved_bytes)
        self.assertIn(manual, saved_bytes)
        newest = self.record()["repair_pending"]["draft"]
        self.assertNotEqual(newest["snapshot"], plan["snapshot"])
        self.assertEqual(workflow.plan_stopped_repair(self.config, self.run_id)["snapshot"], newest["snapshot"])

    def test_legacy_selection_never_labels_an_ambiguous_archive_as_the_stopped_draft(self):
        name = self.target.name
        agent = self.run_path / "agent-workspace"
        agent.mkdir()
        (agent / name).write_bytes(b"class Stopped {}\n")
        stopped = workflow._snapshot_stopped_repair(
            self.run_path, agent, (Path(name),), {name: sha256(self.target.read_bytes()).hexdigest()},
            workspace_unchanged=True,
        )
        self.save_record({**self.record(), "repair_pending": {"draft": stopped, "details": {}}})

        def save_as_legacy(choice):
            plan = workflow.plan_stopped_repair(self.config, self.run_id)
            workflow.resolve_stopped_repair(self.config, self.run_id, plan["version"], {name: choice})
            record = self.record()
            selected = record["repair_pending"]["draft"]
            del selected["stopped_snapshot"]
            (self.run_path / selected["snapshot"] / "draft.json").write_text(json.dumps(selected), encoding="utf-8")
            self.save_record(record)

        save_as_legacy("worktree")
        self.assertEqual(workflow.plan_stopped_repair(self.config, self.run_id)["snapshot"], stopped["snapshot"])
        save_as_legacy("draft")
        with self.assertRaisesRegex(ValueError, "元の停止下書きを特定できません"):
            workflow.plan_stopped_repair(self.config, self.run_id)
        self.assertEqual(self.resume(self.build, ("fixture-checker", 0, b""))["status"], "checker_succeeded")
        self.assertEqual(self.target.read_text(encoding="utf-8-sig"), "class Stopped {}\n")

    def test_selected_repair_recovers_before_verifying_changed_diagnostics(self):
        names = ("Example.cs", "Second.cs", "Third.cs")
        self.config["units"][0]["targets"] = list(names)
        self.config["max_repair_attempts"] = 1
        for name in names[1:]:
            (self.worktree / name).write_bytes(b"class Original {}\n")
        self.save_record({**self.record(), "targets": list(names)})
        original_record = self.record()
        agent = self.run_path / "agent-workspace"
        agent.mkdir()

        def findings(indexes):
            return ("fixture-checker", 1, "".join(
                f"F{index} {names[index]}:1 error: correction needed\n" for index in indexes
            ).encode())

        def interrupt(_command, _completion, _token, targets, **kwargs):
            targets[0].write_bytes(b"class FirstDraft {}\n")
            targets[1].write_bytes(b"class SecondDraft {}\n")
            raise OperationCancelled("stop after saving both targets")

        for affected in ((1,), (0, 1, 2), (1, 2), (2,), ()):
            with self.subTest(affected=affected):
                self.save_record(original_record)
                for name in names:
                    (self.worktree / name).write_bytes(b"class Original {}\n")
                with (
                    patch.object(junie_cli, "require_interactive_cli"),
                    patch.object(junie_cli, "run_interactive", side_effect=interrupt),
                    self.assertRaises(workflow.RunInterrupted),
                ):
                    self.resume(self.build, findings((0, 1)))
                stopped = self.record()["repair_pending"]["draft"]
                self.target.write_bytes(b"class Manual {}\n")
                plan = workflow.plan_stopped_repair(self.config, self.run_id)
                workflow.resolve_stopped_repair(self.config, self.run_id, plan["version"], {
                    names[0]: "draft", names[1]: "worktree",
                })
                choices = {names[0]: "worktree", names[1]: "draft"}
                with self.assertRaisesRegex(ValueError, "更新"):
                    workflow.resolve_stopped_repair(self.config, self.run_id, plan["version"], choices)
                next_plan = workflow.plan_stopped_repair(self.config, self.run_id)
                self.assertEqual(next_plan["snapshot"], stopped["snapshot"])
                workflow.resolve_stopped_repair(self.config, self.run_id, next_plan["version"], choices)

                selected = self.record()["repair_pending"]["draft"]
                saved_file = self.run_path / selected["snapshot"] / "1.draft"
                partial = saved_file.read_bytes()
                saved_file.write_bytes(b"tampered")
                self.assertIn("snapshot", self.resume()["error"])
                self.assertIn("restore_from", self.record()["repair_pending"])
                saved_file.write_bytes(partial)
                if affected == (1,):
                    replace = Path.replace

                    def fail_adoption(source, destination):
                        if destination == self.worktree / names[1]:
                            raise OSError("injected worktree write failure")
                        return replace(source, destination)

                    with patch.object(Path, "replace", fail_adoption):
                        self.assertIn("worktree write failure", self.resume()["error"])
                    self.assertIn("restore_from", self.record()["repair_pending"])
                    self.assertEqual((agent / names[0]).read_bytes(), b"class Manual {}\n")

                def repair(_command, completion, token, targets, **kwargs):
                    self.assertEqual(tuple(path.name for path in targets), tuple(names[index] for index in affected))
                    self.assertEqual((agent / names[0]).read_bytes(), b"class Manual {}\n")
                    self.assertEqual((agent / names[1]).read_bytes(), partial)
                    for target in targets:
                        target.write_text(f"// fixed {target.name}\n", encoding="utf-8")
                    completion.write_text(token, encoding="utf-8")
                    return 0

                commands = [self.build, findings(affected)] if affected else []
                commands += [self.build, ("fixture-checker", 0, b"")]
                run_patch, calls = self.mock_commands(*commands)
                run = run_patch.kwargs["side_effect"]

                def verify_restored(command, **kwargs):
                    if not calls:
                        self.assertEqual(self.target.read_bytes(), b"class Manual {}\n")
                        self.assertEqual((self.worktree / names[1]).read_text(encoding="utf-8-sig"),
                                         partial.decode())
                    return run(command, **kwargs)

                with (
                    patch.object(junie_cli, "require_interactive_cli"),
                    patch.object(junie_cli, "run_interactive", side_effect=repair) as junie,
                    patch.object(workflow, "run_process", side_effect=verify_restored),
                    redirect_stdout(io.StringIO()),
                ):
                    self.assertEqual(workflow.resume_with_junie(self.config, self.run_id)["status"],
                                     "checker_succeeded")
                self.assertEqual(len(calls), len(commands))
                self.assertEqual(junie.call_count, bool(affected))
                self.assertIn(stopped, self.record()["stopped_repair_recovery"])
                self.assertIn(selected, self.record()["stopped_repair_recovery"])
                if 1 not in affected:
                    self.assertEqual((self.worktree / names[1]).read_text(encoding="utf-8-sig"), partial.decode())

    def test_missing_selected_version_outside_diagnosis_requires_reselection(self):
        second = self.worktree / "Second.cs"
        second.write_bytes(b"class Original {}\n")
        names = (self.target.name, second.name)
        self.config["units"][0]["targets"] = list(names)
        self.config["max_repair_attempts"] = 1
        self.save_record({**self.record(), "targets": list(names)})
        (self.run_path / "agent-workspace").mkdir()

        def interrupt(_command, _completion, _token, targets, **kwargs):
            targets[0].write_bytes(b"class FirstDraft {}\n")
            targets[1].unlink()
            raise OperationCancelled("stop with one missing target")

        with (
            patch.object(junie_cli, "require_interactive_cli"),
            patch.object(junie_cli, "run_interactive", side_effect=interrupt),
            self.assertRaises(workflow.RunInterrupted),
        ):
            self.resume(self.build, ("fixture-checker", 1, b"A Example.cs:1 error: A\nB Second.cs:1 error: B\n"))
        plan = workflow.plan_stopped_repair(self.config, self.run_id)
        workflow.resolve_stopped_repair(self.config, self.run_id, plan["version"], dict.fromkeys(names, "draft"))
        with patch.object(junie_cli, "run_interactive") as junie:
            result = self.resume(self.build, ("fixture-checker", 0, b""))
            junie.assert_not_called()
        self.assertEqual(result["status"], "checker_needs_review")
        self.assertIn("進捗画面", result["error"])
        self.assertTrue(second.is_file())
        self.assertFalse((self.run_path / "agent-workspace" / second.name).exists())
        self.assertEqual(self.target.read_text(encoding="utf-8-sig"), "class FirstDraft {}\n")
        plan = workflow.plan_stopped_repair(self.config, self.run_id)
        workflow.resolve_stopped_repair(self.config, self.run_id, plan["version"], dict.fromkeys(names, "worktree"))
        self.assertEqual(self.resume(self.build, ("fixture-checker", 0, b""))["status"], "checker_succeeded")

    def test_usage_save_failure_preserves_only_unconfirmed_repairs_for_explicit_resume(self):
        self.config["max_repair_attempts"] = 2
        (self.run_path / "agent-workspace").mkdir()
        original_record = self.record()
        original = self.target.read_bytes()
        saved_draft = b"class Interrupted {}\n"
        build_failure = ("dotnet", 1, b"Example.cs(1,1): error CS1002: missing semicolon\n")
        checker_failure = ("fixture-checker", 1, self.diagnostic)
        success = (self.build, ("fixture-checker", 0, b""))

        for responses in ((build_failure,), (self.build, checker_failure)):
            for failure in ("direct stop", "wrapped stop", "ordinary save failure"):
                with self.subTest(phase=responses[-1][0], failure=failure):
                    self.save_record(original_record)
                    self.target.write_bytes(original)
                    fail_usage = failure != "direct stop"
                    replace = Path.replace

                    def fail_usage_replace(source, target):
                        nonlocal fail_usage
                        if fail_usage and target == self.record_path and "token_usage" in json.loads(source.read_bytes()):
                            fail_usage = False
                            raise PermissionError("fixture: final usage could not be saved")
                        return replace(source, target)

                    def interrupt(command, completion, token, targets, **kwargs):
                        targets[0].write_bytes(saved_draft)
                        raise KeyboardInterrupt

                    def fail(command, completion, token, targets, *, report_usage, **kwargs):
                        self.assertEqual(targets[0].read_bytes(), saved_draft)
                        junie_cli.launch_input_observer.get()(command[-1])
                        entry, = self.record()["last_context_plan"]["targets"]
                        self.assertEqual(workflow.target_hash(targets[0]), entry["staged_sha256"])
                        self.assertNotEqual(workflow.target_hash(self.target), entry["staged_sha256"])
                        targets[0].write_bytes(b"class Unconfirmed {}\n")
                        completion.write_text(token, encoding="utf-8")
                        try:
                            if failure != "ordinary save failure":
                                raise junie_cli.JunieStopError("fixture: termination unconfirmed")
                        finally:
                            if failure != "direct stop":
                                report_usage([1, 0, 0, 0, 1])

                    def complete(command, completion, token, targets, **kwargs):
                        self.assertEqual(targets[0].read_bytes(), original)
                        targets[0].write_bytes(b"class Fixed {}\n")
                        completion.write_text(token, encoding="utf-8")
                        return 0

                    with (patch.object(junie_cli, "require_interactive_cli"),
                          patch.object(junie_cli, "run_interactive", side_effect=interrupt) as junie,
                          patch.object(Path, "replace", fail_usage_replace)):
                        with self.assertRaises(workflow.RunInterrupted):
                            self.resume(*responses)
                        self.assertIn("draft", self.record()["repair_pending"])
                        junie.side_effect = fail
                        if failure == "ordinary save failure":
                            result = self.resume(*responses)
                            self.assertIn("final usage could not be saved", result["error"])
                        else:
                            commands, calls = self.mock_commands(*responses)
                            with (commands, redirect_stdout(io.StringIO()),
                                  self.assertRaises(OSError) as stopped):
                                workflow.resume_with_junie(self.config, self.run_id)
                            self.assertIsInstance(
                                junie_cli.unconfirmed_junie_stop(stopped.exception), junie_cli.JunieStopError,
                            )
                            self.assertEqual(len(calls), len(responses))
                        self.assertFalse(fail_usage)
                        self.assertEqual(junie.call_count, 2)
                        self.assertEqual(self.target.read_bytes(), original)
                        saved = self.record()
                        if failure == "ordinary save failure":
                            self.assertEqual(len(saved["repair_history"]), 1)
                            self.assertNotIn("repair_pending", saved)
                            self.resume(*responses)
                            self.assertEqual(junie.call_count, 2)
                        else:
                            self.assertEqual(saved["repair_history"], [])
                            self.assertIn("repair_pending", saved)
                            self.assertNotIn("draft", saved["repair_pending"])
                            archived, = saved["stopped_repair_recovery"]
                            self.assertEqual((self.run_path / archived["snapshot"] / "0.draft").read_bytes(), saved_draft)
                            junie.side_effect = complete
                            result = self.resume(*responses, *success)
                            self.assertEqual(result["status"], "checker_succeeded")
                            self.assertEqual(junie.call_count, 3)
                            self.assertEqual(len(self.record()["repair_history"]), 1)
                            self.assertNotIn("repair_pending", self.record())

    def test_free_form_checker_output_reaches_junie_before_rebuild(self):
        self.config["max_repair_attempts"] = 2
        log = b'{"expected": 42, "actual": 0}\n'
        with (
            patch.object(workflow, "_repair_target") as repair,
            patch.object(junie_cli, "require_interactive_cli"),
        ):
            result = self.resume(
                self.build, ("fixture-checker", 2, log),
                self.build, ("fixture-checker", 0, b""),
            )
        self.assertEqual(result["status"], "checker_succeeded")
        repair.assert_called_once()
        self.assertEqual(repair.call_args.args[5], log.decode())
        self.assertEqual(repair.call_args.args[2], (Path("Example.cs"),))
        self.assertTrue(self.record()["repair_history"][0]["raw_checker_output"])
        self.assertEqual((self.run_path / "checker-before-repair-1.log").read_bytes(), log)

    def test_opt_in_junie_advisory_neither_repairs_nor_passes_free_output(self):
        self.config.update(max_repair_attempts=2, checker_review_free_output=True)
        log = b"Read-only result: expected 42, actual 0\n"
        before = self.target.read_bytes()
        (self.run_path / "checker.log").write_text("previous attempt only", encoding="utf-8")
        with (
            patch.object(workflow, "_repair_target") as repair,
            patch.object(junie_cli, "require_interactive_cli"),
            patch("migration_harness.verification.checker_advisory.run_text_advisory",
                  return_value='{"verdict":"informational","evidence_lines":[1]}') as agent,
        ):
            result = self.resume(self.build, ("fixture-checker", 2, log))
        self.assertEqual(result["status"], "checker_needs_review")
        self.assertEqual(self.target.read_bytes(), before)
        self.assertEqual(self.record()["checker_advisory"]["evidence_lines"], [1])
        self.assertEqual(self.record()["checker_review_reason"], "free_output_advisory")
        self.assertEqual(self.record()["repair_history"], [])
        repair.assert_not_called()
        agent.assert_called_once()
        self.assertIn("expected 42", agent.call_args.args[0])
        self.assertNotIn("previous attempt", agent.call_args.args[0])

    def test_apply_confirmed_free_success_without_edit_or_attempt(self):
        self.config.update(junie_apply_output_decisions=True, checker_review_free_output=True)
        for code, message, expected in (
            (0, "20件の確認が完了しました。すべて正常です。", "checker_succeeded"),
            (0, "問題ありません。\n送料が誤っています", "checker_needs_review"),
            (2, "問題ありません。", "checker_needs_review"),
        ):
            with (
                self.subTest(code=code, message=message),
                patch.object(workflow, "_repair_target") as repair,
                patch.object(junie_cli, "require_interactive_cli"),
                patch("migration_harness.verification.checker_advisory.run_text_advisory",
                      return_value='{"verdict":"passed","evidence_lines":[1]}'),
            ):
                result = self.resume(self.build, ("fixture-checker", code, message.encode()))
                self.assertEqual(result["status"], expected)
                self.assertEqual(self.record()["repair_history"], [])
                repair.assert_not_called()

    def test_advisory_repairs_only_matching_target_with_fresh_build_and_check(self):
        self.config.update(junie_apply_output_decisions=True, checker_review_free_output=True, max_repair_attempts=2)
        diagnostic = b"A Example.cs:1 warning: wrong total\n"
        mixed = diagnostic + b"Do not send this unrecognized line to repair.\n"
        with (
            patch.object(workflow, "_repair_target") as repair,
            patch.object(junie_cli, "require_interactive_cli"),
            patch("migration_harness.verification.checker_advisory.run_text_advisory",
                  return_value='{"verdict":"problem","evidence_lines":[1]}'),
        ):
            result = self.resume(self.build, ("fixture-checker", 2, mixed), self.build, ("fixture-checker", 0, b""))
        self.assertEqual(result["status"], "checker_succeeded")
        repair.assert_called_once()
        self.assertEqual(repair.call_args.args[2], (Path("Example.cs"),))
        self.assertEqual(repair.call_args.args[5], diagnostic.decode().strip())
        attempt, = self.record()["repair_history"]
        self.assertEqual(attempt["advisory_evidence"]["applied"], "repair_requested")

    def test_grounded_free_form_checker_is_allowed_only_by_separate_opt_in(self):
        self.config.update(junie_apply_output_decisions=True, checker_review_free_output=True, max_repair_attempts=2)
        log = b"Shipping charge is not added in the migrated result.\n"
        with (
            patch.object(workflow, "grounded_diagnostic") as grounded,
            patch.object(workflow, "_repair_target") as repair,
            patch.object(junie_cli, "require_interactive_cli"),
            patch("migration_harness.verification.checker_advisory.run_text_advisory",
                  return_value='{"verdict":"problem","evidence_lines":[1]}'),
        ):
            result = self.resume(self.build, ("fixture-checker", 2, log))
        self.assertEqual(result["status"], "checker_needs_review")
        grounded.assert_not_called()
        repair.assert_not_called()

    def test_grounded_free_form_checker_repair_rechecks_and_uses_saved_model(self):
        self.config.update(junie_apply_output_decisions=True, checker_review_free_output=True,
                           junie_ground_output_repairs=True, max_repair_attempts=2,
                           junie_model="configured-model", junie_effort="low")
        self.save_record({**self.record(), "junie_model": "saved-model", "junie_effort": "high"})
        log = b"Shipping charge is not added in the migrated result.\n"
        citation = {"target": "Example.cs", "source_id": "php-source", "source_sha256": "example"}
        with (
            patch.object(workflow, "grounded_diagnostic", return_value={"status": "grounded", "citations": [citation]}) as grounded,
            patch.object(workflow, "_repair_target") as repair,
            patch.object(junie_cli, "require_interactive_cli"),
            patch("migration_harness.verification.checker_advisory.run_text_advisory",
                  return_value='{"verdict":"problem","evidence_lines":[1]}') as agent,
        ):
            result = self.resume(self.build, ("fixture-checker", 2, log), self.build, ("fixture-checker", 0, b""))
        self.assertEqual(result["status"], "checker_succeeded")
        repair.assert_called_once()
        self.assertEqual(repair.call_args.args[2], (Path("Example.cs"),))
        self.assertEqual(grounded.call_args.kwargs["model_options"], {"model": "saved-model", "effort": "high"})
        self.assertEqual(agent.call_args.kwargs, {"model": "saved-model", "effort": "high"})

    def test_environment_failure_omitted_from_citations_blocks_even_structured_repair(self):
        self.config.update(junie_apply_output_decisions=True, checker_review_free_output=True,
                           junie_ground_output_repairs=True, max_repair_attempts=2)
        log = b"A Example.cs:1 warning: wrong total\nAuthentication failed: result incomplete\n"
        with (
            patch.object(workflow, "grounded_diagnostic") as grounded,
            patch.object(workflow, "_repair_target") as repair,
            patch.object(junie_cli, "require_interactive_cli"),
            patch("migration_harness.verification.checker_advisory.run_text_advisory",
                  return_value='{"verdict":"problem","evidence_lines":[1]}'),
        ):
            result = self.resume(self.build, ("fixture-checker", 2, log))
        self.assertEqual(result["status"], "checker_needs_review")
        grounded.assert_not_called()
        repair.assert_not_called()

    def test_opt_in_advisory_preserves_nonzero_exit_even_with_canonical_success_text(self):
        self.config.update(max_repair_attempts=2, checker_review_free_output=True)
        with (
            patch.object(workflow, "_repair_target") as repair,
            patch.object(junie_cli, "require_interactive_cli"),
            patch("migration_harness.verification.checker_advisory.run_text_advisory",
                  return_value='{"verdict":"informational","evidence_lines":[1]}'),
        ):
            result = self.resume(self.build, ("fixture-checker", 9, "特に気になる点はありません。".encode()))
        self.assertEqual(result["status"], "checker_needs_review")
        self.assertEqual(self.record()["checker_exit_code"], 9)
        repair.assert_not_called()

    def test_opt_in_advisory_cancel_stays_a_review_and_skips_repair(self):
        self.config.update(max_repair_attempts=2, checker_review_free_output=True)
        with (
            patch.object(workflow, "_repair_target") as repair,
            patch.object(junie_cli, "require_interactive_cli"),
            patch("migration_harness.verification.checker_advisory.run_text_advisory", side_effect=KeyboardInterrupt),
            self.assertRaises(workflow.RunInterrupted),
        ):
            self.resume(self.build, ("fixture-checker", 1, b"an unknown result\n"))
        self.assertEqual(self.record()["status"], "checker_needs_review")
        self.assertEqual(self.record()["checker_review_reason"], "free_output_advisory_interrupted")
        repair.assert_not_called()

    def test_repeated_free_form_checker_output_stops_before_another_repair(self):
        self.config["max_repair_attempts"] = 2
        with patch.object(workflow, "_repair_target") as repair, patch.object(junie_cli, "require_interactive_cli"):
            result = self.resume(
                self.build, ("fixture-checker", 2, b"expected 42; actual 0\n"),
                self.build, ("fixture-checker", 2, b"expected   42; actual 0\n"),
            )
        repair.assert_called_once()
        self.assertEqual(result["status"], "checker_needs_review")
        self.assertIn("previous checker finding", result["error"])
        self.assertEqual(self.record()["checker_review_reason"], "finding_repeated")

    def test_checker_repair_limit_records_the_review_reason(self):
        self.config["max_repair_attempts"] = 1
        with patch.object(workflow, "_repair_target") as repair, patch.object(junie_cli, "require_interactive_cli"):
            result = self.resume(
                self.build, ("fixture-checker", 1, b"first diagnostic\n"),
                self.build, ("fixture-checker", 1, b"second diagnostic\n"),
            )
        repair.assert_called_once()
        self.assertEqual(result["status"], "checker_needs_review")
        self.assertEqual(self.record()["checker_review_reason"], "repair_limit")

    def test_unreadable_checker_output_requires_review(self):
        self.config["max_repair_attempts"] = 2
        for log in (b"", b"\xff\n", self.diagnostic + b"unknown\0data\n"):
            with self.subTest(log=log), patch.object(workflow, "_repair_target") as repair:
                result = self.resume(self.build, ("fixture-checker", 2, log))
                repair.assert_not_called()
                self.assertEqual(result["status"], "checker_needs_review")
                self.assertEqual(self.checker_log.read_bytes(), log)
                self.assertTrue(self.review()["unrecognized"])

    # 有効な指摘だけなら通常の自動修正経路を使用することを検証する。
    def test_supported_diagnostics_still_allow_automatic_repair(self):
        self.config["max_repair_attempts"] = 1
        with (
            patch.object(junie_cli, "require_interactive_cli"),
            patch.object(workflow, "_repair_target") as repair,
        ):
            result = self.resume(
                self.build, ("fixture-checker", 1, self.diagnostic),
                self.build, ("fixture-checker", 0, b""),
            )
        self.assertEqual(result["status"], "checker_succeeded")
        repair.assert_called_once()
        self.assertEqual(repair.call_args.args[5], self.diagnostic.decode().strip())
        self.assertEqual(len(self.record()["repair_history"]), 1)
        self.assertEqual((self.run_path / "checker-before-repair-1.log").read_bytes(), self.diagnostic)

    # AIに渡せない大きさの指摘でも、人は一覧と承認を操作できることを検証する。
    def test_character_and_byte_limits_only_block_automatic_repair(self):
        self.config["max_repair_attempts"] = 1
        for count, size, error in ((70, 110, "too much text"), (350, 180, "too large")):
            with self.subTest(count=count):
                log = b"".join(
                    f"MIG101 Example.cs:{line} warning: {line} {'x' * size}\n".encode()
                    for line in range(1, count + 1)
                )
                self.assertGreater(len(log), 8_000)
                self.assertEqual(len(log) > 65_536, count == 350)
                with patch.object(workflow, "_repair_target") as repair:
                    result = self.resume(self.build, ("fixture-checker", 1, log))
                repair.assert_not_called()
                self.assertEqual(result["status"], "checker_needs_review")
                self.assertIn(error, result["error"])
                report = self.review()
                self.assertEqual(len(report["findings"]), count)
                self.assertEqual(report["locations"][-1]["line"], str(count))
                last_id = report["findings"][-1][0]
                self.review(last_id, "Reviewed the last finding against the migration.")
                self.assertTrue(self.review()["findings"][-1][2])
                self.assertEqual(len(self.record()["repair_history"]), 0)

    # 大きなログでも有効な承認だけが残れば完了できることを検証する。
    def test_large_approved_log_can_complete_on_resume(self):
        self.config["max_repair_attempts"] = 1
        log = b"MIG101 Example.cs:1 warning: " + b"x" * 65_536 + b"\n"
        self.resume(self.build, ("fixture-checker", 1, log))
        finding_id = self.review()["findings"][0][0]
        self.review(finding_id, "The reported code uses the intended behavior.")
        result = self.resume(self.build, ("fixture-checker", 1, log))
        self.assertEqual(result["status"], "checker_accepted")
        self.assertEqual(self.record()["checker_exit_code"], 1)

    # 既知の指摘をすべて承認しても、未知の出力が残る間は完了にしないことを検証する。
    def test_unknown_output_prevents_previously_approved_findings_from_completing_run(self):
        self.resume(self.build, ("fixture-checker", 1, self.diagnostic))
        self.review(self.review()["findings"][0][0], "The warning is expected.")
        result = self.resume(self.build, ("fixture-checker", 1, self.diagnostic + b"checker offline\n"))
        self.assertEqual(result["status"], "checker_needs_review")
        self.assertIn("unrecognized", result["error"])

    # 同じ識別子の指摘を個別承認と誤認しないことを検証する。
    def test_duplicate_id_remains_ambiguous(self):
        log = self.diagnostic + self.diagnostic.replace(b":1", b":2")
        self.resume(self.build, ("fixture-checker", 1, log))
        first, second = self.review()["findings"]
        self.assertEqual(first[0], second[0])
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            self.review(first[0], "Cannot distinguish these findings.")
        self.assertFalse(self.record().get("checker_reviews"))

    # 承認後に編集した対象を再確認に戻すことを検証する。
    def test_approval_expires_when_target_changes(self):
        self.resume(self.build, ("fixture-checker", 1, self.diagnostic))
        finding_id = self.review()["findings"][0][0]
        self.review(finding_id, "The original content was reviewed.")
        self.target.write_text("class Changed {}\n", encoding="utf-8")
        self.assertFalse(self.review()["findings"][0][2])
        with self.assertRaisesRegex(ValueError, "changed or was missing"):
            self.review(finding_id, "Cannot approve a stale finding.")
        result = self.resume(self.build, ("fixture-checker", 1, self.diagnostic))
        self.assertEqual(result["status"], "checker_needs_review")
        self.assertEqual(self.review()["findings"][0], (finding_id, self.diagnostic.decode().strip(), False))
        self.assertEqual(len(self.record()["checker_reviews"]), 1)


class CheckerResumeApprovalTests(SavedRunFixture):
    diagnostic = b"MIG101 Example.cs:1 warning: use SharedUtility\n"

    def setUp(self):
        super().setUp()
        self.config["build_project"] = "App.csproj"
        (self.worktree / "App.csproj").write_text(
            '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><TargetFramework>net9.0</TargetFramework>'
            '</PropertyGroup></Project>', encoding="utf-8",
        )
        sdk = self.worktree.parent / "fixture-sdk"
        pack = sdk / "packs/Microsoft.NETCore.App.Ref"
        pack.mkdir(parents=True)
        (pack / "fixture.dll").write_bytes(b"fixture reference")
        self.dotnet = sdk / "dotnet"
        self.dotnet.write_bytes(b"fixture launcher")
        sdk_resolver = patch.object(build_receipt.shutil, "which", return_value=str(self.dotnet))
        sdk_resolver.start()
        self.addCleanup(sdk_resolver.stop)
        self.assertEqual(self.verify(self.diagnostic)["status"], "checker_needs_review")
        finding, = self.review()["findings"]
        self.review(finding[0], "This use of the existing helper was checked against the source.")

    def verify(self, output, *, only=False, code=7, rule_failure=False, request=None):
        calls = []

        def process(command, **kwargs):
            calls.append(command[0])
            if command[0] == "dotnet":
                for name, data in {"bin/Debug/net9.0/App.dll": b"fixture build",
                                   "obj/project.assets.json": b'{"libraries":{}}'}.items():
                    path = self.worktree / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data)
                return subprocess.CompletedProcess(command, 0)
            self.assertEqual(command, ["fixture-checker"])
            kwargs["stdout"].write(output)
            if rule_failure:
                env = kwargs["env"]
                request_data = json.loads(Path(env["MIGRATION_RULE_CHECK_REQUEST"]).read_bytes())
                name, rule = next(iter(request_data["rules"].items()))
                Path(env["MIGRATION_RULE_CHECK_RECEIPT"]).write_text(json.dumps({
                    "schema": 1, "request_id": request_data["request_id"],
                    "results": [{"rule": name, "sha256": rule["sha256"], "start_line": 1,
                                 "end_line": rule["saved_file_lines"], "status": "failed", "evidence": "not reflected"}],
                }), encoding="utf-8")
            return subprocess.CompletedProcess(command, code)

        if only and request is None:
            request = {**workflow.plan_checker_resume(self.config, self.run_id), "id": str(uuid.uuid4())}
        with (patch.object(workflow, "run_process", side_effect=process),
              patch.object(junie_cli, "run_interactive") as junie,
              redirect_stdout(io.StringIO())):
            try:
                result = workflow.resume_with_junie(self.config, self.run_id, verification_request=request)
            except ValueError:
                self.assertEqual(calls, [])
                raise
            finally:
                junie.assert_not_called()
        self.assertEqual(calls, ["fixture-checker"] if only else ["dotnet", "fixture-checker"])
        self.assertEqual(result["checker_exit_code"], code)
        self.assertEqual(self.record()["checker_exit_code"], code)
        return result

    def assert_both_statuses(self, output, expected, **options):
        before = self.record()
        for only in (True, False):
            with self.subTest(checker_only=only):
                self.save_record(before)
                self.assertEqual(self.verify(output, only=only, **options)["status"], expected)
                self.assertEqual(self.record()["status"], expected)
                result = workflow.migration_ledger(self.config, self.run_id)["completion"]
                self.assertIs(result["verification_passed"], expected == "checker_accepted")

    def test_same_approved_finding_is_accepted_by_both_paths(self):
        self.assert_both_statuses(self.diagnostic, "checker_accepted")

    def test_new_or_unrecognized_finding_keeps_both_paths_in_review(self):
        for extra in (b"MIG102 Example.cs:1 warning: distinct warning\n", b"checker offline\n",
                      self.diagnostic.replace(b":1", b":2")):
            self.assert_both_statuses(self.diagnostic + extra, "checker_needs_review")
        self.assert_both_statuses(b"", "checker_needs_review")

    def test_changed_target_invalidates_the_receipt_and_existing_approval(self):
        plan = {**workflow.plan_checker_resume(self.config, self.run_id), "id": "old-target"}
        self.target.write_text("class Updated {}\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "再ビルド"):
            self.verify(self.diagnostic, only=True, request=plan)
        self.assertFalse(self.review()["findings"][0][2])
        shutil.rmtree(self.worktree / "bin")
        shutil.rmtree(self.worktree / "obj")
        self.assertEqual(self.verify(self.diagnostic)["status"], "checker_needs_review")
        self.assertEqual(self.verify(self.diagnostic, only=True)["status"], "checker_needs_review")
        self.assertFalse(workflow.migration_ledger(self.config, self.run_id)["completion"]["verification_passed"])

    def test_change_during_checker_only_is_detected_before_approval(self):
        plan = {**workflow.plan_checker_resume(self.config, self.run_id), "id": "change-during-check"}

        def process(command, **kwargs):
            self.assertEqual(command, ["fixture-checker"])
            kwargs["stdout"].write(self.diagnostic)
            self.target.write_bytes(b"class Updated {}")
            return subprocess.CompletedProcess(command, 7)

        with (patch.object(workflow, "run_process", side_effect=process) as checker,
              patch.object(junie_cli, "run_interactive") as junie, redirect_stdout(io.StringIO())):
            result = workflow.resume_with_junie(self.config, self.run_id, verification_request=plan)
        checker.assert_called_once()
        junie.assert_not_called()
        self.assertEqual(result["status"], "checker_failed")
        self.assertIn("再ビルド", result["error"])
        self.assertEqual(self.record()["status"], "checker_failed")
        self.assertFalse(workflow.migration_ledger(self.config, self.run_id)["completion"]["verification_passed"])

    def test_failed_rule_evidence_blocks_approved_diagnostics_in_both_paths(self):
        capsule = self.run_path / "capsule"
        material = capsule / "materials/rule-1.txt"
        material.parent.mkdir()
        data = b"Apply the reference behavior.\n"
        material.write_bytes(data)
        manifest_path = capsule / "manifest.json"
        manifest = json.loads(manifest_path.read_bytes())
        manifest["rule_files"] = [{"file": material.name, "usage": "required", "sha256": sha256(data).hexdigest()}]
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        self.config["checker_rule_evidence"] = True
        self.assert_both_statuses(self.diagnostic, "checker_needs_review", code=0, rule_failure=True)
        self.assertEqual(self.record()["checker_review_reason"], "rule_not_reflected")


class SavedDashboardTests(SavedRunFixture):
    """保存済みRunの画面取得と、明示した操作だけが起動することを検証する。"""

    @staticmethod
    def get_ready_event(url, *, after=0):
        """SSEから操作可能になった時点の連番と状態を取得する。"""
        with urlopen(url + "events", timeout=3) as response:
            for line in response:
                if line.startswith(b"id: "):
                    revision = int(line.split(b": ", 1)[1])
                elif line.startswith(b"data: "):
                    state = json.loads(line.split(b": ", 1)[1])
                    if state.get("ready") and revision > after:
                        return revision, state
        raise AssertionError("no ready dashboard event")

    @staticmethod
    def post(url, endpoint, payload):
        origin = urlsplit(url)
        headers = {"Origin": f"{origin.scheme}://{origin.netloc}", "Content-Type": "application/json"}
        return urlopen(Request(url + endpoint, json.dumps(payload).encode(), headers), timeout=3)

    def open_dashboard(self, inspect, *, resume=False):
        terminal = io.StringIO()
        view = dashboard.Dashboard()
        dequeue = view.actions.get
        inspected = False

        def interact(*, timeout):
            nonlocal inspected
            if inspected:
                if resume:
                    with view.manager.condition:
                        self.assertTrue(view.manager.condition.wait_for(
                            lambda: self.run_id not in view.manager.workers, timeout=10,
                        ), "saved run did not finish")
                raise KeyboardInterrupt
            inspected = True
            first_line = terminal.getvalue().splitlines()[0]
            inspect(first_line.removeprefix("Progress dashboard: "))
            if resume:
                return dequeue(timeout=timeout)
            raise KeyboardInterrupt

        with (
            redirect_stdout(terminal),
            patch.object(dashboard, "Dashboard", return_value=view),
            patch.object(view.actions, "get", side_effect=interact),
        ):
            return dashboard.run_dashboard(self.config, run_id=self.run_id)

    def test_recovery_reads_selected_saved_run_and_rejects_a_different_run(self):
        self.save_record({**self.record(), "status": "script_failed", "script_phase": "before_migration",
                          "script_inflight": {"phase": "before_migration", "number": 2}})

        def inspect(url):
            _, state = self.get_ready_event(url)
            self.assertFalse(state["resumable"])
            with urlopen(f"{url}recovery?run_id={self.run_id}", timeout=3) as response:
                data = json.load(response)
            self.assertEqual(data["run_id"], self.run_id)
            self.assertEqual(data["unknown_script"], {"phase": "before_migration", "number": 2,
                                                      "log": "before_migration.log"})
            with self.assertRaises(HTTPError) as rejected:
                urlopen(f"{url}recovery?run_id=00000000-0000-4000-8000-000000000001", timeout=3)
            self.assertEqual(rejected.exception.code, 409)

        self.open_dashboard(inspect)

    def test_recovery_dashboard_compares_and_selects_current_draft_safely(self):
        agent = self.run_path / "agent-workspace"
        agent.mkdir()
        draft_file = agent / self.target.name
        draft_file.write_bytes(b"class StoppedDraft {}\n")
        base = {self.target.name: sha256(self.target.read_bytes()).hexdigest()}
        draft = workflow._save_target_snapshot(self.run_path, {self.target.name: draft_file}, base, workspace_unchanged=True)
        self.target.write_bytes(b"class Manual {}\n")
        self.save_record({**self.record(), "status": "interrupted", "repair_pending": {"draft": draft}})

        def inspect(url):
            self.get_ready_event(url)
            query = urlencode({"run_id": self.run_id, "kind": "draft"})
            with urlopen(url + "recovery-plan?" + query, timeout=3) as response:
                plan = json.load(response)
            preview_query = urlencode({"run_id": self.run_id, "version": plan["version"], "target": self.target.name})
            with urlopen(url + "recovery-file?" + preview_query, timeout=3) as response:
                preview = json.load(response)
            self.assertEqual(preview["draft"]["text"], "class StoppedDraft {}\n")
            self.assertEqual(preview["worktree"]["text"], "class Manual {}\n")

            self.target.write_bytes(b"class ChangedAgain {}\n")
            action = {"run_id": self.run_id, "kind": "draft", "version": plan["version"],
                      "choices": {self.target.name: "draft"}}
            for request in (lambda: urlopen(url + "recovery-file?" + preview_query, timeout=3),
                            lambda: self.post(url, "recovery-plan", action)):
                with self.assertRaises(HTTPError) as rejected:
                    request()
                self.assertEqual(rejected.exception.code, 409)
                rejected.exception.close()
            with urlopen(url + "recovery-plan?" + query, timeout=3) as response:
                action["version"] = json.load(response)["version"]
            with self.post(url, "recovery-plan", action) as response:
                self.assertEqual(response.status, 200)
            with urlopen(url + "recovery?run_id=" + self.run_id, timeout=3) as response:
                self.assertTrue(json.load(response)["draft_selected"])
            self.assertIn("restore_from", self.record()["repair_pending"])
            self.assertEqual(self.target.read_text(encoding="utf-8"), "class ChangedAgain {}\n")
            self.assertEqual(draft_file.read_text(encoding="utf-8"), "class StoppedDraft {}\n")

        self.open_dashboard(inspect)

    def test_recovery_keeps_legacy_migration_and_protection_states(self):
        original = self.record()

        def inspect(url):
            self.get_ready_event(url)
            for changes, draft, process_check in (
                ({}, None, False),
                ({"repair_pending": {"snapshot": "legacy-snapshot"}}, "repair", False),
                ({"repair_pending": {"draft": {"snapshot": "modern-snapshot"}},
                  "unconfirmed_command": {"log": "unconfirmed.log"}}, "repair", True),
                ({"repair_pending": {"draft": {"snapshot": "modern-snapshot"}},
                  "stopped_migration_draft": {"snapshot": "migration-snapshot"}}, "migration", False),
                ({"repair_pending": {"draft": {"targets": {}}}}, None, False),
            ):
                with self.subTest(changes=changes):
                    self.save_record({**original, **changes})
                    with urlopen(f"{url}recovery?run_id={self.run_id}", timeout=3) as response:
                        data = json.load(response)
                    self.assertEqual((data["draft"], data["needs_process_check"]), (draft, process_check))
                    self.assertEqual(data["unconfirmed_command"], changes.get("unconfirmed_command"))

            with editor_queue(self.run_path) as queue:
                queue["edits"][self.target.name] = {
                    "id": "fixture-edit", "base": "class Original {}", "content": "class Manual {}", "status": "conflict",
                }
            self.save_record({**original, "repair_pending": {"draft": {"snapshot": "modern-snapshot"}}})
            with urlopen(f"{url}recovery?run_id={self.run_id}", timeout=3) as response:
                data = json.load(response)
            self.assertEqual(data["draft"], "repair")
            self.assertTrue(data["conflict"])
            self.assertEqual(data["pending_edits"], 1)

        self.open_dashboard(inspect)

    def test_checker_dashboard_displays_raw_output_and_resumes_with_instruction(self):
        raw = b'{"expected": 42, "actual": 0}\n'
        self.checker_log.write_bytes(raw)
        self.save_record({**self.record(), "checker_review_reason": "finding_repeated",
                          "error": "a previous checker finding is still present"})
        instruction = "Keep the shared conversion and correct the returned value."

        def send_instruction(url):
            revision, state = self.get_ready_event(url)
            self.assertEqual(state["checker_review_reason"], "finding_repeated")
            self.assertIn("previous checker finding", state["error"])
            route = url + "checker-output?run_id=" + self.run_id
            with self.assertRaises(HTTPError) as stale:
                urlopen(url + "checker-output?run_id=wrong", timeout=3)
            self.assertEqual(stale.exception.code, 409)
            stale.exception.close()
            with urlopen(route, timeout=3) as response:
                self.assertEqual(json.load(response), {"output": raw.decode(), "truncated": False})
            with urlopen(route + "&download=1", timeout=3) as response:
                self.assertEqual(response.read(), raw)
            with self.post(url, "control", {
                "action": "resume", "revision": revision, "checker_instruction": instruction,
            }) as response:
                self.assertEqual(response.status, 202)

        mock, calls = self.mock_commands(("dotnet", 0, b"build ok"), ("fixture-checker", 0, b""))
        with mock, patch.object(workflow, "_repair_target") as repair:
            result = self.open_dashboard(send_instruction, resume=True)
        self.assertEqual(result["status"], "checker_succeeded")
        self.assertNotIn("checker_review_reason", self.record())
        self.assertEqual(len(calls), 2)
        self.assertEqual(repair.call_args.args[5], raw.decode())
        self.assertEqual(self.record()["repair_history"][0]["instruction"], instruction)

    def test_build_failure_dashboard_saves_a_large_target_and_rebuilds(self):
        self.save_record({**self.record(), "status": "build_failed", "build_exit_code": 1})
        original = "class Original {}\n/*\n" + "\t\\ コメント\n" * 100_000 + "*/\n"
        self.target.write_text(original, encoding="utf-8", newline="")
        edited = original.replace("class Original", "class Repaired", 1)

        def inspect(url):
            revision, state = self.get_ready_event(url)
            self.assertEqual(state["status"], "build_failed")
            with urlopen(f"{url}build-review?run_id={self.run_id}&target={self.target.name}", timeout=3) as response:
                source = json.load(response)
            self.assertEqual(source["content"], original)
            self.assertEqual(source["bom_bytes"], 3)
            with self.post(url, "build-response", {
                "action": "build-response", "revision": revision,
                "edit": {"target": self.target.name, "version": source["version"], "content": edited},
            }) as response:
                self.assertEqual(response.status, 202)

        commands, calls = self.mock_commands(("dotnet", 0, b"ok\n"), ("fixture-checker", 0, b""))
        with commands:
            result = self.open_dashboard(inspect, resume=True)
        self.assertEqual(result["status"], "checker_succeeded")
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.target.read_bytes(), edited.encode("utf-8-sig"))

    # 完了状態で外部コマンドを起動せず、記録と比較を閲覧できることを検証する。
    def test_complete_runs_show_saved_results_ledger_and_diff_without_running_commands(self):
        self.config["diff_viewer_anchor_reference"] = None
        original = self.record()
        for status, checker_exit in (("checker_succeeded", 0), ("checker_accepted", 1), ("build_succeeded", None)):
            with self.subTest(status=status):
                saved = {**original, "status": status}
                if checker_exit is None:
                    self.config["checker_command"] = None
                    saved.pop("checker_exit_code")
                    saved.pop("checker_log_path")
                else:
                    saved["checker_exit_code"] = checker_exit
                self.save_record(saved)
                before = self.record_path.read_bytes()

                def inspect(url):
                    revision, state = self.get_ready_event(url)
                    self.assertEqual(state["status"], status)
                    self.assertEqual(state["build_exit_code"], 0)
                    self.assertEqual(state.get("checker_exit_code"), checker_exit)
                    self.assertTrue(state["resumable"])
                    with urlopen(url, timeout=3) as response:
                        response.readline()
                        root_tag = response.readline()
                    self.assertIn(b"data-saved", root_tag)
                    self.assertIn(b"data-viewer-enabled", root_tag)
                    with urlopen(url + "migration-ledger", timeout=3) as response:
                        ledger = json.load(response)
                        self.assertEqual(ledger["capsules"][0]["sources"][0]["path"], "example.php")
                        self.assertFalse(ledger["completion"]["complete"])
                    with self.assertRaises(HTTPError) as stale_comparison:
                        self.post(url, "comparison", {"run_id": "a-different-run"})
                    with stale_comparison.exception as response:
                        self.assertEqual(response.code, 409)
                    with self.post(url, "comparison", {"run_id": self.run_id}) as response:
                        comparison = json.load(response)
                    with urlopen(f"{url}review/{comparison['run_id']}/{comparison['id']}.json", timeout=3) as response:
                        self.assertIn("class Original", json.load(response)["workspace"]["rightText"])
                    with self.assertRaises(HTTPError) as rejected:
                        self.post(url, "control", {
                            "action": "accept", "finding_id": "123456789abc", "reason": "Done", "revision": revision,
                        })
                    with rejected.exception as response:
                        self.assertEqual(response.code, 409)

                with (
                    patch.object(workflow.subprocess, "run") as commands,
                    patch.object(dashboard, "run_with_junie") as junie,
                    patch.object(dashboard, "resume_with_junie") as resume,
                    patch.object(dashboard, "generate_anchors") as anchors,
                ):
                    result = self.open_dashboard(inspect)
                commands.assert_not_called()
                junie.assert_not_called()
                resume.assert_not_called()
                anchors.assert_not_called()
                self.assertEqual(result["status"], status)
                self.assertEqual(self.record_path.read_bytes(), before)

    def test_reference_preview_streams_and_cancels_without_saving(self):
        from migration_harness.execution import check_cancelled, report_activity

        project = Path(self.config["project_root"])
        (project / ".git").mkdir()
        config_path = project.parent / "settings.toml"
        self.config["config_path"] = str(config_path)
        content = (
            '[project]\nroot = "project"\nworktree_root = "trees"\nrun_root = "runs"\n'
            '[target_context]\nroots = []\nfolders = ["Shared"]\nmax_files = 0\n'
            '[[units]]\nid = "例"\nsource = "example.php"\ntarget = "Example.cs"\n'
        )
        shared = project / "Shared"
        shared.mkdir()
        (shared / "Helper.cs").write_bytes(b"helper")
        release = Event()

        def inspect(url):
            request = {"mode": "reference-preview", "content": content, "unit": "例",
                       "request_id": str(uuid.uuid4())}
            with self.post(url, "settings", request) as response:
                events = [json.loads(line) for line in response]
            self.assertTrue(events[0]["progress"])
            result = events[-1]["result"]
            self.assertEqual((result["roots"], result["files"], result["bytes"]), (["Shared"], 1, 6))
            self.assertEqual(result["exceeded"]["max_files"], {"actual": 1, "limit": "0"})

            def wait_for_cancel(*_args):
                report_activity("fixture scanning")
                self.assertTrue(release.wait(5))
                check_cancelled()
                raise AssertionError("preview should be cancelled")

            with patch.object(dashboard, "preview_target_context", side_effect=wait_for_cancel):
                with self.post(url, "settings", request) as response:
                    self.assertTrue(json.loads(response.readline())["progress"])
                    self.assertEqual(json.loads(response.readline()), {"progress": "fixture scanning"})
                    try:
                        with self.post(url, "settings", {"mode": "reference-cancel",
                                                         "request_id": request["request_id"]}) as cancelled:
                            self.assertEqual(cancelled.status, 200)
                    finally:
                        release.set()
                    self.assertEqual(json.loads(response.readline()), {"cancelled": True})
            self.assertFalse(config_path.exists())
            self.assertFalse((project.parent / "trees").exists())

        self.open_dashboard(inspect)

    def test_settings_unit_context_distinguishes_inheritance_empty_and_zero(self):
        import tomllib

        source = (
            '[target_context]\nfolders = ["Shared"]\nexclude = ["bin"]\n'
            '[[units]]\nid = "a"\n[units.target_context]\nroots = []\n'
            'max_bytes = 9007199254740993 # keep this value\nlegacy_key = "keep"\n'
            '[[units]]\nid = "b"\n[units.target_context]\nrecursive = false\n'
        )
        draft = build_settings_draft(source, {"units": {"0": {"target_context": {
            "folders": [], "exclude": "", "max_files": "0", "recursive": "true",
        }}}})
        context = tomllib.loads(draft["content"])["units"][0]["target_context"]
        self.assertEqual(context, {"roots": [], "folders": [], "max_files": 0, "recursive": True,
                                  "max_bytes": 9007199254740993, "legacy_key": "keep"})
        self.assertEqual(draft["form"]["units"][0]["target_context"]["max_bytes"], "9007199254740993")
        self.assertEqual(draft["form"]["units"][1]["target_context"], {"recursive": "false"})
        self.assertIn("max_bytes = 9007199254740993 # keep this value", draft["content"])
        inherited = build_settings_draft(draft["content"], {"units": {"0": {"target_context": {"folders": ""}}}})
        self.assertNotIn("folders", tomllib.loads(inherited["content"])["units"][0]["target_context"])
        self.assertEqual(tomllib.loads(inherited["content"])["target_context"]["folders"], ["Shared"])

    def test_dashboard_validates_settings_before_saving_and_detects_external_edits(self):
        project = Path(self.config["project_root"])
        (project / ".git").mkdir()
        config_path = project.parent / "settings.toml"
        self.config["config_path"] = str(config_path)
        self.config.update(checker_from_cli=True, toml_checker_command=None, checker_command=["cli-checker"])
        valid = '[project]\nroot = "project"\nworktree_root = "trees"\nrun_root = "runs"\n'
        valid += '\n[[units]]\nid = "メニュー"\nsource = "example.php"\ntarget = "Example.cs"\n'

        def inspect(url):
            revision, _ = self.get_ready_event(url)
            with urlopen(url + "settings", timeout=3) as response:
                settings = json.load(response)
            self.assertEqual(settings["version"], "missing")
            request = {"revision": revision, "version": settings["version"]}
            with self.assertRaises(HTTPError) as invalid:
                self.post(url, "settings", {**request, "content": 'project = "invalid"'})
            self.assertEqual(invalid.exception.code, 409)
            invalid.exception.close()
            self.assertFalse(config_path.exists())
            with self.post(url, "settings", {**request, "content": valid}) as response:
                saved = json.load(response)
            revision, _ = self.get_ready_event(url, after=revision)
            self.assertIsNone(self.config["checker_command"])
            self.assertNotIn("checker_from_cli", self.config)
            self.assertNotIn("toml_checker_command", self.config)
            incomplete = valid.replace('target = "Example.cs"', 'target = "Pages/Example.cshtml"')
            with self.assertRaises(HTTPError) as invalid:
                self.post(url, "settings", {"revision": revision, "version": saved["version"], "content": incomplete})
            with invalid.exception as response:
                self.assertEqual(response.code, 409)
                self.assertIn("Pages/Example.cshtml.cs", response.read().decode("utf-8"))
            self.assertEqual(config_path.read_text(encoding="utf-8"), valid)
            source = valid.replace('target = "Example.cs"', 'target = "Example.cs" # Keep target note')
            source += '\n[checker]\n# Keep this checker\ncommand = ["fixture-checker"]\n'
            pair = ["Example.cshtml.cs", "Example.cshtml"]
            with self.post(url, "settings", {
                "mode": "preview", "content": source,
                "changes": {"units": {"0": {"id": "注文確認", "targets": pair}}},
            }) as response:
                draft = json.load(response)
            self.assertEqual(draft["form"]["units"][0]["id"], "注文確認")
            self.assertIn('# Keep this checker\ncommand = ["fixture-checker"]', draft["content"])
            self.assertIn('targets = ["Example.cshtml.cs", "Example.cshtml"] # Keep target note', draft["content"])
            self.assertEqual(config_path.read_text(encoding="utf-8"), valid)
            with urlopen(url + "workspace", timeout=3) as response:
                self.assertEqual(json.load(response)["units"], ["メニュー"])
            external_edit = valid + "# Updated on disk\n"
            config_path.write_text(external_edit, encoding="utf-8", newline="")
            with self.assertRaises(HTTPError) as stale:
                self.post(url, "settings", {"revision": revision, "version": saved["version"], "content": valid})
            self.assertEqual(stale.exception.code, 409)
            stale.exception.close()
            self.assertEqual(config_path.read_text(encoding="utf-8"), external_edit)

        self.open_dashboard(inspect)

    def test_settings_form_edits_rules_and_reference_pairs_without_changing_other_inputs(self):
        source = """[[units]]
id = "メニュー"
source = "menu.php"
target = "Menu.cs"

[[units.inputs]]
role = "migration_rules"
path = "rules.md" # Keep range
start_line = 2
end_line = 4

[[units.inputs]]
role = "reference_php"
path = "old.php"
shared = true

[[units.inputs]]
role = "reference_csharp"
path = "Old.cs"
shared = true

[[units.inputs]]
role = "target_constraints"
path = "constraints.md"
start_line = 1
end_line = 2
shared = true

[[units]]
id = "検索"
source = "search.php"
target = "Search.cs"
"""
        def inspect(url):
            def preview(content, changes):
                with self.post(url, "settings", {
                    "mode": "preview", "content": content, "changes": changes,
                }) as response:
                    return json.load(response)

            migrated = preview(source, {})
            self.assertEqual([item["path"] for item in migrated["form"]["references"]], ["old.php", "Old.cs"])
            draft = preview(migrated["content"], {
                "migrationRules": [
                    {"index": 0, "role": "migration_rules", "path": "updated.md"},
                    {"role": "migration_rules", "path": "extra.md"},
                ],
                "references": [
                    {"role": "reference_php", "path": "example.php"},
                    {"role": "reference_csharp", "path": "Example.cshtml.cs"},
                    {"role": "reference_razor", "path": "Example.cshtml"},
                ],
            })
            rules = draft["form"]["migrationRules"]
            self.assertEqual(rules[0], {
                "index": 0, "role": "migration_rules", "path": "updated.md", "start_line": 2, "end_line": 4,
            })
            self.assertEqual([item["role"] for item in draft["form"]["references"]], [
                "reference_php", "reference_csharp", "reference_razor",
            ])
            self.assertIn('path = "updated.md" # Keep range', draft["content"])
            self.assertIn("[[project.migration_rules]]", draft["content"])
            self.assertIn("[[project.references]]", draft["content"])
            self.assertEqual(draft["content"].count("[[units.inputs]]"), 1)
            draft = preview(draft["content"], {"migrationRules": [rules[1]], "references": []})
            self.assertNotIn('path = "example.php"', draft["content"])
            self.assertNotIn('path = "updated.md"', draft["content"])
            self.assertIn('path = "constraints.md"', draft["content"])
            self.assertEqual(draft["form"]["migrationRules"], [
                {"index": 0, "role": "migration_rules", "path": "extra.md"},
            ])

        self.open_dashboard(inspect)

    def test_settings_cleanup_removes_only_empty_unit_at_its_index(self):
        content = (
            '[[units]]\nid = "existing"\nsource = "existing.php"\ntarget = "Existing.cs"\n'
            '[[units]]\nid = " "\ntargets = []\n'
            '[[units]]\nid = "added"\nsource = "added.php"\ntarget = "Added.cs"\n'
        )
        draft = build_settings_draft(content, {"removeEmptyUnits": [1]})
        self.assertEqual([unit["id"] for unit in draft["form"]["units"]], ["existing", "added"])
        with self.assertRaisesRegex(ValueError, "入力済み"):
            build_settings_draft(content, {"removeEmptyUnits": [0]})

    def test_settings_form_updates_build_and_checker_and_can_restore_defaults(self):
        content = (
            '[build]\nproject = "App.sln" # Build this project\n'
            '[checker]\ncommand = ["py", "check.py"]\nmax_repair_attempts = 2\n'
            '[project]\nbase_ref = "stable"\n'
        )
        form = build_settings_draft(content)["form"]
        self.assertEqual(form["build"]["project"], "App.sln")
        self.assertEqual(form["checker"], {"command": ["py", "check.py"], "max_repair_attempts": "2"})
        command = ["powershell.exe", "-File", "tools/check migration.ps1", ""]
        draft = build_settings_draft(content, {
            "build": {"project": "src/App.csproj"},
            "checker": {"command": command, "max_repair_attempts": "0"},
        })
        self.assertIn('project = "src/App.csproj" # Build this project', draft["content"])
        self.assertIn('base_ref = "stable"', draft["content"])
        self.assertIn("max_repair_attempts = 0", draft["content"])
        self.assertEqual(draft["form"]["checker"], {"command": command, "max_repair_attempts": "0"})

        cleared = build_settings_draft(draft["content"], {
            "build": {"project": ""},
            "checker": {"command": [], "max_repair_attempts": ""},
        })
        self.assertNotIn("max_repair_attempts =", cleared["content"])
        self.assertEqual(cleared["form"]["build"]["project"], "")
        self.assertEqual(cleared["form"]["checker"], {"command": [], "max_repair_attempts": ""})
        with self.assertRaisesRegex(ValueError, "0以上の整数"):
            build_settings_draft(draft["content"], {"checker": {"max_repair_attempts": "-1"}})

    def test_settings_form_edits_project_options_and_preserves_legacy_inputs(self):
        content = (
            '[project]\nroot = "app"\nbase_ref = "stable" # Keep base reference note\n'
            '[diff_viewer]\nanchor_reference = "old.workspace.json"\n'
            '[[units]]\nid = "legacy"\ntarget = "Legacy.cs"\nsource_encoding = "auto"\n'
            '[[units.inputs]]\nrole = "php"\npath = "legacy.php"\nstart_line = 1\nend_line = 2\ncustom = "keep"\n'
        )
        form = build_settings_draft(content)["form"]
        self.assertEqual(form["project"]["base_ref"], "stable")
        self.assertNotIn("inputs", form["units"][0])
        self.assertNotIn("source_encoding", form["units"][0])
        self.assertEqual(form["diff_viewer"]["anchor_reference"], "old.workspace.json")
        draft = build_settings_draft(content, {
            "project": {"base_ref": "main", "max_context_chars": "25000"},
            "junie": {"max_steps": "20"},
            "diff_viewer": {"html": "viewer.html", "enabled": "false"},
            "migrationRules": [{"role": "migration_rules", "path": "rules.md", "encoding": "utf-8"}],
            "units": {"0": {"id": "renamed"}},
        })
        self.assertIn('base_ref = "main" # Keep base reference note', draft["content"])
        self.assertIn('custom = "keep"', draft["content"])
        self.assertIn('source_encoding = "auto"', draft["content"])
        self.assertIn('anchor_reference = "old.workspace.json"', draft["content"])
        self.assertIn("enabled = false", draft["content"])
        self.assertEqual(draft["form"]["diff_viewer"]["html"], "viewer.html")
        self.assertEqual(draft["form"]["diff_viewer"]["enabled"], "false")
        self.assertIn('end_line = 2', draft["content"])
        self.assertEqual(draft["form"]["junie"]["max_steps"], "20")
        self.assertEqual(draft["form"]["migrationRules"][0]["encoding"], "utf-8")
        with self.assertRaisesRegex(ValueError, "1000以上"):
            build_settings_draft(content, {"project": {"max_context_chars": "999"}})

    # 保存済みRunを開くだけでは再開せず、操作を受けてから実行することを検証する。
    def test_saved_resumable_run_waits_for_explicit_resume(self):
        mock, calls = self.mock_commands(("dotnet", 0, b"rebuilt\n"), ("fixture-checker", 0, b""))
        before = self.record_path.read_bytes()

        def inspect(url):
            revision, state = self.get_ready_event(url)
            self.assertTrue(state["resumable"])
            self.assertEqual(state["status"], "checker_needs_review")
            self.assertEqual(calls, [])
            self.assertEqual(self.record_path.read_bytes(), before)
            with self.post(url, "control", {"action": "resume", "revision": revision}) as response:
                self.assertEqual(response.status, 202)

        with mock:
            result = self.open_dashboard(inspect, resume=True)
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["status"], "checker_succeeded")

    def test_resume_from_run_selection_rechecks_the_selected_run(self):
        self.config["diff_viewer_html"] = None
        mock, calls = self.mock_commands(("dotnet", 0, b"rebuilt\n"), ("fixture-checker", 0, b""))

        def inspect(url):
            revision, state = self.get_ready_event(url)
            self.assertFalse(state["diff_viewer_enabled"])
            for endpoint, payload in (
                ("comparison", {}),
                ("control", {"action": "anchors", "revision": revision}),
            ):
                with self.assertRaises(HTTPError) as rejected:
                    self.post(url, endpoint, payload)
                with rejected.exception as response:
                    self.assertEqual(response.code, 409)
            with self.post(url, "control", {"action": "resume-run", "run_id": self.run_id, "revision": revision}) as response:
                self.assertEqual(response.status, 202)

        with mock:
            result = self.open_dashboard(inspect, resume=True)
        self.assertEqual(result["status"], "checker_succeeded")
        self.assertEqual(len(calls), 2)
