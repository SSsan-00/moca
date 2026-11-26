from __future__ import annotations

import hashlib
import io
import json
import re
import subprocess
import sys
import textwrap
from contextlib import redirect_stdout
from http.client import HTTPConnection
from pathlib import Path
from select import select
from threading import Thread
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from tests.support.fixtures import HarnessCase, prepare_fixture
from migration_harness.web import dashboard, dashboard_editor
from migration_harness.runs import editor_queue, run_records, worktree_targets
from migration_harness.runs.editor_view import update_history_delta
from migration_harness.junie import junie_cli
from migration_harness import workflow
from migration_harness import errors, execution
from migration_harness.runs import editor_queue as editor_queue_store
from migration_harness.verification import repairs
from migration_harness.config import load_config
from migration_harness.execution import control_scope
from migration_harness.verification import build_receipt

BASE = "class Page\n{\n    int First = 1;\n    int Middle = 0;\n    int Last = 1;\n}\n"


class EditorHistoryTests(TestCase):
    def test_delta_removes_vanished_entries_but_never_uses_another_runs_version(self):
        versions = {}
        first = {"run_id": "A", "prompts": [{"id": "p", "text": "old"}], "conversation": [{"id": "c"}]}
        update_history_delta(first, None, versions)
        latest = {"run_id": "A", "prompts": [{"id": "p", "text": "new"}], "conversation": []}
        update_history_delta(latest, first["history_version"], versions)
        self.assertEqual(latest["history_delta"], {
            "prompts": {"changed": [{"id": "p", "text": "new"}], "removed": []},
            "conversation": {"changed": [], "removed": ["c"]},
        })

        other = {"run_id": "B", "prompts": [{"id": "p", "text": "old"}], "conversation": [{"id": "c"}]}
        update_history_delta(other, first["history_version"], versions)
        self.assertNotEqual(other["history_version"], first["history_version"])
        self.assertNotIn("history_delta", other)
        self.assertEqual(other["prompts"], first["prompts"])
        self.assertEqual(other["conversation"], first["conversation"])


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
        observer = run_records.run_observer.set(self.view.observe)
        self.addCleanup(run_records.run_observer.reset, observer)

    def request(self, route="editor", payload=None):
        request = Request(
            self.url + route, data=None if payload is None else json.dumps(payload).encode("utf-8"),
            headers={"Origin": self.origin, "Content-Type": "application/json"},
        )
        with urlopen(request, timeout=4) as response:
            return json.load(response)

    def test_cached_run_cards_keep_preflight_holds_distinct_from_a_failed_build_without_sending_diagnostics(self):
        record = {"status": "build_failed", "roslyn_preflight": {"status": "blocked", "findings": ["large report"]}}
        listed = dashboard.Dashboard.list_record(record)
        self.assertEqual(listed["review_kind"], "roslyn_preflight")
        self.assertNotIn("roslyn_preflight", listed)
        self.view.observe(listed)
        self.assertEqual(self.view.state["review_kind"], "roslyn_preflight")
        self.view.observe(record | {"build_exit_code": 1})
        self.assertNotIn("review_kind", self.view.state)

    def test_recovered_build_draft_saves_against_original_raw_version_and_invalidates_verification(self):
        self.enterContext(patch.object(junie_cli, "run_interactive", side_effect=AssertionError("unexpected Junie call")))
        (self.project / "App.csproj").write_text(
            '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><TargetFramework>net9.0</TargetFramework>'
            '</PropertyGroup></Project>', encoding="utf-8",
        )
        self.commit_project()
        self.settings.update(checker_command=["fixture-checker"], build_project="App.csproj")
        prepared = self.finished_run()
        run_id = prepared["run_id"]
        run_path, worktree, _, _ = run_records.load_saved_run(self.settings, run_id)
        target = worktree / "Page.cs"
        before = target.read_bytes()
        dotnet = self.root / "fixture-sdk" / "dotnet"
        pack = dotnet.parent / "packs/Microsoft.NETCore.App.Ref"
        pack.mkdir(parents=True)
        dotnet.write_bytes(b"fixture launcher")
        (pack / "fixture.dll").write_bytes(b"fixture reference")
        self.enterContext(patch.object(build_receipt, "shutil", SimpleNamespace(which=lambda _: str(dotnet))))

        def process(command, **kwargs):
            if command[:2] == ["dotnet", "build"]:
                for name, content in {"bin/Debug/net9.0/App.dll": b"compiled fixture",
                                      "obj/project.assets.json": b'{"libraries":{}}'}.items():
                    artifact = worktree / name
                    artifact.parent.mkdir(parents=True, exist_ok=True)
                    artifact.write_bytes(content)
            else:
                self.assertEqual(command, ["fixture-checker"])
            return subprocess.CompletedProcess(command, 0)

        with patch.object(execution, "run_process", side_effect=process) as execute, redirect_stdout(io.StringIO()):
            self.assertEqual(workflow.resume_with_junie(self.settings, run_id)["status"], "checker_succeeded")
        self.assertEqual(execute.call_count, 2)
        self.view.ready(self.settings, run_records.load_saved_run(self.settings, run_id)[2])
        checker_route = "resume-plan?" + urlencode({"run_id": run_id, "start_stage": "checker"})
        try:
            old_plan = self.request(checker_route)
        except HTTPError as error:
            with error:
                self.fail(error.read().decode("utf-8"))
        self.assertEqual(old_plan["start_stage"], "checker")
        self.assertTrue(workflow.migration_ledger(self.settings, run_id)["completion"]["verification_passed"])
        base = before.decode("utf-8-sig").replace("\r\n", "\n")
        request = {"kind": "save", "run_id": run_id, "target": "Page.cs", "base": base,
                   "content": base + "// recovered\n", "base_version": hashlib.sha256(before).hexdigest(),
                   "save_id": "a" * 32}
        other_run = self.finished_run()["run_id"]
        with self.assertRaises(HTTPError) as raised:
            self.request(payload=request)
        self.assertEqual(raised.exception.code, 409)
        raised.exception.close()
        self.assertNotEqual(other_run, run_id)

        self.view.observe(run_records.load_saved_run(self.settings, run_id)[2])
        self.view.ready(self.settings, self.view.state)
        target.write_bytes(base.encode("utf-8"))
        with self.assertRaises(HTTPError) as raised:
            self.request(payload=request)
        self.assertEqual(raised.exception.code, 409)
        raised.exception.close()
        self.assertEqual(target.read_bytes(), base.encode("utf-8"))

        target.write_bytes(before)
        saved = self.request(payload=request)
        self.assertEqual(saved["accepted_save_id"], request["save_id"])
        self.assertFalse(saved["queued"])
        self.assertIn("// recovered", target.read_text(encoding="utf-8-sig"))
        self.assertEqual(self.request(payload=request)["accepted_save_id"], request["save_id"])
        self.assertEqual(run_records.load_saved_run(self.settings, run_id)[2]["status"], "checker_succeeded")
        self.assertFalse(workflow.migration_ledger(self.settings, run_id)["completion"]["verification_passed"])
        with self.assertRaises(HTTPError) as raised:
            self.request(checker_route)
        self.assertEqual(raised.exception.code, 409)
        self.assertIn("再ビルド", raised.exception.read().decode("utf-8"))
        raised.exception.close()
        with patch.object(execution, "run_process") as execute, self.assertRaises(ValueError):
            workflow.resume_with_junie(self.settings, run_id, verification_request={**old_plan, "id": "old-checker"})
        execute.assert_not_called()
        with editor_queue.editor_queue(run_path) as queue:
            self.assertFalse(queue["edits"])

    def test_syntax_diagnoses_only_the_requested_unsaved_target_without_recording_an_edit(self):
        result = self.finished_run()
        run_id = result["run_id"]
        run_path, worktree, _, _ = run_records.load_saved_run(self.settings, run_id)
        before = {p: p.read_bytes() if p.is_file() else None for p in
                  (worktree / "Page.cs", run_path / "agent-workspace/Page.cs", run_path / "editor.json", run_path / "run.json")}
        payload = {"run_id": run_id, "target": "Page.cs", "content": "class 未保存 {\n", "version": "draft:3"}
        route = "roslyn-diagnostics"
        with patch("migration_harness.verification.roslyn_syntax.editor_diagnostics",
                   return_value={"target": "Page.cs", "version": "draft:3", "status": "errors"}) as diagnose:
            for changes in ({"target": "source.php"}, {"target": "../Page.cs"}, {"target": "Other.cs"},
                            {"content": None}, {"run_id": "stale"}, {"version": ""}):
                with self.subTest(changes=changes), self.assertRaises(HTTPError) as error:
                    self.request(route, payload | changes)
                self.assertEqual(error.exception.code, 409)
                error.exception.close()
            denied = Request(self.url + route, b"", headers={"Origin": "http://localhost"})
            stale = Request(self.url + route, json.dumps(payload).encode(), headers={
                "Origin": self.origin, "Content-Type": "application/json", "X-Run-ID": "stale",
            })
            for scenario, request, expected in (("origin", denied, 403), ("run", stale, 409)):
                with self.subTest(scenario=scenario):
                    with self.assertRaises(HTTPError) as error:
                        urlopen(request, timeout=4)
                    with error.exception as response:
                        self.assertEqual(response.code, expected)
            diagnose.assert_not_called()
            self.assertEqual(self.request(route, payload)["version"], "draft:3")
            diagnose.assert_called_once_with("Page.cs", payload["content"], "draft:3")
        self.assertEqual(before, {p: p.read_bytes() if p.is_file() else None for p in before})

    def queue_edit(self, state, content, *, resolve=False, target="Page.cs"):
        return self.request(payload={
            "kind": "save", "run_id": state["run_id"], "target": target,
            "base": state["content"], "content": content, "edit_id": (state["edit"] or {}).get("id"),
            "resolve": resolve,
        })

    def finished_run(self):
        def junie(command, completion, token, targets, **kwargs):
            completion.write_text(token)
            return 0

        def verify(config, unit, result, record):
            record.update(status="build_succeeded", build_exit_code=0)
            run_records.write_run_record(Path(result["run_path"]) / "run.json", record)
            return {**result, "status": "build_succeeded"}

        with patch.object(junie_cli, "require_interactive_cli"), \
                patch.object(junie_cli, "run_interactive", side_effect=junie), \
                patch.object(workflow, "_verify_worktree", side_effect=verify):
            result = workflow.run_with_junie(self.settings, "sample")
        self.view.ready(self.settings, result)
        return result

    def test_edits_can_be_saved_during_build_check_repair_scripts_stopping_and_anchor_generation(self):
        result = self.finished_run()
        run_path, worktree, record, _ = run_records.load_saved_run(self.settings, result["run_id"])
        original = (worktree / "Page.cs").read_bytes()
        cases = [(phase, {}) for phase in ("building", "checking", "repairing", "script_running")]
        cases += [("junie_running", {"stop_requested": True}),
                  ("build_succeeded", {"generating_anchors": True})]
        for index, (phase, flags) in enumerate(cases, 2):
            with self.subTest(phase=phase, flags=flags):
                record["status"] = phase
                run_records.write_run_record(run_path / "run.json", record)
                self.view.begin_operation()
                self.view.publish({**self.view.state, **flags})
                state = self.request()
                self.assertTrue(state["can_save"])
                self.assertEqual(state["save_timing"], "repair_restart" if phase == "repairing" else "resume")
                text = BASE.replace("Last = 1", f"Last = {index}")
                saved = self.queue_edit(state, text)
                self.assertEqual(saved["edit"]["content"], text)
                self.assertEqual(self.request()["edit"], saved["edit"])
                self.assertEqual((worktree / "Page.cs").read_bytes(), original)
                self.assertEqual((run_path / "agent-workspace/Page.cs").read_bytes(), original)
                with self.assertRaises(HTTPError) as stale:
                    self.queue_edit(state, "stale edit")
                self.assertEqual(stale.exception.code, 409)
                stale.exception.close()

    def test_build_time_edits_remain_saved_when_idle_and_merge_on_resume_without_junie(self):
        result = self.finished_run()
        run_path, worktree, record, _ = run_records.load_saved_run(self.settings, result["run_id"])
        record["status"] = "building"
        run_records.write_run_record(run_path / "run.json", record)
        self.view.begin_operation()
        self.queue_edit(self.request(), BASE.replace("Last = 1", "Last = 3"))
        record["status"] = "build_succeeded"
        run_records.write_run_record(run_path / "run.json", record)
        self.view.ready(self.settings, record)
        state = self.request()
        self.assertTrue(state["queued"])
        saved = self.queue_edit(state, BASE.replace("Last = 1", "Last = 4"))
        self.assertEqual(saved["save_timing"], "resume")
        current = BASE.replace("First = 1", "First = 2")
        (worktree / "Page.cs").write_bytes(current.replace("\n", "\r\n").encode("utf-8-sig"))

        def verify(config, unit, result, record):
            self.assertEqual((worktree / "Page.cs").read_bytes(),
                             current.replace("Last = 1", "Last = 4").replace("\n", "\r\n").encode("utf-8-sig"))
            self.assertIsNone(self.request()["edit"])
            return result

        self.view.begin_operation()
        with patch.object(junie_cli, "require_interactive_cli", side_effect=AssertionError("Junie is not needed")), \
                patch.object(junie_cli, "run_interactive", side_effect=AssertionError("Junie is not needed")), \
                patch.object(workflow, "_verify_worktree", side_effect=verify) as verified:
            workflow.resume_with_junie(self.settings, result["run_id"])
        verified.assert_called_once()

    def test_operation_starting_during_save_keeps_the_edit_without_changing_running_inputs(self):
        result = self.finished_run()
        state = self.request()
        read_target = dashboard_editor.editor_target

        def start_operation(*args):
            value = read_target(*args)
            self.view.begin_operation()
            return value

        with patch.object(dashboard_editor, "editor_target", side_effect=start_operation):
            saved = self.queue_edit(state, BASE.replace("Last = 1", "Last = 4"))
        self.assertEqual(saved["edit"]["content"], BASE.replace("Last = 1", "Last = 4"))
        self.assertEqual(saved["save_timing"], "resume")
        self.assertEqual((Path(result["worktree_path"]) / "Page.cs").read_text(encoding="utf-8-sig"), BASE)

    def test_saving_and_sending_during_repair_automatically_hands_off_and_rebuilds(self):
        for supported, handoffs, cancelled in ((True, 1, False), (False, 1, False), (True, 2, False), (True, 1, True)):
            with self.subTest(supported=supported, handoffs=handoffs, cancelled=cancelled):
                result = self.finished_run()
                run_path, worktree, record, _ = run_records.load_saved_run(self.settings, result["run_id"])
                self.settings["max_repair_attempts"] = 1
                self.view.begin_operation()
                control = self.view.control
                control.trusted_junie_workspaces.add(str(run_path / "agent-workspace"))
                processes, builds, sent = [], [], []

                def request_permission(request):
                    if request is None:
                        return None
                    from concurrent.futures import Future
                    state = self.request()
                    self.assertEqual(state["save_timing"], "repair_restart")
                    text = state["content"].replace("Last = 1", "Last = 42") if not sent else state["content"].replace("Middle = 0", "Middle = 7")
                    saved = self.queue_edit(state, text)
                    self.assertEqual(saved["prompt_timing"], "repair_restart")
                    instruction = "保存した Last = 42 を維持して再ビルドしてください。" if not sent else "Middle = 7 も維持してください。"
                    response = self.request("prompts", {"run_id": result["run_id"], "text": instruction})
                    self.assertEqual(response["timing"], "repair_restart")
                    sent.append(instruction)
                    if cancelled:
                        control.cancelled.set()
                    reply = Future()
                    reply.set_result("allow")
                    return reply

                control.on_junie_request = request_permission
                launch = junie_cli._launch_process

                def launch_agent(command, **streams):
                    self.assertTrue(all(process.poll() is not None for process in processes))
                    root = Path(command[command.index("--project") + 1])
                    handing_off = len(processes) < handoffs
                    if len(processes):
                        self.assertEqual(self.request()["prompts"][0]["status"], "sending")
                        self.assertEqual(self.request()["edit"], None)
                        self.assertEqual(len(run_records.load_saved_run(self.settings, result["run_id"])[2].get("repair_history", [])), 0)
                    script = f"import os; os.chdir({str(root)!r})\nsupported = {supported!r}\nhanding_off = {handing_off!r}\n" + textwrap.dedent("""
                        import json, re, sys
                        from pathlib import Path
                        def read():
                            return json.loads(sys.stdin.readline())
                        def send(**message):
                            print(json.dumps(dict(jsonrpc="2.0", **message)), flush=True)
                        assert read()["method"] == "initialize"
                        send(id=1, result={"protocolVersion": 1, "_meta": {"steering": {"supported": supported}}})
                        assert read()["method"] == "session/new"
                        send(id=2, result={"sessionId": "repair"})
                        request = read()
                        assert request["method"] == "session/prompt"
                        prompt = request["params"]["prompt"][0]["text"]
                        target = Path("Page.cs")
                        target.write_text(target.read_text(encoding="utf-8-sig").replace("First = 1", "First = 2"), encoding="utf-8")
                        if handing_off:
                            send(id=100, method="session/request_permission", params={"sessionId": "repair",
                                 "toolCall": {"toolCallId": "ready", "title": "修正を継続"},
                                 "options": [{"optionId": "allow", "name": "許可", "kind": "allow_once"}]})
                            sys.stdin.read()
                        else:
                            assert "Last = 42" in target.read_text(encoding="utf-8-sig")
                            instruction = re.search(r"Read and follow the operator's instruction in `([^`]+)`", prompt)
                            assert instruction and "保存した Last = 42" in Path(instruction[1]).read_text(encoding="utf-8")
                            match = re.search(r"write exactly `([a-f0-9]+)` as plain text to `(migration-complete-[^`]+)`", prompt)
                            assert match
                            Path(match[2]).write_text(match[1])
                            send(id=3, result={"stopReason": "end_turn"})
                            sys.stdin.read()
                    """)
                    process = launch([sys.executable, "-u", "-c", script], **streams)
                    processes.append(process)
                    return process

                def build(command, **kwargs):
                    self.assertEqual(command[:2], ["dotnet", "build"])
                    builds.append((worktree / "Page.cs").read_text(encoding="utf-8-sig"))
                    failed = len(builds) == 1
                    kwargs["stdout"].write(b"Page.cs(1,1): error CS1002: expected semicolon\n" if failed else b"Build succeeded\n")
                    return subprocess.CompletedProcess(command, int(failed))

                with control_scope(control), \
                        patch.object(junie_cli, "require_interactive_cli"), \
                        patch.object(junie_cli, "_launch_process", side_effect=launch_agent), \
                        patch.object(execution, "run_process", side_effect=build), \
                        patch.dict("os.environ", {"JUNIE_HOME": str(self.root / "junie-home")}):
                    if cancelled:
                        with self.assertRaises(workflow.RunInterrupted):
                            workflow.resume_with_junie(self.settings, result["run_id"])
                    else:
                        completed = workflow.resume_with_junie(self.settings, result["run_id"])
                if cancelled:
                    self.assertEqual(len(processes), 1)
                    self.assertIsNotNone(processes[0].poll())
                    self.assertEqual(len(builds), 1)
                    self.assertEqual(self.request()["edit"]["status"], "queued")
                    self.assertEqual(self.request()["prompts"][0]["status"], "queued")
                    continue
                self.assertEqual(completed["status"], "build_succeeded", completed.get("error"))
                self.assertEqual(len(builds), 2)
                self.assertEqual(len(processes), handoffs + 1)
                self.assertIn("First = 2", builds[-1])
                self.assertIn("Last = 42", builds[-1])
                if handoffs == 2:
                    self.assertIn("Middle = 7", builds[-1])
                self.assertTrue(all(prompt["status"] == "sent" for prompt in self.request("prompts")["prompts"]))
                self.assertEqual(len(run_records.load_saved_run(self.settings, result["run_id"])[2]["repair_history"]), 1)

    def test_repair_handoff_handles_completion_race_conflict_and_unsafe_changes(self):
        for scenario in ("completed", "conflict", "unsafe"):
            with self.subTest(scenario=scenario):
                result = self.finished_run()
                run_path, worktree, record, _ = run_records.load_saved_run(self.settings, result["run_id"])
                record["repair_history"] = []
                self.view.begin_operation()
                calls = []

                def junie(command, completion, token, targets, **kwargs):
                    calls.append(command)
                    if len(calls) == 1:
                        self.queue_edit(self.request(), BASE.replace("Last = 1", "Last = 42"))
                        self.request("prompts", {"run_id": result["run_id"], "text": "保存した内容で再ビルドしてください。"})
                        targets[0].write_text(BASE.replace("First = 1", "First = 2") if scenario != "conflict"
                                              else BASE.replace("Last = 1", "Last = 99"))
                        if scenario == "unsafe":
                            (completion.parent / "unexpected.txt").write_text("unexpected")
                        if scenario != "completed":
                            raise junie_cli.JunieEditHandoff()
                    completion.write_text(token)
                    return 0

                with patch.object(junie_cli, "require_interactive_cli"), \
                        patch.object(junie_cli, "run_interactive", side_effect=junie):
                    if scenario == "completed":
                        repairs.attempt_repair(self.settings, (Path("Page.cs"),), (Path("Page.cs"),), worktree,
                                                 run_path, "diagnostic", record, {"kind": "build"})
                        self.assertEqual(len(calls), 2)
                        self.assertIn("Last = 42", (worktree / "Page.cs").read_text(encoding="utf-8-sig"))
                    else:
                        with self.assertRaises(errors.CheckerRepairBlocked) as blocked:
                            repairs.attempt_repair(self.settings, (Path("Page.cs"),), (Path("Page.cs"),), worktree,
                                                     run_path, "diagnostic", record, {"kind": "build"})
                        self.assertEqual(blocked.exception.reason, "operator_conflict" if scenario == "conflict" else "handoff_unsafe")
                        self.assertEqual(len(calls), 1)
                        self.assertEqual(record.get("repair_history", []), [])
                        state = self.request()
                        self.assertEqual(state["edit"]["status"], "conflict" if scenario == "conflict" else "queued")
                        self.assertEqual(state["edit"]["content"], BASE.replace("Last = 1", "Last = 42"))
                        self.assertEqual(state["prompts"][0]["status"], "queued")

    def test_roslyn_repair_handoff_rediagnoses_the_verified_merged_version(self):
        invalid = BASE.replace("    int Middle = 0;", "    int Broken = ;\n    int Middle = 0;")
        for scenario in ("remaining", "clear", "external", "conflict", "unsafe"):
            with self.subTest(scenario=scenario):
                prepared = self.finished_run()
                run_id = prepared["run_id"]
                run_path, worktree, _, _ = run_records.load_saved_run(self.settings, run_id)
                target = worktree / "Page.cs"
                target.write_text(invalid, encoding="utf-8")
                self.settings.update(checker_command=None, max_repair_attempts=1)
                self.view.begin_operation()
                calls, builds = [], []
                instruction = "Keep Last = 42 when continuing."
                merge = editor_queue_store.apply_editor_edits

                def edit_handoff(*args):
                    applied = merge(*args)
                    if scenario == "external" and applied:
                        target.write_text(target.read_text(encoding="utf-8-sig") + "// external edit\n", encoding="utf-8")
                    return applied

                def junie(command, completion, token, targets, **kwargs):
                    payload = command[-1]
                    path = re.search(r"Read `(checker-findings-[a-f0-9]+[.]txt)` and repair", payload)
                    diagnostic = (completion.parent / path[1]).read_text(encoding="utf-8")
                    calls.append(diagnostic)
                    if len(calls) == 1:
                        self.assertIn("Page.cs(4,", diagnostic)
                        self.assertIn("CS1525", diagnostic)
                        state = self.request()
                        self.queue_edit(state, state["content"].replace("Last = 1", "Last = 42"))
                        self.request("prompts", {"run_id": run_id, "text": instruction})
                        source = "// Junie partial repair\n" + invalid.replace("First = 1", "First = 2")
                        if scenario == "clear":
                            source = source.replace("Broken = ;", "Broken = 7;")
                        if scenario == "conflict":
                            source = source.replace("Last = 1", "Last = 99")
                        if scenario == "unsafe":
                            (completion.parent / "unlisted.txt").write_text("outside target")
                        targets[0].write_text(source, encoding="utf-8")
                        raise junie_cli.JunieEditHandoff()
                    current = targets[0].read_text(encoding="utf-8-sig")
                    self.assertIn("First = 2", current)
                    self.assertIn("Last = 42", current)
                    detail = re.search(r"operator's instruction in `([^`]+)`", payload)
                    self.assertIn(instruction, (completion.parent / detail[1]).read_text(encoding="utf-8"))
                    self.assertEqual(self.request()["prompts"][0]["status"], "sending")
                    self.assertEqual(run_records.load_saved_run(self.settings, run_id)[2].get("repair_history", []), [])
                    if scenario == "remaining":
                        self.assertIn("Page.cs(5,", diagnostic)
                        self.assertIn("CS1525", diagnostic)
                        targets[0].write_text(current.replace("Broken = ;", "Broken = 7;"), encoding="utf-8")
                    else:
                        self.assertNotIn("CS1525", diagnostic)
                        self.assertIn("No C# syntax errors", diagnostic)
                    completion.write_text(token, encoding="ascii")
                    return 0

                def build(command, **kwargs):
                    self.assertEqual(command[:2], ["dotnet", "build"])
                    builds.append(target.read_text(encoding="utf-8-sig"))
                    kwargs["stdout"].write(b"Build succeeded")
                    return subprocess.CompletedProcess(command, 0)

                with (
                    patch.object(junie_cli, "require_interactive_cli"),
                    patch.object(junie_cli, "run_interactive", side_effect=junie),
                    patch.object(editor_queue_store, "apply_editor_edits", side_effect=edit_handoff),
                    patch.object(execution, "run_process", side_effect=build), redirect_stdout(io.StringIO()),
                ):
                    result = workflow.resume_with_junie(self.settings, run_id)

                saved = run_records.load_saved_run(self.settings, run_id)[2]
                if scenario == "external":
                    with editor_queue.editor_queue(run_path) as queue:
                        state = {"prompts": queue["prompts"][:]}
                else:
                    state = self.request()
                if scenario in {"remaining", "clear"}:
                    self.assertEqual(result["status"], "build_succeeded", saved.get("roslyn_preflight", {}).get("repair_error"))
                    self.assertEqual((len(calls), len(builds)), (2, 1))
                    self.assertEqual(len(saved["repair_history"]), 1)
                    self.assertEqual(state["prompts"][0]["status"], "sent")
                    self.assertEqual(state["edit"], None)
                    self.assertIn("Broken = 7;", builds[0])
                    self.assertIn("First = 2", builds[0])
                    self.assertIn("Last = 42", builds[0])
                else:
                    self.assertEqual(result["status"], "build_failed")
                    self.assertEqual((len(calls), len(builds)), (1, 0))
                    self.assertEqual(state["prompts"][0]["status"], "queued")
                    self.assertEqual(saved.get("repair_history", []), [])
                    if scenario == "external":
                        self.assertIn("// external edit", target.read_text(encoding="utf-8-sig"))
                        self.assertIn("Last = 42", target.read_text(encoding="utf-8-sig"))
                    else:
                        self.assertEqual(state["edit"]["status"], "conflict" if scenario == "conflict" else "queued")

    def test_edits_submitted_as_repair_finishes_are_applied_before_rebuilding(self):
        self.settings.update(junie_model="sonnet", junie_effort="high")
        result = self.finished_run()
        self.settings.update(junie_model="opus", junie_effort="low")
        run_path, worktree, _, _ = run_records.load_saved_run(self.settings, result["run_id"])
        self.view.begin_operation()
        calls, builds = [], []
        charge = repairs._charge_repair

        def finish_repair(record):
            charge(record)
            if len(calls) == 1 and not self.request()["edit"]:
                state = self.request()
                self.queue_edit(state, state["content"].replace("Last = 1", "Last = 42"))
                self.request("prompts", {"run_id": result["run_id"], "text": "保存した修正で再ビルドしてください。"})

        def junie(command, completion, token, targets, **kwargs):
            calls.append(command)
            self.assertEqual(command[command.index("--model") + 1], "sonnet")
            self.assertEqual(command[command.index("--effort") + 1], "high")
            if len(calls) == 1:
                targets[0].write_text(BASE.replace("First = 1", "First = 2"))
            else:
                self.assertIn("Last = 42", targets[0].read_text(encoding="utf-8-sig"))
                self.assertIn("operator's follow-up instructions", command[-1])
            completion.write_text(token)
            return 0

        def build(command, **kwargs):
            builds.append((worktree / "Page.cs").read_text(encoding="utf-8-sig"))
            failed = len(builds) == 1
            kwargs["stdout"].write(b"Page.cs(1,1): error CS1002: expected semicolon\n" if failed else b"Build succeeded\n")
            return subprocess.CompletedProcess(command, int(failed))

        with patch.object(junie_cli, "require_interactive_cli"), \
                patch.object(junie_cli, "run_interactive", side_effect=junie), \
                patch.object(repairs, "_charge_repair", side_effect=finish_repair), \
                patch.object(execution, "run_process", side_effect=build):
            completed = workflow.resume_with_junie(self.settings, result["run_id"])
        self.assertEqual(completed["status"], "build_succeeded", completed.get("error"))
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(builds), 2)
        self.assertIn("First = 2", builds[-1])
        self.assertIn("Last = 42", builds[-1])
        self.assertEqual(self.request()["prompts"][0]["status"], "sent")

    def test_missing_targets_can_be_saved_when_idle_or_before_junie_starts(self):
        for busy in (False, True):
            with self.subTest(busy=busy):
                name = f"Pages/New{busy}.cs"
                self.settings.update(self.config(f'target = "{name}"'))
                result = prepare_fixture(self.settings, "sample")
                _, _, record, _ = run_records.load_saved_run(self.settings, result["run_id"])
                self.view.ready(self.settings, record)
                if busy:
                    self.view.begin_operation()
                state = self.request()
                self.assertFalse(state["exists"])
                self.assertTrue(state["can_save"])
                text = "// 日本語の下書き\nclass Page {}\n"
                saved = self.queue_edit(state, text, target=name)
                target = Path(result["worktree_path"]) / name
                if busy:
                    self.assertFalse(target.exists())
                    self.assertEqual(self.request()["edit"]["content"], text)

                    def junie(command, completion, token, targets, **kwargs):
                        self.assertEqual(targets[0].read_bytes(), text.encode("utf-8-sig"))
                        completion.write_text(token)
                        return 0

                    with patch.object(junie_cli, "require_interactive_cli"), \
                            patch.object(junie_cli, "run_interactive", side_effect=junie), \
                            patch.object(workflow, "_verify_worktree", side_effect=lambda config, unit, result, record: result):
                        workflow.resume_with_junie(self.settings, result["run_id"])
                else:
                    self.assertTrue(saved["exists"])
                    self.assertIsNone(saved["edit"])
                self.assertEqual(target.read_bytes(), text.encode("utf-8-sig"))
                with self.assertRaisesRegex(ValueError, "更新"):
                    worktree_targets.save_target_edit(target.parents[1], (Path(name),), {
                        "target": name, "version": None, "content": "overwrite",
                    })
                self.assertEqual(target.read_bytes(), text.encode("utf-8-sig"))

    def test_http_prompt_reaches_active_junie_before_capsule_finishes_and_is_applied_once(self):
        self.view.begin_operation()
        received = []
        launches = []
        control = self.view.control

        def activity(entry):
            self.view.activity(entry)
            if entry["message"] == "Junieのプロセスを起動しました。":
                state = self.request("prompts")
                self.request("prompts", {"run_id": state["run_id"], "text": "Last を 42 にしてください。"})
            elif "追加指示を受け取りました" in entry["message"]:
                state = self.request("prompts")
                self.assertEqual(state["timing"], "immediate")
                self.assertEqual(state["prompts"][0]["status"], "steering")
                received.append(state)

        control.on_activity = activity

        def trust(request):
            if request is not None:
                from concurrent.futures import Future
                answer = Future()
                answer.set_result("trust")
                return answer

        control.on_junie_request = trust
        launch = junie_cli._launch_process

        def launch_agent(command, **streams):
            launches.append(command)
            root = Path(command[command.index("--project") + 1])
            script = f"import os; os.chdir({str(root)!r})\n" + textwrap.dedent("""
                import json, re, sys, time
                from pathlib import Path
                def read():
                    return json.loads(sys.stdin.readline())
                def send(**message):
                    print(json.dumps(dict(jsonrpc="2.0", **message)), flush=True)
                assert read()["method"] == "initialize"
                send(id=1, result={"protocolVersion": 1, "_meta": {"steering": {"supported": True}}})
                assert read()["method"] == "session/new"
                send(id=2, result={"sessionId": "migration"})
                original = read()
                assert original["method"] == "session/prompt"
                steer = read()
                assert steer["method"] == "_session/steering"
                text = steer["params"]["prompt"][0]["text"]
                assert "Last を 42" in text
                send(id=steer["id"], result={"outcome": "injected"})
                time.sleep(0.4)
                path = Path("Page.cs")
                path.write_text(path.read_text(encoding="utf-8-sig").replace("Last = 1", "Last = 42"))
                receipt = re.search(r"write exactly ([a-f0-9]+) to `([^`]+)`", text)
                Path(receipt[2]).write_text(receipt[1])
                completion = re.search(r"write exactly ([a-f0-9]+) to `(migration-complete-[^`]+)`", text)
                Path(completion[2]).write_text(completion[1])
                send(id=3, result={"stopReason": "end_turn"})
                sys.stdin.read()
            """)
            return launch([sys.executable, "-u", "-c", script], **streams)

        def verify(config, unit, result, record):
            self.assertEqual(record["completed_capsules"], 1)
            self.assertIn("Last = 42", (Path(result["worktree_path"]) / "Page.cs").read_text(encoding="utf-8-sig"))
            self.assertEqual(self.request("prompts")["prompts"][0]["status"], "sent")
            return result

        with (
            control_scope(control),
            patch.object(junie_cli, "require_interactive_cli"),
            patch.object(junie_cli, "_launch_process", side_effect=launch_agent),
            patch.object(workflow, "_verify_worktree", side_effect=verify) as verified,
            patch.dict("os.environ", {"JUNIE_HOME": str(self.root / "junie-home")}),
        ):
            workflow.run_with_junie(self.settings, "sample")
        self.assertEqual(len(received), 1)
        self.assertEqual(len(launches), 1)
        verified.assert_called_once()

    def test_pending_human_edits_change_live_prompt_timing_to_checkpoint(self):
        self.view.begin_operation()

        def junie(command, completion, token, targets, **kwargs):
            self.view.control.junie_steering = True
            state = self.request()
            self.assertEqual(state["prompt_timing"], "immediate")
            saved = self.queue_edit(state, BASE.replace("Last = 1", "Last = 42"))
            self.assertEqual(saved["prompt_timing"], "checkpoint")
            self.assertEqual(self.request("prompts")["timing"], "checkpoint")
            self.view.control.junie_steering = False
            completion.write_text(token)
            return 0

        with patch.object(junie_cli, "require_interactive_cli"), \
                patch.object(junie_cli, "run_interactive", side_effect=junie), \
                patch.object(workflow, "_verify_worktree", side_effect=lambda config, unit, result, record: result):
            result = workflow.run_with_junie(self.settings, "sample")
        self.assertIn("Last = 42", (Path(result["worktree_path"]) / "Page.cs").read_text(encoding="utf-8-sig"))

    def test_live_prompt_is_requeued_if_the_result_changes_unlisted_files(self):
        def junie(command, completion, token, targets, **kwargs):
            run_path = completion.parent.parent
            with editor_queue.editor_queue(run_path) as queue:
                editor_queue.add_prompt(queue, {"text": "Last を更新してください。"})
            prompts = kwargs["prompts"]
            identifiers, _ = prompts.take()
            prompts.finish(identifiers, True)
            targets[0].write_text(BASE.replace("Last = 1", "Last = 42"))
            (completion.parent / "unexpected.txt").write_text("unexpected")
            completion.write_text(token)
            return 0

        with patch.object(junie_cli, "require_interactive_cli"), \
                patch.object(junie_cli, "run_interactive", side_effect=junie):
            with self.assertRaisesRegex(ValueError, "outside the configured targets"):
                workflow.run_with_junie(self.settings, "sample")
        state = self.request("prompts")
        self.assertEqual(state["prompts"][0]["status"], "queued")
        self.assertNotIn("Last = 42", (Path(self.view.state["worktree_path"]) / "Page.cs").read_text(encoding="utf-8-sig"))

    def test_editor_accepts_eight_mib_and_rejects_larger_files_without_changing_them(self):
        path = self.project / "Page.cs"
        path.write_bytes(b"a" * (8 * 1024 * 1024))
        data, text = editor_queue.editor_target(self.project, Path("Page.cs"))
        self.assertEqual(len(data), 8 * 1024 * 1024)
        self.assertEqual(text.encode(), data)
        with path.open("ab") as stream:
            stream.write(b"x")
        with self.assertRaisesRegex(ValueError, "8 MiB.*外部エディタ"):
            editor_queue.editor_target(self.project, Path("Page.cs"))
        self.assertEqual(path.stat().st_size, 8 * 1024 * 1024 + 1)

    def test_editor_poll_does_not_read_or_resend_seven_mib_unchanged_payloads(self):
        result = self.finished_run()
        run_path = Path(result["run_path"])
        target = Path(result["worktree_path"]) / "Page.cs"
        original = "a" * (7 * 1024 * 1024)
        target.write_text(original, encoding="utf-8")
        with editor_queue.editor_queue(run_path) as queue:
            queue["edits"]["Page.cs"] = {"id": "draft", "status": "queued", "base": original,
                                         "content": "b" + original[1:]}
        first = self.request()
        self.assertEqual(len(first["edit"]["base"]), len(original))
        versions = {name: first[name] for name in ("view_version", "content_version", "edit_version", "history_version")}
        route = "editor?" + urlencode({"run_id": result["run_id"], "target": "Page.cs", **versions})
        with patch.object(dashboard_editor, "editor_target") as read, patch.object(dashboard_editor, "editor_queue") as queue:
            for _ in range(3):
                self.assertEqual(self.request(route), {"unchanged": True})
            read.assert_not_called()
            queue.assert_not_called()
        with editor_queue.editor_queue(run_path) as queue:
            editor_queue.add_prompt(queue, {"text": "途中の編集を確認してください。"})
        update = self.request(route)
        self.assertNotIn("content", update)
        self.assertNotIn("edit", update)
        self.assertNotIn("prompts", update)
        self.assertEqual(len(update["history_delta"]["prompts"]["changed"]), 1)
        version_after_prompt = update["history_version"]
        with editor_queue.editor_queue(run_path) as queue:
            editor_queue.add_prompt(queue, {"text": "追記だけ表示してください。"})
        later = self.request("editor?" + urlencode({**versions, "history_version": version_after_prompt}))
        self.assertEqual([item["text"] for item in later["history_delta"]["prompts"]["changed"]],
                         ["追記だけ表示してください。"])
        target.write_text("c" + original[1:], encoding="utf-8")
        external = self.request(route)
        self.assertTrue(external["content"].startswith("c"))
        self.assertNotIn("edit", external)

    def test_php_view_reads_the_run_snapshot_and_accepts_only_get(self):
        other = self.project / "other.php"
        other.write_bytes("<?php echo '注文';".encode("cp932"))
        annotated = "<?php\n// 移行ツールのコメント\necho '注文';\n"
        (self.project / "annotated.txt").write_bytes(annotated.encode("euc-jp"))
        annotated_crlf = annotated.replace("\n", "\r\n")
        (self.project / "annotated").write_bytes(annotated_crlf.encode("utf-8"))
        self.commit_project()
        config_path = self.root / "test.toml"
        with config_path.open("a", encoding="utf-8") as config:
            for name, encoding in (("annotated.txt", "euc-jp"), ("annotated", "utf-8")):
                config.write(f'\n[[units.inputs]]\nrole = "php"\npath = "{name}"\nencoding = "{encoding}"\nstart_line = 1\nend_line = 1\n')
        self.settings.update(load_config(config_path))
        inputs = self.settings["units"][0]["inputs"]
        inputs.append({**inputs[0], "path": "other.php", "encoding": "cp932"})
        prepared = prepare_fixture(self.settings, "sample")
        (self.project / "source.php").write_text("<?php echo 'updated after preparation';", encoding="utf-8")
        route = "editor-php?" + urlencode({"run_id": prepared["run_id"]})
        source = self.request(route)
        self.assertEqual(source["run_id"], prepared["run_id"])
        self.assertEqual(source["sources"], ["source.php", "annotated.txt", "annotated", "other.php"])
        self.assertEqual(source["source"], "source.php")
        self.assertEqual(source["content"], "<?php echo 1;")
        other_source = self.request(route + "&" + urlencode({"source": "other.php"}))
        self.assertEqual(other_source["content"], "<?php echo '注文';")
        for name in ("annotated.txt", "annotated"):
            with self.subTest(source=name):
                annotated_source = self.request(route + "&" + urlencode({"source": name}))
                self.assertEqual(annotated_source["source"], name)
                self.assertEqual(annotated_source["content"], annotated if name == "annotated.txt" else annotated_crlf)
        with self.assertRaises(HTTPError) as denied:
            self.request(route + "&" + urlencode({"source": "../Page.cs"}))
        self.assertEqual(denied.exception.code, 409)
        denied.exception.close()
        with self.assertRaises(HTTPError) as method:
            self.request("editor-php", {"kind": "save", "target": "source.php"})
        self.assertEqual(method.exception.code, 404)
        method.exception.close()

    def test_php_view_rejects_post_when_body_arrives_after_headers(self):
        address = urlsplit(self.url)
        connection = HTTPConnection(address.hostname, address.port, timeout=4)
        self.addCleanup(connection.close)
        body = b'{"kind":"save","target":"source.php"}'
        connection.putrequest("POST", address.path + "editor-php")
        connection.putheader("Origin", self.origin)
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", str(len(body)))
        connection.endheaders()
        self.assertFalse(select([connection.sock], [], [], 0.25)[0],
                         "server closed or responded before receiving the request body")
        connection.send(body)
        with connection.getresponse() as response:
            self.assertEqual(response.status, 404)
            response.read()
        self.assertEqual((self.project / "source.php").read_text(encoding="utf-8"), "<?php echo 1;")

    def test_editor_get_rejects_another_run_with_the_same_target(self):
        first = self.finished_run()
        second = self.finished_run()
        self.assertNotEqual(first["run_id"], second["run_id"])
        with self.assertRaises(HTTPError) as stale:
            self.request("editor?" + urlencode({"run_id": first["run_id"], "target": "Page.cs"}))
        self.assertEqual(stale.exception.code, 409)
        stale.exception.close()
        current = self.request("editor?" + urlencode({"run_id": second["run_id"], "target": "Page.cs"}))
        self.assertEqual(current["run_id"], second["run_id"])

    def test_php_view_excludes_shared_php_from_default_selection_and_requests(self):
        unrelated = ["rules.php", "reference.php", "other-reference", "other.php"]
        for name in unrelated:
            (self.project / name).write_text(f"<?php // {name}\n", encoding="utf-8")
        self.commit_project()
        config_path = self.root / "test.toml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace("\n[[units]]", """
