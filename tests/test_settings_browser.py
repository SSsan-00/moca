"""保存と再読込みをインストール済み Chromium の画面で確認する。"""
import io
import json
import os
import subprocess
import tempfile
import tomllib
import unittest
import uuid
from contextlib import redirect_stdout
from pathlib import Path
from threading import Thread
from unittest.mock import patch
from urllib.parse import urlsplit

from browser_run import run_browser

from migration_harness import workflow
from migration_harness.runs import editor_queue as editor_queue_store, reference_refresh, target_snapshots
from migration_harness.config import target_paths
from migration_harness.context.source_context import stage_target_context
from migration_harness.junie.junie_conversation import saved_edits, target_versions
from migration_harness.runs.editor_queue import editor_queue
from migration_harness.web import dashboard
from migration_harness.junie.junie_notifications import RunNotice
from migration_harness.junie.junie_thoughts import FILENAME as JUNIE_LOG_NAME
from ui_dependencies import node_executable, require_ui_dependency
from stopped_repair_browser_fixture import resume_after_external_edit, resume_with_blocked_keyword, stopped_repair_run


class SettingsBrowserTests(unittest.TestCase):
    def test_settings_round_trip_in_chromium(self):
        applications = [
            *(Path(base) / vendor / "Application" / executable
              for key in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")
              if (base := os.environ.get(key))
              for vendor, executable in (("Google/Chrome", "chrome.exe"), ("Microsoft/Edge", "msedge.exe"))),
        ]
        chrome = next((str(path) for path in applications if path.is_file()), None)
        node = node_executable(self)
        require_ui_dependency(self, chrome, "Chrome or Edge is required for the Windows browser test")
        node_args = [node]
        probe = "process.exit(typeof WebSocket === 'function' ? 0 : 1)"
        if subprocess.run([node, "-e", probe], timeout=10).returncode:
            node_args.append("--experimental-websocket")
            supported = subprocess.run([*node_args, "-e", probe], capture_output=True, timeout=10).returncode == 0
            require_ui_dependency(self, supported, "WebSocket support in Node.js is required for the browser test")
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
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
targets = ["Example.cs", "Pages/Index.cshtml", "Pages/Index.cshtml.cs", "wwwroot/Index.cshtml", "wwwroot/Index.cshtml.cs"]
[units.target_context]
max_bytes = 9007199254740993 # preserve this value
""", encoding="utf-8")
            config = dashboard.load_config(path)
            run_id = str(uuid.uuid4())
            run = root / "runs" / run_id
            worktree = root / "trees" / run_id
            run.mkdir(parents=True)
            worktree.mkdir(parents=True)
            (worktree / "App.csproj").write_text(
                '<Project Sdk="Microsoft.NET.Sdk.Web"><PropertyGroup><TargetFramework>net9.0</TargetFramework>'
                '<ImplicitUsings>enable</ImplicitUsings></PropertyGroup></Project>', encoding="utf-8",
            )
            (worktree / "Example.cs").write_bytes(b"public class Example { public int Number => new Sample.IndexModel().Number; }")
            (worktree / "Pages").mkdir()
            (worktree / "wwwroot").mkdir()
            (worktree / "wwwroot/Index.cshtml").write_bytes(b"@model Sample.IndexModel\n<p>@Model.Number</p>\n")
            (worktree / "wwwroot/Index.cshtml.cs").write_bytes(
                b"namespace Sample; public class IndexModel { public int Missing => 42; }\n",
            )
            (worktree / "_ViewImports.cshtml").write_bytes(b"@using Sample\n")
            (worktree / "Pages/Index.cshtml").write_bytes(b"@page\n@model IndexModel\n<p>@Model.Number</p>\n")
            (worktree / "Pages/Index.cshtml.cs").write_bytes(
                b"namespace Sample; public class IndexModel { public int Number => 42; }\n",
            )
            record = {
                "run_id": run_id, "unit_id": "例", "target": "Example.cs",
                "targets": ["Example.cs", "Pages/Index.cshtml", "Pages/Index.cshtml.cs", "wwwroot/Index.cshtml", "wwwroot/Index.cshtml.cs"],
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
            fixture_root = urlsplit(url).path
            other_run_id = str(uuid.uuid4())
            other_run = run.parent / other_run_id
            other_tree = worktree.parent / other_run_id
            post = server.RequestHandlerClass.do_POST
            retained_dependency = None

            def allow_fixture_resume(handler):
                nonlocal retained_dependency
                if handler.path == fixture_root + "fixture-editor-queue":
                    view.publish({**view.state, "ready": False})
                elif handler.path == fixture_root + "fixture-editor-adopt":
                    unit = config["units"][0]
                    with editor_queue(run) as queue:
                        request = {"edits": saved_edits(queue), "edit_revision": queue["edit_revision"],
                                   "targets": target_versions(worktree, unit)}
                    workflow._apply_verification_edits(run, worktree, unit, request)
                    view.publish({**view.state, "ready": True})
                elif handler.path == fixture_root + "fixture-editor-merge":
                    record_path = run / "run.json"
                    saved = json.loads(record_path.read_bytes())
                    unit = config["units"][0]
                    targets = target_paths(unit)
                    saved["target_hashes"] = target_versions(worktree, unit)
                    with editor_queue(run, recover=(worktree, targets)) as queue:
                        adopted = editor_queue_store.apply_editor_edits(
                            queue, run / "agent-workspace", worktree, targets, saved, record_path,
                        )
                    view.publish({**view.state, "ready": True})
                    handler.send_body(200 if adopted else 409, "application/json", b"{}")
                    return
                elif handler.path in {fixture_root + "fixture-preserve-dependency-stamp",
                                      fixture_root + "fixture-restore-dependency-stamp"}:
                    dependency = worktree / "Pages/Index.cshtml.cs"
                    if handler.path.endswith("preserve-dependency-stamp"):
                        original = dependency.read_bytes()
                        retained_dependency = (original, dependency.stat())
                        source = original.replace(b"Number", b"Rename")
                    else:
                        source = retained_dependency[0]
                    stamp = retained_dependency[1]
                    dependency.write_bytes(source)
                    os.utime(dependency, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
                    current = dependency.stat()
                    self.assertEqual((current.st_size, current.st_mtime_ns), (stamp.st_size, stamp.st_mtime_ns))
                elif handler.path == fixture_root + "fixture-editor-pin-reference":
                    record_path = run / "run.json"
                    saved = json.loads(record_path.read_bytes())
                    unit = config["units"][0]
                    targets = target_paths(unit)
                    capsule = run / "capsule"
                    agent = run / "agent-workspace"
                    reference = worktree / "refs" / "Library.cs"
                    reference.parent.mkdir()
                    reference.write_bytes(b"reference A")
                    stage_target_context(worktree, targets, capsule, agent, policy={"roots": [], "folders": ["refs"]})
                    (capsule / "manifest.json").write_text('{"parts": [{"index": 1}]}', encoding="utf-8")
                    saved["target_hashes"] = target_versions(worktree, unit)
                    saved["capsule_checkpoints"] = {"1": target_snapshots.save_capsule_checkpoint(
                        run, worktree, targets, saved["target_hashes"],
                    )}
                    record_path.write_text(json.dumps(saved), encoding="utf-8")
                    with editor_queue(run) as queue:
                        queue["operator_edits"].clear()
                    reference.write_bytes(b"reference B")
                elif handler.path == fixture_root + "fixture-editor-refresh-reference":
                    saved = json.loads((run / "run.json").read_bytes())
                    proposal = run / "reference-proposal-fixture"
                    with patch.object(reference_refresh, "_propose_reference_refresh", return_value=proposal):
                        reference_refresh.refresh_target_context_on_resume(
                            config, config["units"][0], run, worktree, saved, migration_complete=True,
                        )
                    view.observe(saved)
                    view.ready(config, saved)
                elif handler.path == fixture_root + "fixture-editor-switch":
                    if view.state["run_id"] == run_id:
                        other_run.mkdir()
                        other_tree.mkdir()
                        (other_tree / "Example.cs").write_bytes(b"class OtherRun {}\n")
                        selected = {**record, "run_id": other_run_id, "worktree_path": str(other_tree.resolve())}
                        (other_run / "run.json").write_text(json.dumps(selected), encoding="utf-8")
                    else:
                        selected = json.loads((run / "run.json").read_bytes())
                    view.observe(selected)
                    view.ready(config, selected)
                elif handler.path in {fixture_root + "fixture-stopped-repair-build", fixture_root + "fixture-stopped-repair-checker"}:
                    phase = handler.path.rsplit("-", 1)[-1]
                    selected = stopped_repair_run(config, record, worktree, phase)
                    view.observe(selected)
                    view.ready(config, selected)
                    handler.send_body(200, "application/json", json.dumps({"run_id": selected["run_id"]}).encode())
                    return
                elif handler.path == fixture_root + "fixture-stopped-repair-stale":
                    selected = resume_after_external_edit(config, view.state["run_id"])
                    view.observe(selected)
                    view.ready(config, selected, message=selected["error"])
                elif handler.path == fixture_root + "fixture-stopped-repair-keyword":
                    selected = resume_with_blocked_keyword(config, view.state["run_id"])
                    view.observe(selected)
                    view.ready(config, selected, message=selected["error"])
                elif handler.path == fixture_root + "fixture-stopped-repair-reset":
                    selected = json.loads((run / "run.json").read_bytes())
                    view.observe(selected)
                    view.ready(config, selected)
                elif handler.path == fixture_root + "fixture-resumable":
                    record_path = run / "run.json"
                    saved = json.loads(record_path.read_bytes())
                    saved.pop("script_inflight", None)
                    record_path.write_text(json.dumps(saved), encoding="utf-8")
                    view.observe(saved)
                    view.ready(config, saved)
                else:
                    return post(handler)
                handler.send_body(200, "application/json", b"{}")

            server.RequestHandlerClass.do_POST = allow_fixture_resume
            token = "T" * 32
            server.junie_notifications.links[token] = RunNotice(run_id, "script_failed", "failed")
            notification_url = f"http://127.0.0.1:{server.server_port}/junie-open/{token}"
            worker = Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                helper = Path(__file__).with_name("settings_browser.mjs")
                catalog = [
                    {"id": "fixture-luna", "name": "Fixture Luna", "efforts": ["low", "medium"]},
                    {"id": "fixture-lite", "name": "Fixture Lite", "efforts": ["none"]},
                    {"id": "fixture-unknown", "name": "Fixture Unknown"},
                ]
                with patch.object(dashboard, "list_models", return_value=catalog):
                    checks = (
                        (helper.with_name("editor_semantic_browser_driver.mjs"), "editor-browser",
                         [url, run_id, str(path)], 90),
                        (helper, "workflow-browser",
                         [url, notification_url, run_id, str(run / JUNIE_LOG_NAME), str(path)], 110),
                    )
                    for script, profile, arguments, deadline in checks:
                        with self.subTest(scenario=script.stem):
                            result = run_browser([*node_args, str(script), chrome, str(root / profile), *arguments],
                                                 timeout=deadline)
                            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                            print(result.stdout, end="", flush=True)
                self.assertEqual(view.actions.qsize(), 1)
                self.assertEqual(view.actions.get_nowait()["action"], "resume")
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
