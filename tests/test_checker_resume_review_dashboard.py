from __future__ import annotations

import io
import json
import subprocess
import sys
import unittest
import uuid
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from migration_harness import cli, dashboard, junie_cli, workflow
from migration_harness.settings_form import build_settings_draft


class JunieConnectionTests(unittest.TestCase):
    def test_matching_junie_session_counts_complete_usage_events_once(self):
        def event(task, **usage):
            return {"kind": "SessionA2uxEvent", "taskId": task, "event": {"agentEvent": {
                "kind": "LlmResponseMetadataEvent", "modelUsage": [usage],
            }}}

        def line(entry):
            return json.dumps(entry).encode("utf-8") + b"\n"

        with TemporaryDirectory() as directory, patch.dict("os.environ", {"JUNIE_HOME": directory}):
            events = junie_cli._JunieSessionEvents("this-launch")
            sessions = Path(directory) / "sessions"
            for name in ("current", "unrelated"):
                (sessions / name).mkdir(parents=True)
            task = {"kind": "TaskStartedEvent", "taskId": "task"}
            metadata = event("task", inputTokens=10, outputTokens=4, cacheInputTokens=5, cacheCreateTokens=3)
            (sessions / "unrelated/events.jsonl").write_bytes(
                line({"kind": "UserPromptEvent", "prompt": "unrelated"}) + line(task) + line(metadata),
            )
            current = sessions / "current/events.jsonl"
            current.write_bytes(
                line({"kind": "UserPromptEvent", "prompt": "this-launch"}) + line(task)
                + line(event("another-task", inputTokens=100, outputTokens=100))
                + line(event("task", inputTokens=None, outputTokens=100)) + line(metadata)[:-1],
            )
            self.assertFalse(events.read_new_events())
            self.assertEqual(events.usage, [0, 0, 0, 0, 0])
            with current.open("ab") as stream:
                stream.write(b"\n")
            for _ in range(2):
                self.assertFalse(events.read_new_events())
                self.assertEqual(events.usage, [18, 4, 5, 3, 1])
            with current.open("ab") as stream:
                stream.write(line({**task, "taskId": "continue"}) + line(event("continue", inputTokens=2, outputTokens=1)))
            self.assertFalse(events.read_new_events())
            self.assertEqual(events.usage, [20, 5, 5, 3, 2])

    def test_usage_persists_across_launches_without_resetting_dashboard_actions(self):
        with TemporaryDirectory() as directory:
            run_path = Path(directory)
            view = dashboard.Dashboard()
            handle = workflow.run_observer.set(view.observe)
            record = {"run_id": "run", "status": "checker_succeeded"}
            try:
                workflow._write_run_record(run_path / "run.json", record)
                view.publish({**view.state, "ready": False, "generating_anchors": True})
                for delta in ([18, 4, 5, 3, 1], [2, 1, 0, 0, 1]):
                    report = workflow.junie_usage_recorder(run_path, "anchors")
                    report([0, 0, 0, 0, 0])
                    report(delta)
                    self.assertTrue(view.state["generating_anchors"])
                loaded = json.loads((run_path / "run.json").read_bytes())
                self.assertEqual(loaded["token_usage"], [{
                    "phase": "anchors", "capsule": None, "input": 20, "output": 5,
                    "cache_read": 5, "cache_write": 3, "responses": 2,
                }])
                self.assertEqual(view.state["token_usage"], loaded["token_usage"])
            finally:
                workflow.run_observer.reset(handle)

    def test_only_current_tasks_terminal_connection_failure_retries(self):
        failure = {"kind": "SessionA2uxEvent", "taskId": "our-task", "event": {
            "state": "FAILED", "agentEvent": {
                "kind": "AgentFailureEvent", "errorCode": "ConnectionFailed", "agent": {"kind": "MainAgent"},
            },
        }}
        with TemporaryDirectory() as directory, patch.dict("os.environ", {"JUNIE_HOME": directory}):
            events = junie_cli._JunieSessionEvents("current-completion-token")
            sessions = Path(directory) / "sessions"
            for name in ("ours", "unrelated"):
                (sessions / name).mkdir(parents=True)
            (sessions / "unrelated/events.jsonl").write_text(json.dumps(failure) + "\n")
            current = sessions / "ours/events.jsonl"
            prefix = [
                {"kind": "UserPromptEvent", "prompt": "Write current-completion-token"},
                {"kind": "TaskStartedEvent", "taskId": "our-task"},
                {**failure, "taskId": "another-task"},
                {**failure, "event": {**failure["event"], "state": "IN_PROGRESS"}},
                {**failure, "event": {**failure["event"], "agentEvent": {
                    **failure["event"]["agentEvent"], "errorCode": "AuthenticationFailed",
                }}},
            ]
            current.write_text("\n".join(map(json.dumps, prefix)) + "\n")
            self.assertFalse(events.read_new_events())
            with current.open("a") as stream:
                stream.write(json.dumps(failure) + "\n")
            self.assertTrue(events.read_new_events())

    def test_connection_failure_relaunches_at_most_twice(self):
        command = ["junie", "--prompt", "task"]
        path = Path("fixture")
        error = junie_cli.JunieConnectionError("temporary")
        usage_updates = []
        report_usage = usage_updates.append
        with (
            patch.object(junie_cli, "_run_session", side_effect=[error, error, 0]) as launch,
            patch.object(junie_cli.time, "sleep") as delay,
        ):
            self.assertEqual(junie_cli.run_interactive(command, path, "token", (path,), report_usage=report_usage), 0)
            self.assertEqual(launch.call_count, 3)
            self.assertTrue(all(call.kwargs["report_usage"] is report_usage for call in launch.call_args_list))
            self.assertIn("without duplicating completed changes", launch.call_args.args[0][-1])
            self.assertEqual([call.args[0] for call in delay.call_args_list], [1, 3])
        with (
            patch.object(junie_cli, "_run_session", side_effect=error) as launch,
            patch.object(junie_cli.time, "sleep") as delay,
            io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict") as stdout,
            redirect_stdout(stdout),
            self.assertRaisesRegex(junie_cli.JunieConnectionError, "after 2 retries"),
        ):
            junie_cli.run_interactive(command, path, "token", (path,))
        self.assertEqual(launch.call_count, 3)
        self.assertEqual([call.args[0] for call in delay.call_args_list], [1, 3])

    def test_connection_retry_notice_with_cp1252_stdout(self):
        path = Path("fixture")
        with (
            io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict") as stdout,
            redirect_stdout(stdout),
            patch.object(junie_cli, "_run_session", side_effect=[junie_cli.JunieConnectionError("temporary"), 0]) as launch,
            patch.object(junie_cli.time, "sleep") as delay,
        ):
            self.assertEqual(junie_cli.run_interactive(["junie", "--prompt", "task"], path, "token", (path,)), 0)
            self.assertIn(b"Retrying Junie", stdout.buffer.getvalue())
        self.assertEqual(launch.call_count, 2)
        delay.assert_called_once_with(1)


