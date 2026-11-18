"""保存と再読込みをインストール済み Chromium の画面で確認する。"""
import io
import os
import shutil
import subprocess
import tempfile
import tomllib
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from threading import Thread

from migration_harness import dashboard


class SettingsBrowserTests(unittest.TestCase):
    def test_settings_round_trip_in_chromium(self):
        applications = [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            *(Path(base) / vendor / "Application" / executable
              for key in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")
              if (base := os.environ.get(key))
              for vendor, executable in (("Google/Chrome", "chrome.exe"), ("Microsoft/Edge", "msedge.exe"))),
        ]
        chrome = next((str(path) for path in applications if path.is_file()), None)
        chrome = chrome or shutil.which("google-chrome") or shutil.which("chromium")
        node = shutil.which("node")
        if not chrome or not node:
            self.skipTest("Chromium と Node.js が必要です。")
        node_args = [node]
        probe = "process.exit(typeof WebSocket === 'function' ? 0 : 1)"
        if subprocess.run([node, "-e", probe], timeout=10).returncode:
            node_args.append("--experimental-websocket")
            if subprocess.run([*node_args, "-e", probe], capture_output=True, timeout=10).returncode:
                self.skipTest("WebSocket 対応の Node.js が必要です。")
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=os.name == "nt") as temporary:
            root = Path(temporary)
            project = root / "app"
            project.mkdir()
            (project / ".git").mkdir()
            (project / "example.php").write_text("<?php echo 42;", encoding="utf-8")
            (project / "viewer.html").write_text("<!doctype html>", encoding="utf-8")
            (root / "anchors.json").write_text("{}", encoding="utf-8")
            (project / "Shared").mkdir()
            (project / "Shared/Helper.cs").write_bytes(b"class Helper {}")
            path = root / "harness.toml"
            path.write_text("""
[project]
root = "app"
worktree_root = "trees"
run_root = "runs"
[target_context]
roots = []
folders = ["Shared"]
[diff_viewer]
html = "viewer.html"
anchor_reference = ["anchors.json"]
[[units]]
id = "例"
source = "example.php"
target = "Example.cs"
[units.target_context]
max_bytes = 9007199254740993 # preserve this value
""", encoding="utf-8")
            view = dashboard.Dashboard()
            view.publish({"status": "idle", "ready": True})
            package = Path(dashboard.__file__).parent
            template = dashboard.harness_asset("progress-demo.html").read_text(encoding="utf-8").split("<!-- dashboard-script -->")[0]
            html = (template.replace('<html lang="ja">', '<html lang="ja" data-live data-workspace>')
                    .replace("./src/migration_harness/dashboard_runs.js", "dashboard_runs.js")
                    + '<script src="dashboard.js" defer></script><script src="dashboard_editor.js" defer></script></body></html>')
            terminal = io.StringIO()
            with redirect_stdout(terminal):
                server = view.server(dashboard.load_config(path), html.encode(), (package / "dashboard.js").read_bytes())
            url = terminal.getvalue().splitlines()[0].removeprefix("Progress dashboard: ")
            worker = Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                helper = Path(__file__).with_name("settings_browser.mjs")
                result = subprocess.run([*node_args, str(helper), chrome, str(root / "browser"), url],
                                        capture_output=True, text=True, encoding="utf-8", timeout=60)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            finally:
                server.shutdown()
                worker.join()
                server.server_close()
            saved = tomllib.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["units"][0]["target_context"], {
                "max_bytes": 9007199254740993, "max_files": 0, "roots": [],
            })
            self.assertEqual(saved["target_context"]["folders"], ["Shared"])
            self.assertEqual(saved["diff_viewer"]["html"], "viewer.html")
            self.assertIn("# preserve this value", path.read_text(encoding="utf-8"))
