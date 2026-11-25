import io
import json
import subprocess
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from fixtures import SavedRunFixture
from migration_harness import workflow
from migration_harness.execution import OperationCancelled
from migration_harness.verification import roslyn_syntax


INVALID = "class Example { int Value = ; }\n"
VALID = "class Example { int Value = 42; }\n"


class RoslynPreflightTests(SavedRunFixture):
    def setUp(self):
        super().setUp()
        self.config.update(checker_command=None, max_repair_attempts=3)
        record = self.record()
        record["status"] = "build_failed"
        self.save_record(record)

    def run_verification(self, *, repair=None, build=True):
        command_patch, calls = self.mock_commands(*([("dotnet", 0, b"Build succeeded\n")] if build else []))
        with command_patch, patch.object(workflow, "_repair_target", side_effect=repair) as junie, redirect_stdout(io.StringIO()):
            result = workflow.resume_with_junie(self.config, self.run_id)
        return result, calls, junie.call_count

    def test_clear_still_builds_with_no_preflight_artifact(self):
        self.target.write_text(VALID, encoding="utf-8")
        result, calls, attempts = self.run_verification()
        self.assertEqual(result["status"], "build_succeeded")
        self.assertEqual(len(calls), 1)
        self.assertEqual(attempts, 0)
        self.assertEqual(self.record()["roslyn_preflight"]["status"], "clear")
        self.assertFalse((self.worktree / "bin").exists())
        self.assertFalse((self.worktree / "obj").exists())

    def test_repairs_syntax_before_only_formal_build(self):
        self.target.write_text(INVALID, encoding="utf-8")

        def repair(config, targets, affected, worktree, run_path, findings, record):
            self.assertEqual(affected, (Path(self.target.name),))
            self.assertIn("CS1525", findings)
            self.assertFalse((self.worktree / "obj").exists())
            self.target.write_text(VALID, encoding="utf-8")

        result, calls, attempts = self.run_verification(repair=repair)
        self.assertEqual(result["status"], "build_succeeded")
        self.assertEqual((len(calls), attempts), (1, 1))
        record = self.record()
        self.assertEqual(len(record["roslyn_preflight_seconds"]), 2)
        self.assertTrue(record["repair_history"][0]["roslyn_preflight"])

    def test_no_edits_blocks_without_build_and_does_not_retry_after_resume(self):
        self.target.write_text(INVALID, encoding="utf-8")
        for attempts in (1, 0):
            result, calls, actual = self.run_verification(repair=lambda *args: None, build=False)
            self.assertEqual((len(calls), actual), (0, attempts))
            self.assertEqual(result["status"], "build_failed")
            self.assertIsNone(result["build_exit_code"])
            self.assertIsNone(result["build_log_path"])
            record = self.record()
            self.assertEqual(record["roslyn_preflight"]["reason"], "no_edits")
            self.assertNotIn("checker_build", record)
            self.assertNotIn("build_operation_id", record)
            self.assertIn("CS1525", json.dumps(record["roslyn_preflight"]))
            review = workflow.build_failure_review(self.config, self.run_id)
            self.assertEqual(review["review_kind"], "roslyn_preflight")
            self.assertEqual(review["preferred_target"], self.target.name)
            self.assertIn("CS1525", review["errors"][0])
            self.assertEqual(review["log"], "")
        self.target.write_text(VALID, encoding="utf-8")
        self.assertEqual(self.run_verification()[0]["status"], "build_succeeded")

    def test_same_finding_despite_moved_line_stops_and_preserves_edit(self):
        self.target.write_text(INVALID, encoding="utf-8")
        modified = "\n" + INVALID
        def repair(*args):
            self.target.write_text(modified, encoding="utf-8")
        result, calls, attempts = self.run_verification(repair=repair, build=False)
        self.assertEqual((len(calls), attempts), (0, 1))
        self.assertEqual(result["roslyn_preflight"]["reason"], "repair_stalled")
        self.assertEqual(self.target.read_text(encoding="utf-8"), modified)

    def test_unavailable_and_timeout_use_the_original_build(self):
        self.target.write_text(VALID, encoding="utf-8")
        with patch.object(roslyn_syntax, "_prepare_tool", side_effect=OSError("missing SDK")):
            result, calls, attempts = self.run_verification()
            self.assertEqual((result["status"], len(calls), attempts), ("build_succeeded", 1, 0))
            self.assertEqual(self.record()["roslyn_preflight"]["reason"], "analyzer_unavailable")
        with patch.object(roslyn_syntax, "_checked_process", side_effect=subprocess.TimeoutExpired("dotnet", 4)):
            result, calls, attempts = self.run_verification()
            self.assertEqual((result["status"], len(calls), attempts), ("build_succeeded", 1, 0))
            self.assertEqual(self.record()["roslyn_preflight"]["reason"], "timeout")

    def test_conditional_source_is_not_a_blocking_guess(self):
        self.target.write_text("#if FLAG\n" + INVALID + "#endif\n", encoding="utf-8")
        result, calls, attempts = self.run_verification()
        self.assertEqual((result["status"], len(calls), attempts), ("build_succeeded", 1, 0))
        self.assertEqual(self.record()["roslyn_preflight"]["skipped"][0]["reason"], "project_preprocessor_required")

    def test_two_attempt_limit_even_with_strict_progress(self):
        self.target.write_text(VALID, encoding="utf-8")

        def report(*_):
            findings = [
                dict(path=self.target.name, line=1, column=1, start=0, length=1, code=f"CS000{index}", message="error")
                for index in range(3 - len(repairs))
            ]
            return dict(status="errors", findings=findings, truncated=False, seconds=0)

        repairs = []
        def repair(*_):
            repairs.append(None)
            self.target.write_text(VALID * (len(repairs) + 1), encoding="utf-8")

        with patch.object(workflow, "syntax_diagnostics", side_effect=report):
            result, commands, attempts = self.run_verification(repair=repair, build=False)
        self.assertEqual((len(commands), attempts), (0, 2))
        self.assertEqual(result["roslyn_preflight"]["reason"], "repair_budget")

    def test_operator_stop_keeps_saved_target_and_does_not_build(self):
        self.target.write_text(INVALID, encoding="utf-8")
        command_patch, calls = self.mock_commands()
        with command_patch, redirect_stdout(io.StringIO()), patch.object(
            roslyn_syntax, "_checked_process", side_effect=OperationCancelled(),
        ):
            with self.assertRaises(workflow.RunInterrupted):
                workflow.resume_with_junie(self.config, self.run_id)
        self.assertEqual(calls, [])
        self.assertEqual(self.record()["roslyn_preflight"]["status"], "cancelled")
        self.assertEqual(workflow.build_failure_review(self.config, self.run_id)["log"], "")
        self.assertEqual(self.target.read_text(encoding="utf-8"), INVALID)
        self.target.write_text(VALID, encoding="utf-8")
        self.assertEqual(self.run_verification()[0]["status"], "build_succeeded")

    def test_junie_session_failure_stops_without_spending_attempt_and_explicit_resume_retries(self):
        self.target.write_text(INVALID, encoding="utf-8")
        failure = workflow.junie_cli.JunieSessionError(["junie"], 1, "synthetic provider unavailable")
        result, calls, attempts = self.run_verification(repair=failure, build=False)
        self.assertEqual((len(calls), attempts), (0, 1))
        self.assertEqual(result["roslyn_preflight"]["reason"], "junie_unavailable")
        self.assertEqual(self.record()["repair_history"], [])
        self.assertEqual(self.target.read_text(encoding="utf-8"), INVALID)

        result, calls, attempts = self.run_verification(
            repair=lambda *_: self.target.write_text(VALID, encoding="utf-8"),
        )
        self.assertEqual((result["status"], len(calls), attempts), ("build_succeeded", 1, 1))
        self.assertEqual(len(self.record()["repair_history"]), 1)

    def test_before_build_hook_output_is_diagnosed(self):
        self.target.write_text(VALID, encoding="utf-8")
        def before_build(config, phase, *args):
            self.assertEqual(phase, "before_build")
            self.target.write_text(INVALID, encoding="utf-8")
            return False
        with patch.object(workflow, "_verification_script", side_effect=before_build):
            result, calls, attempts = self.run_verification(repair=lambda *args: None, build=False)
        self.assertEqual((len(calls), attempts), (0, 1))
        self.assertEqual(result["roslyn_preflight"]["reason"], "no_edits")

    def test_razor_and_editor_diagnose_without_enforcing_guessed_project_context(self):
        razor = self.worktree / "Page.cshtml"
        razor.write_text("@{", encoding="utf-8")
        diagnostics = roslyn_syntax.syntax_diagnostics(self.worktree, self.run_path, [Path(razor.name)])
        self.assertEqual((diagnostics["status"], diagnostics["reason"]), ("clear", "razor_advisory"))
        self.assertEqual([f["code"] for f in diagnostics["razor"]], ["RZ1006"])
        editor = roslyn_syntax.editor_diagnostics(razor.name, "@(Model.Value", "v2")
        self.assertEqual((editor["status"], editor["version"]), ("errors", "v2"))
        self.assertEqual((editor["razor"][0]["line"], editor["razor"][0]["column"]), (1, 2))
        self.assertEqual(razor.read_text(encoding="utf-8"), "@{")
        self.assertEqual(roslyn_syntax.editor_diagnostics("source.php", "<?php", "v3")["reason"], "unsupported_file_type")

    def test_review_diff_uses_unified_source_paths(self):
        self.target.write_text(INVALID, encoding="utf-8")
        agent = self.run_path / "agent-workspace"
        agent.mkdir()
        (agent / self.target.name).write_text(VALID, encoding="utf-8")
        text, truncated = workflow._roslyn_repair_diff(self.worktree, agent, [Path(self.target.name)])
        self.assertFalse(truncated)
        self.assertIn("--- a/Example.cs", text)
        self.assertIn("+class Example { int Value = 42; }", text)
