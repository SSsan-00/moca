import copy
import io
import json
import subprocess
import sys
import time
from contextlib import redirect_stdout
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock, Thread
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from fixtures import HarnessCase
from migration_harness import dashboard, junie_cli, workflow
from migration_harness.execution import check_cancelled, report_activity
from migration_harness.run_manager import RunManager
from migration_harness.run_records import execution_lock, load_saved_run, write_run_record


class ParallelDashboardTests(HarnessCase):
    def setUp(self):
        super().setUp()
        (self.project / "source.php").write_text("<?php echo 42;\n")
        self.commit_project()
        self.settings = self.config()
        unit = self.settings["units"][0]
        self.settings["units"] = [{**copy.deepcopy(unit), "id": f"unit-{i}", "target": f"Page{i}.cs"} for i in range(7)]
        self.started = [Event() for _ in range(7)]
        self.release = [Event() for _ in range(7)]
        self.guard = Lock()
        self.active = self.maximum = 0
        command = workflow.run_process

        def migrate(_command, completion, token, targets, **kwargs):
            index = int(targets[0].stem.removeprefix("Page"))
            targets[0].write_text(f"class Page{index} {{}}\n")
            with self.guard:
                self.active += 1
                self.maximum = max(self.maximum, self.active)
            self.started[index].set()
            try:
                report_activity(f"unit-{index} is editing")
                while not self.release[index].wait(.02):
                    check_cancelled()
                check_cancelled()
                completion.write_text(token)
                return 0
            finally:
                with self.guard:
                    self.active -= 1

        def process(args, **kwargs):
            return subprocess.CompletedProcess(args, 0) if args[0] == "dotnet" else command(args, **kwargs)

        for target, name, replacement in ((junie_cli, "require_interactive_cli", lambda: None),
                                          (junie_cli, "run_interactive", migrate), (workflow, "run_process", process)):
            mocked = patch.object(target, name, side_effect=replacement)
            mocked.start()
            self.addCleanup(mocked.stop)
        self.view = dashboard.Dashboard()
        self.view.publish({"status": "idle", "ready": True})
        self.manager = RunManager(self.settings, self.view)
        self.view.manager = self.manager
        self.addCleanup(self.manager.close)

    def wait_until(self, predicate):
        end = time.monotonic() + 8
        while not predicate() and time.monotonic() < end:
            time.sleep(.02)
        self.assertTrue(predicate())

    def open_http_dashboard(self):
        terminal = io.StringIO()
        with redirect_stdout(terminal):
            server = self.view.server(self.settings, b"", b"")
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        url = terminal.getvalue().splitlines()[0].removeprefix("Progress dashboard: ")
        origin = urlsplit(url)

        def post(endpoint, body, run_id=None):
            headers = {"Content-Type": "application/json", "Origin": f"{origin.scheme}://{origin.netloc}"}
            if run_id is not None:
                headers["X-Run-ID"] = run_id
            with urlopen(Request(url + endpoint, json.dumps(body).encode(), headers), timeout=3) as response:
                return response.status, json.load(response)

        return post

    def test_cancelling_queued_anchors_finishes_the_original_http_request(self):
        self.settings["diff_viewer_html"] = "viewer.html"
        run_id = self.manager.submit({"action": "prepare", "unit_id": "unit-0"})["run_id"]
        self.wait_until(lambda: not self.manager.busy)
        self.manager.limit = 0
        post = self.open_http_dashboard()
        with patch.object(dashboard, "generate_anchors") as generate, ThreadPoolExecutor() as pool:
            request = pool.submit(post, "control", {"action": "anchors", "revision": 0}, run_id)
            self.wait_until(lambda: run_id in self.manager.pending)
            status, _ = post("control", {"action": "cancel-queued", "revision": 0, "run_id": run_id})
            self.assertEqual(status, 202)
            with self.assertRaises(HTTPError) as response:
                request.result(timeout=2)
            with response.exception as error:
                self.assertEqual(error.code, 409)
                self.assertIn("取り消", error.read().decode())
            self.manager.limit = 5
            self.manager.start_pending()
            generate.assert_not_called()
        self.assertTrue(self.manager.get(run_id).state["ready"])
        self.assertFalse(self.manager.get(run_id).state["generating_anchors"])

    def test_five_workers_queue_stop_isolation_and_resume(self):
        ids = self.manager.submit({"action": "run", "unit_ids": [f"unit-{i}" for i in range(6)]})["run_ids"]
        for event in self.started[:5]:
            self.assertTrue(event.wait(10))
        self.assertFalse(self.started[5].is_set())
        self.assertEqual(self.manager.row(self.manager.get(ids[5]))["queue_position"], 1)
        self.assertEqual(self.manager.workspace()["parallel_limit"], 5)
        a, b = self.manager.get(ids[0]), self.manager.get(ids[1])
        self.assertIn("unit-0", a.state["latest_activity"]["message"])
        self.assertIn("unit-1", b.state["latest_activity"]["message"])
        with self.assertRaisesRegex(RuntimeError, "使用中"):
            workflow.resume_with_junie(self.settings, ids[0])
        self.assertTrue(a.request_stop({"operation_id": a.state["operation_id"]}))
        self.assertTrue(self.started[5].wait(5))
        self.assertTrue(all(not self.manager.get(run_id).control.cancelled.is_set() for run_id in ids[1:5]))
        self.wait_until(lambda: a.state.get("ready"))
        self.assertTrue(a.state["resumable"])
        for event in self.release:
            event.set()
        self.wait_until(lambda: not self.manager.busy)
        self.manager.submit({"action": "resume-run", "run_id": ids[0]})
        self.wait_until(lambda: not self.manager.busy)
        self.assertEqual(self.maximum, 5)
        for run_id in ids:
            self.assertEqual(self.manager.get(run_id).state["status"], "build_succeeded")
        self.assertEqual(len({self.manager.get(run_id).state["worktree_path"] for run_id in ids}), 6)

    def test_cancel_and_duplicate_enqueue_are_atomic(self):
        ids = self.manager.submit({"action": "run", "unit_ids": [f"unit-{i}" for i in range(6)]})["run_ids"]
        for run_id in (ids[0], ids[5]):
            with self.assertRaisesRegex(ValueError, "処理中"):
                self.manager.submit({"action": "hide-run", "run_id": run_id})
        with self.assertRaisesRegex(ValueError, "実行中または実行待ち"):
            self.manager.submit({"action": "run", "unit_ids": ["unit-6", "unit-0"]})
        self.assertFalse(any(job["unit_id"] == "unit-6" for job in self.manager.jobs.values()))
        self.manager.submit({"action": "cancel-queued", "run_id": ids[5]})
        self.assertEqual(self.manager.get(ids[5]).state["status"], "cancelled")
        self.assertFalse(self.started[5].is_set())
        self.assertFalse((Path(self.settings["run_root"]) / ids[5]).exists())
        self.manager.submit({"action": "hide-run", "run_id": ids[5]})
        self.assertNotIn(ids[5], {row["run_id"] for row in self.manager.workspace()["runs"]})

    def test_remove_saved_run_from_list_without_deleting_its_files(self):
        removed, retained = [
            self.manager.submit({"action": "prepare", "unit_id": unit})["run_id"]
            for unit in ("unit-0", "unit-1")
        ]
        self.wait_until(lambda: not self.manager.busy)
        run_path, worktree, record, _ = load_saved_run(self.settings, removed)
        target = worktree / "Page0.cs"
        target.write_text("class Preserved {}\n", encoding="utf-8")
        with execution_lock(self.settings, removed):
            with self.assertRaisesRegex(RuntimeError, "使用中"):
                self.manager.submit({"action": "hide-run", "run_id": removed})

        self.manager.submit({"action": "hide-run", "run_id": removed})
        self.assertEqual([row["run_id"] for row in self.manager.workspace()["runs"]], [retained])
        self.assertEqual({row["run_id"] for row in self.manager.workspace(include_usage=True)["runs"]}, {removed, retained})
        # 一覧から削除する前に読み込んだ進捗が保存されても、削除状態は取り消されない。
        write_run_record(run_path / "run.json", record)
        self.manager.close()
        with_run = RunManager(self.settings, dashboard.Dashboard())
        self.addCleanup(with_run.close)
        self.assertEqual([row["run_id"] for row in with_run.workspace()["runs"]], [retained])
        self.assertEqual(with_run.get(removed).state["status"], "prepared")
        self.assertEqual(target.read_text(encoding="utf-8"), "class Preserved {}\n")

    def test_hide_preparation_failures_and_preserve_records(self):
        (self.project / "source.php").unlink()
        missing = self.manager.submit({"action": "prepare", "unit_id": "unit-0"})["run_id"]
        self.wait_until(lambda: not self.manager.busy)
        self.settings["scripts"] = {"before_prepare": [[sys.executable, "-c", "raise SystemExit(3)"]]}
        hook = self.manager.submit({"action": "prepare", "unit_id": "unit-1"})["run_id"]
        self.wait_until(lambda: not self.manager.busy)
        with patch.object(workflow, "_validate_inputs", side_effect=KeyboardInterrupt):
            self.settings.pop("scripts")
            stopped = self.manager.submit({"action": "prepare", "unit_id": "unit-2"})["run_id"]
            self.wait_until(lambda: not self.manager.busy)
        self.assertEqual(self.manager.get(stopped).state["status"], "preparation_interrupted")
        for run_id in (missing, hook, stopped):
            with self.subTest(run_id=run_id):
                path = Path(self.settings["run_root"]) / run_id
                self.assertFalse((Path(self.settings["worktree_root"]) / run_id).exists())
                self.manager.submit({"action": "hide-run", "run_id": run_id})
                self.assertTrue(json.loads((path / "run.json").read_bytes())["hidden_from_dashboard"])
        self.assertTrue((Path(self.settings["run_root"]) / hook / "before_prepare.log").is_file())
        self.assertEqual(self.manager.workspace()["runs"], [])
        self.manager.close()
        restarted = RunManager(self.settings, dashboard.Dashboard())
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.workspace()["runs"], [])
        self.assertEqual(restarted.get(stopped).state["status"], "preparation_interrupted")

    def test_hide_does_not_write_a_foreign_preparation_record(self):
        (self.project / "source.php").unlink()
        run_id = self.manager.submit({"action": "prepare", "unit_id": "unit-0"})["run_id"]
        self.wait_until(lambda: not self.manager.busy)
        path = Path(self.settings["run_root"]) / run_id / "run.json"
        record = json.loads(path.read_bytes())
        record["project_root"] = "other-project"
        path.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "configured project"):
            self.manager.submit({"action": "hide-run", "run_id": run_id})
        self.assertEqual(json.loads(path.read_bytes()), record)
        with self.assertRaisesRegex(ValueError, "UUID"):
            self.manager.hide_run("../another-run")

    def test_create_run_after_hiding_the_selected_cancelled_run(self):
        self.manager.limit = 0
        run_id = self.manager.submit({"action": "prepare", "unit_id": "unit-0"})["run_id"]
        self.manager.submit({"action": "cancel-queued", "run_id": run_id})
        self.manager.default = self.manager.get(run_id)
        post = self.open_http_dashboard()
        self.assertEqual(post("control", {"action": "hide-run", "revision": 0, "run_id": run_id}, run_id)[0], 202)
        for action, unit in (("prepare", "unit-1"), ("run", "unit-2")):
            status, result = post("control", {"action": action, "revision": 0, "unit_id": unit}, run_id)
            self.assertEqual(status, 202)
            self.assertNotEqual(result["run_id"], run_id)
        self.assertNotIn("run_id", self.manager.get().state)

    def test_resume_cannot_duplicate_a_unit_already_running(self):
        old = self.manager.submit({"action": "prepare", "unit_id": "unit-0"})["run_id"]
        self.wait_until(lambda: not self.manager.busy)
        self.manager.submit({"action": "run", "unit_id": "unit-0"})
        with self.assertRaisesRegex(ValueError, "別のRunで実行中"):
            self.manager.submit({"action": "resume-run", "run_id": old})

    def test_new_and_resumed_runs_share_fifo_and_cancel_restores_saved_state(self):
        saved = self.manager.submit({"action": "prepare", "unit_id": "unit-6"})["run_id"]
        self.wait_until(lambda: not self.manager.busy)
        ids = self.manager.submit({"action": "run", "unit_ids": [f"unit-{i}" for i in range(6)]})["run_ids"]
        self.manager.submit({"action": "resume-run", "run_id": saved})
        self.assertEqual(list(self.manager.pending), [ids[5], saved])
        self.assertEqual(self.manager.row(self.manager.get(saved))["queue_position"], 2)
        self.manager.submit({"action": "cancel-queued", "run_id": saved})
        self.assertEqual(list(self.manager.pending), [ids[5]])
        view = self.manager.get(saved)
        self.assertEqual(view.state["status"], "prepared")
        self.assertTrue(view.state["ready"] and view.state["resumable"])
        self.assertFalse(view.state["queued"])

    def test_queue_survives_shutdown_but_started_runs_do_not_restart(self):
        ids = self.manager.submit({"action": "run", "unit_ids": [f"unit-{i}" for i in range(6)]})["run_ids"]
        for event in self.started[:5]:
            self.assertTrue(event.wait(10))
        self.manager.close()
        restored = RunManager(self.settings, dashboard.Dashboard())
        self.addCleanup(restored.close)
        self.assertEqual(list(restored.pending), [ids[5]])
        self.assertEqual(restored.get(ids[0]).state["status"], "interrupted")
        self.assertTrue(restored.get(ids[0]).state["resumable"])
        restored.start_pending()
        self.assertTrue(self.started[5].wait(5))
        self.assertEqual(len(restored.workers), 1)

    def test_settings_switch_releases_old_owner_and_clears_old_runs(self):
        ids = self.manager.submit({"action": "prepare", "unit_ids": ["unit-0"]})["run_ids"]
        self.wait_until(lambda: not self.manager.busy)
        previous = copy.deepcopy(self.settings)
        updated = {**self.settings, "run_root": str(self.root / "another-runs")}
        self.manager.configure(updated)
        self.assertEqual(self.manager.workspace()["runs"], [])
        self.assertNotIn("run_id", self.view.state)
        with self.assertRaises(ValueError):
            self.manager.get(ids[0])
        other = RunManager(previous, dashboard.Dashboard())
        self.addCleanup(other.close)
        with self.assertRaisesRegex(RuntimeError, "使用中"):
            self.manager.configure(previous)
        self.assertEqual(self.manager.config["run_root"], updated["run_root"])

    def test_workspace_preserves_operation_failure_message(self):
        run_id = self.manager.submit({"action": "prepare", "unit_id": "unit-0"})["run_id"]
        self.wait_until(lambda: not self.manager.busy)
        view = self.manager.get(run_id)
        view.failed(ValueError("Junieを起動できません"))
        self.manager.workspace()
        self.assertIn("Junieを起動できません", view.state["error_details"])

    def test_http_selection_editor_and_questions_are_scoped_to_run(self):
        ids = self.manager.submit({"action": "run", "unit_ids": ["unit-0", "unit-1"]})["run_ids"]
        for event in self.started[:2]:
            self.assertTrue(event.wait(5))
        terminal = io.StringIO()
        with redirect_stdout(terminal):
            server = self.view.server(self.settings, b"<!doctype html>", b"")
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        url = terminal.getvalue().splitlines()[0].removeprefix("Progress dashboard: ")
        origin = urlsplit(url)

        with urlopen(url + "dashboard.js", timeout=3) as response:
            self.assertEqual(response.headers.get_content_type(), "text/javascript")
        with urlopen(url + "dashboard_runs.js", timeout=3) as response:
            self.assertIn(b"bindRunManager", response.read())

        def post(endpoint, data, run_id=None):
            headers = {"Content-Type": "application/json", "Origin": f"{origin.scheme}://{origin.netloc}"}
            if run_id:
                headers["X-Run-ID"] = run_id
            return urlopen(Request(url + endpoint, json.dumps(data).encode(), headers), timeout=3)

        for index, run_id in enumerate(ids):
            with urlopen(Request(url + "editor", headers={"X-Run-ID": run_id}), timeout=3) as response:
                record = json.load(response)
            self.assertEqual(record["run_id"], run_id)
            self.assertIn(f"class Page{index}", record["content"])
        with post("control", {"action": "open-run", "run_id": ids[1], "revision": 0}):
            pass
        self.assertNotIn("run_id", self.view.state)
        with self.assertRaises(HTTPError) as rejected:
            post("settings", {"content": "", "version": "missing", "revision": self.view.revision})
        self.assertEqual(rejected.exception.code, 409)
        rejected.exception.close()
        futures = [self.manager.get(run_id).show_junie_request({"title": "確認", "options": [{"id": "yes", "label": "続行"}]}) for run_id in ids]
        b = self.manager.get(ids[1])
        with post("control", {"action": "junie-answer", "run_id": ids[1], "revision": b.revision,
                              "operation_id": b.state["operation_id"], "request_id": b.state["junie_request"]["id"], "option_id": "yes"}):
            pass
        self.assertFalse(futures[0].done())
        self.assertEqual(futures[1].result(), "yes")
        with post("prompts", {"run_id": ids[0], "text": "unit zero only"}, ids[0]):
            pass
        with urlopen(Request(url + "prompts", headers={"X-Run-ID": ids[1]}), timeout=3) as response:
            self.assertEqual(json.load(response)["prompts"], [])

    def test_same_target_runs_keep_editor_files_php_and_prompts_separate(self):
        ids = []
        for number in (1, 2):
            (self.project / "source.php").write_bytes(f"<?php echo {number};\n".encode())
            run_id = self.manager.submit({"action": "prepare", "unit_id": "unit-0"})["run_id"]
            self.wait_until(lambda: not self.manager.busy)
            ids.append(run_id)
            (Path(self.manager.get(run_id).state["worktree_path"]) / "Page0.cs").write_text(f"class Run{number} {{}}\n")

        terminal = io.StringIO()
        with redirect_stdout(terminal):
            server = self.view.server(self.settings, b"", b"")
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        url = terminal.getvalue().splitlines()[0].removeprefix("Progress dashboard: ")
        origin = urlsplit(url)

        def request(run_id, endpoint="editor", body=None):
            headers = {"X-Run-ID": run_id, "Content-Type": "application/json",
                       "Origin": f"{origin.scheme}://{origin.netloc}"}
            with urlopen(Request(url + endpoint, None if body is None else json.dumps(body).encode(), headers), timeout=3) as response:
                return json.load(response)

        first, second = (request(run_id) for run_id in ids)
        self.assertEqual(first["target"], second["target"])
        self.assertNotEqual(first["content"], second["content"])
        for number, run_id in enumerate(ids, 1):
            php = request(run_id, f"editor-php?run_id={run_id}")
            self.assertEqual(php["run_id"], run_id)
            self.assertEqual(php["content"], f"<?php echo {number};\n")
        saved = request(ids[0], body={"kind": "save", "run_id": ids[0], "target": first["target"],
                                     "base": first["content"], "content": "class Edited {}\n", "edit_id": None})
        self.assertEqual(saved["content"], "class Edited {}\n")
        request(ids[0], "prompts", {"run_id": ids[0], "text": "first run only"})
        self.assertEqual(request(ids[1])["content"], second["content"])
        self.assertEqual(request(ids[1], "prompts")["prompts"], [])
        self.assertEqual(len(self.manager.workspace()["runs"]), 2)
