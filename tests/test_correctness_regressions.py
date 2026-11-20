import copy
import hashlib
import io
import json
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from fixtures import HarnessCase, SavedRunFixture
from migration_harness import junie_cli, rule_materials, workflow


class CheckerProvenanceTests(SavedRunFixture):
    def test_checker_only_cli_override_keeps_the_unit_default(self):
        project = self.worktree / "App.csproj"
        project.write_text(
            '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup>'
            '<TargetFramework>net8.0</TargetFramework>'
            '</PropertyGroup></Project>', encoding="utf-8",
        )
        toolchain = self.worktree.parent / "sdk"
        packs = toolchain / "packs/Microsoft.NETCore.App.Ref"
        packs.mkdir(parents=True)
        dotnet = toolchain / "dotnet"
        dotnet.write_bytes(b"fixture dotnet")
        (packs / "reference.dll").write_bytes(b"fixture reference")
        dll = project.parent / "bin/Debug/net8.0/App.dll"
        assets = project.parent / "obj/project.assets.json"
        override = [sys.executable, "-c", "raise SystemExit(0)"]
        unit_default = [sys.executable, "-c", "raise SystemExit(5)"]
        global_default = [sys.executable, "-c", "raise SystemExit(7)"]
        self.config.update(build_project=project.name, checker_from_cli=True, checker_command=override,
                           toml_checker_command=global_default)
        self.config["units"][0]["checker_command"] = unit_default

        run_process = workflow.run_process

        def run(command, **kwargs):
            if command[0] != "dotnet":
                return run_process(command, **kwargs)
            dll.parent.mkdir(parents=True, exist_ok=True)
            dll.write_bytes(b"compiled fixture")
            assets.parent.mkdir(parents=True, exist_ok=True)
            assets.write_bytes(b'{"libraries": {}}')
            return subprocess.CompletedProcess(command, 0)

        with (patch("migration_harness.build_receipt.shutil.which", return_value=str(dotnet)),
              patch.object(workflow, "run_process", side_effect=run) as executed,
              patch.object(junie_cli, "run_interactive", side_effect=AssertionError("Junie must not run")),
              redirect_stdout(io.StringIO())):
            build = workflow.plan_build_resume(self.config, self.run_id)
            workflow.resume_with_junie(self.config, self.run_id, verification_request={**build, "id": "build"})
            self.assertEqual(self.record()["verification"]["toml_checker_command"], unit_default)
            plan = workflow.plan_checker_resume(self.config, self.run_id)
            result = workflow.resume_with_junie(self.config, self.run_id, verification_request={**plan, "id": "checker"})
            self.assertEqual(result["status"], "checker_succeeded")
            self.assertEqual(self.record()["verification"]["toml_checker_command"], unit_default)
            self.assertEqual([call.args[0] for call in executed.call_args_list if call.args[0][0] != "dotnet"], [override])

            workflow.review_migration_ranges(self.config, self.run_id, ["1:1"], "confirmed", "Checked synthetic range",
                                             confirmed_targets=[self.target.name])
            regular = copy.deepcopy(self.config)
            regular.pop("checker_from_cli")
            regular.pop("toml_checker_command")
            regular["checker_command"] = global_default
            self.assertTrue(workflow.migration_ledger(regular, self.run_id)["completion"]["verification_passed"])
            regular["checker_command"] = [sys.executable, "-c", "raise SystemExit(9)"]
            self.assertTrue(workflow.migration_ledger(regular, self.run_id)["completion"]["verification_passed"])
            regular["checker_command"] = global_default
            del regular["units"][0]["checker_command"]
            self.assertFalse(workflow.migration_ledger(regular, self.run_id)["completion"]["verification_passed"])
            regular["units"][0]["checker_command"] = global_default
            completion = workflow.migration_ledger(regular, self.run_id)["completion"]
            self.assertFalse(completion["verification_passed"])
            self.assertFalse(completion["complete"])
            self.assertEqual(subprocess.run(global_default, check=False).returncode, 7)

            stale = self.record()
            stale["verification"].pop("toml_checker_scope")
            stale["verification"]["toml_checker_command"] = global_default
            self.save_record(stale)
            self.assertFalse(workflow.migration_ledger(regular, self.run_id)["completion"]["verification_passed"])
            plan = workflow.plan_checker_resume(regular, self.run_id)
            workflow.resume_with_junie(regular, self.run_id, verification_request={**plan, "id": "ordinary"})
            self.assertFalse(self.record()["verification"]["checker_from_cli"])
            self.assertNotIn("toml_checker_command", self.record()["verification"])
            self.assertFalse(workflow.migration_ledger(regular, self.run_id)["completion"]["verification_passed"])