[[project.migration_rules]]
path = "rules.php"

[[project.references]]
role = "reference_php"
path = "reference.php"

[[project.references]]
role = "reference_php"
path = "other-reference"

[[units]]""", 1) + """
[[units]]
id = "other"
source = "other.php"
target = "Other.cs"

""", encoding="utf-8",
        )
        self.settings.update(load_config(config_path))
        prepared = prepare_fixture(self.settings, "sample")
        route = "editor-php?" + urlencode({"run_id": prepared["run_id"]})
        manifest_path = Path(prepared["capsule_path"]) / "manifest.json"
        self.assertEqual(self.request(route), {
            "run_id": prepared["run_id"],
            "sources": ["source.php"], "source": "source.php", "content": "<?php echo 1;", "error": None,
        })
        for name in unrelated:
            with self.subTest(source=name):
                with self.assertRaises(HTTPError) as denied:
                    self.request(route + "&" + urlencode({"source": name}))
                self.assertEqual(denied.exception.code, 409)
                denied.exception.close()
        manifest = json.loads(manifest_path.read_bytes())
        manifest.pop("php_sources")
        rejected = json.dumps(manifest).encode()
        manifest_path.write_bytes(rejected)
        with self.assertRaises(HTTPError) as denied:
            self.request(route)
        self.assertEqual(denied.exception.code, 409)
        self.assertIn("php_sources", denied.exception.read().decode("utf-8"))
        denied.exception.close()
        self.assertEqual(manifest_path.read_bytes(), rejected)

    def test_php_view_rejects_missing_pin_without_reading_the_live_source(self):
        prepared = prepare_fixture(self.settings, "sample")
        manifest_path = Path(prepared["capsule_path"]) / "manifest.json"
        manifest = json.loads(manifest_path.read_bytes())
        manifest["php_sources"] = []
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        route = "editor-php?" + urlencode({"run_id": prepared["run_id"]})
        with self.assertRaises(HTTPError) as missing:
            self.request(route)
        self.assertEqual(missing.exception.code, 409)
        self.assertIn("固定PHPの一覧と保存された範囲が一致しません: source.php", missing.exception.read().decode("utf-8"))
        missing.exception.close()

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
            self.assertEqual((worktree / "Index.cshtml").read_text(encoding="utf-8-sig"),
                             updated["Index.cshtml"].replace("Orders", "Recent orders"))
            self.assertEqual((worktree / "Index.cshtml.cs").read_text(encoding="utf-8-sig"), updated["Index.cshtml.cs"])
            return result

        with patch.object(junie_cli, "require_interactive_cli"), patch.object(junie_cli, "run_interactive", side_effect=junie), \
                patch.object(workflow, "_verify_worktree", side_effect=build) as verified:
            workflow.run_with_junie(self.settings, "sample")
        verified.assert_called_once()

    def test_large_live_edits_and_final_prompt_are_applied_before_build(self):
        prompts = []
        original = BASE + "// 日本語を保持する大きな移行先ファイル\n" * 12_000
        self.assertGreater(len(original.encode("utf-8")), 512 * 1024)
        (self.project / "Page.cs").write_bytes(original.replace("\n", "\r\n").encode("utf-8-sig"))
        self.commit_project()
        remote = original.replace("First = 1", "First = 2")
        local = original.replace("Last = 1", "Last = 3")
        merged = remote.replace("Last = 1", "Last = 3")

        def junie(command, completion, token, targets, **kwargs):
            prompts.append(command[-1])
            if len(prompts) == 1:
                state = self.request()
                self.assertEqual(state["content"], original)
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
        self.assertEqual(saved.read_text(encoding="utf-8-sig"), resolution)

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

    def test_human_edits_are_shared_and_question_pauses_before_overwriting(self):
        (self.project / "source.php").write_text("<?php /* " + "x" * 2100 + " */", encoding="utf-8")
        self.commit_project()
        saved = BASE.replace("Last = 1", "Last = 3")
        proposed = saved.replace("Last = 3", "Last = 4")
        question = "Page.cs の Last は 3 のままでよいですか？"
        reason = "Page.cs: Last を 4 に変更。担当者が確認した期待値に合わせた。"
        calls = []

        def junie(command, completion, token, targets, **kwargs):
            calls.append(command[-1])
            if len(calls) == 1:
                self.queue_edit(self.request(), saved)
                targets[0].write_text(BASE.replace("First = 1", "First = 2"), encoding="utf-8")
            else:
                inputs = completion.parent / ".migration-materials/operator-edits.json"
                history = json.loads(inputs.read_text(encoding="utf-8"))["Page.cs"]
                self.assertEqual(len(history), 1)
                self.assertIn("+    int Last = 3;", history[0])
                self.assertNotIn("+    int First = 2;", history[0])
                self.assertIn("Preserve the human's work", command[-1])
                report = completion.parent / f".migration-scratch/operator-review-{token}.json"
                if len(calls) == 2:
                    report.write_text(json.dumps({"question": question}), encoding="utf-8")
                    targets[0].write_text(proposed, encoding="utf-8")
                    return 0
                elif len(calls) == 3:
                    self.assertIn(question, command[-1])
                    self.assertIn("4 でお願いします", (completion.parent / f"checker-findings-{token}-operator.txt")
                                  .read_text(encoding="utf-8"))
                    targets[0].write_text(proposed.replace("First = 1", "First = 2"), encoding="utf-8")
                    report.write_text(json.dumps({"fixes": [reason]}), encoding="utf-8")
            completion.write_text(token, encoding="utf-8")
            return 0

        with patch.object(junie_cli, "require_interactive_cli"), patch.object(junie_cli, "run_interactive", side_effect=junie), \
                patch.object(workflow, "_verify_worktree", side_effect=lambda config, unit, result, record: result):
            result = workflow.run_with_junie(self.settings, "sample")
            self.assertEqual(result["status"], "editor_checkpoint")
            self.assertEqual(result["completed_capsules"], 1)
            self.assertEqual(self.request()["operator_question"], question)
            for root in (result["agent_workspace_path"], result["worktree_path"]):
                self.assertEqual((Path(root) / "Page.cs").read_text(encoding="utf-8-sig"),
                                 saved.replace("First = 1", "First = 2"))
            unanswered = workflow.resume_with_junie(self.settings, result["run_id"])
            self.assertEqual(unanswered["status"], "editor_checkpoint")
            self.assertEqual(len(calls), 2)
            self.request("prompts", {"run_id": result["run_id"], "text": "4 でお願いします"})
            workflow.resume_with_junie(self.settings, result["run_id"])
            self.assertIsNone(self.request()["operator_question"])
            run_record = json.loads((Path(result["run_path"]) / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(run_record["operator_edit_fixes"], [reason])
            self.assertEqual((Path(result["worktree_path"]) / "Page.cs").read_text(encoding="utf-8-sig"),
                             proposed.replace("First = 1", "First = 2"))

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
                run_records.write_run_record(Path(result["run_path"]) / "run.json", record)
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
            run_records.write_run_record(Path(result["run_path"]) / "run.json", record)
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
