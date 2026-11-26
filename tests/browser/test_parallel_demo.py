import subprocess
import unittest
from pathlib import Path


from tests.browser.ui_dependencies import node_executable


class ParallelDemoTests(unittest.TestCase):
    def test_five_workers_fifo_resume_and_cancellation(self):
        root = Path(__file__).resolve().parents[2]
        subprocess.run([
            node_executable(self), str(Path(__file__).with_name("parallel_demo.cjs")),
            str(root / "src/migration_harness/web/dashboard_runs_demo.js"),
        ], check=True, capture_output=True, text=True, timeout=10)
