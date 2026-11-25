import copy
import io
import json
import subprocess
import sys
import time
import tomllib
import uuid
from contextlib import redirect_stdout
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock, Thread
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from fixtures import HarnessCase, prepare_fixture
from migration_harness.web import dashboard
from migration_harness.junie import junie_cli
from migration_harness.runs import run_manager
from migration_harness import workflow
from migration_harness.execution import check_cancelled, report_activity
from migration_harness.runs.run_manager import RunManager
from migration_harness.runs.run_records import execution_lock, load_saved_run, write_run_record


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

    def make_saved_run(self, unit):
        run_id = prepare_fixture(self.settings, unit)["run_id"]
        self.manager.get(run_id)
        return run_id

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

        def get(endpoint, run_id=None):
            headers = {"X-Run-ID": run_id} if run_id is not None else {}
            with urlopen(Request(url + endpoint, headers=headers), timeout=3) as response:
                return json.load(response)

        return post, get

    def test_recovery_plan_io_does_not_hold_the_run_manager_lock(self):
        run_id = self.make_saved_run("unit-0")
        post, get = self.open_http_dashboard()
        for mode in ("read", "save"):
            with self.subTest(mode=mode):
                started, finish = Event(), Event()

                def slow(*_args):
                    started.set()
                    if not finish.wait(5):
                        raise RuntimeError("recovery test timed out")
                    return {"version": "a" * 64} if mode == "read" else None

                name = "plan_stopped_repair" if mode == "read" else "resolve_stopped_repair"
                with patch.object(dashboard, name, side_effect=slow), ThreadPoolExecutor(max_workers=1) as pool:
                    if mode == "read":
                        future = pool.submit(get, f"recovery-plan?run_id={run_id}&kind=draft", run_id)
                    else:
                        future = pool.submit(post, "recovery-plan", {"run_id": run_id, "kind": "draft",
                            "version": "a" * 64, "choices": {"Page0.cs": "draft"}}, run_id)
                    try:
                        self.assertTrue(started.wait(3))
                        acquired = self.manager.condition.acquire(timeout=1)
                        self.assertTrue(acquired, "recovery I/O held the run manager lock")
                        if acquired:
                            self.manager.condition.release()
                    finally:
                        finish.set()
                    future.result(timeout=5)

    def test_only_finished_runs_notify_and_user_stop_is_silent(self):
        notices = []
        self.manager.notifications = SimpleNamespace(settings=lambda: {}, post=lambda notice: notices.append(notice) if notice else None)
        completed = self.manager.submit({"action": "run", "unit_id": "unit-0"})["run_id"]
        stopped = self.manager.submit({"action": "run", "unit_id": "unit-1"})["run_id"]
        self.assertTrue(self.started[0].wait(8))
        self.assertTrue(self.started[1].wait(8))
        self.assertEqual(notices, [])
        view = self.manager.get(stopped)
        self.assertTrue(view.request_stop({"operation_id": view.state["operation_id"]}))
        self.release[0].set()
        self.wait_until(lambda: not self.manager.workers)
        self.assertEqual([(notice.run_id, notice.kind) for notice in notices], [(completed, "built")])

    def test_cancelling_queued_anchors_finishes_the_original_http_request(self):
        self.settings["diff_viewer_html"] = "viewer.html"
        run_id = self.make_saved_run("unit-0")
        self.wait_until(lambda: not self.manager.busy)
        self.manager.limit = 0
        post, _ = self.open_http_dashboard()
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

    def test_first_normal_run_queue_save_retries_without_starting_early(self):
        write_failed, recovered = Event(), Event()
        persist = self.manager.persist

        def write():
            if not recovered.is_set():
                write_failed.set()
                raise PermissionError("queue disk unavailable")
            return persist()

        post, _ = self.open_http_dashboard()
        with patch.object(self.manager, "persist", side_effect=write):
            status, response = post("control", {"action": "run", "unit_id": "unit-0", "revision": 0})
            self.assertEqual(status, 202)
            self.assertTrue(write_failed.is_set())
            self.assertFalse(self.started[0].is_set())
            self.assertTrue(self.manager.row(self.manager.get(response["run_id"]))["queue_saving"])
            recovered.set()
            try:
                self.assertTrue(self.started[0].wait(6))
            finally:
                self.release[0].set()
            self.wait_until(lambda: not self.manager.busy)
        self.assertEqual(self.manager.results[response["run_id"]]["status"], "build_succeeded")

    def test_a_started_anchor_job_keeps_its_own_reply(self):
        self.settings["diff_viewer_html"] = "viewer.html"
        run_id = self.make_saved_run("unit-0")
        self.wait_until(lambda: not self.manager.busy)
        started, release = Event(), Event()
        reply = Future()

        def generate(*args, **kwargs):
            started.set()
            self.assertTrue(release.wait(5))
            return Path("example.json"), 2

        with patch.object(dashboard, "generate_anchors", side_effect=generate):
            self.manager.submit({"action": "anchors", "run_id": run_id, "reply": reply})
            try:
                self.assertTrue(started.wait(5))
                with self.assertRaisesRegex(ValueError, "すでに実行"):
                    self.manager.submit({"action": "cancel-queued", "run_id": run_id})
                self.assertFalse(reply.done())
            finally:
                release.set()
            self.assertEqual(reply.result(timeout=3), (run_id, "example", 2))
            self.wait_until(lambda: not self.manager.busy)

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
            self.make_saved_run(unit)
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
        missing = self.manager.submit({"action": "run", "unit_id": "unit-0"})["run_id"]
        self.wait_until(lambda: not self.manager.busy)
        self.settings["scripts"] = {"before_prepare": [[sys.executable, "-c", "raise SystemExit(3)"]]}
        hook = self.manager.submit({"action": "run", "unit_id": "unit-1"})["run_id"]
        self.wait_until(lambda: not self.manager.busy)
        with patch.object(workflow, "_validate_inputs", side_effect=KeyboardInterrupt):
            self.settings.pop("scripts")
            stopped = self.manager.submit({"action": "run", "unit_id": "unit-2"})["run_id"]
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
        run_id = self.manager.submit({"action": "run", "unit_id": "unit-0"})["run_id"]
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

    def test_hide_after_git_worktree_creation_fails(self):
        self.settings["base_ref"] = "missing-git-base"
        run_id = self.manager.submit({"action": "run", "unit_id": "unit-0"})["run_id"]
        self.wait_until(lambda: not self.manager.busy)
        record_path = Path(self.settings["run_root"]) / run_id / "run.json"
        record = json.loads(record_path.read_bytes())
        self.assertEqual(record["status"], "preparation_failed")
        self.assertNotIn("worktree_path", record)
        self.manager.submit({"action": "hide-run", "run_id": run_id})
        self.assertTrue(json.loads(record_path.read_bytes())["hidden_from_dashboard"])

    def test_hide_rejects_a_dangling_run_record_symlink(self):
        (self.project / "source.php").unlink()
        run_id = self.manager.submit({"action": "run", "unit_id": "unit-0"})["run_id"]
        self.wait_until(lambda: not self.manager.busy)
        path = Path(self.settings["run_root"]) / run_id / "run.json"
        path.unlink()
        try:
            path.symlink_to(path.with_name("missing.json"))
        except (NotImplementedError, OSError):
            self.skipTest("Creating symlinks is unavailable")
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.manager.submit({"action": "hide-run", "run_id": run_id})
        self.assertTrue(path.is_symlink())

    def test_create_run_after_hiding_the_selected_cancelled_run(self):
        self.manager.limit = 0
        run_id = self.manager.submit({"action": "run", "unit_id": "unit-0"})["run_id"]
        self.manager.submit({"action": "cancel-queued", "run_id": run_id})
        self.manager.default = self.manager.get(run_id)
        post, _ = self.open_http_dashboard()
        self.assertEqual(post("control", {"action": "hide-run", "revision": 0, "run_id": run_id}, run_id)[0], 202)
        for action, unit in (("run", "unit-1"), ("run", "unit-2")):
            status, result = post("control", {"action": action, "revision": 0, "unit_id": unit}, run_id)
            self.assertEqual(status, 202)
            self.assertNotEqual(result["run_id"], run_id)
        self.assertNotIn("run_id", self.manager.get().state)

    def test_conversation_status_before_worktree_survives_prepare(self):
        self.manager.limit = 0
        run_id, other = self.manager.submit({"action": "run", "unit_ids": ["unit-0", "unit-1"]})["run_ids"]
        post, get = self.open_http_dashboard()

        def send(text, identifier=None, **flags):
            body = {"kind": "conversation", "run_id": run_id, "id": identifier or uuid.uuid4().hex, "text": text, **flags}
            status, receipt = post("editor", body, run_id)
            self.assertEqual(status, 202)
            self.assertEqual(receipt["status"], "received")
            return receipt["id"]

        state = get("editor", run_id)
        self.assertEqual(state["run_id"], run_id)
        self.assertTrue(state["status_only"])
        self.assertTrue(state["can_prompt"])
        queued = send("今何してる？")
        self.assertIn("実行待ち", get("editor", run_id)["conversation"][0]["message"])
        self.assertEqual(get("editor", other)["conversation"], [])
        self.assertFalse((Path(self.settings["run_root"]) / run_id).exists())

        started, release = Event(), Event()
        validate = workflow._validate_inputs

        def wait_for_input(root, unit):
            if unit["id"] == "unit-0":
                started.set()
                self.assertTrue(release.wait(5))
            return validate(root, unit)

        with patch.object(workflow, "_validate_inputs", side_effect=wait_for_input):
            self.release[0].set()
            self.release[1].set()
            self.manager.limit = 1
            self.manager.start_pending()
            try:
                self.assertTrue(started.wait(5))
                preparing = send("今の状態は？")
                diagnostics = send("前回のビルドはなぜ落ちた？")
                rejected = send("ビルドして")
                messages = {row["id"]: row["message"] for row in get("editor", run_id)["conversation"]}
                self.assertIn("準備中", messages[preparing])
                self.assertIn("保存済みの結果はありません", messages[diagnostics])
                self.assertIn("状況の確認", messages[rejected])
                self.assertFalse(self.manager.get(run_id).control.cancelled.is_set())
            finally:
                release.set()
            self.wait_until(lambda: not self.manager.busy)
        send("今何してる？", queued)
        send("ビルドして", status_only=True)
        state = get("editor", run_id)
        self.assertFalse(state.get("status_only"))
        self.assertIn("Page0.cs", state["targets"])
        self.assertEqual(len(state["conversation"]), 5)
        self.assertFalse(self.manager.busy)

    def test_waiting_conversation_is_restored_after_dashboard_restart(self):
        self.manager.limit = 0
        run_id = self.manager.submit({"action": "run", "unit_id": "unit-0"})["run_id"]
        action = {"id": uuid.uuid4().hex, "text": "今何してる？"}
        self.manager.conversations.submit(run_id, action)
        self.manager.close()
        restarted = RunManager(self.settings, dashboard.Dashboard())
        self.addCleanup(restarted.close)
        history = restarted.conversations.preparation_replies(run_id, restarted.get(run_id).state)
        self.assertEqual([item["id"] for item in history], [action["id"]])
        self.assertIn("実行待ち", history[0]["message"])
        self.assertIn(run_id, restarted.pending)
        self.assertNotIn(run_id, restarted.workers)

    def test_resume_cannot_duplicate_a_unit_already_running(self):
        old = self.make_saved_run("unit-0")
        self.wait_until(lambda: not self.manager.busy)
        self.manager.submit({"action": "run", "unit_id": "unit-0"})
        with self.assertRaisesRegex(ValueError, "別のRunで実行中"):
            self.manager.submit({"action": "resume-run", "run_id": old})

    def test_new_and_resumed_runs_share_fifo_and_cancel_restores_saved_state(self):
        saved = self.make_saved_run("unit-6")
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

    def test_direct_open_shares_restored_run_progress_and_stop(self):
        run_id = self.make_saved_run("unit-0")
        self.wait_until(lambda: not self.manager.busy)
        self.manager.limit = 0
        self.manager.submit({"action": "resume-run", "run_id": run_id})
        operation_id = self.manager.jobs[run_id]["operation_id"]
        self.manager.close()

        opened, finished = Event(), Event()
        terminal = io.StringIO()
        original_server = dashboard.Dashboard.server
        saved_view = None

        def open_server(view, *args):
            nonlocal saved_view
            saved_view = view
            next_action = view.actions.get

            def action_or_shutdown(*, timeout):
                if finished.is_set():
                    raise KeyboardInterrupt
                return next_action(timeout=timeout)

            view.actions.get = action_or_shutdown
            with redirect_stdout(terminal):
                server = original_server(view, *args)
            opened.set()
            return server

        def read_state(stream, matches):
            revision = None
            for line in iter(stream.readline, b""):
                if line.startswith(b"id: "):
                    revision = int(line[4:])
                elif line.startswith(b"data: "):
                    state = json.loads(line[6:])
                    if matches(state):
                        self.assertEqual(state["run_id"], run_id)
                        return revision, state
            self.fail("Event stream closed before the expected Run state")

        with ThreadPoolExecutor(max_workers=1) as pool, patch.object(dashboard.Dashboard, "server", open_server):
            future = pool.submit(dashboard.run_dashboard, self.settings, run_id=run_id)
            try:
                self.assertTrue(opened.wait(15))
                self.assertTrue(self.started[0].wait(15))
                url = terminal.getvalue().splitlines()[0].removeprefix("Progress dashboard: ")
                origin = urlsplit(url)
                headers = {"Content-Type": "application/json", "Origin": f"{origin.scheme}://{origin.netloc}"}

                def control(action, revision, **data):
                    body = json.dumps({"action": action, "revision": revision, **data}).encode()
                    with urlopen(Request(url + "control", body, headers), timeout=8) as response:
                        self.assertEqual(response.status, 202)

                with (urlopen(url + "events", timeout=8) as default,
                      urlopen(url + f"events?run_id={run_id}", timeout=8) as selected):
                    editing = lambda state: state.get("latest_activity", {}).get("message") == "unit-0 is editing"
                    _, explicit = read_state(selected, editing)
                    self.assertTrue(explicit["cancellable"])
                    try:
                        revision, initial = read_state(default, editing)
                    except TimeoutError:
                        self.fail("The default URL did not publish the restored Run's progress")
                    self.assertEqual(initial, explicit)
                    self.assertFalse(initial.get("ready"))
                    self.assertEqual(initial["operation_id"], operation_id)
                    control("stop", revision, operation_id=operation_id)

                    stopped = lambda state: state.get("ready") and "停止しました" in state.get("message", "")
                    revision, stopped_default = read_state(default, stopped)
                    _, stopped_selected = read_state(selected, stopped)
                    self.assertEqual(stopped_default, stopped_selected)
                    self.assertFalse(stopped_default["cancellable"])
                    self.wait_until(lambda: not saved_view.manager.busy)

                    self.release[0].set()
                    control("resume-run", revision, run_id=run_id)
                    completed = lambda state: state.get("ready") and state["status"] == "build_succeeded"
                    self.assertEqual(read_state(default, completed), read_state(selected, completed))
            finally:
                finished.set()
                future.result(timeout=20)

    def test_failed_start_save_leaves_job_waiting_across_shutdown(self):
        self.manager.limit = 0
        run_id = self.manager.submit({"action": "run", "unit_id": "unit-0"})["run_id"]
        operation_id = self.manager.jobs[run_id]["operation_id"]
        self.manager.limit = 1
        with patch.object(run_manager, "monotonic", return_value=10) as clock:
            with (self.manager.condition,
                  patch.object(self.manager, "persist", side_effect=PermissionError("fixture: queue unavailable")) as persist):
                self.manager.start_pending()
                self.manager.retry_queue()
                persist.assert_called_once()
                clock.return_value = 11
                self.manager.retry_queue()
                clock.return_value = 12
                self.manager.retry_queue()
                self.assertEqual(persist.call_count, 2)
            self.manager.close()
            clock.return_value = 20
            with patch.object(self.manager, "persist") as persist:
                self.manager.retry_queue()
                persist.assert_not_called()
        self.assertFalse(self.started[0].is_set())

        restored = RunManager(self.settings, dashboard.Dashboard())
        self.addCleanup(restored.close)
        self.assertEqual(list(restored.pending), [run_id])
        self.assertEqual(restored.jobs[run_id]["operation_id"], operation_id)
        self.assertTrue(restored.get(run_id).state["queued"])
        restored.start_pending()
        self.assertTrue(self.started[0].wait(5))
        self.release[0].set()
        self.wait_until(lambda: not restored.busy)

    def test_queue_save_retry_does_not_carry_over_to_new_storage(self):
        self.manager.limit = 0
        run_id = self.manager.submit({"action": "run", "unit_id": "unit-0"})["run_id"]
        old_path = self.manager.queue_path
        self.manager.limit = 1
        with patch.object(run_manager, "monotonic", return_value=10) as clock, self.manager.condition:
            with patch.object(self.manager, "persist", side_effect=PermissionError("fixture: queue unavailable")):
                self.manager.start_pending()
            self.manager.submit({"action": "cancel-queued", "run_id": run_id})
            self.manager.configure({**self.settings, "run_root": str(self.root / "another-runs")})
            clock.return_value = 12
            with patch.object(self.manager, "persist") as persist:
                self.manager.retry_queue()
                persist.assert_not_called()
        self.assertEqual(json.loads(old_path.read_bytes())["jobs"], [])
        self.assertNotEqual(self.manager.queue_path, old_path)
        self.assertFalse(self.started[0].is_set())

    def test_settings_switch_releases_old_owner_and_clears_old_runs(self):
        ids = [self.make_saved_run("unit-0")]
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

    def test_failed_settings_replace_keeps_old_scope_and_new_queue_untouched(self):
        post, _ = self.open_http_dashboard()
        path = Path(self.settings["config_path"])
        original, version = dashboard._read_settings(path)
        previous = copy.deepcopy(self.settings)
        old_id = str(uuid.uuid4())
        old_view = self.manager.new_view(old_id, "unit-0")
        old_revision = old_view.revision
        self.manager.results[old_id] = {"status": "previous"}
        new_root = self.root / "new-runs"
        new_root.mkdir()
        new_config = {**self.settings, "run_root": str(new_root)}
        queue = new_root / "dashboard-queue.json"
        queue.write_text(json.dumps({"project_root": self.settings["project_root"], "jobs": [
            {"action": "run", "run_id": str(uuid.uuid4()), "unit_id": "sample"},
        ]}), encoding="utf-8")
        saved_queue = queue.read_bytes()
        replace = Path.replace

        def fail_settings_only(candidate, destination):
            if destination == path:
                raise PermissionError("fixture: settings file is locked")
            return replace(candidate, destination)

        content = original.decode().replace('run_root = "runs"', 'run_root = "new-runs"')
        with patch.object(Path, "replace", fail_settings_only), patch.object(self.manager, "start_pending") as start:
            with self.assertRaises(HTTPError) as rejected:
                post("settings", {"content": content, "version": version, "revision": self.view.revision})
            with rejected.exception as error:
                self.assertEqual(error.code, 409)
                self.assertIn("settings file is locked", error.read().decode())
            start.assert_not_called()
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.settings, previous)
        self.assertEqual(self.manager.root, previous["run_root"])
        self.assertIs(self.manager.get(old_id), old_view)
        self.assertEqual(old_view.revision, old_revision)
        self.assertEqual(self.manager.results[old_id], {"status": "previous"})
        self.assertFalse(self.manager.jobs or self.manager.pending)
        self.assertEqual(queue.read_bytes(), saved_queue)
        with execution_lock(new_config, "dashboard"):
            pass
        with self.assertRaises(RuntimeError), execution_lock(previous, "dashboard"):
            pass

    def test_settings_preflight_failure_leaves_saved_toml_and_old_owner(self):
        post, _ = self.open_http_dashboard()
        path = Path(self.settings["config_path"])
        original, version = dashboard._read_settings(path)
        previous = copy.deepcopy(self.settings)
        root = self.root / "new-runs"
        proposed = {**self.settings, "run_root": str(root)}
        content = original.decode().replace('run_root = "runs"', 'run_root = "new-runs"')
        request = {"content": content, "version": version, "revision": self.view.revision}

        def rejected():
            with self.assertRaises(HTTPError) as response:
                post("settings", request)
            with response.exception as error:
                self.assertEqual(error.code, 409)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(self.settings, previous)
            self.assertEqual(self.manager.root, previous["run_root"])
            with self.assertRaises(RuntimeError), execution_lock(previous, "dashboard"):
                pass

        with execution_lock(proposed, "dashboard"):
            rejected()
        queue = root / "dashboard-queue.json"
        queue.write_bytes(b"invalid queue")
        rejected()
        self.assertEqual(queue.read_bytes(), b"invalid queue")
        with execution_lock(proposed, "dashboard"):
            pass

    def test_first_settings_save_keeps_empty_manager_on_failure_then_commits(self):
        path = Path(self.settings["config_path"])
        content = path.read_text(encoding="utf-8")
        self.manager.close()
        path.unlink()
        self.settings = {"config_path": str(path)}
        self.view = dashboard.Dashboard()
        self.manager = RunManager(self.settings, self.view)
        self.view.manager = self.manager
        self.addCleanup(self.manager.close)
        post, _ = self.open_http_dashboard()
        request = {"content": content, "version": "missing", "revision": self.view.revision}
        replace = Path.replace

        def fail_settings_only(candidate, destination):
            if destination == path:
                raise PermissionError("fixture: first settings save")
            return replace(candidate, destination)

        with patch.object(Path, "replace", fail_settings_only), self.assertRaises(HTTPError) as rejected:
            post("settings", request)
        with rejected.exception as error:
            self.assertEqual(error.code, 409)
        self.assertFalse(path.exists())
        self.assertIsNone(self.manager.owner)
        self.assertIsNone(self.manager.root)
        self.assertEqual(self.settings, {"config_path": str(path)})
        status, saved = post("settings", request)
        self.assertEqual(status, 200)
        self.assertEqual(dashboard._read_settings(path)[1], saved["version"])
        self.assertEqual(self.manager.root, self.settings["run_root"])
        with self.assertRaises(RuntimeError), execution_lock(self.settings, "dashboard"):
            pass

    def test_settings_worktree_switch_keeps_owner_and_restores_waiting_job_after_commit(self):
        post, _ = self.open_http_dashboard()
        path = Path(self.settings["config_path"])
        original, version = dashboard._read_settings(path)
        owner, root, queue = self.manager.owner, self.manager.root, self.manager.queue_path
        self.manager.limit = 0
        old_id = str(uuid.uuid4())
        self.manager.new_view(old_id, "unit-0")
        self.manager.results[old_id] = {"status": "previous"}
        run_id, operation = str(uuid.uuid4()), uuid.uuid4().hex
        job = {"action": "run", "run_id": run_id, "unit_id": "sample", "operation_id": operation}
        queue.write_text(json.dumps({"project_root": self.settings["project_root"], "jobs": [job]}), encoding="utf-8")
        content = original.decode().replace('worktree_root = "trees"', 'worktree_root = "other-trees"')
        seen = []
        replace = Path.replace

        def record_publish(candidate, destination):
            if destination in {path, queue}:
                seen.append(destination)
            return replace(candidate, destination)

        with patch.object(Path, "replace", record_publish):
            status, _ = post("settings", {"content": content, "version": version, "revision": self.view.revision})
        self.assertEqual(status, 200)
        self.assertEqual(seen, [path, queue])
        self.assertEqual(path.read_bytes(), content.encode())
        self.assertEqual(Path(self.settings["worktree_root"]).name, "other-trees")
        self.assertEqual(self.manager.root, root)
        self.assertIs(self.manager.owner, owner)
        self.assertNotIn(old_id, self.manager.views)
        self.assertNotIn(old_id, self.manager.results)
        self.assertEqual(list(self.manager.pending), [run_id])
        self.assertEqual(self.manager.jobs[run_id], job)
        self.assertTrue(self.manager.get(run_id).state["queued"])
        self.assertEqual(json.loads(queue.read_bytes())["jobs"], [job])
        self.assertFalse(self.manager.workers)
        with self.assertRaises(RuntimeError), execution_lock(self.settings, "dashboard"):
            pass

    def test_workspace_preserves_operation_failure_message(self):
        run_id = self.make_saved_run("unit-0")
        self.wait_until(lambda: not self.manager.busy)
        view = self.manager.get(run_id)
        view.failed(ValueError("Junieを起動できません"))
        self.manager.workspace()
        self.assertIn("Junieを起動できません", view.state["error_details"])

    def test_rules_do_not_mix_externally_changed_project_with_live_settings(self):
        post, get = self.open_http_dashboard()
        path = Path(self.settings["config_path"])
        original, version = dashboard._read_settings(path)
        (self.project / "original.md").write_bytes(b"PROJECT_A_RULE\n")
        project_b = self.root / "app-b"
        project_b.mkdir()
        subprocess.run(["git", "init", "-q", str(project_b)], check=True, capture_output=True)
        (project_b / "source.php").write_bytes(b"<?php echo 22;\n")
        original_b = project_b / "original.md"
        original_b.write_bytes(b"PROJECT_B_RULE\n")

        def save(content, version):
            return post("settings", {"content": content, "version": version, "revision": self.view.revision})

        def rule(action, draft=None, **fields):
            if draft:
                fields.update(id=draft["id"], revision=draft["revision"])
            return post("rule-workspace", {"action": action, **fields})

        def apply(draft):
            return rule("apply", draft, confirmed=True, preview=draft["preview"]["token"])

        def rejected(request, message):
            with self.assertRaises(HTTPError) as response:
                request()
            with response.exception as error:
                self.assertEqual(error.code, 409)
                self.assertIn(message, error.read().decode())

        toml_a = original.decode() + '\n[[project.migration_rules]]\npath = "original.md"\n'
        self.assertEqual(save(toml_a, version)[0], 200)
        live_a = self.settings["project_root"]
        capsule = Path(prepare_fixture(self.settings, "sample")["capsule_path"])
        manifest = json.loads((capsule / "manifest.json").read_bytes())
        pinned = capsule / "materials" / manifest["rule_files"][0]["file"]
        pinned_a = pinned.read_bytes()
        self.assertIn(b"PROJECT_A_RULE", pinned_a)
        old_draft = rule("preview", rule("start")[1])[1]
        toml_b = toml_a.replace('root = "app"', 'root = "app-b"')
        path.write_bytes(toml_b.encode())
        rejected(lambda: rule("start"), "設定画面で")
        rejected(lambda: apply(old_draft), "設定画面で")
        self.assertEqual(path.read_bytes(), toml_b.encode())
        self.assertEqual(self.settings["project_root"], live_a)
        self.assertEqual([draft["id"] for draft in get("rule-workspace")["drafts"]],
                         [old_draft["id"]])

        self.assertEqual(save(toml_b, dashboard._read_settings(path)[1])[0], 200)
        self.assertEqual(Path(self.settings["project_root"]), project_b)
        new_draft = rule("start")[1]
        source = f'rule-workspace?id={new_draft["id"]}&source=rule-0001.md'
        self.assertEqual(get(source)["text"], "PROJECT_B_RULE\n")
        original_b.write_bytes(b"PROJECT_B_RULE\nEDITED_BEFORE_PREVIEW\n")
        rejected(lambda: rule("preview", new_draft), "登録元")
        preview = rule("preview", rule("start")[1])[1]
        original_b.write_bytes(b"PROJECT_B_RULE\nEDITED_AFTER_PREVIEW\n")
        rejected(lambda: apply(preview), "登録元")
        self.assertEqual(path.read_bytes(), toml_b.encode())
        preview = rule("preview", rule("start")[1])[1]
        self.assertTrue(apply(preview)[1]["applied"])
        rules = tomllib.loads(path.read_text(encoding="utf-8"))["project"]["migration_rules"]
        published = Path(rules[0]["path"]).read_text(encoding="utf-8")
        self.assertIn("EDITED_AFTER_PREVIEW", published)
        self.assertNotIn("PROJECT_A_RULE", published)
        self.assertEqual(pinned.read_bytes(), pinned_a)

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
            run_id = self.make_saved_run("unit-0")
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
