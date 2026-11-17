import shutil
import subprocess
import unittest
from pathlib import Path


class EditorRunIsolationTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required for the editor behavior check")
    def test_same_file_in_multiple_runs_with_delayed_responses(self):
        root = Path(__file__).resolve().parent.parent
        result = subprocess.run([
            shutil.which("node"), str(root / "tests/editor_run_isolation.cjs"),
            str(root / "src/migration_harness/dashboard_editor.js"),
            str(root / "src/migration_harness/dashboard.js"),
        ], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
