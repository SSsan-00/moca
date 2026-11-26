import hashlib
import json
import subprocess
import sys
from contextlib import contextmanager
from unittest.mock import patch

from tests.support.fixtures import SavedRunFixture
from migration_harness import workflow
from migration_harness import execution
from migration_harness.junie import junie_cli
from migration_harness.verification import repairs


class InterruptedVerificationScriptTests(SavedRunFixture):
    def configure(self, phase, first="Unreconciled amount"):
        self.config.update(junie_apply_output_decisions=True, junie_review_script_output=True)
        self.counter = self.worktree / "calls.txt"
        self.config["scripts"] = {phase: [[sys.executable, "-c",
            "from pathlib import Path; p=Path('calls.txt'); old=p.read_text() if p.exists() else ''; "
            f"p.write_text(old+'x'); print('1 test passed' if old else {first!r})"]]}

    @contextmanager
    def controlled_commands(self, answer):
        real = execution.run_process
        builds = []

        def child(command, **kwargs):
            if command[0] in {"dotnet", "fixture-checker"}:
                if command[0] == "dotnet":
                    builds.append(1)
                return subprocess.CompletedProcess(command, 0)
            self.assertEqual(command[0], sys.executable)
            return real(command, **kwargs)

        with patch.object(execution, "run_process", side_effect=child), patch(
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


    def test_build_through_still_reviews_current_pending_output_and_can_repair_it(self):
        self.configure("after_build", first="T001 Example.cs:1 warning: class Original uses deprecated API")
        self.config["max_repair_attempts"] = 1
        informational = '{"verdict":"informational","evidence_lines":[1]}'
        problem = '{"verdict":"problem","evidence_lines":[1]}'
        passed = '{"verdict":"passed","evidence_lines":[1]}'
        with (self.controlled_commands([KeyboardInterrupt, informational, problem, passed]) as (builds, judge),
              patch.object(junie_cli, "require_interactive_cli"),
              patch.object(repairs, "attempt_repair") as repair):
            with self.assertRaises(workflow.RunScriptError):
                self.resume_run()
            plan = workflow.plan_build_resume(self.config, self.run_id, through_completion=True)
            with self.assertRaisesRegex(workflow.RunScriptError, "needs operator review"):
                workflow.resume_with_junie(self.config, self.run_id, verification_request={**plan, "id": "review"})
            self.assertEqual((len(builds), judge.call_count), (1, 2))
            repair.assert_not_called()
            plan = workflow.plan_build_resume(self.config, self.run_id, through_completion=True)
            self.assertEqual(workflow.resume_with_junie(
                self.config, self.run_id, verification_request={**plan, "id": "repair"},
            )["status"], "checker_succeeded")
            repair.assert_called_once()
            self.assertEqual((len(builds), judge.call_count), (2, 4))

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
                passed = '{"verdict":"passed","evidence_lines":[1]}'
                with self.controlled_commands([passed]) as (builds, judge), patch.object(
                    junie_cli, "require_interactive_cli"
                ), patch.object(repairs, "attempt_repair") as repair:
                    with self.assertRaises(workflow.RunScriptError):
                        self.resume_run()
                    pending = self.record()["verification_script_pending"]
                    self.assertEqual(pending["target_versions"][changed.name], original_hash)
                    repair.assert_not_called()
                    with self.assertRaisesRegex(workflow.RunScriptError, "target changed"):
                        self.resume_run()
                    repair.assert_not_called()
                    self.assertEqual((len(builds), judge.call_count), (1, 0))
                    self.assertEqual(self.explicit_build()["status"], "build_succeeded")
                    self.assertEqual((len(builds), judge.call_count), (2, 1))
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
            self.assertEqual((len(builds), judge.call_count), (1, 0))
        record = self.record()
        self.assertEqual(record["status"], "script_failed")
        self.assertEqual(record["script_advisories"][-1]["applied"], "review_required")
        self.assertEqual(
            record["verification_script_pending"]["target_versions"][self.target.name],
            original_hash,
        )


    def test_silent_hooks_cannot_accept_verification_of_previous_target(self):
        for phase in ("after_build", "before_checker", "after_checker"):
            with self.subTest(phase=phase):
                self.configure(phase)
                self.target.write_text("class Original {}", encoding="utf-8")
                self.config["scripts"][phase][0][2] = (
                    "from pathlib import Path; Path('Example.cs').write_text('class Changed {}')"
                )
                with self.controlled_commands([]) as (builds, judge):
                    with self.assertRaisesRegex(workflow.RunScriptError, "target changed"):
                        self.resume_run()
                    self.assertEqual((len(builds), judge.call_count), (1, 0))
                self.assertEqual(self.record()["status"], "script_failed")

    def test_silent_before_build_hook_may_generate_the_target_that_is_then_built(self):
        self.configure("before_build")
        self.config["scripts"]["before_build"][0][2] = (
            "from pathlib import Path; Path('Example.cs').write_text('class Generated {}')"
        )
        with self.controlled_commands([]) as (builds, judge):
            self.assertEqual(self.resume_run()["status"], "checker_succeeded")
            self.assertEqual((len(builds), judge.call_count), (1, 0))
        self.assertEqual(
            self.record()["verification"]["target_hashes"][self.target.name],
            hashlib.sha256(b"class Generated {}").hexdigest(),
        )


    def test_explicit_build_revalidates_silent_hook_even_without_advisory_review(self):
        self.configure("after_build")
        self.config["junie_apply_output_decisions"] = False
        self.config["junie_review_script_output"] = False
        hook = self.config["scripts"]["after_build"][0]
        hook[2] = (
            "from pathlib import Path; p=Path('Example.cs'); "
            "p.write_text('class Original {}' if 'Changed' in p.read_text() else 'class Changed {}')"
        )
        with self.controlled_commands([]) as (builds, judge):
            with self.assertRaisesRegex(workflow.RunScriptError, "target changed"):
                self.resume_run()
            with self.assertRaisesRegex(workflow.RunScriptError, "target changed"):
                self.explicit_build()
            hook[2] = "pass"
            self.assertEqual(self.explicit_build()["status"], "build_succeeded")
            self.assertEqual((len(builds), judge.call_count), (3, 0))

    def configure_raw_failure(self, failure="b'x'*140000+b'\\n'"):
        self.configure_dependency_hook()
        self.config["scripts"]["after_build"][0][2] = (
            "from pathlib import Path; import sys; p=Path('calls.txt'); "
            "p.write_text((p.read_text() if p.exists() else '')+'x'); "
            "ready=Path('dependency.ready').exists(); "
            f"sys.stdout.buffer.write(b'1 test passed\\n' if ready else {failure}); "
            "sys.exit(0 if ready else 1)"
        )

    def stop_on_failure_then_edit(self):
        with self.assertRaises(workflow.RunScriptError):
            self.resume_run()
        self.target.write_text("class OperatorCorrection {}")
        return self.record()["verification_script_pending"]

    def test_explicit_build_checks_whole_large_output_without_sending_it_to_junie(self):
        self.configure_raw_failure()
        with self.controlled_commands(['{"verdict":"passed","evidence_lines":[1]}']) as (builds, judge):
            first = self.stop_on_failure_then_edit()
            old_advice = self.record()["script_advisories"][-1]
            judge.assert_not_called()
            self.dependency.touch()
            log = self.run_path / first["log"]
            original = log.read_bytes()
            with log.open("ab") as out:
                out.write(b"later output outside the recorded command\n")
            self.assertEqual(self.explicit_build()["status"], "build_succeeded")
            self.assertEqual((len(builds), self.counter.read_text()), (2, "xx"))
            judge.assert_called_once()
            self.assertEqual(json.loads(judge.call_args.args[0].split("\n", 1)[1])["untrusted_lines"], ["1 test passed"])
            self.assertTrue(log.read_bytes().startswith(original))
            self.assertIn(old_advice, self.record()["script_advisories"])
            self.assertEqual(old_advice["reason"], "input_too_large")
            self.assertNotIn("verification_script_pending", self.record())

    def test_large_output_tampering_blocks_rebuild_and_failed_retry_keeps_both_diagnostics(self):
        self.configure_raw_failure()
        with self.controlled_commands([]) as (builds, judge):
            first = self.stop_on_failure_then_edit()
            old_advice = self.record()["script_advisories"][-1]
            log = self.run_path / first["log"]
            original = log.read_bytes()
            for name, cut in (("middle", first["byte_offset"] + 70000), ("last", len(original) - 1),
                              ("truncated", None), ("missing", None)):
                with self.subTest(name):
                    if name == "missing":
                        log.unlink()
                    elif cut is None:
                        log.write_bytes(original[:-1])
                    else:
                        log.write_bytes(original[:cut] + b"!" + original[cut+1:])
                    with self.assertRaisesRegex(workflow.RunScriptError, "missing or changed"):
                        self.explicit_build()
                    self.assertEqual(self.record()["verification_script_pending"], first)
                    self.assertEqual(len(builds), 1)
                    log.write_bytes(original)
            with self.assertRaisesRegex(workflow.RunScriptError, "32 KiB"):
                self.explicit_build()
            second = self.record()["verification_script_pending"]
            self.assertGreater(second["byte_offset"], first["byte_offset"])
            self.assertEqual((len(builds), self.counter.read_text()), (2, "xx"))
            self.assertIn(old_advice, self.record()["script_advisories"])
            self.assertEqual(self.record()["status"], "script_failed")
            judge.assert_not_called()

    def test_old_prefix_digest_cannot_authorize_explicit_build_of_edited_target(self):
        self.configure_raw_failure()
        with self.controlled_commands([]) as (builds, judge):
            self.stop_on_failure_then_edit()
            record = self.record()
            saved = record["verification_script_pending"]
            saved.pop("log_hash_scope", None)
            offset = saved["byte_offset"]
            raw = (self.run_path / saved["log"]).read_bytes()
            saved["log_sha256"] = hashlib.sha256(raw[offset:offset+32769]).hexdigest()
            self.save_record(record)
            with self.assertRaisesRegex(workflow.RunScriptError, "full-range hash"):
                self.explicit_build()
            self.assertEqual((len(builds), self.counter.read_text()), (1, "x"))
            judge.assert_not_called()


    def test_unedited_target_does_not_rerun_unreviewed_oversized_failure(self):
        self.configure_raw_failure()
        with self.controlled_commands([]) as (builds, judge):
            with self.assertRaisesRegex(workflow.RunScriptError, "32 KiB"):
                self.resume_run()
            first = self.record()["verification_script_pending"]
            self.dependency.touch()
            with self.assertRaisesRegex(workflow.RunScriptError, "32 KiB"):
                self.explicit_build()
            (self.run_path / first["log"]).unlink()
            with self.assertRaisesRegex(workflow.RunScriptError, "missing or changed"):
                self.explicit_build()
            self.assertEqual((len(builds), self.counter.read_text()), (1, "x"))
            judge.assert_not_called()

    def test_old_complete_binary_digest_allows_rebuild_without_decoding_or_sending_old_bytes(self):
        self.configure_raw_failure("b'\\xff'*32769")
        with self.controlled_commands(['{"verdict":"passed","evidence_lines":[1]}']) as (builds, judge):
            self.stop_on_failure_then_edit()
            record = self.record()
            record["verification_script_pending"].pop("log_hash_scope")
            self.save_record(record)
            self.dependency.touch()
            self.assertEqual(self.explicit_build()["status"], "build_succeeded")
            self.assertEqual((len(builds), self.counter.read_text()), (2, "xx"))
            judge.assert_called_once()
            self.assertIn("1 test passed", judge.call_args.args[0])