class DetailReviewEvidenceTests(HarnessCase):
    def test_review_gets_pinned_target_source_and_required_materials_and_reuses_decision(self):
        helper = "class Helper { public const bool PreserveZeros = true; }\n"
        (self.project / "Context").mkdir()
        for name, body in {
            "source.php": "<?php echo '0012';\n",
            "Page.cs": "class Page {}\n",
            "Context/Helper.cs": helper,
            "required.md": "Follow the relevant preservation detail when Helper.PreserveZeros is true.\n",
            "detail.md": "If true, preserve leading zeros.\n",
            "sample.php": "<?php echo '0012';\n",
        }.items():
            (self.project / name).write_text(body, encoding="utf-8")
        self.commit_project()
        config = self.config()
        unit = config["units"][0]
        unit["target_context"] = {"roots": [], "folders": ["Context"]}
        common = {"encoding": "utf-8", "start_line": 1, "end_line": None, "shared": True}
        unit["inputs"].extend([
            {**common, "path": "required.md", "role": "migration_rules", "usage": "required", "description": ""},
            {**common, "path": "detail.md", "role": "migration_rules", "usage": "reference",
             "description": "Check the configured formatting helper, following the required rule."},
            {**common, "path": "sample.php", "role": "reference_php"},
        ])
        prepared = workflow.prepare_run(config, unit["id"])
        run_path, worktree = Path(prepared["run_path"]), Path(prepared["worktree_path"])
        capsule, manifest = workflow.load_capsule_manifest(run_path)
        agent_root = run_path / "agent-workspace"
        agent_root.mkdir()
        record = json.loads((run_path / "run.json").read_bytes())
        targets = (Path("Page.cs"),)
        rule = rule_materials.detail_index(manifest)[0]["file"]
        context_file = "evidence/target/Context/Helper.cs"
        expected_helper = helper
        expected_bytes = (worktree / "Context/Helper.cs").read_bytes()
        expected_applies = True

        def decide(command, completion, token, _outputs, **kwargs):
            review_root = completion.parent
            inventory = json.loads((review_root / "evidence.json").read_bytes())
            self.assertEqual((review_root / context_file).read_text(encoding="utf-8"), expected_helper)
            self.assertEqual((review_root / context_file).read_bytes(), expected_bytes)
            self.assertEqual(inventory[context_file]["sha256"], hashlib.sha256(expected_bytes).hexdigest())
            self.assertTrue(any("preservation detail" in (review_root / path).read_text(encoding="utf-8")
                                for path, entry in inventory.items() if entry["path"] == "required.md"))
            self.assertTrue(any(path.endswith("/reference-0001.txt") for path in inventory))
            self.assertIn("insufficient_context", command[-1])
            kwargs["accept_result"](json.dumps({"review_token": token, "decisions": {
                rule: {"applies": expected_applies, "reason": f"Fixture oracle: {expected_applies}",
                       "context_evidence": {"file": context_file, "lines": [1, 1]}},
            }}))
            return 0

        with patch.object(junie_cli, "run_interactive", side_effect=decide) as reviewer:
            args = config, unit, run_path, record, worktree, targets, agent_root, capsule, manifest
            workflow._prepare_migration_materials(*args, followup_only=False)
            (worktree / "Context/Helper.cs").write_text(helper.replace("true", "false"), encoding="utf-8")
            workflow._prepare_migration_materials(*args, followup_only=False)
            self.assertEqual(reviewer.call_count, 1)
        self.assertEqual((agent_root / "Context/Helper.cs").read_text(encoding="utf-8"), helper)
        self.assertIn("preserve leading zeros", (agent_root / ".migration-materials/guidelines.md").read_text(encoding="utf-8"))
        saved = json.loads((run_path / "run.json").read_bytes())
        self.assertEqual(saved["detail_applicability"][rule]["context_evidence"]["file"], context_file)
        self.assertIn(context_file, saved["detail_review_evidence"])

        expected_helper = helper.replace("true", "false")
        expected_applies = False
        (self.project / "Context/Helper.cs").write_text(expected_helper, encoding="utf-8")
        self.git("add", "Context/Helper.cs")
        self.git("commit", "-qm", "Change fixture implementation")
        other = workflow.prepare_run(config, unit["id"])
        other_run = Path(other["run_path"])
        other_worktree = Path(other["worktree_path"])
        expected_bytes = (other_worktree / "Context/Helper.cs").read_bytes()
        other_capsule, other_manifest = workflow.load_capsule_manifest(other_run)
        other_agent = other_run / "agent-workspace"
        other_agent.mkdir()
        other_record = json.loads((other_run / "run.json").read_bytes())
        with patch.object(junie_cli, "run_interactive", side_effect=decide) as reviewer:
            workflow._prepare_migration_materials(config, unit, other_run, other_record, other_worktree,
                                                  targets, other_agent, other_capsule, other_manifest, followup_only=False)
            self.assertEqual(reviewer.call_count, 1)
        self.assertFalse(other_record["detail_applicability"][rule]["applies"])
        self.assertNotEqual(other_record["detail_review_evidence"][context_file]["sha256"],
                            saved["detail_review_evidence"][context_file]["sha256"])
        self.assertNotIn("preserve leading zeros", (other_agent / ".migration-materials/guidelines.md").read_text(encoding="utf-8"))

    def test_insufficient_evidence_is_not_a_negative_decision(self):
        file = "rule-0001.txt"
        index = [{"file": file}]
        decision = {"applies": True, "reason": "Missing dependency", "insufficient_context": True}
        self.assertEqual(rule_materials.selected_details(index, {file: decision}), {file})
        with self.assertRaisesRegex(ValueError, "insufficient context"):
            rule_materials.selected_details(index, {file: {**decision, "applies": False}})
        with self.assertRaisesRegex(ValueError, "invalid review context evidence"):
            rule_materials.selected_details(index, {
                file: {**decision, "context_evidence": {"file": "evidence/target/wrong.cs", "lines": [1, 2]}},
            }, evidence_inventory={})
