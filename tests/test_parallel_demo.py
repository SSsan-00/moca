import shutil
import subprocess
import unittest
from pathlib import Path


class ParallelDemoTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required for the demo behavior check")
    def test_five_workers_fifo_resume_and_cancellation(self):
        root = Path(__file__).resolve().parent.parent
        subprocess.run([
            shutil.which("node"), str(root / "tests/parallel_demo.cjs"),
            str(root / "src/migration_harness/dashboard_runs_demo.js"),
        ], check=True, capture_output=True, text=True, timeout=10)
