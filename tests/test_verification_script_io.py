import hashlib
import subprocess
import sys
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

from fixtures import SavedRunFixture
from migration_harness import workflow
from migration_harness import execution


PASSED = '{"verdict":"passed","evidence_lines":[1]}'


@unittest.skipUnless(sys.platform == "win32", "Windows file locks")
class VerificationScriptIoTests(SavedRunFixture):
    def configure_hook(self, phase="after_build"):
        self.config.update(junie_review_script_output=True, junie_apply_output_decisions=True)
        self.config["scripts"] = {phase: [[sys.executable, "-c",
            "from pathlib import Path; import sys; calls=Path('calls.txt'); first=not calls.exists(); "
            "calls.write_text((calls.read_text() if calls.exists() else '')+'x'); "
            "print('Unreconciled amount: first hook failed' if first else '1 test passed'); sys.exit(int(first))",
        ]]}
        self.counter = self.worktree / "calls.txt"
        self.log = self.run_path / f"{phase}.log"

    def resume_run(self):
        return workflow.resume_with_junie(self.config, self.run_id)

    def rebuild(self):
        record = workflow.plan_build_resume(self.config, self.run_id)
        return workflow.resume_with_junie(self.config, self.run_id, verification_request={**record, "id": "operator-build"})

    @staticmethod
    @contextmanager
    def deny_reads(path):
        import msvcrt

        with path.open("r+b") as locked:
            msvcrt.locking(locked.fileno(), msvcrt.LK_NBLCK, path.stat().st_size)
            yield

    @contextmanager
    def commands(self, *advisories, lock_after_hook=None):
        run = execution.run_process
        builds = []
        with ExitStack() as stack:
            def call(command, **kwargs):
                if command[0] == "dotnet":
                    builds.append(command)
                    return subprocess.CompletedProcess(command, 0)
                if command[0] == "fixture-checker":
                    return subprocess.CompletedProcess(command, 0)
                self.assertEqual(command[0], sys.executable)
                result = run(command, **kwargs)
                if lock_after_hook is not None:
                    stack.enter_context(self.deny_reads(lock_after_hook))
                return result

            stack.enter_context(patch.object(execution, "run_process", side_effect=call))
            judge = stack.enter_context(patch(
                "migration_harness.verification.checker_advisory.run_text_advisory", side_effect=advisories,
            ))
            yield builds, judge

    def interrupt_completed_hook_read(self, path):
        self.configure_hook()
        open_file = Path.open
        receipts_at_interrupt = []

        def interrupt_read(file, *args, **kwargs):
            stream = open_file(file, *args, **kwargs)
            if file == path and args == ("rb",) and self.counter.exists():
                read = stream.read

                def read_then_interrupt(size):
                    self.assertTrue(read(size))
                    receipts_at_interrupt.append(self.record().get("verification_script_pending"))
                    raise KeyboardInterrupt

                stream.read = read_then_interrupt
            return stream

        with self.commands() as (builds, judge), patch.object(Path, "open", interrupt_read):
            with self.assertRaises(workflow.RunScriptError) as error:
                self.resume_run()
            self.assertTrue(error.exception.interrupted)
            self.assertEqual(len(builds), 1)
            judge.assert_not_called()
        record = self.record()
        self.assertEqual(record["status"], "script_failed")
        pending = record["verification_script_pending"]
        self.assertEqual(receipts_at_interrupt, [pending])
        self.assertEqual(pending["exit_code"], 1)
        self.assertIsNone(pending["target_versions_match"])
        audit = record["script_advisories"][-1]
        self.assertEqual((audit["status"], audit["exit_code"]), ("interrupted", 1))
        raw = self.log.read_bytes()
        start = pending["byte_offset"]
        output = raw[start:start + pending["byte_length"]]
        self.assertEqual(output.strip(), b"Unreconciled amount: first hook failed")
        return pending, output

    def test_target_hash_interrupt_requires_explicit_rebuild_and_original_output(self):
        pending, output = self.interrupt_completed_hook_read(self.target)
        self.assertEqual(pending["log_sha256"], hashlib.sha256(output).hexdigest())
        self.assertEqual(pending["log_hash_scope"], "full_range")
        interrupted = self.record()["script_advisories"][-1]
        raw = self.log.read_bytes()
        with self.commands(PASSED) as (builds, judge):
            with self.assertRaisesRegex(workflow.RunScriptError, "no verified target version"):
                self.resume_run()
            self.log.write_bytes(raw.replace(b"Unreconciled", b"unreconciled"))
            with self.assertRaisesRegex(workflow.RunScriptError, "missing or changed"):
                self.rebuild()
            self.assertEqual((len(builds), judge.call_count, self.counter.read_text()), (0, 0, "x"))
            self.log.write_bytes(raw)
            self.assertEqual(self.rebuild()["status"], "build_succeeded")
            self.assertEqual((len(builds), judge.call_count, self.counter.read_text()), (1, 1, "xx"))
        self.assertTrue(self.log.read_bytes().startswith(raw))
        self.assertIn(interrupted, self.record()["script_advisories"])

    def test_log_hash_interrupt_disallows_replay_even_after_target_and_log_are_readable(self):
        pending, _ = self.interrupt_completed_hook_read(self.run_path / "after_build.log")
        self.assertIsNone(pending["log_sha256"])
        self.assertNotIn("log_hash_scope", pending)
        with self.commands() as (builds, judge):
            for resume in (self.resume_run, self.rebuild):
                with self.assertRaisesRegex(workflow.RunScriptError, "no verifiable full-range hash"):
                    resume()
            self.assertEqual((len(builds), judge.call_count, self.counter.read_text()), (0, 0, "x"))

    def test_after_hook_read_error_retains_output_for_verified_explicit_rebuild(self):
        self.configure_hook()
        original = self.target.read_bytes()
        with self.commands(lock_after_hook=self.target) as (builds, judge):
            with self.assertRaises(workflow.RunScriptError):
                self.resume_run()
            judge.assert_not_called()
            self.assertEqual(len(builds), 1)
        with self.commands(PASSED) as (builds, judge):
            with self.assertRaisesRegex(workflow.RunScriptError, "no verified target version"):
                self.resume_run()
            judge.assert_not_called()
            self.assertEqual(builds, [])
            self.assertEqual(self.counter.read_text(), "x")
            pending = self.record()["verification_script_pending"]
            previous = self.record()["script_advisories"][-1]
            self.assertEqual(pending["exit_code"], 1)
            self.assertIsNone(pending["target_versions_match"])
            original_log = self.log.read_bytes()
            start = pending["byte_offset"]
            output = original_log[start:start + pending["byte_length"]]
            self.assertEqual(output.strip(), b"Unreconciled amount: first hook failed")
            self.assertEqual(pending["log_sha256"], hashlib.sha256(output).hexdigest())
            self.assertEqual(pending["log_hash_scope"], "full_range")
            self.assertEqual(previous["reason"], "target_unreadable")
            self.assertEqual(self.rebuild()["status"], "build_succeeded")
            self.assertEqual((len(builds), judge.call_count), (1, 1))
        self.assertEqual(self.target.read_bytes(), original)
        self.assertEqual(self.counter.read_text(), "xx")
        self.assertTrue(self.log.read_bytes().startswith(original_log))
        self.assertIn(previous, self.record()["script_advisories"])

    def test_both_resume_modes_reject_missing_and_modified_log_before_new_commands(self):
        self.configure_hook()
        with self.commands(lock_after_hook=self.target):
            with self.assertRaises(workflow.RunScriptError):
                self.resume_run()
        initial_log = self.log.read_bytes()
        for log_problem in ("missing", "changed"):
            with self.subTest(log_problem=log_problem):
                if log_problem == "missing":
                    self.log.unlink()
                else:
                    self.log.write_bytes(initial_log.replace(b"Unreconciled", b"unreconciled"))
                with self.commands(PASSED) as (builds, judge):
                    for resume in (self.resume_run, self.rebuild):
                        with self.assertRaisesRegex(workflow.RunScriptError, "missing or changed"):
                            resume()
                    judge.assert_not_called()
                    self.assertEqual(builds, [])
                self.assertEqual(self.counter.read_text(), "x")

    def test_read_error_before_hook_does_not_create_a_completed_execution_receipt(self):
        self.configure_hook("before_build")
        with self.deny_reads(self.target), self.commands() as (builds, judge):
            with self.assertRaises(workflow.RunScriptError):
                self.resume_run()
            judge.assert_not_called()
            self.assertEqual(builds, [])
        self.assertFalse(self.counter.exists())
        self.assertEqual(self.record()["status"], "script_failed")
        self.assertNotIn("verification_script_pending", self.record())
        self.assertNotIn("script_advisories", self.record())

    def test_missing_log_hash_cannot_authorize_replay_even_when_reading_recovers(self):
        self.configure_hook()
        with self.commands(lock_after_hook=self.log) as (builds, judge):
            with self.assertRaises(workflow.RunScriptError):
                self.resume_run()
            judge.assert_not_called()
            self.assertEqual(len(builds), 1)
        record = self.record()
        self.assertEqual(record["status"], "script_failed")
        pending = record["verification_script_pending"]
        self.assertEqual(pending["exit_code"], 1)
        self.assertGreater(pending["byte_length"], 0)
        self.assertIsNone(pending["log_sha256"])
        self.assertEqual(record["script_advisories"][-1]["reason"], "output_unverified")
        with self.commands() as (builds, judge):
            for resume in (self.resume_run, self.rebuild):
                with self.assertRaisesRegex(workflow.RunScriptError, "no verifiable full-range hash"):
                    resume()
            judge.assert_not_called()
            self.assertEqual(builds, [])
        self.assertEqual(self.counter.read_text(), "x")
