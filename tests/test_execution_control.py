import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from threading import Thread
from unittest.mock import patch

from fixtures import HarnessCase
from migration_harness import run_records, workflow
from migration_harness.dashboard import Dashboard
from migration_harness.execution import ExecutionControl, OperationCancelled, control_scope, run_process
from migration_harness.timings import freeze_timings, update_timings


class TimingTests(unittest.TestCase):
    def test_repeated_stages_accumulate_without_paused_time(self):
        record = {"status": "junie_running"}
        update_timings(record, now=10)
        record["status"] = "building"
        update_timings(record, now=30)
        record["status"] = "interrupted"
        update_timings(record, now=35)
        record["status"] = "junie_running"
        update_timings(record, now=1000)
        record["status"] = "checker_succeeded"
        update_timings(record, now=1007)
        self.assertEqual(record["stage_timings"]["3"]["elapsed_seconds"], 27)
        self.assertEqual(record["stage_timings"]["4"]["elapsed_seconds"], 5)
        self.assertIsNone(record["active_stage"])
        self.assertNotIn("6", record["stage_timings"])

    def test_saved_active_run_freezes_at_last_observed_time(self):
        record = {"status": "building"}
        update_timings(record, now=100)
        update_timings(record, now=120)
        frozen = freeze_timings(record)
        self.assertEqual(frozen["4"], {"elapsed_seconds": 20, "started_at": None})
        self.assertEqual(record["stage_timings"]["4"]["started_at"], 100)
        self.assertEqual(freeze_timings({"created_at": "2020-01-01"}), {})


class ControlTests(unittest.TestCase):
    def test_phase_and_work_report_survive_heartbeat_but_not_another_stage_or_run(self):
        view = Dashboard()
        view.observe({"run_id": "first", "status": "repairing"})
        view.begin_operation()
        phase = {"kind": "phase", "at": 10, "message": "修正中", "detail": "自動修正 1/2回目"}
        report = {"kind": "activity", "at": 12, "message": "ファイルの編集"}
        view.activity(phase)
        view.activity(report)
        view.activity({"kind": "heartbeat", "at": 20, "message": "alive"})
        view.observe({"run_id": "first", "status": "repairing", "token_usage": [{"input": 10}]})
        self.assertEqual(view.state["phase_activity"], phase)
        self.assertEqual(view.state["latest_activity"], report)
        self.assertEqual(view.state["heartbeat"]["at"], 20)
        view.observe({"run_id": "first", "status": "building"})
        self.assertFalse({"phase_activity", "latest_activity", "heartbeat"} & view.state.keys())
        view.activity(phase)
        view.observe({"run_id": "second", "status": "building"})
        self.assertNotIn("phase_activity", view.state)
        view.activity(phase)
        view.begin_operation()
        self.assertNotIn("phase_activity", view.state)

    def test_stop_is_bound_to_operation_even_after_activity_revision_changes(self):
        view = Dashboard()
        view.publish({"status": "prepared", "ready": True})
        self.assertTrue(view.submit({"action": "resume-run", "revision": view.revision}))
        operation = view.state["operation_id"]
        revision = view.revision
        view.activity({"kind": "heartbeat", "message": "alive", "at": 1})
        self.assertTrue(view.request_stop({"operation_id": operation, "revision": revision}))
        self.assertTrue(view.control.cancelled.is_set())
        view.actions.get_nowait()
        view.ready({}, {"status": "prepared"})
        self.assertFalse(view.request_stop({"operation_id": operation}))
        view.begin_operation()
        self.assertNotIn("activity", view.state)
        self.assertFalse(view.request_stop({"operation_id": operation}))
        self.assertFalse(view.control.cancelled.is_set())

    def test_process_output_and_nonzero_exit_are_preserved(self):
        with control_scope(ExecutionControl(lambda _event: None)):
            result = run_process([sys.executable, "-c", "print('hello')"], label="test", capture_output=True, text=True)
            self.assertEqual(result.stdout.strip(), "hello")
            with self.assertRaises(subprocess.CalledProcessError) as failure:
                run_process([sys.executable, "-c", "import sys; print('reason', file=sys.stderr); sys.exit(7)"],
                            label="test", capture_output=True, check=True)
            self.assertEqual(failure.exception.returncode, 7)
            self.assertIn(b"reason", failure.exception.stderr)


