import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


class EditorRunIsolationTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required for the editor behavior check")
    def test_same_file_in_multiple_runs_with_delayed_responses(self):
        root = Path(__file__).resolve().parent.parent
        source = (root / "src/migration_harness/dashboard.js").read_text(encoding="utf-8")
        with TemporaryDirectory() as directory:
            path = Path(directory) / "dashboard.js"
            for label, newline in (("LF", "\n"), ("CRLF", "\r\n")):
                with self.subTest(newline=label):
                    path.write_text(source, encoding="utf-8", newline=newline)
                    result = subprocess.run([
                        shutil.which("node"), str(root / "tests/editor_run_isolation.cjs"),
                        str(root / "src/migration_harness/dashboard_editor.js"), str(path),
                    ], capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
