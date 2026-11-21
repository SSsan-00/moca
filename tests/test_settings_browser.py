"""保存と再読込みをインストール済み Chromium の画面で確認する。"""
import io
import json
import os
import shutil
import subprocess
import tempfile
import tomllib
import unittest
import uuid
from contextlib import redirect_stdout
from pathlib import Path
from threading import Thread

from migration_harness import dashboard
from migration_harness.junie_notifications import RunNotice
from ui_dependencies import node_executable, require_ui_dependency


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
        node = node_executable(self)
        require_ui_dependency(self, chrome, "Chromium, Chrome, or Edge is required for the browser test")
        node_args = [node]
        probe = "process.exit(typeof WebSocket === 'function' ? 0 : 1)"
        if subprocess.run([node, "-e", probe], timeout=10).returncode:
            node_args.append("--experimental-websocket")
            supported = subprocess.run([*node_args, "-e", probe], capture_output=True, timeout=10).returncode == 0
            require_ui_dependency(self, supported, "WebSocket support in Node.js is required for the browser test")
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
[junie]
warn_input_bytes = 1000
max_input_bytes = 2000
[target_context]
roots = []
folders = ["Shared"]
exclude = ["bin"]
recursive = false
max_files = 42
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
            config = dashboard.load_config(path)
            run_id = str(uuid.uuid4())
            run = root / "runs" / run_id
            worktree = root / "trees" / run_id
            run.mkdir(parents=True)
            worktree.mkdir(parents=True)
            record = {
                "run_id": run_id, "unit_id": "例", "target": "Example.cs",
                "project_root": config["project_root"], "worktree_path": str(worktree.resolve()),
                "status": "script_failed", "script_phase": "before_migration",
                "completed_capsules": 1, "context_capsules": 3,
                "script_inflight": {"phase": "before_migration", "number": 2},
            }
            (run / "run.json").write_text(json.dumps(record), encoding="utf-8")
            (run / "editor.json").write_text(json.dumps({
                "edits": {}, "prompts": [{"id": "pending", "text": "fixture", "status": "queued"}],
            }), encoding="utf-8")
            view = dashboard.Dashboard()
            view.observe(record)
            view.ready(config, record)
            package = Path(dashboard.__file__).parent
            terminal = io.StringIO()
            with redirect_stdout(terminal):
                server = view.server(config, dashboard.render_dashboard_html(" data-live data-workspace"),
                                     (package / "dashboard.js").read_bytes())
            url = terminal.getvalue().splitlines()[0].removeprefix("Progress dashboard: ")
            token = "T" * 32
            server.junie_notifications.links[token] = RunNotice(run_id, "script_failed", "failed")
            notification_url = f"http://127.0.0.1:{server.server_port}/junie-open/{token}"
            worker = Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                helper = Path(__file__).with_name("settings_browser.mjs")
                result = subprocess.run([*node_args, str(helper), chrome, str(root / "browser"), url, notification_url, run_id],
                                        capture_output=True, text=True, encoding="utf-8", timeout=110)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                print(result.stdout, end="", flush=True)
            finally:
                server.shutdown()
                worker.join()
                server.server_close()
            saved = tomllib.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["units"][0]["target_context"], {
                "max_bytes": 9007199254740993,
            })
            self.assertEqual(saved["target_context"]["folders"], ["Shared", "Extra"])
            self.assertEqual(saved["target_context"]["exclude"], ["bin"])
            self.assertFalse(saved["target_context"]["recursive"])
            self.assertEqual(saved["target_context"]["max_files"], 42)
            self.assertEqual(saved["junie"]["warn_input_bytes"], 1000)
            self.assertEqual(saved["junie"]["max_input_bytes"], 2000)
            self.assertEqual(saved["diff_viewer"]["html"], "viewer.html")
            self.assertIn("# preserve this value", path.read_text(encoding="utf-8"))