class SavedRunFixture(unittest.TestCase):
    """外部ツールを起動せずに再開やHTTPの操作を検証できるRunを用意する。"""

    def setUp(self):
        """チェッカーが判断待ちになった直後のRunと移行先を作る。"""
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        self.run_id = str(uuid.uuid4())
        self.run_path = root / "runs" / self.run_id
        self.worktree = root / "worktrees" / self.run_id
        self.worktree.mkdir(parents=True)
        self.target = self.worktree / "Example.cs"
        self.target.write_text("class Original {}\n", encoding="utf-8")
        project = root / "project"
        project.mkdir()
        (project / "example.php").write_text("<?php echo 'example';\n", encoding="utf-8")
        (project / "viewer.html").write_text(
            '<!doctype html><body><input id="workspace-import-file"></body>', encoding="utf-8",
        )
        self.config = {
            "run_root": str(self.run_path.parent),
            "worktree_root": str(self.worktree.parent),
            "project_root": str(project),
            "units": [{"id": "example", "target": self.target.name}],
            "build_project": None,
            "checker_command": ["fixture-checker"],
            "max_repair_attempts": 0,
            "junie_max_steps": None,
            "diff_viewer_html": "viewer.html",
            "diff_viewer_anchor_reference": [root / "reference.json"],
        }
        capsule = self.run_path / "capsule"
        capsule.mkdir(parents=True)
        context = capsule / "context.txt"
        context.write_text("example", encoding="utf-8")
        (capsule / "manifest.json").write_text(json.dumps({
            "parts": [{
                "index": 1, "path": str(context), "chars": 7,
                "sources": [{"path": "example.php", "start_line": 1, "end_line": 1, "encoding": "utf-8"}],
            }],
        }), encoding="utf-8")
        self.record_path = self.run_path / "run.json"
        self.build_log = self.run_path / "build.log"
        self.checker_log = self.run_path / "checker.log"
        self.build_log.write_bytes(b"old successful build\n")
        self.checker_log.write_bytes(b"PREVIOUS Example.cs:1 warning: previous finding\n")
        self.save_record({
            "run_id": self.run_id, "unit_id": "example", "target": self.target.name,
            "project_root": str(project), "worktree_path": str(self.worktree),
            "status": "checker_needs_review", "completed_capsules": 1, "context_capsules": 1,
            "max_context_chars": 1000, "source_boundary_review_required": False,
            "build_exit_code": 0, "build_log_path": str(self.build_log),
            "checker_exit_code": 1, "checker_log_path": str(self.checker_log),
        })

    def save_record(self, record):
        """シナリオの開始状態をRun記録に書き込む。"""
        self.record_path.write_text(json.dumps(record), encoding="utf-8")

    def record(self):
        """見かけの戻り値とは別に、実際に保存されたRun記録を取得する。"""
        return json.loads(self.record_path.read_text(encoding="utf-8"))

    def mock_commands(self, *responses):
        """外部コマンドの順序とログを制御する。"""
        expected = iter(responses)
        calls = []
        run_process = subprocess.run

        def run(command, *, label, cancellable=True, **kwargs):
            """指定した順序と実行場所で呼ばれたことを確認し、結果を返す。"""
            if command[0] == "git":
                return run_process(command, **kwargs)
            calls.append(command)
            name, returncode, log = next(expected)
            self.assertEqual(command, ["dotnet", "build"] if name == "dotnet" else self.config["checker_command"])
            self.assertEqual(Path(kwargs["cwd"]), self.worktree)
            self.assertEqual(kwargs["stderr"], subprocess.STDOUT)
            self.assertFalse(kwargs["check"])
            kwargs["stdout"].write(log)
            return subprocess.CompletedProcess(command, returncode)

        return patch.object(workflow, "run_process", side_effect=run), calls

    def resume(self, *responses):
        """外部ツールを差し替えて通常の再開経路を実行する。"""
        mock, calls = self.mock_commands(*responses)
        with mock, redirect_stdout(io.StringIO()):
            result = workflow.resume_with_junie(self.config, self.run_id)
        self.assertEqual(len(calls), len(responses))
        return result

    def review(self, finding_id=None, reason=None):
        """同じRunの一覧取得と指摘への承認を呼び分ける。"""
        return workflow.review_checker(self.config, self.run_id, finding_id, reason)