class ExecutionIntegrationTests(HarnessCase):
    def setUp(self):
        super().setUp()
        (self.project / "source.php").write_text("<?php echo 42;\n", encoding="utf-8")
        self.commit_project()
        self.settings = self.config()

    def test_migration_and_verification_keep_counting_through_transitions(self):
        config = {**self.settings, "checker_command": ["checker"]}
        clock = 100
        updates = []
        run_command = workflow.run_process

        def observe(record):
            updates.append((record["status"], record.get("active_stage")))

        def migrate(_command, completion, token, targets, **_kwargs):
            nonlocal clock
            targets[0].write_text("class Page {}", encoding="utf-8")
            completion.write_text(token, encoding="utf-8")
            clock += 13
            return 0

        def execute(command, **_kwargs):
            nonlocal clock
            if command[0] == "git":
                return run_command(command, **_kwargs)
            clock += 7 if command[0] == "dotnet" else 11
            return subprocess.CompletedProcess(command, 0)

        observer = run_records.run_observer.set(observe)
        self.addCleanup(run_records.run_observer.reset, observer)
        with (
            patch.object(run_records, "update_timings",
                         side_effect=lambda record, **kwargs: update_timings(record, now=clock, **kwargs)),
            patch.object(workflow, "monotonic", side_effect=lambda: clock),
            patch.object(workflow.junie_cli, "require_interactive_cli"),
            patch.object(workflow.junie_cli, "run_interactive", side_effect=migrate),
            patch.object(workflow, "run_process", side_effect=execute),
        ):
            result = workflow.run_with_junie(config, "sample")

        record = json.loads((Path(result["run_path"]) / "run.json").read_bytes())
        self.assertEqual(record["status"], "checker_succeeded")
        self.assertIn(("junie_finished", 3), updates)
        self.assertIn(("junie_finished", 4), updates)
        self.assertIn(("build_succeeded", 5), updates)
        self.assertEqual(updates[-1], ("checker_succeeded", None))
        self.assertEqual([record["stage_timings"][str(i)]["elapsed_seconds"] for i in (3, 4, 5)], [13, 7, 11])
        self.assertEqual(record["build_attempt_seconds"], [7])

    def test_stop_during_capsule_preparation_does_not_create_worktree(self):
        control = ExecutionControl(lambda _event: control.cancelled.set())
        with control_scope(control), self.assertRaises(workflow.RunInterrupted):
            workflow.prepare_run(self.settings, "sample")
        self.assertFalse(Path(self.settings["worktree_root"]).exists())
        path, = Path(self.settings["run_root"]).glob("*/run.json")
        record = json.loads(path.read_bytes())
        self.assertEqual(record["status"], "preparation_interrupted")
        self.assertIsNone(record["active_stage"])

    def test_stopping_a_real_command_terminates_its_children(self):
        control = ExecutionControl(lambda _event: None)
        pid_file = self.root / "child.pid"
        marker = self.root / "child-wrote-after-stop"
        child_code = (
            "import os, signal, time; from pathlib import Path; "
            + ("signal.signal(signal.SIGTERM, signal.SIG_IGN); " if os.name != "nt" else "")
            + f"Path({str(pid_file)!r}).write_text(str(os.getpid())); "
            + f"time.sleep(2); Path({str(marker)!r}).write_text('still alive'); time.sleep(30)"
        )
        command = [sys.executable, "-c", f"import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', {child_code!r}]); time.sleep(30)"]
        observed = []

        def cancel():
            deadline = time.monotonic() + 5
            while not pid_file.exists() and time.monotonic() < deadline:
                time.sleep(.02)
            observed.append(pid_file.exists())
            control.cancelled.set()

        thread = Thread(target=cancel)
        thread.start()
        try:
            with control_scope(control), self.assertRaises(OperationCancelled):
                run_process(command, label="test", capture_output=True)
        finally:
            thread.join()
        self.assertEqual(observed, [True])
        time.sleep(2.1)
        self.assertFalse(marker.exists())

    def test_stopped_build_can_resume_same_run(self):
        result = workflow.prepare_run(self.settings, "sample")
        run_path = Path(result["run_path"])
        record_path = run_path / "run.json"
        record = json.loads(record_path.read_bytes())
        (Path(result["worktree_path"]) / "Page.cs").write_text("class Page {}", encoding="utf-8")
        record.update(status="junie_finished", completed_capsules=1)
        run_records.write_run_record(record_path, record)
        control = ExecutionControl(lambda _event: None)
        control.cancelled.set()
        with control_scope(control), self.assertRaises(workflow.RunInterrupted):
            workflow.resume_with_junie(self.settings, result["run_id"])
        stopped = json.loads(record_path.read_bytes())
        self.assertEqual(stopped["status"], "build_failed")
        self.assertEqual(stopped.get("repair_history", []), [])
        self.assertEqual(len(stopped["build_attempt_seconds"]), 1)
        with (
            patch.object(workflow, "monotonic", side_effect=(10, 12)),
            patch.object(workflow, "run_process", return_value=subprocess.CompletedProcess(["dotnet"], 0)),
        ):
            resumed = workflow.resume_with_junie(self.settings, result["run_id"])
        self.assertEqual(resumed["status"], "build_succeeded")
        self.assertEqual(resumed["run_id"], result["run_id"])
        saved = json.loads(record_path.read_bytes())
        self.assertEqual(saved["build_attempt_seconds"], [*stopped["build_attempt_seconds"], 2])
