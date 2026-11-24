import hashlib
import subprocess
import sys
from contextlib import ExitStack, contextmanager
from unittest.mock import patch

from fixtures import SavedRunFixture
from migration_harness import workflow


PASSED = '{"verdict":"passed","evidence_lines":[1]}'


class OnceScriptReceiptTests(SavedRunFixture):
    def configure_hook(self, phase="after_migration", output="1 test passed"):
        self.config.update(junie_review_script_output=True, junie_apply_output_decisions=True)
        self.phase = phase
        self.counter = self.worktree / "calls.txt"
        self.log = self.run_path / (phase + ".log")
        self.config["scripts"] = {phase: [[sys.executable, "-c",
            "from pathlib import Path; calls=Path('calls.txt'); "
            "calls.write_text((calls.read_text() if calls.exists() else '')+'x'); "
            + (f"print({output!r})" if output is not None else "pass"),
        ]]}

    @contextmanager
    def commands(self, *advisories, lock_output=False):
        run = workflow.run_process
        verifications = []
        with ExitStack() as stack:
            def child(command, **kwargs):
                if command[0] in {"dotnet", "fixture-checker"}:
                    verifications.append(command[0])
                    return subprocess.CompletedProcess(command, 0)
                self.assertEqual(command[0], sys.executable)
                process = run(command, **kwargs)
                if lock_output:
                    import msvcrt
                    locked = stack.enter_context(self.log.open("r+b"))
                    msvcrt.locking(locked.fileno(), msvcrt.LK_NBLCK, self.log.stat().st_size)
                return process

            stack.enter_context(patch.object(workflow, "run_process", side_effect=child))
            judge = stack.enter_context(patch(
                "migration_harness.verification.checker_advisory.run_text_advisory", side_effect=advisories,
            ))
            yield verifications, judge

    def resume(self):
        return workflow.resume_with_junie(self.config, self.run_id)

    def run_phase(self):
        workflow._run_script_phase(self.config, self.phase, self.worktree, self.run_path, self.record(), once=True)

    def pending_output(self):
        record = self.record()
        self.assertEqual(record["status"], "script_failed")
        self.assertNotIn("script_inflight", record)
        pending = record["script_decisions_pending"][self.phase]
        raw = self.log.read_bytes()
        start = pending["byte_offset"]
        return pending, raw[start:start + pending["byte_length"]]

    def test_interrupted_hash_keeps_original_pending_and_live_resume_does_not_skip(self):
        output = "URGENT migration is incomplete, operator review required"
        self.configure_hook(output=output)
        at_interruption = []

        def interrupt(run_path, item):
            at_interruption.append(self.record().get("script_decisions_pending"))
            raise KeyboardInterrupt

        with self.commands() as (commands, judge):
            with patch.object(workflow, "_script_output_sha256", side_effect=interrupt):
                with self.assertRaises(workflow.RunScriptError) as error:
                    self.resume()
            self.assertTrue(error.exception.interrupted)
            with self.assertRaisesRegex(workflow.RunScriptError, "no verifiable full-range hash"):
                self.resume()
            self.assertEqual(commands, [])
            judge.assert_not_called()
        pending, raw = self.pending_output()
        self.assertEqual(at_interruption, [{self.phase: pending}])
        self.assertEqual(raw.strip(), output.encode("ascii"))
        self.assertEqual(pending["exit_code"], 0)
        self.assertIsNone(pending["log_sha256"])
        self.assertIsNone(pending["target_versions"])
        self.assertEqual(self.counter.read_text(), "x")
        with patch.dict(self.config, scripts={}):
            with self.assertRaisesRegex(ValueError, "Resolve the saved one-time script response"):
                workflow.plan_checker_resume(self.config, self.run_id)

    def test_unreadable_log_then_readable_does_not_allow_review_or_skip(self):
        self.configure_hook()
        with self.commands(lock_output=True) as (commands, judge):
            with self.assertRaisesRegex(workflow.RunScriptError, "missing or changed"):
                self.resume()
            self.assertEqual(commands, [])
            judge.assert_not_called()
        pending, raw = self.pending_output()
        self.assertIsNone(pending["log_sha256"])
        self.assertEqual(raw.strip(), b"1 test passed")
        with self.commands() as (commands, judge):
            with self.assertRaisesRegex(workflow.RunScriptError, "no verifiable full-range hash"):
                self.resume()
            self.assertEqual(commands, [])
            judge.assert_not_called()
        self.assertEqual(self.counter.read_text(), "x")

    def test_interrupted_target_snapshot_is_not_recovered_by_hashing_later(self):
        self.configure_hook()
        with self.commands() as (commands, judge):
            with patch.object(workflow, "_script_decision_versions", side_effect=KeyboardInterrupt):
                with self.assertRaises(workflow.RunScriptError) as error:
                    self.resume()
            self.assertTrue(error.exception.interrupted)
            with self.assertRaisesRegex(workflow.RunScriptError, "no verified target version"):
                self.resume()
            self.assertEqual(commands, [])
            judge.assert_not_called()
        pending, raw = self.pending_output()
        self.assertEqual(pending["log_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertIsNone(pending["target_versions"])
        self.assertEqual(self.counter.read_text(), "x")

    def test_after_prepare_and_before_migration_share_conservative_receipt(self):
        for phase in ("after_prepare", "before_migration"):
            with self.subTest(phase=phase):
                self.configure_hook(phase)
                with self.commands() as (commands, judge):
                    with patch.object(workflow, "_script_output_sha256", side_effect=KeyboardInterrupt):
                        with self.assertRaises(workflow.RunScriptError):
                            self.run_phase()
                    with self.assertRaisesRegex(workflow.RunScriptError, "no verifiable full-range hash"):
                        self.run_phase()
                    self.assertEqual(commands, [])
                    judge.assert_not_called()
                self.assertEqual(self.pending_output()[1].strip(), b"1 test passed")
        self.assertEqual(self.counter.read_text(), "xx")

    def test_verified_interrupted_review_checks_original_bytes_before_model(self):
        self.configure_hook()
        with self.commands(KeyboardInterrupt, PASSED) as (commands, judge):
            with self.assertRaises(workflow.RunScriptError):
                self.resume()
            pending, raw = self.pending_output()
            self.assertEqual(pending["log_sha256"], hashlib.sha256(raw).hexdigest())
            self.assertIsNotNone(pending["target_versions"])
            original = self.log.read_bytes()
            self.log.write_bytes(original.replace(b"1 test passed", b"2 test passed"))
            with self.assertRaisesRegex(workflow.RunScriptError, "missing or changed"):
                self.resume()
            self.assertEqual(commands, [])
            self.assertEqual(judge.call_count, 1)
            self.log.write_bytes(original)
            self.assertEqual(self.resume()["status"], "checker_succeeded")
            self.assertEqual(commands, ["dotnet", "fixture-checker"])
            self.assertEqual(judge.call_count, 2)
        self.assertEqual(self.log.read_bytes(), original)
        self.assertEqual(self.counter.read_text(), "x")

    def test_silent_success_does_not_require_review_or_repeat(self):
        self.configure_hook(output=None)
        with self.commands() as (commands, judge):
            for _ in range(2):
                self.assertEqual(self.resume()["status"], "checker_succeeded")
            self.assertEqual(commands, ["dotnet", "fixture-checker"] * 2)
            judge.assert_not_called()
        self.assertEqual(self.counter.read_text(), "x")
        self.assertFalse(self.record().get("script_decisions_pending"))
