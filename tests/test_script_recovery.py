import sys
from unittest.mock import patch

from fixtures import SavedRunFixture
from migration_harness import workflow
from migration_harness.runs.editor_queue import editor_queue
from migration_harness.runs.recovery_status import recovery_status


class SimulatedCrash(BaseException):
    pass


class ScriptRecoveryTests(SavedRunFixture):
    def setUp(self):
        super().setUp()
        self.phase = "before_migration"
        self.counter = self.worktree / "counter"
        self.marker = self.worktree / "next"
        self.command = [sys.executable, "-c",
                        "from pathlib import Path; p=Path('counter'); "
                        "p.write_text(str(int(p.read_text())+1) if p.exists() else '1')"]
        self.next_command = [sys.executable, "-c", "from pathlib import Path; Path('next').touch()"]
        self.config["scripts"] = {self.phase: [self.command, self.next_command]}
        self.save_record({**self.record(), "status": "prepared", "completed_capsules": 0})

    def run_phase(self):
        workflow._run_script_phase(self.config, self.phase, self.worktree, self.run_path, self.record(), once=True)

    def simulate_crash_after_exit(self):
        original = workflow.write_run_record

        def fail_before_commit(path, record, **kwargs):
            if record.get("script_command_progress"):
                raise SimulatedCrash
            return original(path, record, **kwargs)

        with patch.object(workflow, "write_run_record", side_effect=fail_before_commit):
            with self.assertRaises(SimulatedCrash):
                self.run_phase()

    def test_crash_after_success_stops_all_resume_paths_and_requires_current_explicit_retry(self):
        self.simulate_crash_after_exit()
        self.assertEqual(self.counter.read_text(), "1")
        self.assertEqual(self.record()["script_inflight"]["number"], 1)
        with self.assertRaisesRegex(ValueError, "実行結果不明"):
            self.run_phase()
        with self.assertRaisesRegex(ValueError, "実行結果不明"):
            workflow.resume_with_junie(self.config, self.run_id)
        for stage in ("build", "migration", "checker"):
            with self.assertRaisesRegex(ValueError, "実行結果不明"):
                workflow.plan_stage_resume(self.config, self.run_id, stage)
        self.assertFalse(workflow.can_resume_run(self.config, self.record()))
        plan = workflow.script_recovery(self.config, self.run_id)
        self.assertTrue(plan["can_retry"])
        self.assertEqual(self.counter.read_text(), "1")
        self.target.write_text("class Modified {}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "対象版が変わりました"):
            workflow.script_recovery(self.config, self.run_id, retry_version=plan["version"])
        plan = workflow.script_recovery(self.config, self.run_id)
        original = self.config["scripts"][self.phase][0]
        self.config["scripts"][self.phase][0] = self.next_command
        self.assertFalse(workflow.script_recovery(self.config, self.run_id)["can_retry"])
        with self.assertRaisesRegex(ValueError, "コマンド"):
            workflow.script_recovery(self.config, self.run_id, retry_version=plan["version"])
        self.config["scripts"][self.phase][0] = original
        outcome = workflow.script_recovery(self.config, self.run_id, retry_version=plan["version"])
        self.assertTrue(outcome["retried"])
        self.assertEqual(self.counter.read_text(), "2")
        self.assertFalse(self.marker.exists())
        self.assertNotIn("script_inflight", self.record())
        with self.assertRaisesRegex(ValueError, "ありません"):
            workflow.script_recovery(self.config, self.run_id, retry_version=plan["version"])
        self.run_phase()
        self.assertTrue(self.marker.exists())
        self.assertEqual(self.counter.read_text(), "2")

    def test_launch_is_not_attempted_if_start_record_cannot_be_written(self):
        original = workflow.write_run_record

        def fail_attempt(path, record, **kwargs):
            if record.get("script_inflight"):
                raise OSError("write failed")
            return original(path, record, **kwargs)

        with (patch.object(workflow, "write_run_record", side_effect=fail_attempt),
              patch.object(workflow, "run_process") as process):
            with self.assertRaisesRegex(OSError, "write failed"):
                self.run_phase()
            process.assert_not_called()
        self.assertFalse(self.counter.exists())
        self.assertNotIn("script_inflight", self.record())

    def test_known_nonzero_exit_and_clean_success_do_not_create_an_unknown_outcome(self):
        self.config["scripts"][self.phase] = [[sys.executable, "-c", "raise SystemExit(3)"]]
        with self.assertRaises(workflow.RunScriptError):
            self.run_phase()
        self.assertNotIn("script_inflight", self.record())
        self.config["scripts"][self.phase] = [self.command]
        self.run_phase()
        self.run_phase()
        self.assertEqual(self.counter.read_text(), "1")

    def test_stop_after_start_is_reported_as_unknown_and_preserves_pending_operations(self):
        with patch.object(workflow, "run_process", side_effect=KeyboardInterrupt):
            with self.assertRaises(workflow.RunScriptError) as failure:
                self.run_phase()
        self.assertTrue(failure.exception.interrupted)
        with editor_queue(self.run_path) as queue:
            queue["edits"][self.target.name] = {"status": "conflict", "content": "private draft"}
            queue["prompts"] = [{"status": "queued", "text": "private instruction"}, {"status": "sent"}]
        data = recovery_status(self.config, self.run_id, blocked=True)
        self.assertEqual(data["unknown_script"]["log"], self.phase + ".log")
        self.assertTrue(data["available"] and data["needs_process_check"] and data["conflict"])
        self.assertEqual((data["completed"], data["pending_edits"], data["pending_prompts"]), (0, 1, 1))
        self.assertNotIn("private", repr(data))

    def test_legacy_missing_progress_is_not_zero_or_success(self):
        record = self.record()
        record.pop("completed_capsules")
        record.pop("context_capsules")
        self.save_record({**record, "status": "interrupted"})
        data = recovery_status(self.config, self.run_id)
        self.assertTrue(data["available"])
        self.assertIsNone(data["completed"])
        self.assertIsNone(data["total"])
        self.assertIsNone(data["unknown_script"])
