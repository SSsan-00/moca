import ctypes
import subprocess
import sys
import tempfile
import time
import unittest
from ctypes import wintypes
from pathlib import Path
from unittest.mock import patch

from fixtures import SavedRunFixture
from migration_harness import workflow
from migration_harness.execution import ConfiguredProcessIncomplete, ExecutionControl, control_scope, run_process
from migration_harness.runs.recovery_status import recovery_status
from migration_harness.windows_job import WindowsJob


def launcher(*, waits, pid_path=None):
    child = ("import sys; print('child stdout'); print('child stderr', file=sys.stderr); sys.exit(7)"
             if waits else "import time; time.sleep(15)")
    code = f"import subprocess, sys; from pathlib import Path; child = subprocess.Popen([sys.executable, '-c', {child!r}]); "
    if pid_path is not None:
        code += f"Path({str(pid_path)!r}).write_text(str(child.pid), encoding='ascii'); "
    code += "sys.exit(child.wait())" if waits else "print('launcher returned')"
    return [sys.executable, "-c", code]


class ConfiguredProcessControlTests(unittest.TestCase):
    def test_job_accounting_failure_does_not_certify_parent_exit(self):
        with patch.object(WindowsJob, "active_processes", side_effect=OSError("fixture accounting unavailable")):
            with self.assertRaises(ConfiguredProcessIncomplete):
                run_process([sys.executable, "-c", "pass"], label="fixture", synchronous=True)

    def test_synchronous_child_output_and_nonzero_status_are_preserved_without_dashboard(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "hook.log"
            with log.open("wb") as stream:
                completed = run_process(launcher(waits=True), label="fixture", synchronous=True,
                                        stdout=stream, stderr=subprocess.STDOUT, check=False)
            self.assertEqual(completed.returncode, 7)
            self.assertCountEqual(log.read_bytes().splitlines(), [b"child stdout", b"child stderr"])

    def test_normal_launcher_return_reaps_only_its_own_remaining_child(self):
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        with tempfile.TemporaryDirectory() as directory:
            pid = Path(directory) / "child.pid"
            observed = None
            unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(15)"])

            def observe(_event):
                nonlocal observed
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if pid.is_file() and (value := pid.read_text(encoding="ascii")).isdigit():
                        observed = kernel.OpenProcess(0x00100000, False, int(value))
                        if observed:
                            return
                    time.sleep(.01)
                self.fail("fixture child was not observed")

            try:
                with ((Path(directory) / "hook.log").open("wb") as stream,
                      control_scope(ExecutionControl(observe)),
                      self.assertRaises(ConfiguredProcessIncomplete)):
                    run_process(launcher(waits=False, pid_path=pid), label="fixture", synchronous=True,
                                stdout=stream, stderr=subprocess.STDOUT)
                self.assertEqual(kernel.WaitForSingleObject(observed, 3000), 0)
                self.assertIsNone(unrelated.poll())
            finally:
                if observed:
                    kernel.CloseHandle(observed)
                if unrelated.poll() is None:
                    unrelated.terminate()
                unrelated.wait(timeout=5)


class ConfiguredProcessRunTests(SavedRunFixture):
    def assert_review_required(self, phase, log):
        record = self.record()
        self.assertEqual(record["unconfirmed_command"], {"phase": phase, "log": log.name})
        self.assertIn("新しい Run", record["error"])
        self.assertFalse(workflow.can_resume_run(self.config, record))
        self.assertFalse(workflow.can_resume_build(record))
        availability = recovery_status(self.config, self.run_id)
        self.assertTrue(availability["needs_process_check"])
        self.assertIsNone(availability["unknown_script"])
        self.assertEqual(availability["unconfirmed_command"], record["unconfirmed_command"])
        previous = log.read_bytes()
        with patch.object(workflow, "run_process") as command:
            actions = (
                lambda: workflow.resume_with_junie(self.config, self.run_id),
                lambda: workflow.script_recovery(self.config, self.run_id),
                *(lambda stage=stage: workflow.plan_stage_resume(self.config, self.run_id, stage)
                  for stage in ("migration", "build", "checker")),
            )
            for action in actions:
                with self.assertRaisesRegex(ValueError, "新しい Run"):
                    action()
            command.assert_not_called()
        self.assertEqual(log.read_bytes(), previous)
        self.assertFalse(workflow.migration_ledger(self.config, self.run_id)["completion"]["verification_passed"])

    def test_incomplete_once_hook_preserves_receipt_and_never_starts_verification(self):
        hook = launcher(waits=False)
        self.config["scripts"] = {"after_migration": [hook]}
        original = workflow.run_process

        def execute(command, **kwargs):
            self.assertEqual(command, hook)
            self.assertTrue(kwargs.get("synchronous"))
            return original(command, **kwargs)

        with patch.object(workflow, "run_process", side_effect=execute) as runner:
            with self.assertRaisesRegex(workflow.RunScriptError, "新しい Run"):
                workflow.resume_with_junie(self.config, self.run_id)
            runner.assert_called_once()
        record = self.record()
        self.assertEqual(record["status"], "script_failed")
        self.assertIn("script_inflight", record)
        self.assertNotIn("script_command_progress", record)
        log = self.run_path / "after_migration.log"
        self.assertIn(b"launcher returned", log.read_bytes())
        self.assert_review_required("after_migration", log)

    def test_incomplete_checker_does_not_certify_run_or_trigger_after_checker(self):
        checker = launcher(waits=False)
        self.config["checker_command"] = checker
        self.config["scripts"] = {"after_checker": [[sys.executable, "-c", "pass"]]}
        original = workflow.run_process

        def execute(command, **kwargs):
            if command[0] == "dotnet":
                self.assertFalse(kwargs.get("synchronous"))
                return subprocess.CompletedProcess(command, 0)
            self.assertEqual(command, checker)
            self.assertTrue(kwargs.get("synchronous"))
            return original(command, **kwargs)

        with patch.object(workflow, "run_process", side_effect=execute) as runner:
            completed = workflow.resume_with_junie(self.config, self.run_id)
            self.assertEqual(runner.call_count, 2)
        self.assertEqual(completed["status"], "checker_failed")
        self.assertIsNone(completed["checker_exit_code"])
        self.assertFalse(self.record()["verification"]["scripts_passed"])
        self.assertFalse((self.run_path / "after_checker.log").exists())
        log = self.run_path / "checker.log"
        self.assertEqual(log.read_bytes().splitlines(), [b"launcher returned"])
        self.assert_review_required("checker", log)
