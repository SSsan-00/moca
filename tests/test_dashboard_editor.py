from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from threading import Thread
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from fixtures import HarnessCase
from migration_harness import dashboard, junie_cli, workflow
from migration_harness.config import load_config

BASE = "class Page\n{\n    int First = 1;\n    int Middle = 0;\n    int Last = 1;\n}\n"


class DashboardEditorTests(HarnessCase):
    def setUp(self):
        super().setUp()
        (self.project / "source.php").write_text("<?php echo 1;", encoding="utf-8")
        (self.project / "Page.cs").write_bytes(BASE.replace("\n", "\r\n").encode("utf-8-sig"))
        self.commit_project()
        self.settings = self.config()
        self.view = dashboard.Dashboard()
        captured = io.StringIO()
        with redirect_stdout(captured):
            server = self.view.server(self.settings, b"", b"")
        self.url = captured.getvalue().splitlines()[0].split()[-1]
        url = urlsplit(self.url)
        self.origin = f"{url.scheme}://{url.netloc}"
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        observer = workflow.run_observer.set(self.view.observe)
        self.addCleanup(workflow.run_observer.reset, observer)

    def request(self, route="editor", payload=None):
        request = Request(
            self.url + route, data=None if payload is None else json.dumps(payload).encode("utf-8"),
            headers={"Origin": self.origin, "Content-Type": "application/json"},
        )
        with urlopen(request, timeout=4) as response:
            return json.load(response)

    def queue_edit(self, state, content, *, resolve=False, target="Page.cs"):
        return self.request(payload={
            "kind": "save", "run_id": state["run_id"], "target": target,
            "base": state["content"], "content": content, "edit_id": (state["edit"] or {}).get("id"),
            "resolve": resolve,
        })

    def test_php_view_reads_the_run_snapshot_and_accepts_only_get(self):
        other = self.project / "other.php"
        other.write_bytes("<?php echo '注文';".encode("cp932"))
        annotated = "<?php\n// 移行ツールのコメント\necho '注文';\n"
        (self.project / "annotated.txt").write_bytes(annotated.encode("euc-jp"))
        (self.project / "annotated").write_text(annotated, encoding="utf-8")
        self.commit_project()
        config_path = self.root / "test.toml"
        with config_path.open("a", encoding="utf-8") as config:
            for name, encoding in (("annotated.txt", "euc-jp"), ("annotated", "utf-8")):
                config.write(f'\n[[units.inputs]]\nrole = "php"\npath = "{name}"\nencoding = "{encoding}"\nstart_line = 1\nend_line = 1\n')
        self.settings.update(load_config(config_path))
        inputs = self.settings["units"][0]["inputs"]
        inputs.append({**inputs[0], "path": "other.php", "encoding": "cp932"})
        prepared = workflow.prepare_run(self.settings, "sample")
        (self.project / "source.php").write_text("<?php echo 'updated after preparation';", encoding="utf-8")
        route = "editor-php?" + urlencode({"run_id": prepared["run_id"]})
        source = self.request(route)
        self.assertEqual(source["sources"], ["source.php", "annotated.txt", "annotated", "other.php"])
        self.assertEqual(source["source"], "source.php")
        self.assertEqual(source["content"], "<?php echo 1;")
        other_source = self.request(route + "&" + urlencode({"source": "other.php"}))
        self.assertEqual(other_source["content"], "<?php echo '注文';")
        for name in ("annotated.txt", "annotated"):
            with self.subTest(source=name):
                annotated_source = self.request(route + "&" + urlencode({"source": name}))
                self.assertEqual(annotated_source["source"], name)
                self.assertEqual(annotated_source["content"], annotated)
        with self.assertRaises(HTTPError) as denied:
            self.request(route + "&" + urlencode({"source": "../Page.cs"}))
        self.assertEqual(denied.exception.code, 409)
        denied.exception.close()
        with self.assertRaises(HTTPError) as method:
            self.request("editor-php", {"kind": "save", "target": "source.php"})
        self.assertEqual(method.exception.code, 404)
        method.exception.close()

    def test_razor_pair_saves_both_targets_at_the_checkpoint(self):
        razor = "@page\n<h1>Orders</h1>\n\n<p>Details</p>\n"
        originals = {"Index.cshtml": razor, "Index.cshtml.cs": BASE}
        updated = {
            "Index.cshtml": razor.replace("Details", "Local details"),
            "Index.cshtml.cs": BASE.replace("Last = 1", "Last = 3"),
        }
        for name, content in originals.items():
            (self.project / name).write_text(content, encoding="utf-8")
        self.commit_project()
        self.settings.update(self.config('targets = ["Index.cshtml", "Index.cshtml.cs"]'))

        def junie(command, completion, token, targets, **kwargs):
            for name, original in originals.items():
                state = self.request("editor?" + urlencode({"target": name}))
                self.assertEqual(state["targets"], list(originals))
                self.assertEqual(state["content"], original)
                self.assertEqual(self.queue_edit(state, updated[name], target=name)["edit"]["status"], "queued")
            razor_target = next(path for path in targets if path.name == "Index.cshtml")
            razor_target.write_text(razor.replace("Orders", "Recent orders"), encoding="utf-8")
            completion.write_text(token, encoding="utf-8")
            return 0

        def build(config, unit, result, record):
            worktree = Path(result["worktree_path"])
            self.assertEqual((worktree / "Index.cshtml").read_text(encoding="utf-8"),
                             updated["Index.cshtml"].replace("Orders", "Recent orders"))
            self.assertEqual((worktree / "Index.cshtml.cs").read_text(encoding="utf-8"), updated["Index.cshtml.cs"])
            return result

        with patch.object(junie_cli, "require_interactive_cli"), patch.object(junie_cli, "run_interactive", side_effect=junie), \
                patch.object(workflow, "_verify_worktree", side_effect=build) as verified:
            workflow.run_with_junie(self.settings, "sample")
        verified.assert_called_once()

    def test_live_edits_and_final_prompt_are_applied_before_build(self):
        prompts = []
        remote = BASE.replace("First = 1", "First = 2")
        local = BASE.replace("Last = 1", "Last = 3")
        merged = remote.replace("Last = 1", "Last = 3")

        def junie(command, completion, token, targets, **kwargs):
            prompts.append(command[-1])
            if len(prompts) == 1:
                state = self.request()
                self.assertEqual(state["content"], BASE)
                with self.assertRaises(HTTPError) as unauthorized:
                    self.queue_edit(state, local, target="../source.php")
                self.assertEqual(unauthorized.exception.code, 409)
                unauthorized.exception.close()
                self.assertEqual(self.queue_edit(state, local)["edit"]["status"], "queued")
                prompts_view = self.request("prompts")
                self.assertEqual(prompts_view["timing"], "checkpoint")
                self.request("prompts", {"run_id": state["run_id"], "text": "Check the date"})
                targets[0].write_bytes(remote.replace("\n", "\r\n").encode("utf-8-sig"))
                state = self.request()
                self.assertEqual(state["content"], remote)
                self.assertEqual(state["edit"]["content"], local)
                self.assertEqual(state["prompts"][0]["status"], "queued")
            else:
                instruction = completion.parent / f"checker-findings-{token}-operator.txt"
                self.assertIn(instruction.name, command[-1])
                self.assertIn("Check the date", instruction.read_text(encoding="utf-8"))
                self.assertEqual(targets[0].read_text(encoding="utf-8-sig"), merged)
                self.assertEqual(self.request()["prompts"][0]["status"], "sending")
            completion.write_text(token, encoding="utf-8")
            return 0

        def build(config, unit, result, record):
            self.assertEqual((Path(result["worktree_path"]) / "Page.cs").read_bytes(),
                             merged.replace("\n", "\r\n").encode("utf-8-sig"))
            self.assertEqual(self.request()["prompts"][0]["status"], "sent")
            return result

        with patch.object(junie_cli, "require_interactive_cli"), patch.object(junie_cli, "run_interactive", side_effect=junie), \
                patch.object(workflow, "_verify_worktree", side_effect=build) as verified:
            workflow.run_with_junie(self.settings, "sample")
        self.assertEqual(len(prompts), 2)
        verified.assert_called_once()

    def test_conflicting_edit_pauses_without_repeating_the_completed_segment(self):
        local = BASE.replace("First = 1", "First = 3")
        remote = BASE.replace("First = 1", "First = 2")
        resolution = BASE.replace("First = 1", "First = 4")

        def junie(command, completion, token, targets, **kwargs):
            self.queue_edit(self.request(), local)
            targets[0].write_text(remote, encoding="utf-8")
            completion.write_text(token, encoding="utf-8")
            return 0

        with patch.object(junie_cli, "require_interactive_cli"), patch.object(junie_cli, "run_interactive", side_effect=junie) as agent, \
                patch.object(workflow, "_verify_worktree", side_effect=lambda config, unit, result, record: result) as verified:
            result = workflow.run_with_junie(self.settings, "sample")
            self.assertEqual(result["status"], "editor_checkpoint")
            self.assertEqual(result["completed_capsules"], 1)
            verified.assert_not_called()
            state = self.request()
            self.assertEqual(state["edit"]["status"], "conflict")
            self.assertEqual((state["content"], state["edit"]["content"]), (remote, local))
            with self.assertRaises(HTTPError) as conflict:
                self.queue_edit(state, resolution)
            conflict.exception.close()
            self.queue_edit(state, resolution, resolve=True)
            workflow.resume_with_junie(self.settings, result["run_id"])
            agent.assert_called_once()
            verified.assert_called_once()
        saved = Path(result["worktree_path"]) / "Page.cs"
        self.assertEqual(saved.read_text(encoding="utf-8"), resolution)

    def test_prompt_queued_in_first_segment_reaches_the_next_segment_once(self):
        (self.project / "source.php").write_text("<?php /* " + "x" * 2100 + " */", encoding="utf-8")
        self.commit_project()
        calls = []

        def junie(command, completion, token, targets, **kwargs):
            instruction = completion.parent / f"checker-findings-{token}-operator.txt"
            calls.append(instruction.read_text(encoding="utf-8") if instruction.is_file() else "")
            if len(calls) == 1:
                state = self.request()
                self.request(payload={"kind": "prompt", "run_id": state["run_id"], "text": "Check the date"})
            completion.write_text(token, encoding="utf-8")
            return 0

        with patch.object(junie_cli, "require_interactive_cli"), patch.object(junie_cli, "run_interactive", side_effect=junie), \
                patch.object(workflow, "_verify_worktree", side_effect=lambda config, unit, result, record: result):
            result = workflow.run_with_junie(self.settings, "sample")
        self.assertGreater(result["completed_capsules"], 1)
        self.assertEqual(len(calls), result["completed_capsules"])
        self.assertIn("Check the date", calls[1])
        self.assertEqual(sum("Check the date" in prompt for prompt in calls), 1)

    def test_progress_accepts_a_prompt_during_build_and_uses_the_latest_file_on_resume(self):
        phases = []
        changed = BASE.replace("Last = 1", "Last = 9")
        repaired = changed.replace("First = 1", "First = 42")

        def junie(command, completion, token, targets, **kwargs):
            phases.append("junie")
            if len(phases) > 1:
                instruction = completion.parent / f"checker-findings-{token}-operator.txt"
                self.assertIn(instruction.name, command[-1])
                self.assertIn("最初の値を変える", instruction.read_text(encoding="utf-8"))
                self.assertEqual(targets[0].read_text(encoding="utf-8-sig"), changed)
                targets[0].write_text(repaired, encoding="utf-8")
            completion.write_text(token, encoding="utf-8")
            return 0

        def build(config, unit, result, record, **kwargs):
            phases.append("build")
            worktree = Path(result["worktree_path"])
            if phases == ["junie", "build"]:
                record["status"] = "building"
                workflow._write_run_record(Path(result["run_path"]) / "run.json", record)
                state = self.request("prompts")
                self.assertEqual(state["timing"], "resume")
                with self.assertRaises(HTTPError) as stale:
                    self.request("prompts", {"run_id": "other-run", "text": "wrong run"})
                self.assertEqual(stale.exception.code, 409)
                stale.exception.close()
                response = self.request("prompts", {"run_id": state["run_id"], "text": "最初の値を変える"})
                self.assertEqual(response["prompts"][0]["status"], "queued")
                record.update(status="build_succeeded", build_exit_code=0)
            else:
                self.assertEqual(self.request("prompts")["prompts"][0]["status"], "sent")
                self.assertEqual((worktree / "Page.cs").read_text(encoding="utf-8-sig"), repaired)
                record.update(status="build_succeeded", build_exit_code=0)
            workflow._write_run_record(Path(result["run_path"]) / "run.json", record)
            return {**result, "status": record["status"]}

        with (
            patch.object(junie_cli, "require_interactive_cli"),
            patch.object(junie_cli, "run_interactive", side_effect=junie),
            patch.object(workflow, "_verify_worktree", side_effect=build),
        ):
            result = workflow.run_with_junie(self.settings, "sample")
            (Path(result["worktree_path"]) / "Page.cs").write_text(changed, encoding="utf-8")
            resumed = workflow.resume_with_junie(self.settings, result["run_id"])
        self.assertEqual(phases, ["junie", "build", "junie", "build"])
        self.assertEqual(resumed["status"], "build_succeeded")
