import hashlib
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


    def explicit_build(self):
        plan = workflow.plan_build_resume(self.config, self.run_id)
        return workflow.resume_with_junie(self.config, self.run_id, verification_request={**plan, "id": "operator-build"})

    def configure_dependency_hook(self):
        self.configure("after_build")
        self.dependency = self.worktree / "dependency.ready"
        self.config["scripts"]["after_build"][0][2] = (
            "from pathlib import Path; import sys; p=Path('calls.txt'); "
            "p.write_text((p.read_text() if p.exists() else '')+'x'); "
            "ready=Path('dependency.ready').is_file(); "
            "print('1 test passed' if ready else 'Missing dependency.'); sys.exit(0 if ready else 1)"
        )

    def test_operator_build_rechecks_a_reported_failure_after_environment_recovery(self):
        self.configure_dependency_hook()
        informational = '{"verdict":"informational","evidence_lines":[1]}'
        passed = '{"verdict":"passed","evidence_lines":[1]}'
        with self.controlled_commands([informational, informational, passed]) as (builds, judge):
            with self.assertRaises(workflow.RunScriptError):
                self.resume_run()
            previous = self.record()["script_advisories"][-1]
            self.dependency.touch()
            with self.assertRaisesRegex(workflow.RunScriptError, "needs operator review"):
                self.resume_run()
            self.assertEqual(self.counter.read_text(), "x")
            self.assertEqual(self.explicit_build()["status"], "build_succeeded")
            self.assertEqual((len(builds), judge.call_count), (2, 3))
        self.assertEqual(self.counter.read_text(), "xx")
        record = self.record()
        self.assertNotIn("verification_script_pending", record)
        self.assertIn(previous, record["script_advisories"])
        log = (self.run_path / previous["log"]).read_bytes()
        start = previous["byte_offset"]
        self.assertEqual(log[start : start + previous["byte_length"]].strip(), b"Missing dependency.")

    def test_operator_build_still_stops_if_dependency_is_not_restored(self):
        self.configure_dependency_hook()
        with self.controlled_commands(['{"verdict":"informational","evidence_lines":[1]}'] * 2) as (builds, judge):
            with self.assertRaises(workflow.RunScriptError):
                self.resume_run()
            first = self.record()["verification_script_pending"]
            with self.assertRaises(workflow.RunScriptError):
                self.explicit_build()
            second = self.record()["verification_script_pending"]
            self.assertGreater(second["byte_offset"], first["byte_offset"])
            self.assertEqual((len(builds), judge.call_count), (2, 2))
        self.assertEqual(self.counter.read_text(), "xx")
        self.assertEqual(self.record()["status"], "script_failed")

    def test_stale_diagnostic_does_not_repair_any_target_after_hook_changes_one(self):
        for multi_target in (False, True):
            with self.subTest(multi_target=multi_target):
                self.configure("after_build")
                self.config["max_repair_attempts"] = 1
                self.target.write_text("class Original {}", encoding="utf-8")
                if multi_target:
                    secondary = self.worktree / "Secondary.cs"
                    secondary.write_text("class SecondaryOriginal {}", encoding="utf-8")
                    targets = [self.target.name, secondary.name]
                    self.config["units"][0]["targets"] = targets
                    self.save_record({**self.record(), "targets": targets})
                changed = secondary if multi_target else self.target
                original_hash = hashlib.sha256(changed.read_bytes()).hexdigest()
                self.config["scripts"]["after_build"][0][2] = (
                    f"from pathlib import Path; p=Path({changed.name!r})\n"
                    "if 'AlreadyFixed' in p.read_text(): print('1 test passed')\n"
                    "else:\n"
                    " print('T001 Example.cs:1 warning: class Original uses deprecated API')\n"
                    " p.write_text('class AlreadyFixed {}')"
                )
                problem = '{"verdict":"problem","evidence_lines":[1]}'
                passed = '{"verdict":"passed","evidence_lines":[1]}'
                with self.controlled_commands([problem, problem, passed]) as (builds, judge), patch.object(
                    workflow.junie_cli, "require_interactive_cli"
                ), patch.object(workflow, "_attempt_repair") as repair:
                    with self.assertRaises(workflow.RunScriptError):
                        self.resume_run()
                    pending = self.record()["verification_script_pending"]
                    self.assertEqual(pending["target_versions"][changed.name], original_hash)
                    repair.assert_not_called()
                    with self.assertRaisesRegex(workflow.RunScriptError, "target changed"):
                        self.resume_run()
                    repair.assert_not_called()
                    self.assertEqual((len(builds), judge.call_count), (1, 1))
                    judge.side_effect = [passed]
                    self.assertEqual(self.explicit_build()["status"], "build_succeeded")
                    self.assertEqual((len(builds), judge.call_count), (2, 2))
                self.assertEqual(changed.read_text(), "class AlreadyFixed {}")

    def test_operator_build_cannot_bypass_interrupted_or_modified_failure(self):
        self.configure_dependency_hook()
        informational = '{"verdict":"informational","evidence_lines":[1]}'
        with self.controlled_commands([KeyboardInterrupt, informational]) as (builds, judge):
            with self.assertRaises(workflow.RunScriptError):
                self.resume_run()
            self.dependency.touch()
            with self.assertRaisesRegex(workflow.RunScriptError, "needs operator review"):
                self.explicit_build()
            log = self.run_path / "after_build.log"
            log.write_bytes(log.read_bytes().replace(b"Missing dependency.", b"Changed dependency."))
            with self.assertRaisesRegex(workflow.RunScriptError, "missing or changed"):
                self.explicit_build()
            self.assertEqual((len(builds), judge.call_count), (1, 2))
        self.assertEqual(self.counter.read_text(), "x")

    def test_old_saved_hook_without_a_verifiable_target_requires_an_explicit_build(self):
        self.configure("after_build", first="2 tests passed")
        passed = '{"verdict":"passed","evidence_lines":[1]}'
        with self.controlled_commands([KeyboardInterrupt, passed]) as (builds, judge):
            with self.assertRaises(workflow.RunScriptError):
                self.resume_run()
            record = self.record()
            pending = record["verification_script_pending"]
            pending.pop("target_basis", None)
            self.save_record(record)
            with self.assertRaisesRegex(workflow.RunScriptError, "no verified target version"):
                self.resume_run()
            self.assertEqual((len(builds), judge.call_count), (1, 1))
            self.assertEqual(self.explicit_build()["status"], "build_succeeded")
            self.assertEqual((len(builds), judge.call_count), (2, 2))
        self.assertEqual(self.counter.read_text(), "xx")

    def test_hook_cannot_accept_success_output_from_before_its_target_edit(self):
        self.configure("after_build")
        self.config["scripts"]["after_build"][0][2] = (
            "from pathlib import Path; print('1 test passed'); "
            "Path('Example.cs').write_text('class Changed {}')"
        )
        original_hash = hashlib.sha256(self.target.read_bytes()).hexdigest()
        with self.controlled_commands(['{"verdict":"passed","evidence_lines":[1]}']) as (builds, judge):
            with self.assertRaisesRegex(workflow.RunScriptError, "target changed"):
                self.resume_run()
            self.assertEqual((len(builds), judge.call_count), (1, 1))
        record = self.record()
        self.assertEqual(record["status"], "script_failed")
        self.assertEqual(record["script_advisories"][-1]["applied"], "review_required")
        self.assertEqual(
            record["verification_script_pending"]["target_versions"][self.target.name],
            original_hash,
        )