class CheckerResumeAndReviewTests(SavedRunFixture):
    """再開時の検証順序とチェッカー指摘の判定を検証する。"""

    build = ("dotnet", 0, b"rebuilt edited target\n")
    diagnostic = b"MIG101 Example.cs:1 warning: use SharedUtility\n"

    def test_migration_requires_current_verification_and_all_range_decisions(self):
        def completion():
            return workflow.migration_ledger(self.config, self.run_id)["completion"]

        subprocess.run(["git", "init", "-q", str(self.worktree)], check=True)
        self.config["checker_command"] = [sys.executable]
        secondary = self.worktree / "View.cshtml"
        secondary.write_text("<p>View</p>", encoding="utf-8")
        targets = [self.target.name, secondary.name]
        self.config["units"][0]["targets"] = targets
        self.save_record({**self.record(), "targets": targets})
        path = self.run_path / "capsule" / "manifest.json"
        manifest = json.loads(path.read_bytes())
        ranges = manifest["parts"][0]["sources"]
        for number in (2, 3):
            ranges.append({**ranges[0], "start_line": number, "end_line": number})
        path.write_text(json.dumps(manifest), encoding="utf-8")
        check_passed = ("fixture-checker", 0, b"clean\n")
        self.resume(self.build, check_passed)
        self.assertEqual(completion(), {"complete": False, "verification_passed": True, "reviewed": 0, "total": 3})

        def confirm(range_id, target):
            workflow.review_migration_ranges(
                self.config, self.run_id, [range_id], "confirmed", "Checked source and destination",
                confirmed_targets=[target.name],
            )

        confirm("1:1", self.target)
        confirm("1:2", secondary)
        self.assertFalse(completion()["complete"])
        workflow.review_migration_ranges(self.config, self.run_id, ["1:3"], "excluded", "Unused legacy code")
        self.assertTrue(completion()["complete"])

        self.target.write_text("class Edited {}", encoding="utf-8")
        ledger = workflow.migration_ledger(self.config, self.run_id)
        self.assertEqual(
            [source["review"]["state"] for source in ledger["capsules"][0]["sources"]],
            ["stale", "confirmed", "excluded"],
        )
        self.assertFalse(ledger["completion"]["complete"])
        confirm("1:1", self.target)
        self.assertFalse(completion()["verification_passed"])
        self.resume(self.build, ("fixture-checker", 1, self.diagnostic))
        self.assertFalse(completion()["complete"])
        self.resume(("dotnet", 1, b"build error\n"))
        self.assertFalse(completion()["complete"])
        self.resume(self.build, check_passed)
        self.assertTrue(completion()["complete"])
        self.config["checker_command"] = ["different-checker"]
        self.assertFalse(completion()["complete"])

    # 承認や修正履歴がない手編集でも、再ビルドが先に走ることを検証する。
    def test_manual_edit_rebuilds_before_rechecking_without_history_or_approvals(self):
        self.target.write_text("class ManuallyEdited {}\n", encoding="utf-8")
        result = self.resume(self.build, ("fixture-checker", 0, b"no findings\n"))
        self.assertEqual(result["status"], "checker_succeeded")
        saved = self.record()
        self.assertEqual(saved["status"], "checker_succeeded")
        self.assertEqual(saved["build_exit_code"], 0)
        self.assertEqual(saved["build_log_path"], str(self.build_log))
        self.assertEqual(self.build_log.read_bytes(), self.build[2])
        self.assertEqual(self.checker_log.read_bytes(), b"no findings\n")

    # 新しいビルドが失敗した場合に、以前の成功を再利用しないことを検証する。
    def test_failed_rebuild_stops_before_checker_and_records_current_failure(self):
        self.target.write_text("broken C#", encoding="utf-8")
        old_checker_log = self.checker_log.read_bytes()
        result = self.resume(("dotnet", 99, b"edited target does not build\n"))
        self.assertEqual(result["status"], "build_failed")
        saved = self.record()
        self.assertEqual(saved["status"], "build_failed")
        self.assertEqual(saved["build_exit_code"], 99)
        self.assertNotIn("checker_exit_code", saved)
        self.assertEqual(self.build_log.read_bytes(), b"edited target does not build\n")
        self.assertEqual(self.checker_log.read_bytes(), old_checker_log)

    def test_build_response_passes_saved_edit_and_instruction_to_junie_before_verification(self):
        diagnostic = f"{self.target}(1,30): error CS1525: Invalid expression\n".encode()
        self.target.write_bytes(b"\xef\xbb\xbfclass Example { int Value = ; }\r\n")
        self.resume(("dotnet", 1, diagnostic * 2))
        (self.run_path / "agent-workspace").mkdir()
        summary = workflow.build_failure_review(self.config, self.run_id)
        self.assertEqual(summary["errors"], ["Example.cs(1,30): CS1525: Invalid expression"])
        self.assertEqual(summary["preferred_target"], self.target.name)
        source = workflow.build_failure_review(self.config, self.run_id, self.target.name)
        edited = b"\xef\xbb\xbf// human edit\r\nclass Example { int Value = ; }\r\n"
        instruction = "値を 42 にしてください。"

        def repair(command, completion, token, targets, **kwargs):
            instructions = next(completion.parent.glob("checker-findings-*-instruction.txt"))
            self.assertIn(instructions.name, command[-1])
            self.assertEqual(instructions.read_text(encoding="utf-8").strip(), instruction)
            self.assertEqual(targets[0].read_bytes(), edited)
            targets[0].write_bytes(edited.replace(b"= ;", b"= 42;"))
            completion.write_text(token, encoding="utf-8")
            return 0

        commands, calls = self.mock_commands(self.build, ("fixture-checker", 0, b"ok\n"))
        with (
            commands, redirect_stdout(io.StringIO()),
            patch.object(junie_cli, "require_interactive_cli"),
            patch.object(junie_cli, "run_interactive", side_effect=repair) as junie,
        ):
            result = workflow.resume_with_junie(self.config, self.run_id, build_response={
                "instruction": instruction,
                "edit": {"target": self.target.name, "version": source["version"],
                         "content": "// human edit\nclass Example { int Value = ; }\n"},
            })
        junie.assert_called_once()
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["status"], "checker_succeeded")
        self.assertEqual(self.target.read_bytes(), edited.replace(b"= ;", b"= 42;"))

    def test_build_response_rejects_a_stale_edit_before_starting_any_command(self):
        self.resume(("dotnet", 1, b"failed\n"))
        source = workflow.build_failure_review(self.config, self.run_id, self.target.name)
        self.target.write_text("class ChangedExternally {}", encoding="utf-8")
        with patch.object(workflow, "run_process") as process:
            with self.assertRaisesRegex(ValueError, "対象ファイルが更新されました"):
                workflow.resume_with_junie(self.config, self.run_id, build_response={
                    "edit": {"target": self.target.name, "version": source["version"], "content": "class Stale {}"},
                })
        process.assert_not_called()
        self.assertEqual(self.target.read_text(encoding="utf-8"), "class ChangedExternally {}")
        self.assertEqual(self.record()["status"], "build_failed")

    def test_build_diagnostic_repairs_target_and_stops_if_error_repeats(self):
        self.config["max_repair_attempts"] = 2
        error = f"{self.target}(5,3): error CS1002: ; expected\n".encode()
        with (
            patch.object(junie_cli, "require_interactive_cli"),
            patch.object(workflow, "_repair_target") as repair,
        ):
            result = self.resume(("dotnet", 1, error), ("dotnet", 1, error.replace(b"(5,3)", b"(6,3)")))
            self.assertEqual(result["status"], "build_failed")
            repair.assert_called_once()
            self.assertEqual(repair.call_args.args[2], (Path(self.target.name),))
            self.assertEqual((self.run_path / "build-before-repair-1.log").read_bytes(), error)
            repair.reset_mock()
            error = error.replace(b"CS1002: ; expected", b"CS1513: } expected")
            result = self.resume(("dotnet", 1, error), self.build, ("fixture-checker", 0, b"fixed\n"))
            self.assertEqual(result["status"], "checker_succeeded")
            repair.assert_called_once()
            self.assertEqual([entry["kind"] for entry in self.record()["repair_history"]], ["build", "build"])

    def test_interrupted_repair_can_restart_with_a_one_attempt_limit(self):
        self.config["max_repair_attempts"] = 1
        build_failure = ("dotnet", 1, f"{self.target}(5,3): error CS1002: ; expected\n".encode())
        check_failure = ("fixture-checker", 1, self.diagnostic)
        success = (self.build, ("fixture-checker", 0, b"fixed\n"))
        original_record = self.record()
        for responses in ((build_failure,), (self.build, check_failure)):
            with self.subTest(phase=responses[-1][0]):
                self.save_record(original_record)
                with (
                    patch.object(junie_cli, "require_interactive_cli"),
                    patch.object(workflow, "_repair_target", side_effect=[KeyboardInterrupt, None]) as repair,
                ):
                    with self.assertRaises(workflow.RunInterrupted):
                        self.resume(*responses)
                    saved = self.record()
                    self.assertEqual(saved["repair_history"], [])
                    self.assertIn("repair_pending", saved)
                    self.assertTrue(workflow.can_resume_run(self.config, saved))
                    result = self.resume(*responses, *success)
                    self.assertEqual(result["status"], "checker_succeeded")
                    self.assertEqual(repair.call_count, 2)
                    self.assertEqual(len(self.record()["repair_history"]), 1)
                    self.assertNotIn("repair_pending", self.record())

    def test_free_form_checker_output_reaches_junie_before_rebuild(self):
        self.config["max_repair_attempts"] = 2
        log = b'{"expected": 42, "actual": 0}\n'
        with (
            patch.object(workflow, "_repair_target") as repair,
            patch.object(junie_cli, "require_interactive_cli"),
        ):
            result = self.resume(
                self.build, ("fixture-checker", 2, log),
                self.build, ("fixture-checker", 0, b"fixed\n"),
            )
        self.assertEqual(result["status"], "checker_succeeded")
        repair.assert_called_once()
        self.assertEqual(repair.call_args.args[5], log.decode())
        self.assertEqual(repair.call_args.args[2], (Path("Example.cs"),))
        self.assertTrue(self.record()["repair_history"][0]["raw_checker_output"])
        self.assertEqual((self.run_path / "checker-before-repair-1.log").read_bytes(), log)

    def test_repeated_free_form_checker_output_stops_before_another_repair(self):
        self.config["max_repair_attempts"] = 2
        with patch.object(workflow, "_repair_target") as repair, patch.object(junie_cli, "require_interactive_cli"):
            result = self.resume(
                self.build, ("fixture-checker", 2, b"expected 42; actual 0\n"),
                self.build, ("fixture-checker", 2, b"expected   42; actual 0\n"),
            )
        repair.assert_called_once()
        self.assertEqual(result["status"], "checker_needs_review")
        self.assertIn("previous checker finding", result["error"])
        self.assertEqual(self.record()["checker_review_reason"], "finding_repeated")

    def test_checker_repair_limit_records_the_review_reason(self):
        self.config["max_repair_attempts"] = 1
        with patch.object(workflow, "_repair_target") as repair, patch.object(junie_cli, "require_interactive_cli"):
            result = self.resume(
                self.build, ("fixture-checker", 1, b"first diagnostic\n"),
                self.build, ("fixture-checker", 1, b"second diagnostic\n"),
            )
        repair.assert_called_once()
        self.assertEqual(result["status"], "checker_needs_review")
        self.assertEqual(self.record()["checker_review_reason"], "repair_limit")

    def test_unreadable_checker_output_requires_review(self):
        self.config["max_repair_attempts"] = 2
        for log in (b"", b"\xff\n", self.diagnostic + b"unknown\0data\n"):
            with self.subTest(log=log), patch.object(workflow, "_repair_target") as repair:
                result = self.resume(self.build, ("fixture-checker", 2, log))
                repair.assert_not_called()
                self.assertEqual(result["status"], "checker_needs_review")
                self.assertEqual(self.checker_log.read_bytes(), log)
                self.assertTrue(self.review()["unrecognized"])

    # 有効な指摘だけなら通常の自動修正経路を使用することを検証する。
    def test_supported_diagnostics_still_allow_automatic_repair(self):
        self.config["max_repair_attempts"] = 1
        with (
            patch.object(junie_cli, "require_interactive_cli"),
            patch.object(workflow, "_repair_target") as repair,
        ):
            result = self.resume(
                self.build, ("fixture-checker", 1, self.diagnostic),
                self.build, ("fixture-checker", 0, b"fixed\n"),
            )
        self.assertEqual(result["status"], "checker_succeeded")
        repair.assert_called_once()
        self.assertEqual(repair.call_args.args[5], self.diagnostic.decode().strip())
        self.assertEqual(len(self.record()["repair_history"]), 1)
        self.assertEqual((self.run_path / "checker-before-repair-1.log").read_bytes(), self.diagnostic)

    # AIに渡せない大きさの指摘でも、人は一覧と承認を操作できることを検証する。
    def test_character_and_byte_limits_only_block_automatic_repair(self):
        self.config["max_repair_attempts"] = 1
        for count, size, error in ((70, 110, "too much text"), (350, 180, "too large")):
            with self.subTest(count=count):
                log = b"".join(
                    f"MIG101 Example.cs:{line} warning: {line} {'x' * size}\n".encode()
                    for line in range(1, count + 1)
                )
                self.assertGreater(len(log), 8_000)
                self.assertEqual(len(log) > 65_536, count == 350)
                with patch.object(workflow, "_repair_target") as repair:
                    result = self.resume(self.build, ("fixture-checker", 1, log))
                repair.assert_not_called()
                self.assertEqual(result["status"], "checker_needs_review")
                self.assertIn(error, result["error"])
                report = self.review()
                self.assertEqual(len(report["findings"]), count)
                self.assertEqual(report["locations"][-1]["line"], str(count))
                last_id = report["findings"][-1][0]
                self.review(last_id, "Reviewed the last finding against the migration.")
                self.assertTrue(self.review()["findings"][-1][2])
                self.assertEqual(len(self.record()["repair_history"]), 0)

    # 大きなログでも有効な承認だけが残れば完了できることを検証する。
    def test_large_approved_log_can_complete_on_resume(self):
        self.config["max_repair_attempts"] = 1
        log = b"MIG101 Example.cs:1 warning: " + b"x" * 65_536 + b"\n"
        self.resume(self.build, ("fixture-checker", 1, log))
        finding_id = self.review()["findings"][0][0]
        self.review(finding_id, "The reported code uses the intended behavior.")
        result = self.resume(self.build, ("fixture-checker", 1, log))
        self.assertEqual(result["status"], "checker_accepted")
        self.assertEqual(self.record()["checker_exit_code"], 1)

    # 既知の指摘をすべて承認しても、未知の出力が残る間は完了にしないことを検証する。
    def test_unknown_output_prevents_previously_approved_findings_from_completing_run(self):
        self.resume(self.build, ("fixture-checker", 1, self.diagnostic))
        self.review(self.review()["findings"][0][0], "The warning is expected.")
        result = self.resume(self.build, ("fixture-checker", 1, self.diagnostic + b"checker offline\n"))
        self.assertEqual(result["status"], "checker_needs_review")
        self.assertIn("unrecognized", result["error"])

    # 同じ識別子の指摘を個別承認と誤認しないことを検証する。
    def test_duplicate_id_remains_ambiguous(self):
        log = self.diagnostic + self.diagnostic.replace(b":1", b":2")
        self.resume(self.build, ("fixture-checker", 1, log))
        first, second = self.review()["findings"]
        self.assertEqual(first[0], second[0])
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            self.review(first[0], "Cannot distinguish these findings.")
        self.assertFalse(self.record().get("checker_reviews"))

    # 承認後に編集した対象を再確認に戻すことを検証する。
    def test_approval_expires_when_target_changes(self):
        self.resume(self.build, ("fixture-checker", 1, self.diagnostic))
        finding_id = self.review()["findings"][0][0]
        self.review(finding_id, "The original content was reviewed.")
        self.target.write_text("class Changed {}\n", encoding="utf-8")
        self.assertFalse(self.review()["findings"][0][2])
        with self.assertRaisesRegex(ValueError, "changed or was missing"):
            self.review(finding_id, "Cannot approve a stale finding.")
        result = self.resume(self.build, ("fixture-checker", 1, self.diagnostic))
        self.assertEqual(result["status"], "checker_needs_review")
        self.assertEqual(self.review()["findings"][0], (finding_id, self.diagnostic.decode().strip(), False))
        self.assertEqual(len(self.record()["checker_reviews"]), 1)


