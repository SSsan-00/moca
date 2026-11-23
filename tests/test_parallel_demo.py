import subprocess
import unittest
from pathlib import Path


from ui_dependencies import node_executable


class ParallelDemoTests(unittest.TestCase):
    def test_five_workers_fifo_resume_and_cancellation(self):
        root = Path(__file__).resolve().parent.parent
        subprocess.run([
            node_executable(self), str(root / "tests/parallel_demo.cjs"),
            str(root / "src/migration_harness/web/dashboard_runs_demo.js"),
        ], check=True, capture_output=True, text=True, timeout=10)
