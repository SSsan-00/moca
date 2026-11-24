import contextlib
import io
import sys
import unittest
from unittest.mock import patch

import test_before_build_versions as before_build
from migration_harness import workflow
from migration_harness.windows_job import WindowsJob


METHOD = "test_old_before_build_phase_receipt_is_not_treated_as_a_precommand_receipt"


if sys.version_info[:2] == (3, 11):
    class Issue206HostedWindowsProbe(unittest.TestCase):
        def test_early_hook_failures_preserve_original_diagnostics(self):
            causes = [
                (OSError("fixture launch unavailable"), None),
                (workflow.ConfiguredProcessIncomplete("fixture completion unconfirmed"),
                 {"phase": "before_build", "log": "before_build.log"}),
            ]
            for source_error, unconfirmed in causes:
                with self.subTest(cause=type(source_error).__name__):
                    fixture = before_build.BeforeBuildVersionTests(METHOD)
                    fixture.setUp()
                    try:
                        def fail_hook(command, **kwargs):
                            self.assertEqual(command[0], sys.executable)
                            self.assertTrue(kwargs["synchronous"])
                            kwargs["stdout"].write(b"fixture first hook output\n")
                            raise source_error

                        with patch.object(workflow, "run_process", side_effect=fail_hook) as hook:
                            with self.assertRaises(AssertionError) as failure:
                                getattr(fixture, METHOD)()
                        hook.assert_called_once()
                        self.assertIsInstance(failure.exception.__cause__, workflow.RunScriptError)
                        self.assertIs(failure.exception.__cause__.__context__, source_error)
                        self.assertIn(repr(source_error), str(failure.exception))
                        self.assertIn("run.json status: 'script_failed'", str(failure.exception))
                        self.assertIn("run.json error:", str(failure.exception))
                        self.assertIn(f"run.json unconfirmed_command: {unconfirmed!r}", str(failure.exception))
                        self.assertIn("fixture first hook output", str(failure.exception))
                        self.assertEqual(fixture.record().get("unconfirmed_command"), unconfirmed)
                        self.assertNotIn("verification_script_pending", fixture.record())
                    finally:
                        fixture.doCleanups()
            print("ISSUE 206: original launch and unconfirmed-completion diagnostics verified (2/2)")

        def test_repeated_real_synchronous_before_build(self):
            count = 30
            suite = unittest.TestSuite(before_build.BeforeBuildVersionTests(METHOD) for _ in range(count))
            membership = WindowsJob.has_processes_other_than
            stale_count = 0

            def compare(job, exited_pid):
                nonlocal stale_count
                active = job.active_processes()
                others = membership(job, exited_pid)
                if active and not others:
                    stale_count += 1
                return others

            with contextlib.redirect_stdout(io.StringIO()), patch.object(WindowsJob, "has_processes_other_than", compare):
                outcome = suite.run(unittest.TestResult())
            print(f"ISSUE 206: positive Job accounting without any other reported PID ({stale_count} occurrences)")
            self.assertEqual(outcome.testsRun, count)
            self.assertEqual(outcome.skipped, [])
            self.assertTrue(outcome.wasSuccessful(), "\n".join(detail for _, detail in outcome.errors + outcome.failures))
            print(f"ISSUE 206: real Windows/Python 3.11 before_build interruption verified ({count}/{count})")
