import os
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from migration_harness.verification import roslyn_editor_worker as worker


class EditorSemanticWorkerTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        helper = Path(folder.name) / "synthetic_worker.py"
        helper.write_text(
            "import os, sys, time\n"
            "for line in sys.stdin.buffer:\n"
            "    if line == b'crash\\n': sys.exit(3)\n"
            "    if line == b'hang\\n': time.sleep(30)\n"
            "    if line == b'long\\n': sys.stdout.buffer.write(b'x' * (128 * 1024 + 1))\n"
            "    else: sys.stdout.buffer.write(str(os.getpid()).encode() + b':' + line)\n"
            "    sys.stdout.buffer.flush()\n",
            encoding="utf-8",
        )
        self.pool = worker.EditorSemanticWorker()
        self.addCleanup(self.pool.close)
        self.command = [getattr(sys, "_base_executable", sys.executable), "-u", str(helper)]

    def run_worker(self, payload, timeout=4):
        return self.pool.run(self.command, payload, timeout, environment=os.environ.copy())

    def test_reuses_process_without_reusing_request_and_retires_after_limit(self):
        values = (b'{"text":"first\\r\\nsecond"}', '{"text":"新規"}'.encode())
        with patch.object(worker, "_MAX_REQUESTS", 2):
            first = self.run_worker(values[0])
            child = self.pool._helper.process
            second = self.run_worker(values[1])
        self.assertEqual(first.stdout, f"{child.pid}:".encode() + values[0] + b"\n")
        self.assertEqual(second.stdout, f"{child.pid}:".encode() + values[1] + b"\n")
        self.assertIsNotNone(child.poll())
        third = self.run_worker(b"fresh")
        self.assertEqual(third.stdout.split(b":", 1)[1], b"fresh\n")
        self.assertIsNot(self.pool._helper.process, child)

    def test_crash_malformed_limit_and_timeout_are_not_reused(self):
        for payload in (b"crash", b"long", b"hang"):
            with self.subTest(payload=payload):
                self.assertFalse(self.run_worker(b"warm").returncode)
                child = self.pool._helper.process
                if payload == b"hang":
                    with self.assertRaises(subprocess.TimeoutExpired):
                        self.run_worker(payload, timeout=0.1)
                else:
                    self.assertNotEqual(self.run_worker(payload).returncode, 0)
                self.assertIsNotNone(child.poll())
                self.assertIsNone(self.pool._helper)
                self.assertEqual(self.run_worker(b"new").stdout.split(b":", 1)[1], b"new\n")

    def test_idle_expiry_and_explicit_close_leave_no_running_helper(self):
        with patch.object(worker, "_IDLE_SECONDS", 0.1):
            self.assertFalse(self.run_worker(b"idle").returncode)
            child = self.pool._helper.process
        child.wait(timeout=4)
        self.assertIsNotNone(child.poll())
        self.assertFalse(self.run_worker(b"active").returncode)
        child = self.pool._helper.process
        self.pool.close()
        self.assertIsNotNone(child.poll())
        with self.assertRaises(OSError):
            self.run_worker(b"closed")

    def test_close_aborts_active_request_and_concurrent_call_cannot_steal_reply(self):
        self.assertFalse(self.run_worker(b"warm").returncode)
        child = self.pool._helper.process
        observed = threading.Event()
        submit = worker._Process.submit

        def observe(helper, payload):
            result = submit(helper, payload)
            observed.set()
            return result

        with patch.object(worker._Process, "submit", observe), ThreadPoolExecutor(1) as pool:
            pending = pool.submit(self.run_worker, b"hang")
            self.assertTrue(observed.wait(3))
            with self.assertRaises(OSError):
                self.run_worker(b"must not enqueue")
            self.pool.close()
            self.assertNotEqual(pending.result(timeout=3).returncode, 0)
        self.assertIsNotNone(child.poll())
