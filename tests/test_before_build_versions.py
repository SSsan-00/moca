import hashlib
import subprocess
import sys
from contextlib import contextmanager
from unittest.mock import patch

from fixtures import SavedRunFixture
from migration_harness import workflow


PASSED = '{"verdict":"passed","evidence_lines":[1]}'
PROBLEM = '{"verdict":"problem","evidence_lines":[1]}'
GENERATED_DIAGNOSTIC = "T001 Generated.cs:1 warning: class Generated uses deprecated API"


class BeforeBuildVersionTests(SavedRunFixture):
    def configure_hooks(self, target, *scripts):
        self.config.update(junie_review_script_output=True, junie_apply_output_decisions=True, max_repair_attempts=1)
        self.config["scripts"] = {"before_build": [[sys.executable, "-c", script] for script in scripts]}
        self.generator_output = target

    def resume_run(self):
        return workflow.resume_with_junie(self.config, self.run_id)

    def add_generated_target(self):
        target = self.worktree / "Generated.cs"
        target.write_text("class Original {}", encoding="utf-8")
        targets = [self.target.name, target.name]
        self.config["units"][0]["targets"] = targets
        self.save_record({**self.record(), "targets": targets})
        return target

    @contextmanager
    def commands(self, verdicts, repair=None):
        run_process = workflow.run_process
        builds = []

        def process(command, **kwargs):
            if command[0] == "dotnet":
                builds.append(self.generator_output.read_text(encoding="utf-8"))
            elif command[0] != "fixture-checker":
                self.assertEqual(command[0], sys.executable)
                return run_process(command, **kwargs)
            return subprocess.CompletedProcess(command, 0)

        with (
            patch.object(workflow, "run_process", side_effect=process),
            patch("migration_harness.verification.checker_advisory.run_text_advisory", side_effect=verdicts),
            patch.object(workflow.junie_cli, "require_interactive_cli"),
            patch.object(
                workflow, "_attempt_repair", side_effect=repair or AssertionError("unexpected automatic repair")
            ) as repair_attempt,
        ):
            yield builds, repair_attempt

    @staticmethod
    def generate(target):
        return (
            f"from pathlib import Path; p=Path({target.name!r}); "
            "p.write_text('class Generated {}') if 'Original' in p.read_text() else None"
        )

    def test_generated_target_is_validated_by_the_next_hook_then_built(self):
        target = self.add_generated_target()
        self.configure_hooks(
            target, self.generate(target),
            "from pathlib import Path; assert 'Generated' in Path('Generated.cs').read_text(); print('1 test passed')",
        )
        with self.commands([PASSED]) as (builds, repair):
            self.assertEqual(self.resume_run()["status"], "checker_succeeded")
            self.assertEqual(builds, ["class Generated {}"])
            repair.assert_not_called()

    def test_restore_old_target_does_not_apply_diagnostic_of_generated_version(self):
        target = self.add_generated_target()
        original = target.read_bytes()
        self.configure_hooks(target, self.generate(target), f"print({GENERATED_DIAGNOSTIC!r})")
        with self.commands([KeyboardInterrupt, PROBLEM]) as (builds, repair):
            with self.assertRaises(workflow.RunScriptError):
                self.resume_run()
            receipt = self.record()["verification_script_pending"]
            self.assertEqual(receipt["command_number"], 2)
            self.assertEqual(receipt["target_versions"][self.target.name], hashlib.sha256(self.target.read_bytes()).hexdigest())
            actual_version = hashlib.sha256(target.read_bytes()).hexdigest()
            target.write_bytes(original)
            with self.assertRaisesRegex(workflow.RunScriptError, "target changed"):
                self.resume_run()
            self.assertEqual(receipt["target_versions"][target.name], actual_version)
            self.assertEqual(builds, [])
            repair.assert_not_called()

    def test_immediate_repair_uses_the_version_actually_inspected(self):
        target = self.add_generated_target()
        self.configure_hooks(
            target, self.generate(target),
            "from pathlib import Path; "
            f"print({GENERATED_DIAGNOSTIC!r} if 'Generated' in Path('Generated.cs').read_text() else '1 test passed')",
        )

        def repair(_config, _targets, repair_targets, _worktree, _run_path, diagnostic, _record, _details):
            self.assertEqual(tuple(path.name for path in repair_targets), (target.name,))
            self.assertEqual(diagnostic, GENERATED_DIAGNOSTIC)
            self.assertEqual(target.read_text(encoding="utf-8"), "class Generated {}")
            target.write_text("class Repaired {}", encoding="utf-8")

        with self.commands([PROBLEM, PASSED], repair) as (builds, repair_attempt):
            self.assertEqual(self.resume_run()["status"], "checker_succeeded")
            self.assertEqual(builds, ["class Repaired {}"])
            repair_attempt.assert_called_once()

    def test_single_diagnostic_hook_that_changes_its_own_target_is_not_eligible_for_repair(self):
        target = self.target
        source = target.read_bytes()
        diagnostic = "T001 Example.cs:1 warning: class Original uses deprecated API"
        self.configure_hooks(target, f"{self.generate(target)}; print({diagnostic!r})")
        with self.commands([PROBLEM]) as (builds, repair):
            with self.assertRaises(workflow.RunScriptError):
                self.resume_run()
            with self.assertRaisesRegex(workflow.RunScriptError, "target changed"):
                self.resume_run()
            self.assertEqual(
                self.record()["verification_script_pending"]["target_versions"][target.name],
                hashlib.sha256(source).hexdigest(),
            )
            self.assertEqual(builds, [])
            repair.assert_not_called()

    def test_old_before_build_phase_receipt_is_not_treated_as_a_precommand_receipt(self):
        target = self.add_generated_target()
        original = target.read_bytes()
        self.configure_hooks(target, self.generate(target), f"print({GENERATED_DIAGNOSTIC!r})")
        with self.commands([KeyboardInterrupt, PROBLEM]) as (builds, repair):
            with self.assertRaises(workflow.RunScriptError):
                self.resume_run()
            record = self.record()
            receipt = record["verification_script_pending"]
            receipt.update(target_basis="verification")
            receipt["target_versions"][target.name] = hashlib.sha256(original).hexdigest()
            self.save_record(record)
            target.write_bytes(original)
            with self.assertRaisesRegex(workflow.RunScriptError, "no verified target version"):
                self.resume_run()
            self.assertEqual(builds, [])
            repair.assert_not_called()
