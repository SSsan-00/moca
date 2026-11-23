import json
import subprocess
import sys
from contextlib import contextmanager
from unittest.mock import patch

from fixtures import SavedRunFixture
from migration_harness import workflow


class InterruptedVerificationScriptTests(SavedRunFixture):
    def configure(self, phase, first="Unreconciled amount"):
        self.config.update(junie_apply_output_decisions=True, junie_review_script_output=True)
        self.counter = self.worktree / "calls.txt"
        self.config["scripts"] = {phase: [[sys.executable, "-c",
            "from pathlib import Path; p=Path('calls.txt'); old=p.read_text() if p.exists() else ''; "
            f"p.write_text(old+'x'); print('1 test passed' if old else {first!r})"]]}

    @contextmanager
    def controlled_commands(self, answer):
        real = workflow.run_process
        builds = []

        def child(command, **kwargs):
            if command[0] in {"dotnet", "fixture-checker"}:
                if command[0] == "dotnet":
                    builds.append(1)
                return subprocess.CompletedProcess(command, 0)
            self.assertEqual(command[0], sys.executable)
            return real(command, **kwargs)

        with patch.object(workflow, "run_process", side_effect=child), patch(
            "migration_harness.verification.checker_advisory.run_text_advisory", side_effect=answer,
        ) as judge:
            yield builds, judge

    def resume_run(self):
        return workflow.resume_with_junie(self.config, self.run_id)

    def verify_ambiguous(self, phase):
        self.configure(phase)
        with self.controlled_commands([KeyboardInterrupt, '{"verdict":"informational","evidence_lines":[1]}']) as (builds, judge):
            with self.assertRaises(workflow.RunScriptError):
                self.resume_run()
            with self.assertRaisesRegex(workflow.RunScriptError, "needs operator review"):
                self.resume_run()
            lines = [json.loads(call.args[0].split("\n", 1)[1])["untrusted_lines"] for call in judge.call_args_list]
            self.assertEqual(lines, [["Unreconciled amount"], ["Unreconciled amount"]])
            self.assertEqual(len(builds), 1)
        self.assertEqual(self.counter.read_text(), "x")
        self.assertEqual(self.record()["status"], "script_failed")
        self.assertEqual(self.record()["script_advisories"][-1]["applied"], "review_required")

    def test_after_build_keeps_original_warning_until_reviewed(self):
        self.verify_ambiguous("after_build")

    def test_after_checker_keeps_original_warning_until_reviewed(self):
        self.verify_ambiguous("after_checker")

    def test_corroborated_prior_output_still_requires_new_build_and_new_hook(self):
        self.configure("after_checker", first="2 tests passed")
        good = '{"verdict":"passed","evidence_lines":[1]}'
        with self.controlled_commands([KeyboardInterrupt, good, good]) as (builds, judge):
            with self.assertRaises(workflow.RunScriptError):
                self.resume_run()
            self.assertEqual(self.resume_run()["status"], "checker_succeeded")
            self.assertEqual(len(builds), 2)
            lines = [json.loads(call.args[0].split("\n", 1)[1])["untrusted_lines"] for call in judge.call_args_list]
            self.assertEqual(lines, [["2 tests passed"], ["2 tests passed"], ["1 test passed"]])
        self.assertEqual(self.counter.read_text(), "xx")
        self.assertNotIn("verification_script_pending", self.record())

    def test_cannot_skip_an_interrupted_review_by_disabling_review_changing_hook_or_log(self):
        self.configure("after_build", first="2 tests passed")
        with self.controlled_commands([KeyboardInterrupt]) as (builds, judge):
            with self.assertRaises(workflow.RunScriptError):
                self.resume_run()
            saved = self.record()["verification_script_pending"]
            with patch.dict(self.config, junie_apply_output_decisions=False), self.assertRaises(workflow.RunScriptError):
                self.resume_run()
            with patch.dict(self.config, scripts={}), self.assertRaises(workflow.RunScriptError):
                self.resume_run()
            with patch.dict(self.config, scripts={}), self.assertRaisesRegex(ValueError, "saved verification script"):
                workflow.plan_checker_resume(self.config, self.run_id)
            log = self.run_path / saved["log"]
            log.write_bytes(log.read_bytes().replace(b"2 tests passed", b"8 tests passed"))
            with self.assertRaises(workflow.RunScriptError):
                self.resume_run()
            self.assertEqual(len(builds), 1)
            self.assertEqual(judge.call_count, 1)
        self.assertEqual(self.counter.read_text(), "x")
        self.assertEqual(self.record()["verification_script_pending"], saved)

    def test_changed_target_needs_explicit_rebuild_not_a_stale_decision(self):
        self.configure("after_build")
        with self.controlled_commands([KeyboardInterrupt, '{"verdict":"passed","evidence_lines":[1]}']) as (builds, judge):
            with self.assertRaises(workflow.RunScriptError):
                self.resume_run()
            self.target.write_text("class OperatorCorrection {}")
            with self.assertRaisesRegex(workflow.RunScriptError, "target changed"):
                self.resume_run()
            self.assertEqual(self.counter.read_text(), "x")
            self.assertEqual(judge.call_count, 1)
            plan = workflow.plan_build_resume(self.config, self.run_id)
            request = {**plan, "id": "operator-build", "intent": "build", "start_stage": "build"}
            result = workflow.resume_with_junie(self.config, self.run_id, verification_request=request)
            self.assertEqual(result["status"], "build_succeeded")
            self.assertEqual(len(builds), 2)
        self.assertEqual(self.counter.read_text(), "xx")
        self.assertNotIn("verification_script_pending", self.record())
        self.assertIn("rebuild_edited_target", [a.get("applied") for a in self.record()["script_advisories"]])
