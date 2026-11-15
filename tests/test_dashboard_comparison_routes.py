from __future__ import annotations

import io
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from threading import Event
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from fixtures import HarnessCase
from migration_harness import dashboard, junie_cli, workflow
from migration_harness.config import load_config


class DashboardComparisonRoutesTests(HarnessCase):
    @contextmanager
    def live_dashboard(self, config, run_id):
        view = dashboard.Dashboard()
        terminal = io.StringIO()
        listening, stop = Event(), Event()
        dequeue = view.actions.get

        def poll_actions(*, timeout):
            listening.set()
            if stop.is_set():
                raise KeyboardInterrupt
            return dequeue(timeout=timeout)

        with (
            redirect_stdout(terminal),
            patch.object(dashboard, "Dashboard", return_value=view),
            patch.object(view.actions, "get", side_effect=poll_actions),
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            serving = executor.submit(dashboard.run_dashboard, config, run_id=run_id)
            try:
                self.assertTrue(listening.wait(5), "dashboard failed to start")
                yield terminal.getvalue().splitlines()[0].removeprefix("Progress dashboard: ")
            finally:
                stop.set()
                serving.result(timeout=5)

    @staticmethod
    def ready_event(url, *, after=0):
        request = Request(url + "events", headers={"Last-Event-ID": str(after)})
        with urlopen(request, timeout=3) as response:
            for line in response:
                if line.startswith(b"id: "):
                    revision = int(line[4:])
                elif line.startswith(b"data: "):
                    state = json.loads(line[6:])
                    if state.get("ready"):
                        return revision, state
        raise AssertionError("no ready dashboard event")

    @staticmethod
    def post(url, endpoint, payload, status):
        origin = urlsplit(url)
        headers = {"Origin": f"{origin.scheme}://{origin.netloc}", "Content-Type": "application/json"}
        with urlopen(Request(url + endpoint, json.dumps(payload).encode(), headers), timeout=5) as response:
            if response.status != status:
                raise AssertionError(f"{endpoint}: expected HTTP {status}, got {response.status}")
            return json.load(response)

    def read_comparison(self, path):
        with urlopen(path + ".json", timeout=3) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get_content_type(), "application/json")
            payload = json.load(response)
        with urlopen(path + ".html", timeout=3) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get_content_type(), "text/html")
            html = response.read()
        marker = b'<script id="harness-review-workspace" type="application/json">'
        self.assertEqual(json.loads(html.split(marker, 1)[1].split(b"</script>", 1)[0]), payload)
        return payload

    def test_comparisons_stay_with_creating_run_after_open_run_and_settings_save(self):
        (self.project / "source.php").write_text("<?php echo 'Hello';\n", encoding="utf-8")
        (self.project / "Page.cshtml").write_text("<p>Hello</p>\n", encoding="utf-8")
        (self.project / "Page.cshtml.cs").write_text("class PageModel {}\n", encoding="utf-8")
        (self.project / "viewer.html").write_text(
            '<!doctype html><body><input id="workspace-import-file"></body>', encoding="utf-8",
        )
        self.commit_project()
        config_path = self.root / "harness.toml"
        settings = """[project]
root = "app"
worktree_root = "trees"
run_root = "runs"

[[units]]
id = "sample"
source = "source.php"
targets = ["Page.cshtml", "Page.cshtml.cs"]

[diff_viewer]
html = "viewer.html"
"""
        config_path.write_text(settings, encoding="utf-8")
        config = load_config(config_path)
        run_a, run_b = (workflow.prepare_run(config, "sample") for _ in range(2))
        run_a_id, run_b_id = run_a["run_id"], run_b["run_id"]
        record_path = Path(run_a["run_path"]) / "run.json"
        record = json.loads(record_path.read_bytes())
        record.update(status="build_succeeded", completed_capsules=run_a["capsule_count"], build_exit_code=0)
        record_path.write_text(json.dumps(record), encoding="utf-8")

        def junie(command, completion, token, targets, *, report_usage=None):
            proposal, = targets
            proposal.write_text('[{"leftLineNo": 1, "rightLineNo": 1}]', encoding="utf-8")
            completion.write_text(token, encoding="ascii")

        with (
            patch.object(junie_cli, "require_interactive_cli"),
            patch.object(junie_cli, "run_interactive", side_effect=junie),
            self.live_dashboard(config, run_a_id) as url,
        ):
            revision, state = self.ready_event(url)
            self.assertEqual(state["run_id"], run_a_id)
            comparison = self.post(url, "comparison", {}, 201)
            anchored = self.post(url, "control", {"action": "anchors", "revision": revision}, 201)
            self.assertEqual(anchored["count"], 1)
            with urlopen(url + "migration-ledger", timeout=3) as response:
                ledger = json.load(response)
            mapping = ledger["capsules"][0]["sources"][0]["mapping"]
            self.assertEqual((ledger["run_id"], mapping["snapshot_id"]), (run_a_id, anchored["id"]))

            snapshots = {}
            for saved in (comparison, anchored):
                self.assertEqual(saved["run_id"], run_a_id)
                path = f"{url}review/{saved['run_id']}/{saved['id']}"
                snapshot = self.read_comparison(path)
                self.assertEqual(snapshot["snapshot"]["runId"], run_a_id)
                workspace = snapshot["workspace"]
                self.assertEqual([part["fileName"] for part in workspace["leftSegments"]], ["source.php"])
                self.assertEqual(
                    [part["fileName"] for part in workspace["rightSegments"]], ["Page.cshtml", "Page.cshtml.cs"],
                )
                snapshots[path] = snapshot

            revision, _ = self.ready_event(url, after=revision)
            self.post(url, "control", {"action": "open-run", "run_id": run_b_id, "revision": revision}, 202)
            revision, state = self.ready_event(url, after=revision)
            self.assertEqual(state["run_id"], run_b_id)
            for path, snapshot in snapshots.items():
                self.assertEqual(self.read_comparison(path), snapshot)

            with urlopen(url + "settings", timeout=3) as response:
                version = json.load(response)["version"]
            self.post(url, "settings", {"content": settings, "revision": revision, "version": version}, 200)
            _, state = self.ready_event(url, after=revision)
            self.assertEqual(state["status"], "idle")
            self.assertNotIn("run_id", state)
            for path, snapshot in snapshots.items():
                self.assertEqual(self.read_comparison(path), snapshot)

            snapshot_id = comparison["id"]
            review_dir = Path(run_b["run_path"]) / "review-snapshots"
            review_dir.mkdir()
            (review_dir / f"{snapshot_id}.json").write_text(
                json.dumps(snapshots[f"{url}review/{run_a_id}/{snapshot_id}"]), encoding="utf-8",
            )
            record["worktree_path"] = run_b["worktree_path"]
            record_path.write_text(json.dumps(record), encoding="utf-8")
            for review, status in (
                (f"not-a-uuid/{snapshot_id}.json", 409),
                (f"{run_a_id}/invalid.json", 404),
                (f"{run_b_id}/{snapshot_id}.json", 409),
                (f"{run_a_id}/{snapshot_id}.json", 409),
            ):
                with self.subTest(review=review), self.assertRaises(HTTPError) as rejected:
                    urlopen(url + "review/" + review, timeout=3).close()
                with rejected.exception as response:
                    self.assertEqual(response.code, status)