class SavedDashboardTests(SavedRunFixture):
    """保存済みRunの画面取得と、明示した操作だけが起動することを検証する。"""

    @staticmethod
    def get_ready_event(url, *, after=0):
        """SSEから操作可能になった時点の連番と状態を取得する。"""
        with urlopen(url + "events", timeout=3) as response:
            for line in response:
                if line.startswith(b"id: "):
                    revision = int(line.split(b": ", 1)[1])
                elif line.startswith(b"data: "):
                    state = json.loads(line.split(b": ", 1)[1])
                    if state.get("ready") and revision > after:
                        return revision, state
        raise AssertionError("no ready dashboard event")

    @staticmethod
    def post(url, endpoint, payload):
        """同じオリジンからブラウザー相当の操作を送る。"""
        origin = urlsplit(url)
        headers = {"Origin": f"{origin.scheme}://{origin.netloc}", "Content-Type": "application/json"}
        return urlopen(Request(url + endpoint, json.dumps(payload).encode(), headers), timeout=3)

    def open_dashboard(self, inspect, *, resume=False):
        """HTTPが待ち受けている間に、一時サーバーで確認処理を実行する。"""
        terminal = io.StringIO()
        view = dashboard.Dashboard()
        dequeue = view.actions.get
        inspected = False

        def interact(*, timeout):
            """操作待ちのタイミングでブラウザー側の確認処理を呼び出す。"""
            nonlocal inspected
            if inspected:
                raise KeyboardInterrupt
            inspected = True
            first_line = terminal.getvalue().splitlines()[0]
            inspect(first_line.removeprefix("Progress dashboard: "))
            if resume:
                return dequeue(timeout=timeout)
            raise KeyboardInterrupt

        with (
            redirect_stdout(terminal),
            patch.object(dashboard, "Dashboard", return_value=view),
            patch.object(view.actions, "get", side_effect=interact),
        ):
            return dashboard.run_dashboard(self.config, run_id=self.run_id)

    def test_checker_dashboard_displays_raw_output_and_resumes_with_instruction(self):
        raw = b'{"expected": 42, "actual": 0}\n'
        self.checker_log.write_bytes(raw)
        self.save_record({**self.record(), "checker_review_reason": "finding_repeated",
                          "error": "a previous checker finding is still present"})
        instruction = "Keep the shared conversion and correct the returned value."

        def send_instruction(url):
            revision, state = self.get_ready_event(url)
            self.assertEqual(state["checker_review_reason"], "finding_repeated")
            self.assertIn("previous checker finding", state["error"])
            route = url + "checker-output?run_id=" + self.run_id
            with self.assertRaises(HTTPError) as stale:
                urlopen(url + "checker-output?run_id=wrong", timeout=3)
            self.assertEqual(stale.exception.code, 409)
            stale.exception.close()
            with urlopen(route, timeout=3) as response:
                self.assertEqual(json.load(response), {"output": raw.decode(), "truncated": False})
            with urlopen(route + "&download=1", timeout=3) as response:
                self.assertEqual(response.read(), raw)
            with self.post(url, "control", {
                "action": "resume", "revision": revision, "checker_instruction": instruction,
            }) as response:
                self.assertEqual(response.status, 202)

        mock, calls = self.mock_commands(("dotnet", 0, b"build ok"), ("fixture-checker", 0, b"check ok"))
        with mock, patch.object(workflow, "_repair_target") as repair:
            result = self.open_dashboard(send_instruction, resume=True)
        self.assertEqual(result["status"], "checker_succeeded")
        self.assertNotIn("checker_review_reason", self.record())
        self.assertEqual(len(calls), 2)
        self.assertEqual(repair.call_args.args[5], raw.decode())
        self.assertEqual(self.record()["repair_history"][0]["instruction"], instruction)

    def test_build_failure_dashboard_saves_a_large_target_and_rebuilds(self):
        self.save_record({**self.record(), "status": "build_failed", "build_exit_code": 1})
        original = "class Original {}\n/*\n" + "\t\\ コメント\n" * 100_000 + "*/\n"
        self.target.write_text(original, encoding="utf-8", newline="")
        edited = original.replace("class Original", "class Repaired", 1)

        def inspect(url):
            revision, state = self.get_ready_event(url)
            self.assertEqual(state["status"], "build_failed")
            with urlopen(f"{url}build-review?run_id={self.run_id}&target={self.target.name}", timeout=3) as response:
                source = json.load(response)
            self.assertEqual(source["content"], original)
            with self.post(url, "build-response", {
                "action": "build-response", "revision": revision,
                "edit": {"target": self.target.name, "version": source["version"], "content": edited},
            }) as response:
                self.assertEqual(response.status, 202)

        commands, calls = self.mock_commands(("dotnet", 0, b"ok\n"), ("fixture-checker", 0, b"ok\n"))
        with commands:
            result = self.open_dashboard(inspect, resume=True)
        self.assertEqual(result["status"], "checker_succeeded")
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.target.read_text(encoding="utf-8"), edited)

    # 完了状態で外部コマンドを起動せず、記録と比較を閲覧できることを検証する。
    def test_complete_runs_show_saved_results_ledger_and_diff_without_running_commands(self):
        self.config["diff_viewer_anchor_reference"] = None
        original = self.record()
        for status, checker_exit in (("checker_succeeded", 0), ("checker_accepted", 1), ("build_succeeded", None)):
            with self.subTest(status=status):
                saved = {**original, "status": status}
                if checker_exit is None:
                    self.config["checker_command"] = None
                    saved.pop("checker_exit_code")
                    saved.pop("checker_log_path")
                else:
                    saved["checker_exit_code"] = checker_exit
                self.save_record(saved)
                before = self.record_path.read_bytes()

                def inspect(url):
                    """サーバーが動いている間に、今回のシナリオのHTTP操作を確認する。"""
                    revision, state = self.get_ready_event(url)
                    self.assertEqual(state["status"], status)
                    self.assertEqual(state["build_exit_code"], 0)
                    self.assertEqual(state.get("checker_exit_code"), checker_exit)
                    self.assertTrue(state["resumable"])
                    with urlopen(url, timeout=3) as response:
                        response.readline()
                        root_tag = response.readline()
                    self.assertIn(b"data-saved", root_tag)
                    self.assertIn(b"data-viewer-enabled", root_tag)
                    with urlopen(url + "migration-ledger", timeout=3) as response:
                        ledger = json.load(response)
                        self.assertEqual(ledger["capsules"][0]["sources"][0]["path"], "example.php")
                        self.assertFalse(ledger["completion"]["complete"])
                    with self.assertRaises(HTTPError) as stale_comparison:
                        self.post(url, "comparison", {"run_id": "a-different-run"})
                    with stale_comparison.exception as response:
                        self.assertEqual(response.code, 409)
                    with self.post(url, "comparison", {"run_id": self.run_id}) as response:
                        comparison = json.load(response)
                    with urlopen(f"{url}review/{comparison['run_id']}/{comparison['id']}.json", timeout=3) as response:
                        self.assertIn("class Original", json.load(response)["workspace"]["rightText"])
                    with self.assertRaises(HTTPError) as rejected:
                        self.post(url, "control", {
                            "action": "accept", "finding_id": "123456789abc", "reason": "Done", "revision": revision,
                        })
                    with rejected.exception as response:
                        self.assertEqual(response.code, 409)

                with (
                    patch.object(workflow.subprocess, "run") as commands,
                    patch.object(dashboard, "run_with_junie") as junie,
                    patch.object(dashboard, "resume_with_junie") as resume,
                    patch.object(dashboard, "generate_anchors") as anchors,
                ):
                    result = self.open_dashboard(inspect)
                commands.assert_not_called()
                junie.assert_not_called()
                resume.assert_not_called()
                anchors.assert_not_called()
                self.assertEqual(result["status"], status)
                self.assertEqual(self.record_path.read_bytes(), before)

    def test_dashboard_validates_settings_before_saving_and_detects_external_edits(self):
        project = Path(self.config["project_root"])
        (project / ".git").mkdir()
        config_path = project.parent / "settings.toml"
        self.config["config_path"] = str(config_path)
        self.config.update(checker_from_cli=True, toml_checker_command=None, checker_command=["cli-checker"])
        valid = '[project]\nroot = "project"\nworktree_root = "trees"\nrun_root = "runs"\n'
        valid += '\n[[units]]\nid = "メニュー"\nsource = "example.php"\ntarget = "Example.cs"\n'

        def inspect(url):
            revision, _ = self.get_ready_event(url)
            with urlopen(url + "settings", timeout=3) as response:
                settings = json.load(response)
            self.assertEqual(settings["version"], "missing")
            request = {"revision": revision, "version": settings["version"]}
            with self.assertRaises(HTTPError) as invalid:
                self.post(url, "settings", {**request, "content": 'project = "invalid"'})
            self.assertEqual(invalid.exception.code, 409)
            invalid.exception.close()
            self.assertFalse(config_path.exists())
            with self.post(url, "settings", {**request, "content": valid}) as response:
                saved = json.load(response)
            revision, _ = self.get_ready_event(url, after=revision)
            self.assertIsNone(self.config["checker_command"])
            self.assertNotIn("checker_from_cli", self.config)
            self.assertNotIn("toml_checker_command", self.config)
            incomplete = valid.replace('target = "Example.cs"', 'target = "Pages/Example.cshtml"')
            with self.assertRaises(HTTPError) as invalid:
                self.post(url, "settings", {"revision": revision, "version": saved["version"], "content": incomplete})
            with invalid.exception as response:
                self.assertEqual(response.code, 409)
                self.assertIn("Pages/Example.cshtml.cs", response.read().decode("utf-8"))
            self.assertEqual(config_path.read_text(encoding="utf-8"), valid)
            source = valid.replace('target = "Example.cs"', 'target = "Example.cs" # Keep target note')
            source += '\n[checker]\n# Keep this checker\ncommand = ["fixture-checker"]\n'
            pair = ["Example.cshtml.cs", "Example.cshtml"]
            with self.post(url, "settings", {
                "mode": "preview", "content": source,
                "changes": {"units": {"0": {"id": "注文確認", "targets": pair}}},
            }) as response:
                draft = json.load(response)
            self.assertEqual(draft["form"]["units"][0]["id"], "注文確認")
            self.assertIn('# Keep this checker\ncommand = ["fixture-checker"]', draft["content"])
            self.assertIn('targets = ["Example.cshtml.cs", "Example.cshtml"] # Keep target note', draft["content"])
            self.assertEqual(config_path.read_text(encoding="utf-8"), valid)
            with urlopen(url + "workspace", timeout=3) as response:
                self.assertEqual(json.load(response)["units"], ["メニュー"])
            external_edit = valid + "# Updated on disk\n"
            config_path.write_text(external_edit, encoding="utf-8", newline="")
            with self.assertRaises(HTTPError) as stale:
                self.post(url, "settings", {"revision": revision, "version": saved["version"], "content": valid})
            self.assertEqual(stale.exception.code, 409)
            stale.exception.close()
            self.assertEqual(config_path.read_text(encoding="utf-8"), external_edit)

        self.open_dashboard(inspect)

    def test_settings_form_edits_rules_and_reference_pairs_without_changing_other_inputs(self):
        source = """[[units]]
id = "メニュー"
source = "menu.php"
target = "Menu.cs"

[[units.inputs]]
role = "migration_rules"
path = "rules.md" # Keep range
start_line = 2
end_line = 4

[[units.inputs]]
role = "reference_php"
path = "old.php"
shared = true

[[units.inputs]]
role = "reference_csharp"
path = "Old.cs"
shared = true

[[units.inputs]]
role = "target_constraints"
path = "constraints.md"
start_line = 1
end_line = 2
shared = true

[[units]]
id = "検索"
source = "search.php"
target = "Search.cs"
"""
        def inspect(url):
            def preview(content, changes):
                with self.post(url, "settings", {
                    "mode": "preview", "content": content, "changes": changes,
                }) as response:
                    return json.load(response)

            migrated = preview(source, {})
            self.assertEqual([item["path"] for item in migrated["form"]["references"]], ["old.php", "Old.cs"])
            draft = preview(migrated["content"], {
                "migrationRules": [
                    {"index": 0, "role": "migration_rules", "path": "updated.md"},
                    {"role": "migration_rules", "path": "extra.md"},
                ],
                "references": [
                    {"role": "reference_php", "path": "example.php"},
                    {"role": "reference_csharp", "path": "Example.cshtml.cs"},
                    {"role": "reference_razor", "path": "Example.cshtml"},
                ],
            })
            rules = draft["form"]["migrationRules"]
            self.assertEqual(rules[0], {
                "index": 0, "role": "migration_rules", "path": "updated.md", "start_line": 2, "end_line": 4,
            })
            self.assertEqual([item["role"] for item in draft["form"]["references"]], [
                "reference_php", "reference_csharp", "reference_razor",
            ])
            self.assertIn('path = "updated.md" # Keep range', draft["content"])
            self.assertIn("[[project.migration_rules]]", draft["content"])
            self.assertIn("[[project.references]]", draft["content"])
            self.assertEqual(draft["content"].count("[[units.inputs]]"), 1)
            draft = preview(draft["content"], {"migrationRules": [rules[1]], "references": []})
            self.assertNotIn('path = "example.php"', draft["content"])
            self.assertNotIn('path = "updated.md"', draft["content"])
            self.assertIn('path = "constraints.md"', draft["content"])
            self.assertEqual(draft["form"]["migrationRules"], [
                {"index": 0, "role": "migration_rules", "path": "extra.md"},
            ])

        self.open_dashboard(inspect)

    def test_settings_cleanup_removes_only_empty_unit_at_its_index(self):
        content = (
            '[[units]]\nid = "existing"\nsource = "existing.php"\ntarget = "Existing.cs"\n'
            '[[units]]\nid = " "\ntargets = []\n'
            '[[units]]\nid = "added"\nsource = "added.php"\ntarget = "Added.cs"\n'
        )
        draft = build_settings_draft(content, {"removeEmptyUnits": [1]})
        self.assertEqual([unit["id"] for unit in draft["form"]["units"]], ["existing", "added"])
        with self.assertRaisesRegex(ValueError, "入力済み"):
            build_settings_draft(content, {"removeEmptyUnits": [0]})

    def test_settings_form_updates_build_and_checker_and_can_restore_defaults(self):
        content = (
            '[build]\nproject = "App.sln" # Build this project\n'
            '[checker]\ncommand = ["py", "check.py"]\nmax_repair_attempts = 2\n'
            '[project]\nbase_ref = "stable"\n'
        )
        form = build_settings_draft(content)["form"]
        self.assertEqual(form["build"]["project"], "App.sln")
        self.assertEqual(form["checker"], {"command": ["py", "check.py"], "max_repair_attempts": "2"})
        command = ["powershell.exe", "-File", "tools/check migration.ps1", ""]
        draft = build_settings_draft(content, {
            "build": {"project": "src/App.csproj"},
            "checker": {"command": command, "max_repair_attempts": "0"},
        })
        self.assertIn('project = "src/App.csproj" # Build this project', draft["content"])
        self.assertIn('base_ref = "stable"', draft["content"])
        self.assertIn("max_repair_attempts = 0", draft["content"])
        self.assertEqual(draft["form"]["checker"], {"command": command, "max_repair_attempts": "0"})

        cleared = build_settings_draft(draft["content"], {
            "build": {"project": ""},
            "checker": {"command": [], "max_repair_attempts": ""},
        })
        self.assertNotIn("max_repair_attempts =", cleared["content"])
        self.assertEqual(cleared["form"]["build"]["project"], "")
        self.assertEqual(cleared["form"]["checker"], {"command": [], "max_repair_attempts": ""})
        with self.assertRaisesRegex(ValueError, "0以上の整数"):
            build_settings_draft(draft["content"], {"checker": {"max_repair_attempts": "-1"}})

    def test_settings_form_edits_project_options_and_preserves_legacy_inputs(self):
        content = (
            '[project]\nroot = "app"\nbase_ref = "stable" # Keep base reference note\n'
            '[diff_viewer]\nanchor_reference = "old.workspace.json"\n'
            '[[units]]\nid = "legacy"\ntarget = "Legacy.cs"\nsource_encoding = "auto"\n'
            '[[units.inputs]]\nrole = "php"\npath = "legacy.php"\nstart_line = 1\nend_line = 2\ncustom = "keep"\n'
        )
        form = build_settings_draft(content)["form"]
        self.assertEqual(form["project"]["base_ref"], "stable")
        self.assertNotIn("inputs", form["units"][0])
        self.assertNotIn("source_encoding", form["units"][0])
        self.assertEqual(form["diff_viewer"]["anchor_reference"], "old.workspace.json")
        draft = build_settings_draft(content, {
            "project": {"base_ref": "main", "max_context_chars": "25000"},
            "junie": {"max_steps": "20"},
            "diff_viewer": {"html": "viewer.html", "enabled": "false"},
            "migrationRules": [{"role": "migration_rules", "path": "rules.md", "encoding": "utf-8"}],
            "units": {"0": {"id": "renamed"}},
        })
        self.assertIn('base_ref = "main" # Keep base reference note', draft["content"])
        self.assertIn('custom = "keep"', draft["content"])
        self.assertIn('source_encoding = "auto"', draft["content"])
        self.assertIn('anchor_reference = "old.workspace.json"', draft["content"])
        self.assertIn("enabled = false", draft["content"])
        self.assertEqual(draft["form"]["diff_viewer"]["html"], "viewer.html")
        self.assertEqual(draft["form"]["diff_viewer"]["enabled"], "false")
        self.assertIn('end_line = 2', draft["content"])
        self.assertEqual(draft["form"]["junie"]["max_steps"], "20")
        self.assertEqual(draft["form"]["migrationRules"][0]["encoding"], "utf-8")
        with self.assertRaisesRegex(ValueError, "1000以上"):
            build_settings_draft(content, {"project": {"max_context_chars": "999"}})

    # 保存済みRunを開くだけでは再開せず、操作を受けてから実行することを検証する。
    def test_saved_resumable_run_waits_for_explicit_resume(self):
        mock, calls = self.mock_commands(("dotnet", 0, b"rebuilt\n"), ("fixture-checker", 0, b"clean\n"))
        before = self.record_path.read_bytes()

        def inspect(url):
            """サーバーが動いている間に、今回のシナリオのHTTP操作を確認する。"""
            revision, state = self.get_ready_event(url)
            self.assertTrue(state["resumable"])
            self.assertEqual(state["status"], "checker_needs_review")
            self.assertEqual(calls, [])
            self.assertEqual(self.record_path.read_bytes(), before)
            with self.post(url, "control", {"action": "resume", "revision": revision}) as response:
                self.assertEqual(response.status, 202)

        with mock:
            result = self.open_dashboard(inspect, resume=True)
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["status"], "checker_succeeded")

    def test_resume_from_run_selection_rechecks_the_selected_run(self):
        self.config["diff_viewer_html"] = None
        mock, calls = self.mock_commands(("dotnet", 0, b"rebuilt\n"), ("fixture-checker", 0, b"clean\n"))

        def inspect(url):
            revision, state = self.get_ready_event(url)
            self.assertFalse(state["diff_viewer_enabled"])
            for endpoint, payload in (
                ("comparison", {}),
                ("control", {"action": "anchors", "revision": revision}),
            ):
                with self.assertRaises(HTTPError) as rejected:
                    self.post(url, endpoint, payload)
                with rejected.exception as response:
                    self.assertEqual(response.code, 409)
            with self.post(url, "control", {"action": "resume-run", "run_id": self.run_id, "revision": revision}) as response:
                self.assertEqual(response.status, 202)

        with mock:
            result = self.open_dashboard(inspect, resume=True)
        self.assertEqual(result["status"], "checker_succeeded")
        self.assertEqual(len(calls), 2)

    # 二つのCLI起動方法が同じ保存済み表示に入ることを検証する。
    def test_cli_view_and_resume_dashboard_open_saved_run(self):
        for args in (["view"], ["resume", "--dashboard"]):
            with (
                self.subTest(args=args),
                patch.object(sys, "argv", ["harness", *args, "--config", "fixture.toml", "--run-id", self.run_id]),
                patch.object(cli, "load_config", return_value=self.config),
                patch.object(dashboard, "run_dashboard", return_value=self.record()) as open_view,
                patch.object(cli, "resume_with_junie") as resume,
            ):
                cli.main()
                open_view.assert_called_once_with(self.config, run_id=self.run_id)
                resume.assert_not_called()
