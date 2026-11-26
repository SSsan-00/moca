import ctypes
import io
import re
import sys
import unittest
from contextlib import redirect_stdout
from ctypes import wintypes
from pathlib import Path

from browser_run import print_browser_output, run_browser
from ui_dependencies import node_executable


_WAIT_OBJECT_0 = 0
_SYNCHRONIZE = 0x00100000
_ERROR_INVALID_PARAMETER = 87
_CHILD = """import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
print("start stalled scenario; last completed: smoke", flush=True)
print(f"child={child.pid}", flush=True)
sys.stderr.buffer.write("partial browser stderr: 日本語".encode("utf-8"))
sys.stderr.buffer.flush()
"""


class BrowserRunnerTests(unittest.TestCase):
    def assert_owned_child_stopped(self, text):
        match = re.search(r"child=(\d+)", text)
        self.assertIsNotNone(match, text)
        pid = int(match[1])
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        proc = kernel.OpenProcess(_SYNCHRONIZE, False, pid)
        if not proc:
            self.assertEqual(ctypes.get_last_error(), _ERROR_INVALID_PARAMETER)
            return
        try:
            self.assertEqual(kernel.WaitForSingleObject(proc, 500), _WAIT_OBJECT_0)
        finally:
            kernel.CloseHandle(proc)

    def test_deadline_reports_both_partial_streams_and_reaps_the_child_tree(self):
        with self.assertRaisesRegex(AssertionError, "timed out after 3s") as failure:
            run_browser([sys.executable, "-c", _CHILD + "\ntime.sleep(600)"], timeout=3)
        message = str(failure.exception)
        self.assertIn("start stalled scenario; last completed: smoke", message)
        self.assertIn("partial browser stderr: 日本語", message)
        self.assert_owned_child_stopped(message)

    def test_failed_launcher_keeps_output_and_reaps_the_child_tree(self):
        result = run_browser([sys.executable, "-c", _CHILD + "\nsys.exit(4)"], timeout=5)
        self.assertEqual(result.returncode, 4)
        self.assertIn("start stalled scenario; last completed: smoke", result.stdout)
        self.assertIn("partial browser stderr: 日本語", result.stderr)
        self.assert_owned_child_stopped(result.stdout)

    def test_success_keeps_stdout(self):
        result = run_browser([sys.executable, "-c", "print('browser finished')"], timeout=5)
        self.assertEqual((result.returncode, result.stdout.strip(), result.stderr), (0, "browser finished", ""))

    def test_rule_draft_waits_and_diagnostics(self):
        script = Path(__file__).with_name("chromium_waits_test.mjs")
        result = run_browser([node_executable(self), "--test", str(script)], timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_progress_log_does_not_fail_on_cp1252(self):
        message = "[browser +2.00s] still waiting in editor: 診断の指摘なし\n"
        with io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict") as terminal, redirect_stdout(terminal):
            print_browser_output(message)
            text = terminal.buffer.getvalue().decode("cp1252").replace("\r\n", "\n")
        self.assertEqual(text, message.encode("cp1252", errors="backslashreplace").decode("cp1252"))
