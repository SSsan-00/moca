from __future__ import annotations

import io
import json
import re
import shutil
import subprocess
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from threading import Event
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from fixtures import HarnessCase
from migration_harness.web import dashboard
from migration_harness.junie import junie_cli
from migration_harness import workflow
from migration_harness.config import load_config
from migration_harness.web.diff_viewer import review_html


class DashboardComparisonRoutesTests(HarnessCase):
    def test_rewind_prompt_and_ledger_agree_on_launch_target_before_adoption(self):
        from migration_harness.context.context_handoff import record_handoff

        t0, t1 = b"class Page {}\n", "class Adopted {}\n".encode("utf-8-sig")
        (self.project / "source.php").write_text("<?php echo 1;\n", encoding="utf-8")
        (self.project / "Page.cs").write_bytes(t0)
        self.commit_project()
        config = self.config()
        run_id = "00000000-0000-4000-8000-000000000164"
        run = Path(config["run_root"]) / run_id
        record_path = run / "run.json"
        notes = []
        calls = 0
        dashboard_url = ""

        def junie(command, completion, token, targets, **_kwargs):
            nonlocal calls
            calls += 1
            agent, = targets
            record = json.loads(record_path.read_bytes())
            worktree = Path(record["worktree_path"])
            adopted = worktree / "Page.cs"
            self.assertEqual(t0, agent.read_bytes())
            if calls == 1:
                self.assertEqual(t0, adopted.read_bytes())
                for i in range(4):
                    notes.append(record_handoff(run, worktree, (Path("Page.cs"),), {}, "php-0001", 1, 1, f"operator memo {i}"))
                self.assertEqual(4, len(workflow.migration_ledger(config, run_id)["unresolved_operator_notes"]))
                agent.write_bytes(t1)
            else:
                self.assertEqual(t1, adopted.read_bytes())
                prompt = command[-1]
                self.assertIn("1 additional pending operator note", prompt)
                self.assertIn(notes[0]["id"], prompt)
                self.assertNotIn(notes[0]["note"], prompt)
                match = re.search(r"launch memo reference ID ([0-9a-f]{64})", prompt)
                self.assertIsNotNone(match)
                receipt = match.group(1)
                junie_cli.launch_input_observer.get()(prompt)
                before = record_path.read_bytes()
                with urlopen(dashboard_url + "migration-ledger?operator_through=1", timeout=3) as response:
                    current = json.load(response)
                path = dashboard_url + "migration-ledger?operator_through=1&operator_receipt="
                with urlopen(path + receipt, timeout=3) as response:
                    historical = json.load(response)
                self.assertEqual([], current["unresolved_operator_notes"])
                self.assertEqual([n["id"] for n in reversed(notes)], [r["id"] for r in historical["unresolved_operator_notes"]])
                self.assertEqual(current["completion"], historical["completion"])
                for invalid in ("0" * 64, receipt + "&operator_receipt=" + receipt):
                    with self.assertRaises(HTTPError) as error:
                        urlopen(path + invalid, timeout=3)
                    self.assertEqual(409, error.exception.code)
                    error.exception.close()
                self.assertEqual(before, record_path.read_bytes())
                self.assertEqual(t1, adopted.read_bytes())
                self.assertEqual(t0, agent.read_bytes())
            completion.write_text(token, encoding="ascii")
            return 0

        def skip_external_build(_config, _unit, result, record):
            record["status"] = "build_succeeded"
            workflow.write_run_record(record_path, record)
            return result

        with (
            patch.object(junie_cli, "require_interactive_cli"),
            patch.object(junie_cli, "run_interactive", side_effect=junie),
            patch.object(workflow, "_verify_worktree", side_effect=skip_external_build),
        ):
            workflow.run_with_junie(config, "sample", run_id=run_id)
            plan = workflow.plan_migration_resume(config, run_id, 1, rewind_to=1)
            with self.live_dashboard(config, run_id) as dashboard_url:
                self.ready_event(dashboard_url)
                result = workflow.resume_with_junie(config, run_id, verification_request={**plan, "id": "rewind"})
        self.assertEqual(2, calls)
        self.assertEqual(1, result["completed_capsules"])

    def test_omitted_operator_note_can_be_retrieved_from_the_same_capsule_scope(self):
        from migration_harness.context.context_handoff import handoff_prompt, record_handoff

        (self.project / "source.php").write_text("<?php\n" + ("echo 2;" + " " * 75 + "\n") * 30, encoding="utf-8")
        (self.project / "Page.cs").write_text("class Page {}", encoding="utf-8")
        self.commit_project()
        config = self.config()
        config["max_context_chars"] = 650
        config["units"][0]["inputs"][0]["end_line"] = None
        prepared = workflow.prepare_run(config, "sample")
        run_id = prepared["run_id"]
        run = Path(prepared["run_path"])
        args = (run, Path(prepared["worktree_path"]), (Path("Page.cs"),), {})
        earlier = record_handoff(*args, "php-0001", 1, 1, "earlier scope only")
        for i in range(3):
            record_handoff(*args, "php-0001", 1, 1, f"other note {i}")
        later = record_handoff(*args, "php-0001", 30, 30, "later scope only", supersedes=earlier["id"])
        before = (run / "run.json").read_bytes()
        prompt = handoff_prompt(*args, through=1)
        self.assertIn("through capsule 1", prompt)
        self.assertIn(earlier["id"], prompt)
        for item in (earlier, later):
            self.assertNotIn(item["note"], prompt)
        with self.live_dashboard(config, run_id) as url:
            self.ready_event(url)
            with urlopen(url + "migration-ledger?operator_through=1", timeout=3) as response:
                scoped = json.load(response)
            with urlopen(url + "migration-ledger", timeout=3) as response:
                full = json.load(response)
            for ledger, expected, excluded in ((scoped, earlier, later), (full, later, earlier)):
                rows = ledger["unresolved_operator_notes"]
                self.assertEqual(4, len(rows))
                self.assertIn((expected["id"], expected["note"]), [(row["id"], row["note"]) for row in rows])
                self.assertNotIn(excluded["id"], [row["id"] for row in rows])
            self.assertEqual(scoped["completion"], full["completion"])
            for query in ("0", "9999", "", "one", "1&operator_through=2"):
                with self.subTest(query=query), self.assertRaises(HTTPError) as error:
                    urlopen(url + "migration-ledger?operator_through=" + query, timeout=3)
                self.assertEqual(409, error.exception.code)
                error.exception.close()
        self.assertEqual(before, (run / "run.json").read_bytes())

    @unittest.skipUnless(shutil.which("node"), "JavaScript runtime is required for the import race regression")
    def test_autoimport_waits_for_async_viewer_initialization_and_dispatches_once(self):
        viewer = self.project / "viewer.html"
        viewer.write_text('<body><input id="workspace-import-file"></body>', encoding="utf-8")
        payload = {
            "kind": "diff-viewer-workspace", "version": 1,
            "workspace": {
                "leftText": "<?php echo '</script>日本語';", "rightText": "class A {}\nclass Page {\n int n = 1;\n}",
                "rightActiveFile": "Page.cs", "rightCursor": {"lineNumber": 3, "column": 1},
                "rightSegments": [{"fileName": "Page.cs", "startLine": 2, "lineCount": 3}],
            },
        }
        html = review_html({"project_root": str(self.project), "diff_viewer_html": viewer.name}, json.dumps(payload).encode())
        bootstrap = html.split(b"<script>", 1)[1].split(b"</script>", 1)[0].decode()
        result = subprocess.run(["node", "-e", r"""
const assert = require("node:assert/strict");
const vm = require("node:vm");
const {bootstrap, payload} = JSON.parse(require("node:fs").readFileSync(0, "utf8"));
for (const mode of ["delayed", "ready", "slow-start", "abandoned", "unsupported", "jump-open", "slow-import"]) {
  let leftReady = mode === "ready", rightReady = leftReady;
  const intervals = new Map(), timeouts = new Map(), listeners = new Map();
  const dispatched = [], warnings = [];
  const observers = new Set();
  let jumpOpen = mode === "jump-open", jumpedTo;
  const editor = {dispatchEvent(event) {
    if (event.key === "g") {
      jumpOpen = !jumpOpen;
      document.activeElement = jumpOpen ? gotoLine : editor;
    }
  }};
  const gotoLine = {dispatchEvent(event) {
    if (event.key === "Escape") jumpOpen = false;
    if (event.key === "Enter") jumpedTo = Number(this.value);
  }};
  const card = {dataset: {file: "Page.cs"}, click() {document.activeElement = editor;}};
  let timerId = 0;
  const input = {dispatchEvent(event) {
    assert(leftReady && rightReady, "load fired before async editor/import initialization");
    dispatched.push(JSON.parse(this.files[0].parts[0]));
  }};
  const document = {
    querySelector(selector) {
      return ({"#workspace-import-file": input,
        "#harness-review-workspace": {textContent: JSON.stringify(payload)},
        "#left-editor .monaco-editor": leftReady ? {} : null,
        "#right-editor .monaco-editor": rightReady ? {} : null,
        "#right-file-cards": {querySelectorAll: () => [card]},
        "#right-goto-line-input": gotoLine})[selector] ?? null;
    },
    createElement: () => ({setAttribute() {}, remove: () => warnings.splice(0)}),
    body: {prepend: item => warnings.push(item.textContent)},
  };
  const context = {document, console,
    window: {addEventListener(type, listener) {
      listeners.set(type, [...(listeners.get(type) ?? []), listener]);
    }},
    setInterval: callback => {intervals.set(++timerId, callback); return timerId;},
    clearInterval: id => intervals.delete(id),
    setTimeout: callback => {timeouts.set(++timerId, callback); return timerId;},
    clearTimeout: id => timeouts.delete(id),
    DataTransfer: class {constructor() {
      if (mode === "unsupported") throw new Error("DataTransfer is unavailable");
      this.files = []; this.items = {add: file => this.files.push(file)};
    }},
    File: class {constructor(parts) {this.parts = parts;}},
    Event: class {},
    KeyboardEvent: class {constructor(type, options) {Object.assign(this, options);}},
    requestAnimationFrame: callback => callback(),
    MutationObserver: class {
      constructor(callback) {this.callback = callback;}
      observe() {observers.add(this);}
      disconnect() {observers.delete(this);}
    },
  };
  vm.runInNewContext(bootstrap, context);
  for (const listener of listeners.get("load") ?? []) listener();
  const tick = () => Array.from(intervals.values()).forEach(callback => callback());
  if (mode === "abandoned") {
    for (const [id, callback] of timeouts) {timeouts.delete(id); callback();}
    for (const listener of listeners.get("pagehide") ?? []) listener();
    leftReady = rightReady = true;
    tick();
    assert.equal(dispatched.length, 0);
    assert.equal(warnings.length, 1);
  } else {
    if (mode === "slow-start") {
      for (const [id, callback] of timeouts) {timeouts.delete(id); callback();}
      assert.equal(warnings.length, 1);
      assert.equal(dispatched.length, 0);
    }
    if (mode !== "ready") {
      tick();
      assert.equal(dispatched.length, 0);
      leftReady = true;
      tick();
      assert.equal(dispatched.length, 0);
      rightReady = true;
    }
    tick(); tick();
    if (mode === "unsupported") {
      assert.equal(dispatched.length, 0);
      assert.equal(warnings.length, 1);
    } else {
      assert.deepEqual(dispatched, [payload]);
      assert.equal(warnings.length, 0);
      // A slow File.text() must not expire the focus observer before import renders.
      if (mode === "slow-import") {
        for (const [id, callback] of timeouts) {timeouts.delete(id); callback();}
      }
      for (const observer of observers) observer.callback();
      assert.equal(jumpedTo, 2, "jump uses the imported second file's local line");
    }
  }
  for (const listener of listeners.get("pagehide") ?? []) listener();
  assert.equal(observers.size, 0);
  assert.equal(intervals.size, 0);
  assert.equal(timeouts.size, 0);
}
"""], input=json.dumps({"bootstrap": bootstrap, "payload": payload}), capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

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
    def ready_event(url, *, after=0, run_id=None):
        request = Request(url + "events" + ("?run_id=" + run_id if run_id else ""), headers={"Last-Event-ID": str(after)})
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

    def test_rule_verification_dashboard_requires_evidence_and_current_version(self):
        (self.project / "source.php").write_text("<?php echo 42;", encoding="utf-8")
        (self.project / "Page.cs").write_text("class Page {}", encoding="utf-8")
        (self.project / "required.md").write_text("Preserve the result.", encoding="utf-8")
        self.commit_project()
        config = self.config()
        config["units"][0]["inputs"].append({
            "path": "required.md", "role": "migration_rules", "usage": "required", "encoding": "utf-8",
            "start_line": 1, "end_line": None, "shared": True,
        })
        run = workflow.prepare_run(config, "sample")
        run_id = run["run_id"]
        with self.live_dashboard(config, run_id) as url:
            self.ready_event(url)
            endpoint = "rule-review?run_id=" + run_id
            with urlopen(url + endpoint, timeout=5) as response:
                initial = json.load(response)
            rule = initial["rules"][0]
            self.assertEqual(rule["verification_state"], "human_review_needed")
            decision = {"run_id": run_id, "rule": rule["file"], "version": initial["version"],
                        "kind": "passed", "start": 1, "end": 1, "reason": "Compared C# with PHP."}
            with self.assertRaises(HTTPError) as incomplete:
                self.post(url, "rule-review", decision, 200)
            self.assertEqual(incomplete.exception.code, 409)
            decision["reference"] = "source.php:1, Page.cs:1"
            verified = self.post(url, "rule-review", decision, 200)
            self.assertEqual(verified["rules"][0]["verification_state"], "passed")
            (Path(run["worktree_path"]) / "Page.cs").write_text("class Changed {}", encoding="utf-8")
            with self.assertRaises(HTTPError) as stale:
                self.post(url, "rule-review", {**decision, "version": verified["version"]}, 200)
            self.assertEqual(stale.exception.code, 409)
            with urlopen(url + endpoint, timeout=5) as response:
                current = json.load(response)
            self.assertEqual(current["rules"][0]["verification_state"], "human_review_needed")

    def test_comparisons_stay_with_creating_run_after_open_run_and_settings_save(self):
        (self.project / "source.php").write_text("<?php echo 'Hello';\n", encoding="utf-8")
        (self.project / "earlier-example.php").write_text("<?php echo 'Hello';\n", encoding="utf-8")
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

[[project.references]]
role = "reference_php"
path = "earlier-example.php"

[[units]]
id = "sample"
source = "source.php"
targets = ["Page.cshtml", "Page.cshtml.cs"]

[diff_viewer]
html = "viewer.html"
"""
        config_path.write_text(settings, encoding="utf-8")
        config = load_config(config_path)
        run_a = workflow.prepare_run(config, "sample")
        (self.project / "source.php").write_text("<?php echo 'Updated';\n", encoding="utf-8")
        run_b = workflow.prepare_run(config, "sample")
        run_a_id, run_b_id = run_a["run_id"], run_b["run_id"]
        record_path = Path(run_a["run_path"]) / "run.json"
        record = json.loads(record_path.read_bytes())
        record.update(status="build_succeeded", completed_capsules=run_a["capsule_count"], build_exit_code=0)
        record_path.write_text(json.dumps(record), encoding="utf-8")

        def junie(command, completion, token, targets, *, report_usage=None, prefer_task=False):
            self.assertTrue(prefer_task)
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
            plain = self.read_comparison(f"{url}review/{run_a_id}/{comparison['id']}")["workspace"]
            self.assertEqual([part["fileName"] for part in plain["leftSegments"]], ["source.php"])
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
                self.assertEqual(workspace["leftText"], "<?php echo 'Hello';\n")
                self.assertEqual(
                    [part["fileName"] for part in workspace["rightSegments"]], ["Page.cshtml", "Page.cshtml.cs"],
                )
                snapshots[path] = snapshot

            revision, _ = self.ready_event(url, after=revision)
            self.post(url, "control", {"action": "open-run", "run_id": run_b_id, "revision": revision}, 202)
            _, unchanged = self.ready_event(url)
            self.assertEqual(unchanged["run_id"], run_a_id)
            revision, state = self.ready_event(url, run_id=run_b_id)
            self.assertEqual(state["run_id"], run_b_id)
            comparison_b = self.post(url, "comparison", {"run_id": run_b_id}, 201)
            workspace_b = self.read_comparison(f"{url}review/{run_b_id}/{comparison_b['id']}")["workspace"]
            self.assertEqual([part["fileName"] for part in workspace_b["leftSegments"]], ["source.php"])
            self.assertEqual(workspace_b["leftText"], "<?php echo 'Updated';\n")
            self.assertEqual(
                [part["fileName"] for part in workspace_b["rightSegments"]], ["Page.cshtml", "Page.cshtml.cs"],
            )
            for path, snapshot in snapshots.items():
                self.assertEqual(self.read_comparison(path), snapshot)

            with urlopen(url + "settings", timeout=3) as response:
                version = json.load(response)["version"]
            self.post(url, "settings", {"content": settings, "revision": revision, "version": version}, 200)
            _, state = self.ready_event(url)
            self.assertEqual(state["run_id"], run_a_id)
            self.assertEqual(state["status"], "build_succeeded")
            for path, snapshot in snapshots.items():
                self.assertEqual(self.read_comparison(path), snapshot)

            snapshot_id = comparison["id"]
            review_dir = Path(run_b["run_path"]) / "review-snapshots"
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
