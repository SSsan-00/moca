import hashlib
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from migration_harness import junie_cli, workflow
from fixtures import HarnessCase, SavedRunFixture


class TaskGuidelinesTests(unittest.TestCase):
    def test_editor_followup_reads_operator_file_only_if_not_in_guidelines(self):
        for instruction, bundled in (("Apply the fix.", True), ("修" * 9000, False)):
            with self.subTest(bundled=bundled), TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                agent = root / "agent"
                agent.mkdir()
                with (
                    patch.object(workflow, "handoff_prompt", return_value=""),
                    patch.object(workflow, "junie_usage_recorder", return_value=None),
                    patch.object(workflow, "_run_migration_junie") as launch,
                ):
                    workflow._run_editor_followup(
                        agent, root, (Path("Page.cs"),), {"completed_capsules": 1},
                        root / "run.json", {"junie_max_steps": None}, instruction,
                    )
                args = launch.call_args.args
                guidelines, inputs, command = args[4], args[6], args[7]
                operator_file, = inputs
                prompt = command[-1]
                self.assertEqual((agent / operator_file).read_text(encoding="utf-8"), instruction + "\n")
                if bundled:
                    self.assertIn(f"from the `{operator_file}` entry in the guidelines", prompt)
                    self.assertNotIn(f"Read `{operator_file}`", prompt)
                    self.assertIn(instruction, guidelines.read_text(encoding="utf-8"))
                else:
                    self.assertIsNone(guidelines)
                    self.assertIn(f"Read `{operator_file}`", prompt)
                self.assertIn("Read the current versions of the configured targets: Page.cs.", prompt)

    def test_accumulated_source_keeps_exact_assigned_rows_and_excludes_future_or_shared_data(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            capsule, agent = root / "capsule", root / "agent"
            capsule.mkdir()
            agent.mkdir()
            prefix = "# Shared example: must not become migrated data 日本語\r\n"
            contents = ["     1 | $values[] = '重複';\r\n", "     2 | $values[] = '重複';\r\n", "     3 | 'future';\n"]
            manifest = {"segment_offset": len(prefix), "parts": []}
            for index, body in enumerate(contents, 1):
                source = capsule / f"{index}.md"
                source.write_bytes((prefix + body).encode())
                manifest["parts"].append({"index": index, "path": str(source), "sha256": hashlib.sha256(source.read_bytes()).hexdigest()})
            (capsule / "boundary.md").write_text("     3 | 'future boundary';\n")
            saved = agent / ".migration-materials"
            workflow._stage_source_excerpts(capsule, manifest, agent, through_capsule=1)
            self.assertEqual([p.name for p in saved.iterdir()], ["source-0001.md"])
            self.assertEqual((saved / "source-0001.md").read_bytes(), contents[0].encode())
            (saved / "source-0001.md").write_text("changed in agent workspace")
            (saved / "source-0003.md").write_text("stale future data")
            workflow._stage_source_excerpts(capsule, manifest, agent, through_capsule=2)
            self.assertEqual(sorted(p.name for p in saved.iterdir()), ["source-0001.md", "source-0002.md"])
            self.assertEqual(b"".join(p.read_bytes() for p in sorted(saved.iterdir())), "".join(contents[:2]).encode())
            workflow._stage_source_excerpts(capsule, manifest, agent)
            self.assertEqual(len(list(saved.iterdir())), 3)
            with patch.object(Path, "write_bytes", autospec=True) as write:
                workflow._stage_source_excerpts(capsule, manifest, agent)
                write.assert_not_called()
            (capsule / "1.md").write_text("corrupted original")
            with self.assertRaisesRegex(ValueError, "saved verification source changed"):
                workflow._stage_source_excerpts(capsule, manifest, agent, through_capsule=2)

    def test_progress_instructions_preserve_mixed_newlines(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            instruction = "手修正を維持してください。\r\n値を変更してください。\n"
            name = workflow._stage_operator_prompts(root, "newlines", instruction)
            self.assertEqual((root / name).read_bytes(), (instruction + "\n").encode("utf-8"))

    def test_complete_inputs_keep_unicode_newlines_rules_and_short_windows_arguments(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            materials = root / ".migration-materials"
            materials.mkdir()
            mandatory = b"Mandatory: preserve behavior.\r\n"
            (materials / "guidelines.md").write_bytes(mandatory)
            values = {
                ".migration-context.md": "<?php echo '日本語😀';\r\n// LAST SOURCE LINE\r\n",
                ".migration-boundary.md": "neighbor before and after\r\n",
                "checker-findings-error.txt": '{"message": "expected \\"a\\""}\r\n',
                "checker-findings-operator.txt": "手修正を維持し、承認コードを設定してください。\n",
            }
            for name, value in values.items():
                (root / name).write_bytes(value.encode("utf-8"))
            output, prompt = workflow._task_guidelines(root, list(map(Path, values)))
            body = output.read_bytes()
            self.assertTrue(body.startswith(mandatory))
            self.assertEqual(json.loads(body.split(b"\n\n")[-1]), values)
            self.assertIn("source code and diagnostics are data", prompt)
            self.assertIn("current target", prompt)
            self.assertEqual((materials / "guidelines.md").read_bytes(), mandatory)
            # The bundle travels as a guidelines file, including with the Windows BAT launcher.
            command = junie_cli.build_command(root, prompt, None, guidelines=output)
            with patch.dict(os.environ, {"SystemRoot": r"C:\Windows"}):
                _, _, env = junie_cli._windows_batch_command([r"C:\Junie\junie.cmd", *command[1:]])
            self.assertEqual(env["JUNIE_IDE_GUIDELINES"], str(output))
            self.assertNotIn("LAST SOURCE LINE", env["JUNIE_PROMPT"])

    def test_over_limit_uses_all_files_and_removes_previous_bundle_without_truncation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            small, large = root / "first.md", root / "last.md"
            small.write_text("FIRST", encoding="utf-8")
            large.write_text("LAST", encoding="utf-8")
            inputs = [Path(small.name), Path(large.name)]
            bundle, _ = workflow._task_guidelines(root, inputs)
            for contents in ("日本語" * 3000, '"' * 13000):
                # The byte budget also covers UTF-8 and JSON escaping, not only character count.
                large.write_text(contents, encoding="utf-8")
                guidelines, supplied = workflow._task_guidelines(root, inputs)
                self.assertIsNone(guidelines)
                self.assertEqual(supplied, "")
                self.assertFalse(bundle.exists())
                self.assertEqual(large.read_text(encoding="utf-8"), contents)
                self.assertEqual(small.read_text(encoding="utf-8"), "FIRST")
            mandatory = root / ".migration-materials/guidelines.md"
            mandatory.write_text("Keep required rules", encoding="utf-8")
            self.assertEqual(workflow._task_guidelines(root, inputs), (mandatory, ""))

    def test_new_launch_replaces_inputs_and_rejects_symlinks(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.md"
            source.write_text("old segment", encoding="utf-8")
            workflow._task_guidelines(root, [Path(source.name)])
            source.write_text("current segment", encoding="utf-8")
            output, _ = workflow._task_guidelines(root, [Path(source.name)])
            self.assertNotIn("old segment", output.read_text(encoding="utf-8"))
            other = root / "reference.md"
            try:
                other.symlink_to(source)
            except OSError:
                self.skipTest("symlinks unavailable")
            with self.assertRaisesRegex(ValueError, "symlink"):
                workflow._task_guidelines(root, [Path(other.name)])


class PreparationScriptHandoffTests(HarnessCase):
    def test_before_prepare_handoff_survives_git_worktree_creation_only_for_same_target(self):
        (self.project / "source.php").write_text("<?php echo CalculateShipping();", encoding="utf-8")
        target = self.project / "Page.cs"
        target.write_text("class Baseline {}\n", encoding="utf-8")
        self.commit_project()
        for modified in (False, True):
            with self.subTest(modified=modified):
                if modified:
                    target.write_text("class Different {}", encoding="utf-8")
                config = self.config()
                config.update(junie_apply_output_decisions=True, junie_review_script_output=True,
                              scripts={"before_prepare": [[sys.executable, "-c", "print('Check CalculateShipping')"]]})
                with patch("migration_harness.checker_advisory.run_text_advisory",
                           return_value='{"verdict":"handoff","evidence_lines":[1],"symbol":"CalculateShipping"}'):
                    result = workflow.prepare_run(config, "sample")
                run_path = Path(result["run_path"])
                record = json.loads((run_path / "run.json").read_text(encoding="utf-8"))
                context = workflow.script_handoff_context(
                    run_path, record, config["scripts"],
                    workflow._script_decision_versions(Path(result["worktree_path"]), record),
                )
                self.assertEqual("CalculateShipping" in context, not modified)
                self.assertNotIn("Check CalculateShipping", context)


class CycleRecoveryTests(SavedRunFixture):
    build = ("dotnet", 0, b"build passed\n")
    success = ("fixture-checker", 0, b"")
    first = b"A Example.cs:1 warning: wrong price\n"
    second = b"B Example.cs:2 warning: wrong total\n"

    def test_failed_tests_stop_before_checker(self):
        self.config["scripts"] = {"after_build": [["run-tests"]]}
        calls = []

        def process(command, **kwargs):
            calls.append(command)
            failed = command == ["run-tests"]
            kwargs["stdout"].write(b"assertion failed\n" if failed else b"build passed\n")
            return subprocess.CompletedProcess(command, int(failed))

        with patch.object(workflow, "run_process", side_effect=process), self.assertRaises(workflow.RunScriptError):
            workflow.resume_with_junie(self.config, self.run_id)
        self.assertEqual(calls, [["dotnet", "build"], ["run-tests"]])
        self.assertEqual(self.record()["status"], "script_failed")
        self.assertIn("assertion failed", (self.run_path / "after_build.log").read_text())

    def test_configured_scripts_send_only_the_current_child_response_and_keep_exit_gate(self):
        self.config["junie_review_script_output"] = True
        self.config["scripts"] = {"after_build": [
            [sys.executable, "-c", "print('fresh first response')"],
            [sys.executable, "-c", "import sys; print('fresh second response'); sys.exit(2)"],
            [sys.executable, "-c", "print('never run')"],
        ]}
        path = self.run_path / "after_build.log"
        path.write_text("old output from an earlier attempt\n", encoding="utf-8")
        record = self.record()
        with (
            patch("migration_harness.checker_advisory.run_text_advisory",
                  return_value='{"verdict":"informational","evidence_lines":[1]}') as agent,
            self.assertRaises(workflow.RunScriptError),
        ):
            workflow._run_script_phase(self.config, "after_build", self.run_path, self.run_path, record, once=True)
        first, second = [json.loads(call.args[0].split("\n", 1)[1]) for call in agent.call_args_list]
        self.assertEqual(first["untrusted_lines"], ["fresh first response"])
        self.assertEqual(second["untrusted_lines"], ["fresh second response"])
        self.assertEqual([first["exit_code"], second["exit_code"]], [0, 2])
        self.assertNotIn("never run", path.read_text(encoding="utf-8"))
        saved = self.record()
        self.assertEqual(saved["status"], "script_failed")
        self.assertEqual(len(saved["script_command_progress"]["after_build"]), 1)
        self.assertEqual([entry["command_number"] for entry in saved["script_advisories"]], [1, 2])

    def test_advisory_cancellation_does_not_reexecute_a_successful_once_hook(self):
        self.config["junie_review_script_output"] = True
        self.config["scripts"] = {"after_prepare": [[sys.executable, "-c",
            "from pathlib import Path; p = Path('counter.txt'); p.write_text(p.read_text() + 'x'); print('response')"]]}
        counter = self.run_path / "counter.txt"
        counter.write_text("", encoding="utf-8")
        with (
            patch("migration_harness.checker_advisory.run_text_advisory", side_effect=KeyboardInterrupt),
            self.assertRaises(workflow.RunScriptError),
        ):
            workflow._run_script_phase(self.config, "after_prepare", self.run_path, self.run_path, self.record(), once=True)
        saved = self.record()
        self.assertEqual(len(saved["script_command_progress"]["after_prepare"]), 1)
        self.assertEqual(saved["script_advisories"][-1]["status"], "interrupted")
        with patch("migration_harness.checker_advisory.run_text_advisory") as agent:
            workflow._run_script_phase(self.config, "after_prepare", self.run_path, self.run_path, saved, once=True)
        agent.assert_not_called()
        self.assertEqual(counter.read_text(encoding="utf-8"), "x")
        self.assertIn("after_prepare", self.record()["completed_script_phases"])

    def test_once_script_rereads_saved_response_after_stop_without_reexecuting(self):
        self.config.update(junie_apply_output_decisions=True, junie_review_script_output=True)
        self.config["scripts"] = {"after_prepare": [[sys.executable, "-c",
            "from pathlib import Path; p = Path('counter.txt'); p.write_text(p.read_text() + 'x'); print('1 test passed')"]]}
        counter = self.run_path / "counter.txt"
        counter.write_text("", encoding="utf-8")
        with (
            patch("migration_harness.checker_advisory.run_text_advisory", side_effect=KeyboardInterrupt),
            self.assertRaises(workflow.RunScriptError),
        ):
            workflow._run_script_phase(self.config, "after_prepare", self.run_path, self.run_path, self.record(), once=True)
        saved = self.record()
        self.assertEqual(saved["script_decisions_pending"]["after_prepare"]["command_number"], 1)
        with patch("migration_harness.checker_advisory.run_text_advisory",
                   return_value='{"verdict":"passed","evidence_lines":[1]}') as agent:
            workflow._run_script_phase(self.config, "after_prepare", self.run_path, self.run_path, saved, once=True)
        agent.assert_called_once()
        self.assertEqual(counter.read_text(encoding="utf-8"), "x")
        self.assertEqual(self.record()["script_decisions_pending"], {})
        self.assertEqual(self.record()["script_advisories"][-1]["applied"], "continued")

    def test_after_build_child_advisory_repairs_scope_then_rebuilds_and_rechecks(self):
        self.config.update(max_repair_attempts=2, junie_apply_output_decisions=True, junie_review_script_output=True)
        self.config["scripts"] = {"after_build": [[sys.executable, "-c", (
            "from pathlib import Path; import sys; "
            "fixed = 'Fixed' in Path('Example.cs').read_text(); "
            "print('1 test passed' if fixed else 'R Example.cs:1 warning: wrong total'); "
            "sys.exit(0 if fixed else 2)"
        )]]}
        def repair(_config, _targets, allowed, _worktree, _run_path, diagnostic, _record):
            self.assertEqual(allowed, (Path("Example.cs"),))
            self.assertEqual(diagnostic, "R Example.cs:1 warning: wrong total")
            self.target.write_text("class Fixed {}", encoding="utf-8")

        commands = []
        actual = workflow.run_process

        def process(command, **kwargs):
            commands.append(command[0])
            if command[0] in {"dotnet", "fixture-checker"}:
                return subprocess.CompletedProcess(command, 0)
            return actual(command, **kwargs)

        responses = ['{"verdict":"problem","evidence_lines":[1]}', '{"verdict":"passed","evidence_lines":[1]}']
        with (
            patch.object(workflow, "run_process", side_effect=process),
            patch.object(workflow, "_repair_target", side_effect=repair) as repair_call,
            patch.object(junie_cli, "require_interactive_cli"),
            patch("migration_harness.checker_advisory.run_text_advisory", side_effect=responses),
        ):
            result = workflow.resume_with_junie(self.config, self.run_id)
        self.assertEqual(result["status"], "checker_succeeded")
        self.assertEqual(commands, ["dotnet", sys.executable, "dotnet", sys.executable, "fixture-checker"])
        repair_call.assert_called_once()
        attempt, = self.record()["repair_history"]
        self.assertEqual(attempt["kind"], "script")
        self.assertTrue(self.record()["verification"]["scripts_passed"])

    def test_early_successful_script_hands_off_only_corroborated_identifier_once(self):
        self.config.update(junie_apply_output_decisions=True, junie_review_script_output=True)
        self.config["scripts"] = {"after_prepare": [[sys.executable, "-c",
            "from pathlib import Path; p = Path('counter.txt'); p.write_text(p.read_text() + 'x'); print('Inspect CalculateShipping before migration')"]]}
        counter = self.run_path / "counter.txt"
        counter.write_text("", encoding="utf-8")
        with patch("migration_harness.checker_advisory.run_text_advisory",
                   return_value='{"verdict":"handoff","evidence_lines":[1],"symbol":"CalculateShipping"}') as agent:
            for _ in range(2):
                workflow._run_script_phase(self.config, "after_prepare", self.run_path, self.run_path, self.record(), once=True)
        agent.assert_called_once()
        self.assertEqual(counter.read_text(encoding="utf-8"), "x")
        saved = self.record()
        self.assertEqual(saved["script_advisories"][-1]["applied"], "handed_off")
        context = workflow.script_handoff_context(self.run_path, saved, self.config["scripts"], {"Example.cs": None})
        self.assertIn("CalculateShipping", context)
        self.assertNotIn("Inspect CalculateShipping before migration", context)

    def test_after_checker_free_output_repair_rebuilds_from_start(self):
        self.config.update(max_repair_attempts=2, junie_apply_output_decisions=True,
                           junie_review_script_output=True, junie_ground_output_repairs=True)
        self.config["scripts"] = {"after_checker": [[sys.executable, "-c", (
            "from pathlib import Path; import sys; "
            "fixed = 'Fixed' in Path('Example.cs').read_text(); "
            "print('1 test passed' if fixed else 'Shipping charge was omitted'); "
            "sys.exit(0 if fixed else 2)"
        )]]}
        def repair(_config, _targets, allowed, _worktree, _run_path, diagnostic, _record):
            self.assertEqual(allowed, (Path("Example.cs"),))
            self.assertIn("Shipping charge was omitted", diagnostic)
            self.target.write_text("class Fixed {}", encoding="utf-8")

        commands = []
        actual = workflow.run_process

        def process(command, **kwargs):
            commands.append(command[0])
            if command[0] in {"dotnet", "fixture-checker"}:
                return subprocess.CompletedProcess(command, 0)
            return actual(command, **kwargs)

        responses = ['{"verdict":"problem","evidence_lines":[1]}', '{"verdict":"passed","evidence_lines":[1]}']
        with (
            patch.object(workflow, "run_process", side_effect=process),
            patch.object(workflow, "_repair_target", side_effect=repair) as repair_call,
            patch.object(workflow, "grounded_diagnostic",
                         return_value={"status": "grounded", "citations": [{"target":"Example.cs","source_id":"source"}]}),
            patch.object(junie_cli, "require_interactive_cli"),
            patch("migration_harness.checker_advisory.run_text_advisory", side_effect=responses),
        ):
            result = workflow.resume_with_junie(self.config, self.run_id)
        self.assertEqual(result["status"], "checker_succeeded")
        self.assertEqual(commands, ["dotnet", "fixture-checker", sys.executable, "dotnet", "fixture-checker", sys.executable])
        repair_call.assert_called_once()

    def test_operator_repair_gets_original_output_source_and_instruction_together(self):
        self.save_record({**self.record(), "junie_model": "sonnet", "junie_effort": "high"})
        self.config["junie_model"] = "opus"
        self.config["junie_effort"] = "low"
        (self.run_path / "agent-workspace").mkdir()
        self.target.write_text("class Example { const int Manual = 11; const int Requested = 0; }", encoding="utf-8")
        diagnostic = '{"expected": "reviewer value", "actual": 0}\r\n'
        self.checker_log.write_bytes(diagnostic.encode())
        instruction = "手修正の11を維持してください。\r\nRequestedを29にしてください。"

        def junie(command, completion, token, targets, **kwargs):
            self.assertEqual(command[command.index("--model") + 1], "sonnet")
            self.assertEqual(command[command.index("--effort") + 1], "high")
            guidelines = Path(command[command.index("--ide-guidelines") + 1])
            bundle = json.loads(guidelines.read_bytes().split(b"\n\n")[-1])
            self.assertEqual(bundle[".migration-materials/source-0001.md"], "example")
            self.assertIn(diagnostic + "\n", bundle.values())
            self.assertIn(instruction + "\n", bundle.values())
            target, = targets
            self.assertIn("Manual = 11", target.read_text())
            target.write_text(target.read_text().replace("Requested = 0", "Requested = 29"))
            completion.write_text(token)
            return 0

        commands, calls = self.mock_commands(self.build, self.success)
        with commands, patch.object(junie_cli, "require_interactive_cli"), patch.object(junie_cli, "run_interactive", side_effect=junie):
            result = workflow.resume_with_junie(self.config, self.run_id, checker_instruction=instruction)
        self.assertEqual(result["status"], "checker_succeeded")
        self.assertEqual(len(calls), 2)
        self.assertIn("Manual = 11; const int Requested = 29", self.target.read_text())

    def test_fewer_checker_findings_continue_in_same_run_then_rebuild_and_check(self):
        self.config["max_repair_attempts"] = 2
        with patch.object(workflow, "_repair_target") as repair, patch.object(junie_cli, "require_interactive_cli"):
            result = self.resume(
                self.build, ("fixture-checker", 1, self.first + self.second),
                self.build, ("fixture-checker", 1, self.second.replace(b":2", b":3")),
                self.build, self.success,
            )
        self.assertEqual(result["status"], "checker_succeeded")
        self.assertEqual(result["run_id"], self.run_id)
        self.assertEqual(repair.call_count, 2)
        self.assertEqual(len(self.record()["repair_history"]), 2)
        self.assertEqual((self.run_path / "checker-before-repair-1.log").read_bytes(), self.first + self.second)
        self.assertEqual((self.run_path / "checker-before-repair-2.log").read_bytes(), self.second.replace(b":2", b":3"))

    def test_fewer_build_errors_continue_before_checker(self):
        self.config["max_repair_attempts"] = 2
        first = f"{self.target}(1,1): error CS1002: ; expected\n".encode()
        second = f"{self.target}(2,1): error CS0103: name does not exist\n".encode()
        with patch.object(workflow, "_repair_target") as repair, patch.object(junie_cli, "require_interactive_cli"):
            result = self.resume(("dotnet", 1, first + second), ("dotnet", 1, second), self.build, self.success)
        self.assertEqual(result["status"], "checker_succeeded")
        self.assertEqual(repair.call_count, 2)
        self.assertTrue(all(entry["kind"] == "build" for entry in self.record()["repair_history"]))

    def test_mixed_output_continues_after_partial_progress_and_preserves_full_diagnostic(self):
        self.config["max_repair_attempts"] = 2
        first = self.first + self.second + b"Static check failed: 2 findings.\n"
        second = self.second + b"Static check failed: 1 findings.\n"
        with patch.object(workflow, "_repair_target") as repair, patch.object(junie_cli, "require_interactive_cli"):
            result = self.resume(
                self.build, ("fixture-checker", 1, first),
                self.build, ("fixture-checker", 1, second), self.build, ("fixture-checker", 0, b""),
            )
        self.assertEqual(result["status"], "checker_succeeded")
        self.assertEqual([call.args[5] for call in repair.call_args_list], [first.decode(), second.decode()])
        self.assertEqual(len(self.record()["repair_history"]), 2)

    def test_mixed_output_repetition_stops_with_legacy_history(self):
        self.config["max_repair_attempts"] = 2
        diagnostic = self.second + b"Static check failed: 1 findings.\n"
        log = self.run_path / "checker-before-repair-1.log"
        log.write_bytes(diagnostic)
        findings, _ = workflow.checker_findings(log, self.worktree)
        self.save_record({**self.record(), "repair_history": [{
            "findings": [findings[0].finding_id, hashlib.sha256(" ".join(diagnostic.decode().split()).encode()).hexdigest()],
            "raw_checker_output": True,
        }]})
        with patch.object(workflow, "_repair_target") as repair:
            result = self.resume(self.build, ("fixture-checker", 1, diagnostic))
        self.assertEqual(result["status"], "checker_needs_review")
        self.assertEqual(self.record()["checker_review_reason"], "finding_repeated")
        repair.assert_not_called()

    def test_large_repeated_build_errors_repair_once_and_keep_complete_log(self):
        self.config["max_repair_attempts"] = 2
        log = "".join(f"{self.target}({line},3): error CS1003: ',' expected\n" for line in range(1, 20001)).encode()
        self.assertGreater(len(log), 65_536)
        with patch.object(workflow, "_repair_target") as repair, patch.object(junie_cli, "require_interactive_cli"):
            result = self.resume(("dotnet", 1, log), self.build, self.success)
        self.assertEqual(result["status"], "checker_succeeded")
        repair.assert_called_once()
        self.assertEqual(len(self.record()["repair_history"][0]["findings"]), 1)
        self.assertIn("20000 log entries", repair.call_args.args[5])
        self.assertEqual((self.run_path / "build-before-repair-1.log").read_bytes(), log)

    def test_late_uneditable_or_unrecognized_error_in_large_log_still_blocks_repair(self):
        self.config["max_repair_attempts"] = 2
        repeated = f"{self.target}(1,3): error CS1003: ',' expected\n".encode() * 1200
        for tail in (b"Other.cs(1,1): error CS1002: ; expected\n", b"error MSB1009: project missing\n",
                     b"x" * 65_537 + b"\n"):
            with self.subTest(tail=tail[:60]), patch.object(workflow, "_repair_target") as repair:
                result = self.resume(("dotnet", 1, repeated + tail))
                self.assertEqual(result["status"], "build_failed")
                repair.assert_not_called()

    def test_many_distinct_build_messages_still_require_review(self):
        self.config["max_repair_attempts"] = 2
        log = "".join(f"{self.target}({line},3): error CS0103: unknown name symbol{line:05d}\n"
                      for line in range(1, 1001)).encode()
        with patch.object(workflow, "_repair_target") as repair:
            result = self.resume(("dotnet", 1, log))
        self.assertEqual(result["status"], "build_failed")
        repair.assert_not_called()

    def test_progress_does_not_reset_limit(self):
        self.config["max_repair_attempts"] = 1
        with patch.object(workflow, "_repair_target") as repair, patch.object(junie_cli, "require_interactive_cli"):
            result = self.resume(self.build, ("fixture-checker", 1, self.first + self.second),
                                 self.build, ("fixture-checker", 1, self.second))
        self.assertEqual(result["status"], "checker_needs_review")
        self.assertEqual(self.record()["checker_review_reason"], "repair_limit")
        repair.assert_called_once()

    def test_regressions_still_stop_after_partial_progress(self):
        self.config["max_repair_attempts"] = 4
        with patch.object(workflow, "_repair_target") as repair, patch.object(junie_cli, "require_interactive_cli"):
            result = self.resume(
                self.build, ("fixture-checker", 1, self.first + self.second),
                self.build, ("fixture-checker", 1, self.second),
                self.build, ("fixture-checker", 1, self.first),
            )
        self.assertEqual(result["status"], "checker_needs_review")
        self.assertEqual(self.record()["checker_review_reason"], "finding_repeated")
        self.assertEqual(repair.call_count, 2)

    def test_replacing_remaining_findings_with_new_errors_is_not_strict_progress(self):
        self.config["max_repair_attempts"] = 4
        with patch.object(workflow, "_repair_target") as repair, patch.object(junie_cli, "require_interactive_cli"):
            result = self.resume(
                self.build, ("fixture-checker", 1, self.first + self.second),
                self.build, ("fixture-checker", 1, self.second + b"C Example.cs:3 error: regression\n"),
            )
        self.assertEqual(result["status"], "checker_needs_review")
        self.assertEqual(self.record()["checker_review_reason"], "finding_repeated")
        repair.assert_called_once()

    def test_raw_output_and_legacy_fingerprints_do_not_use_subset_shortcut(self):
        self.assertTrue(workflow._repair_stalled({"b"}, [{"findings": ["a", "b"]}], kind="checker", raw=True))
        self.assertTrue(workflow._repair_stalled({"b"}, [{"findings": ["old"]}], kind="checker", legacy={"old"}))
        self.assertTrue(workflow._repair_stalled({"b"}, [
            {"findings": ["a", "b"], "raw_checker_output": True},
        ], kind="checker"))
