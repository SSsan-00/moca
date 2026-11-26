import hashlib
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

from tests.support.fixtures import HarnessCase, prepare_fixture
from migration_harness.junie import junie_cli
from migration_harness.rules import rule_materials
from migration_harness.context import source_context
from migration_harness import workflow
from migration_harness import execution
from migration_harness.context import capsules as capsule_files, migration_materials
from migration_harness.junie import migration_session
from migration_harness.rules import detail_review_evidence, detail_selection, rule_search
from migration_harness.runs import run_records
from migration_harness.verification import pipeline as verification_pipeline


class RuleMaterialsTests(HarnessCase):
    def test_context_evidence_keeps_distinct_policy_example_and_target_spans(self):
        rule = "rule-0001.txt"
        inventory = {f"evidence/{name}": {"file_lines": 3}
                     for name in ("policy.md", "example.cs", "target.cs")}
        index = [{"file": rule}]
        def span(name, lines=None):
            return {"file": f"evidence/{name}", "lines": lines or [1, 2]}

        decision = {"applies": True, "reason": "Both required conditions hold.",
                    "context_evidence": span("policy.md")}
        self.assertEqual(rule_materials.selected_details(index, {rule: decision}, evidence_inventory=inventory), {rule})
        related = [span("example.cs"), span("target.cs", [3, 3])]
        multi = {**decision, "additional_context_evidence": related}
        self.assertEqual(rule_materials.selected_details(index, {rule: multi}, evidence_inventory=inventory), {rule})
        for invalid in ([span("missing.cs")], [span("target.cs", [1, 4])], [span("policy.md")], related + [span("policy.md")]):
            with self.subTest(additional=invalid), self.assertRaises(rule_materials.InvalidDetailContextEvidence):
                rule_materials.selected_details(index, {rule: {**multi, "additional_context_evidence": invalid}},
                                               evidence_inventory=inventory)
        with self.assertRaises(rule_materials.InvalidDetailContextEvidence):
            rule_materials.selected_details(index, {rule: {"applies": False, "reason": "without a primary",
                                                           "additional_context_evidence": related}},
                                           evidence_inventory=inventory)

    def test_operator_rule_review_replaces_false_decision_and_checks_evidence_version(self):
        from migration_harness.rules.rule_review import review_rules

        (self.project / "source.php").write_text("<?php echo 42;", encoding="utf-8")
        (self.project / "Page.cs").write_text("class Page {}", encoding="utf-8")
        (self.project / "details.md").write_text("Preface\n" + "x\n" * 1000 + "Keep the final exception.\n", encoding="utf-8")
        self.commit_project()
        config = self.config()
        config["units"][0]["inputs"].append({
            "path": "details.md", "role": "migration_rules", "usage": "reference", "description": "Exception",
            "encoding": "utf-8", "start_line": 1, "end_line": None, "shared": True,
        })
        result = prepare_fixture(config, "sample")
        run_id = result["run_id"]
        run_path = Path(result["run_path"])
        record_path = run_path / "run.json"
        record = json.loads(record_path.read_bytes())
        rule = "rule-0001.txt"
        record["detail_applicability"] = {rule: {"applies": False, "reason": "Missed exception"}}
        run_records.write_run_record(record_path, record)
        report = review_rules(config, run_id)
        self.assertIsNone(report["rules"][0]["independent_verification"])
        self.assertFalse(report["rules"][0]["applicability"])
        row = report["rules"][0]
        args = {"rule": rule, "start": 1002, "end": 1002, "reason": "Exception at the end"}
        with self.assertRaisesRegex(ValueError, "行範囲"):
            review_rules(config, run_id, kind="include", version=report["version"], **{**args, "end": 99999})
        changed = review_rules(config, run_id, kind="include", version=report["version"], **args)
        self.assertTrue(changed["rules"][0]["applicability"])
        self.assertEqual(changed["rules"][0]["operator_decision"]["end_line"], 1002)
        with self.assertRaisesRegex(ValueError, "更新"):
            review_rules(config, run_id, kind="include", version=report["version"], **args)
        with self.assertRaisesRegex(ValueError, "参照先"):
            review_rules(config, run_id, kind="passed", version=changed["version"], **args)
        reviewed = review_rules(config, run_id, kind="passed", version=changed["version"],
                                reference="tests/test_exception: pass", **args)
        self.assertEqual(reviewed["rules"][0]["independent_verification"]["kind"], "passed")
        self.assertIsNone(reviewed["rules"][0]["application"])
        capsule, manifest = capsule_files.load_capsule_manifest(run_path)
        agent = run_path / "agent-workspace"
        agent.mkdir()
        saved = json.loads(record_path.read_bytes())
        selected = rule_materials.effective_details(manifest, saved)
        migration_materials.stage_migration_materials(capsule, manifest, agent, selected=selected)
        self.assertIn("Keep the final exception.",
                      (agent / ".migration-materials/guidelines.md").read_text(encoding="utf-8"))
        updated = Path(result["worktree_path"]) / next(iter(report["targets"]))
        updated.write_text("class Changed {}", encoding="utf-8")
        self.assertIsNone(review_rules(config, run_id)["rules"][0]["independent_verification"])
        (run_path / row["snapshot"]).write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "saved rule changed"):
            review_rules(config, run_id)

    def test_insufficient_applicability_is_not_resolved_by_verification_alone(self):
        from migration_harness.rules.rule_review import review_rules

        (self.project / "source.php").write_text("<?php echo 1;", encoding="utf-8")
        (self.project / "Page.cs").write_text("class Page {}", encoding="utf-8")
        (self.project / "detail.md").write_text("Use opt-in formatting.", encoding="utf-8")
        self.commit_project()
        config = self.config()
        config["units"][0]["inputs"].append({
            "path": "detail.md", "role": "migration_rules", "usage": "reference",
            "description": "Opt-in formatting", "encoding": "utf-8",
            "start_line": 1, "end_line": None, "shared": True,
        })
        run = prepare_fixture(config, "sample")
        run_id = run["run_id"]
        path = Path(run["run_path"]) / "run.json"
        record = json.loads(path.read_bytes())
        rule = "rule-0001.txt"
        record["detail_applicability"] = {rule: {
            "applies": True, "insufficient_context": True, "reason": "Need the authoritative PHP mode.",
        }}
        run_records.write_run_record(path, record)
        report = review_rules(config, run_id)
        entry, = report["rules"]
        self.assertTrue(entry["applicability"])
        self.assertTrue(entry["applicability_needs_review"])
        self.assertEqual("applicability_pending", entry["verification_state"])
        args = {"rule": rule, "start": 1, "end": 1, "reason": "Compared pinned files."}
        verified = review_rules(config, run_id, kind="passed", version=report["version"],
                                reference="independent checker", **args)
        self.assertEqual("passed", verified["rules"][0]["independent_verification"]["kind"])
        self.assertEqual("applicability_pending", verified["rules"][0]["verification_state"])
        decided = review_rules(config, run_id, kind="include", version=verified["version"], **args)
        self.assertFalse(decided["rules"][0]["applicability_needs_review"])
        self.assertTrue(decided["rules"][0]["operator_decision"]["applies"])
        self.assertEqual("human_review_needed", decided["rules"][0]["verification_state"])

    def test_human_verification_becomes_pending_when_reference_revision_changes(self):
        from migration_harness.rules.rule_review import review_rules

        (self.project / "source.php").write_text("<?php echo 42;", encoding="utf-8")
        (self.project / "Page.cs").write_text("class Page {}", encoding="utf-8")
        (self.project / "required.md").write_text("Match the example.", encoding="utf-8")
        (self.project / "refs").mkdir()
        (self.project / "refs/Example.cs").write_text("class Example { int Version = 1; }", encoding="utf-8")
        self.commit_project()
        config = self.config()
        config["units"][0]["inputs"].append({
            "path": "required.md", "role": "migration_rules", "usage": "required", "encoding": "utf-8",
            "start_line": 1, "end_line": None, "shared": True,
        })
        run = prepare_fixture(config, "sample")
        run_id, capsule = run["run_id"], Path(run["capsule_path"])
        worktree = Path(run["worktree_path"])
        agent = Path(run["run_path"]) / "agent-workspace"
        targets = (Path("Page.cs"),)
        policy = {"roots": [], "folders": ["refs"]}
        source_context.stage_target_context(worktree, targets, capsule, agent, policy=policy)
        initial = review_rules(config, run_id)
        self.assertEqual(initial["rules"][0]["verification_state"], "human_review_needed")
        reviewed = review_rules(
            config, run_id, rule="rule-0001.txt", kind="passed", version=initial["version"],
            start=1, end=1, reason="Checked against example version 1.", reference="manual comparison",
        )
        self.assertEqual(reviewed["rules"][0]["independent_verification"]["kind"], "passed")
        self.assertEqual(reviewed["rules"][0]["verification_state"], "passed")

        (worktree / "refs/Example.cs").write_text("class Example { int Version = 2; }", encoding="utf-8")
        snapshot_name = "target-context-" + "a" * 32
        source_context.stage_target_context(worktree, targets, capsule, agent, policy=policy, snapshot_name=snapshot_name)
        source_context.activate_target_context(capsule, capsule / snapshot_name)
        current = review_rules(config, run_id)
        self.assertEqual(current["targets"], reviewed["targets"])
        self.assertNotEqual(current["version"], reviewed["version"])
        row = current["rules"][0]
        self.assertIsNone(row["independent_verification"])
        self.assertEqual(row["verification_state"], "human_review_needed")
        self.assertEqual(row["history"][-1]["kind"], "passed")

    def test_independent_checker_requires_fresh_evidence_even_on_same_version(self):
        import sys

        from migration_harness.rules.rule_review import review_rules

        (self.project / "source.php").write_text("<?php echo 42;", encoding="utf-8")
        (self.project / "Page.cs").write_text("class Page {}", encoding="utf-8")
        for name in ("first.md", "second.md"):
            (self.project / name).write_text("Match the example.", encoding="utf-8")
        self.commit_project()
        config = self.config()
        config["checker_rule_evidence"] = True
        for name in ("first.md", "second.md"):
            config["units"][0]["inputs"].append({
                "path": name, "role": "migration_rules", "usage": "required", "encoding": "utf-8",
                "start_line": 1, "end_line": None, "shared": True,
            })
        run = prepare_fixture(config, "sample")
        record = json.loads((Path(run["run_path"]) / "run.json").read_text(encoding="utf-8"))
        checker = Path(run["worktree_path"]) / "mock_checker.py"
        checker.write_text(
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "if sys.argv[1] != 'omit':\n"
            " q = json.loads(Path(os.environ['MIGRATION_RULE_CHECK_REQUEST']).read_text(encoding='utf-8'))\n"
            " name, rule = next(iter(q['rules'].items()))\n"
            " row = dict(rule=name, sha256=rule['sha256'], start_line=1, end_line=rule['saved_file_lines'],"
            " status='passed' if sys.argv[1] == 'incomplete' else sys.argv[1], evidence='independent fixture check')\n"
            " Path(os.environ['MIGRATION_RULE_CHECK_RECEIPT']).write_text(json.dumps("
            "dict(schema=1, request_id=q['request_id'], results=[row])), encoding='utf-8')\n"
            "if sys.argv[1] == 'incomplete': print('Error: No checks were performed.')\n",
            encoding="utf-8",
        )
        original_run = execution.run_process

        def process(command, **kwargs):
            if command[0] == "dotnet":
                return subprocess.CompletedProcess(command, 0)
            return original_run(command, **kwargs)

        for verdict, expected, overall in (
            ("passed", "passed", "checker_succeeded"),
            ("omit", "human_review_needed", "checker_succeeded"),
            ("incomplete", "human_review_needed", "checker_needs_review"),
            ("failed", "failed", "checker_needs_review"),
        ):
            with self.subTest(verdict=verdict):
                config["checker_command"] = [sys.executable, str(checker), verdict]
                record.update(status="junie_finished", completed_capsules=record["context_capsules"])
                with patch.object(execution, "run_process", side_effect=process):
                    result = verification_pipeline.run_verification(config, config["units"][0], run, record, process_queued_edits=self.fail)
                self.assertEqual(result["status"], overall)
                rows = review_rules(config, run["run_id"])["rules"]
                self.assertEqual([row["verification_state"] for row in rows], [expected, "human_review_needed"])
                if expected == "human_review_needed":
                    self.assertNotIn("checker_rule_evidence", record)
                    self.assertEqual(record["previous_checker_rule_evidence"]["results"]["rule-0001.txt"]["kind"], "passed")
                else:
                    self.assertEqual(rows[0]["independent_verification"]["source"], "checker")

    def test_hundreds_of_rules_keep_every_line_and_manifest_range(self):
        (self.project / "source.php").write_text("<?php echo 42;\n", encoding="utf-8")
        config = self.config()
        originals = []
        for index in range(200):
            name = f"rule-{index}.md"
            lines = [f"RULE {index} LINE {number} 日本語 ```\r\n" for number in range(1, 41)]
            lines[-1] = f"LAST {index} without newline"
            (self.project / name).write_bytes("".join(lines).encode())
            config["units"][0]["inputs"].append({
                "path": name, "role": "migration_rules", "encoding": "utf-8",
                "start_line": 1, "end_line": None, "shared": True,
            })
            originals.append("".join(f"{number:>6} | {line}" for number, line in enumerate(lines, 1)))
        self.commit_project()
        result = prepare_fixture(config, "sample")
        capsule = Path(result["capsule_path"])
        manifest = json.loads((capsule / "manifest.json").read_bytes())
        self.assertEqual(len(manifest["rule_files"]), 200)
        self.assertEqual(len(manifest["shared_sources"]), 200)
        self.assertEqual(len(manifest["parts"]), 1)
        for index, (entry, source_range) in enumerate(zip(manifest["rule_files"], manifest["shared_sources"], strict=True)):
            content = (capsule / "materials" / entry["file"]).read_bytes()
            self.assertIn(originals[index].encode(), content)
            self.assertEqual(entry["sha256"], hashlib.sha256(content).hexdigest())
            self.assertEqual(entry["path"], f"rule-{index}.md")
            self.assertEqual(source_range["path"], entry["path"])
            self.assertEqual((source_range["start_line"], source_range["end_line"]), (1, 40))

    def prepare(self):
        (self.project / "source.php").write_text("<?php echo 42;\n", encoding="utf-8")
        (self.project / "Page.cs").write_text("class Page {}\n", encoding="utf-8")
        self.commit_project()
        config = self.config()
        config["units"][0]["inputs"].insert(0, {
            "path": "rules.md", "role": "migration_rules", "encoding": "utf-8",
            "start_line": 1, "end_line": None, "shared": True,
        })
        self.original = "FIRST RULE\r\n" + "保持する😀" * 1600 + "\r\nA RULE\r\nB RULE\r\nLAST RULE\r\n"
        (self.project / "rules.md").write_bytes(self.original.encode())
        result = prepare_fixture(config, "sample")
        capsule = Path(result["capsule_path"])
        manifest = json.loads((capsule / "manifest.json").read_bytes())
        agent = Path(result["run_path"]) / "agent-workspace"
        agent.mkdir()
        migration_materials.stage_migration_materials(capsule, manifest, agent)
        return capsule, manifest, agent

    def test_large_existing_rules_are_provided_whole_without_reader_calls(self):
        capsule, manifest, agent = self.prepare()
        entries = manifest["rule_files"]
        self.assertEqual(len(entries), 1)
        content = (capsule / "materials" / entries[0]["file"]).read_bytes()
        expected = "".join(f"{number:>6} | {line}" for number, line in enumerate(self.original.splitlines(keepends=True), 1))
        self.assertTrue(expected.encode() in content)
        self.assertIn(b"LAST RULE", content)
        inline = (agent / ".migration-materials/guidelines.md").read_bytes()
        self.assertIn(self.original.encode(), inline)
        self.assertNotIn(content, inline)
        self.assertIn(b"lines 1-5 (.migration-materials/rule-0001.txt)", inline)
        self.assertEqual((agent / ".migration-materials" / entries[0]["file"]).read_bytes(), content)
        self.assertEqual(len(manifest["parts"]), 1)
        self.assertFalse((agent / "migration-read-rules.py").exists())
        command = junie_cli.build_command(agent, "migrate", 7, guidelines=agent / ".migration-materials/guidelines.md")
        self.assertEqual(command[-2:], ["--prompt", "migrate"])
        self.assertNotIn("FIRST RULE", " ".join(command))  # Windowsの引数には本文を展開しない。
        self.assertIn("--ide-guidelines", command)
        (self.project / "rules.md").unlink()
        (agent / ".migration-materials/guidelines.md").write_text("changed")
        migration_materials.stage_migration_materials(capsule, manifest, agent, repair=True)
        self.assertEqual((agent / ".migration-materials/guidelines.md").read_bytes(), inline)
        self.assertEqual((capsule / "materials" / entries[0]["file"]).read_bytes(), content)

    def test_input_budget_blocks_launch_and_keeps_complete_rules_for_resume(self):
        capsule, manifest, agent = self.prepare()
        config = self.config()
        config["junie_input_limits"] = {"warn_input_bytes": 50, "max_input_bytes": 1_000}
        run_id = capsule.parent.name
        config["units"][0]["inputs"].insert(0, {
            "path": "rules.md", "role": "migration_rules", "encoding": "utf-8",
            "start_line": 1, "end_line": None, "shared": True,
        })
        with patch.object(junie_cli, "require_interactive_cli"), patch.object(junie_cli, "_run_session") as junie:
            with self.assertRaisesRegex(ValueError, "起動を止めました.*rules.md"):
                workflow.resume_with_junie(config, run_id)
            junie.assert_not_called()
        record = json.loads((capsule.parent / "run.json").read_text(encoding="utf-8"))
        usage = record["last_junie_input"]
        self.assertTrue(usage["blocked"])
        self.assertEqual(usage["bytes"], usage["prompt"]["bytes"] + usage["guidelines"]["bytes"])
        content = (capsule / "materials" / manifest["rule_files"][0]["file"]).read_bytes()
        inline = (agent / ".migration-materials/guidelines.md").read_bytes()
        start = inline.index(b"## migration_rules")
        self.assertEqual(usage["rules"][0]["bytes"], len(inline[start:]))
        self.assertEqual(usage["rules"][0]["characters"], len(inline[start:].decode("utf-8")))
        self.assertIn(self.original.encode(), inline)
        self.assertIn(b"     1 | FIRST RULE", content)
        self.assertNotIn(content, inline)

    def test_new_guidelines_keep_original_lines_and_old_runs_keep_numbered_form(self):
        (self.project / "source.php").write_bytes(b"<?php echo 42;\n")
        config = self.config()
        originals = [
            "keep whole\r\n\r\n     2 | original prefix\r\nkeep this too\r\n例外 😀",
            "selected detail\n```md\nNested fence\n```\nfinal detail\n",
            "unselected detail\rcompatibility\r",
        ]
        for index, source in enumerate(originals):
            name = f"rule-{index}.md"
            (self.project / name).write_bytes(source.encode())
            config["units"][0]["inputs"].append({
                "path": name, "role": "migration_rules", "encoding": "utf-8",
                "start_line": 1, "end_line": None, "shared": True,
                "usage": "reference" if index else "required",
                "description": "Razor example only" if index else "",
            })
        self.commit_project()
        result = prepare_fixture(config, "sample")
        capsule, manifest = capsule_files.load_capsule_manifest(Path(result["run_path"]))
        records = manifest["rule_files"]
        selected = frozenset({records[1]["file"]})
        agent = Path(result["run_path"]) / "agent-workspace"
        agent.mkdir()
        migration_materials.stage_migration_materials(capsule, manifest, agent, selected=selected)
        inline = (agent / ".migration-materials/guidelines.md").read_bytes().decode()
        self.assertIn(originals[0], inline)
        self.assertIn(originals[1], inline)
        self.assertNotIn(originals[2], inline)
        self.assertIn("lines 1-5 (.migration-materials/rule-0001.txt)", inline)
        self.assertIn("lines 1-5 (.migration-materials/rule-0002.txt)", inline)
        self.assertLess(inline.index(originals[0]), inline.index(originals[1]))
        self.assertIn("\n例外 😀\n```\n", inline)
        self.assertNotIn("     1 | keep whole", inline)
        numbered = (capsule / "materials" / records[0]["file"]).read_bytes()
        self.assertIn(b"     3 |      2 | original prefix\r\n", numbered)
        self.assertEqual(hashlib.sha256(numbered).hexdigest(), records[0]["sha256"])
        self.assertEqual((agent / ".migration-materials" / records[0]["file"]).read_bytes(), numbered)
        before = {key: value for key, value in manifest.items() if key != "rule_guidelines_format"}
        manifest_path = capsule / "manifest.json"
        original_manifest = manifest_path.read_bytes()
        for old in (before, {**before, "rule_guidelines_format": "unknown-future-format"}):
            rejected = json.dumps(old).encode()
            manifest_path.write_bytes(rejected)
            with self.assertRaisesRegex(ValueError, "invalid saved rule index or format"):
                capsule_files.load_capsule_manifest(capsule.parent)
            self.assertEqual(manifest_path.read_bytes(), rejected)
        manifest_path.write_bytes(original_manifest)
        migration_materials.stage_migration_materials(capsule, manifest, agent, repair=True, selected=selected)
        self.assertEqual((agent / ".migration-materials/guidelines.md").read_bytes().decode(), inline)

    def test_compact_format_does_not_increase_short_rule_input(self):
        material = self.root / "materials"
        short = "## migration_rules — `rules.md` lines 1-1\n\n```text\n     1 | 最初で最後\n```\n\n"
        entries = rule_materials.write_rule_files(material, [({"path": "rules.md"}, short)])
        self.assertIn(short, rule_materials.render_guidelines(material, {"rule_files": entries}))

    def test_new_manifest_leaves_unrecognized_rule_frames_intact(self):
        material = self.root / "materials"
        incorrect = "## migration_rules — `rules.md` lines 5-7\n\n```text\n     5 | valid\n     7 | missing line\n```\n\n"
        opaque = "     1 | This is actual content\r\n123 | retain this too"
        entries = rule_materials.write_rule_files(material, [
            ({"path": "rules.md"}, incorrect), ({"path": "old.md"}, opaque),
        ])
        manifest = {"rule_files": entries, "rule_guidelines_format": rule_materials.COMPACT_RULE_GUIDELINES}
        guidelines = rule_materials.render_guidelines(material, manifest)
        self.assertIn(incorrect, guidelines)
        self.assertIn(opaque, guidelines)

    def test_final_input_is_measured_again_before_communication_retry(self):
        root = self.root
        prompt = "Fixture prompt"
        settings = {"junie_input_limits": {"max_input_bytes": len(prompt)}}
        record = {}
        command = junie_cli.build_command(root, prompt, None)
        with (patch.object(junie_cli, "_run_session", side_effect=junie_cli.JunieConnectionError("fixture")) as run,
              patch.object(junie_cli.time, "sleep")):
            with self.assertRaisesRegex(ValueError, "起動を止めました"):
                migration_session.run_budgeted_junie(settings, root / "run.json", record, "migration", None, root, [],
                                             command, root / "complete", "token", ())
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0][-1], prompt)
        self.assertTrue(record["last_junie_input"]["blocked"])
        self.assertGreater(record["last_junie_input"]["bytes"], len(prompt))

    def test_details_are_pinned_but_only_index_is_in_guidelines(self):
        material = self.root / "materials"
        mandatory = "禁止事項\r\n" + "😀" * 7000 + "\r\n例外は維持"
        detail = "DETAIL_BODY\r\n" * 500
        entries = rule_materials.write_rule_files(material, [
            ({"path": "common.md"}, mandatory),
            ({"path": "detail.md", "usage": "reference", "description": "Razorを扱う場合の説明と例"}, detail),
        ])
        self.assertEqual((material / entries[1]["file"]).read_bytes(), detail.encode())
        guidelines = rule_materials.render_guidelines(material, {"rule_files": entries})
        self.assertIn(mandatory, guidelines)
        self.assertNotIn("DETAIL_BODY", guidelines)
        self.assertIn("Razorを扱う場合", guidelines)
        self.assertIn(".migration-materials/" + entries[1]["file"], guidelines)
        (material / entries[1]["file"]).write_text("corrupted")
        with self.assertRaisesRegex(ValueError, "saved rule changed"):
            rule_materials.render_guidelines(material, {"rule_files": entries})

    def test_details_naming_the_target_are_supplied_whole_and_other_details_stay_indexed(self):
        material = self.root / "materials"
        body = "Implement OrderPage.Values() with existing helpers.\r\nKeep the final exception.\r\n"
        razor = "Use Razor's standard encoding.\r\n"
        unrelated = "OtherOrderPage.Values() uses this separate behavior.\n"
        entries = rule_materials.write_rule_files(material, [
            ({"path": "order.md", "usage": "reference", "description": "注文処理の詳細"}, body),
            ({"path": "view.md", "usage": "reference",
              "description": r"Pages\Orders\Index.cshtml の表示を変更する場合"}, razor),
            ({"path": "other.md", "usage": "reference", "description": "別画面の詳細"}, unrelated),
        ])
        guidelines = rule_materials.render_guidelines(
            material, {"rule_files": entries},
            targets=(Path("Pages/Orders/OrderPage.cs"), Path("Pages/Orders/Index.cshtml")),
        )
        supplied, index = guidelines.split("## Detail and example references")
        self.assertIn(body, supplied)
        self.assertIn(razor, supplied)
        self.assertNotIn(unrelated, guidelines)
        self.assertIn('"source": "other.md"', index)
        self.assertNotIn('"source": "order.md"', index)

    def test_review_covers_every_detail_and_supplies_selected_body_on_resume(self):
        capsule = self.root / "capsule"
        capsule.mkdir()
        body = "Use the existing prefix and suffix for each ordered identifier.\n"
        irrelevant = "DB transactions use a fallback.\n"
        entries = rule_materials.write_rule_files(capsule / "materials", [
            ({"path": "orders.md", "usage": "reference", "description": "Order list output"}, body),
            ({"path": "database.md", "usage": "reference", "description": "DB transactions"}, irrelevant),
        ])
        source = capsule / "context.md"
        source.write_bytes(b"<?php echo json_encode($orders);\r\n")
        last = capsule / "last.md"
        last.write_text("// 最後のカプセルだけにある例外\n", encoding="utf-8")
        manifest = {
            "unit_id": "test", "parts": [
                {"path": str(p), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in (source, last)
            ],
            "rule_files": entries, "materials": {**{row["file"]: row["sha256"] for row in entries},
                "rules.json": hashlib.sha256((capsule / "materials/rules.json").read_bytes()).hexdigest()},
        }
        record = {"status": "junie_running"}
        targets = (Path("SomeTarget.cs"),)

        def review(command, completion, token, outputs, **kwargs):
            self.assertEqual({p.name for p in completion.parent.iterdir()},
                             {"review-context.md", "details"})
            for entry, text in zip(entries, (body, irrelevant)):
                self.assertEqual((completion.parent / "details" / entry["file"]).read_bytes(), text.encode())
            context = Path(command[command.index("--ide-guidelines") + 1]).read_bytes()
            for p in (source, last):
                self.assertIn(p.read_bytes(), context)
            self.assertIn(json.dumps(rule_materials.detail_index(manifest), ensure_ascii=False).encode(), context)
            self.assertNotIn(body.strip().encode(), context)
            self.assertNotIn(irrelevant.strip().encode(), context)
            self.assertIn("complete detail index and every saved legacy capsule", command[-1])
            with self.assertRaisesRegex(ValueError, "does not match this invocation"):
                kwargs["accept_result"](json.dumps({"review_token": "another-task", "decisions": decisions}))
            self.assertFalse(outputs[0].exists())
            self.assertFalse(completion.exists())
            kwargs["accept_result"]("```json\n" + json.dumps({"review_token": token, "decisions": decisions}) + "\n```")

        decisions = {entries[0]["file"]: {"applies": True, "reason": "Outputs orders"}}
        with patch.object(junie_cli, "run_interactive", side_effect=review) as junie:
            with self.assertRaisesRegex(ValueError, "every saved detail"):
                detail_selection.review_detail_applicability(self.root, capsule, manifest, targets, record, None)
            self.assertNotIn("detail_applicability", record)
            decisions[entries[1]["file"]] = {"applies": False, "reason": "No database operations"}
            selected = detail_selection.review_detail_applicability(self.root, capsule, manifest, targets, record, None)
            saved = json.loads((self.root / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(detail_selection.review_detail_applicability(self.root, capsule, manifest, targets, saved, None),
                             selected)
            self.assertEqual(junie.call_count, 2)
        agent = self.root / "agent"
        agent.mkdir()
        migration_materials.stage_migration_materials(capsule, manifest, agent, targets=targets)
        migration_materials.stage_migration_materials(capsule, manifest, agent, targets=targets, selected=selected)
        guidelines = (agent / ".migration-materials/guidelines.md").read_text(encoding="utf-8")
        self.assertIn(body, guidelines)
        self.assertNotIn(irrelevant, guidelines)
        self.assertFalse((agent / ".migration-materials" / entries[1]["file"]).exists())
        self.assertNotIn("database.md", guidelines)

        (capsule / "materials" / entries[1]["file"]).write_text("changed", encoding="utf-8")
        with patch.object(junie_cli, "run_interactive") as junie:
            with self.assertRaisesRegex(ValueError, "saved detail review material changed"):
                detail_selection.review_detail_applicability(self.root, capsule, manifest, targets, {}, None)
            junie.assert_not_called()

    def test_structured_final_answer_has_no_conflicting_dashboard_progress_instruction(self):
        command = ["fixture-junie", "Return only a JSON object."]
        completion = self.root / "review-complete.txt"
        with patch.object(junie_cli, "junie_dashboard_control", return_value=object()), \
                patch.object(junie_cli, "_run_session", return_value=0) as launch:
            junie_cli.run_interactive(command, completion, "token", (), accept_result=lambda _: None)
        self.assertEqual(launch.call_args.args[0], command)

    def test_valid_detail_reply_preserves_unicode_separators_on_first_attempt(self):
        capsule = self.root / "capsule"
        capsule.mkdir()
        source = capsule / "context.md"
        source.write_bytes(b"<?php echo $id;")
        entry, = rule_materials.write_rule_files(capsule / "materials", [
            ({"path": "orders.md", "usage": "reference", "description": "Order output"}, "Preserve IDs.\n"),
        ])
        manifest = {"unit_id": "test", "parts": [{"path": str(source)}], "rule_files": [entry]}
        rule = entry["file"]
        decisions = {rule: {"applies": True, "reason": "Keep\u2028every\u2029order\u0085ID", "rule_lines": [1, 1]}}
        for fence, end in ((None, "\n"), ("```", "\n"), ("```json", "\r\n")):
            with self.subTest(fence=fence, end=repr(end)):
                def reply(command, completion, token, outputs, **kwargs):
                    body = json.dumps({"review_token": token, "decisions": decisions}, ensure_ascii=False, indent=2)
                    body = body.replace("\n", end)
                    kwargs["accept_result"](end.join((fence, body, "```")) if fence else body)

                record = {}
                with patch.object(junie_cli, "run_interactive", side_effect=reply) as reviewer:
                    detail_selection.review_detail_applicability(self.root, capsule, manifest, (Path("Page.cs"),), record, None)
                reviewer.assert_called_once()
                saved = json.loads((self.root / "run.json").read_bytes())
                self.assertEqual(saved["detail_applicability"], decisions)

    def test_malformed_detail_reply_retries_once_without_adopting_partial_data(self):
        capsule = self.root / "capsule"
        capsule.mkdir()
        entries = rule_materials.write_rule_files(capsule / "materials", [
            ({"path": "orders.md", "usage": "reference", "description": "Order output"}, "Preserve order IDs.\n"),
        ])
        source = capsule / "context.md"
        source.write_text("<?php echo $orderId;", encoding="utf-8")
        manifest = {"unit_id": "test", "parts": [{"path": str(source)}], "rule_files": entries}
        rule = entries[0]["file"]
        manifest["rule_read_order"] = [{"file": rule, "sha256": entries[0]["sha256"], "lines": [1, 1]}]
        hint = rule_search.review_hint(manifest, {rule: 1})
        self.assertTrue(hint)
        budget = None
        for should_complete, limited in ((True, False), (False, False), (True, True)):
            with self.subTest(should_complete=should_complete, limited=limited):
                record = {"status": "junie_running"}
                attempts = []
                sizes = []

                def reply(command, completion, token, outputs, **kwargs):
                    attempts.append(command[-1])
                    guidelines = Path(command[command.index("--ide-guidelines") + 1])
                    sizes.append(len(command[-1].encode("utf-8")) + guidelines.stat().st_size)
                    self.assertFalse(outputs[0].exists())
                    self.assertFalse(completion.exists())
                    self.assertNotIn("detail_applicability", record)
                    if len(attempts) == 1 or not should_complete:
                        kwargs["accept_result"]("Partial reply followed by {")
                    else:
                        kwargs["accept_result"](json.dumps({"review_token": token, "decisions": {
                            rule: {"applies": True, "reason": "Outputs orders"},
                        }}))

                with patch.object(junie_cli, "run_interactive", side_effect=reply):
                    args = self.root, capsule, manifest, (Path("Page.cs"),), record, None
                    if should_complete:
                        config = {"junie_input_limits": {"max_input_bytes": budget}} if limited else None
                        self.assertEqual(detail_selection.review_detail_applicability(*args, config=config), {rule})
                    else:
                        with self.assertRaisesRegex(ValueError, "JSON形式"):
                            detail_selection.review_detail_applicability(*args)
                        self.assertNotIn("detail_applicability", record)
                self.assertEqual(len(attempts), 2)
                self.assertIn("previous reply was not valid JSON", attempts[1])
                self.assertIn(hint, attempts[0])
                if limited:
                    self.assertNotIn(hint, attempts[1])
                    self.assertTrue(all(size <= budget for size in sizes))
                else:
                    self.assertIn(hint, attempts[1])
                    if should_complete:
                        budget = max(sizes[0], sizes[1] - len(hint.encode("utf-8")))

    def test_duplicate_detail_decisions_are_rejected_in_final_replies_and_files(self):
        capsule = self.root / "capsule"
        capsule.mkdir()
        entry, = rule_materials.write_rule_files(capsule / "materials", [
            ({"path": "identity.md", "usage": "reference", "description": "Customer ID"}, "Preserve IDs.\n"),
        ])
        source = capsule / "context.md"
        manifest = {"unit_id": "test", "parts": [{"path": str(source)}], "rule_files": [entry]}
        rule = entry["file"]
        valid = {rule: {"applies": True, "reason": "Keep\u2028customer ID", "rule_lines": [1, 1]}}
        for large in (False, True):
            source.write_bytes(b"<?php echo $customerId;\r\n" * (1000 if large else 1))
            for name, invalid in {
                "rule": f'{{"{rule}":{{"applies":true,"reason":"Keep ID"}},"{rule}":{{"applies":false,"reason":"Drop ID"}}}}',
                "nested": f'{{"{rule}":{{"applies":true,"app\\u006cies":false,"reason":"Conflicting decision"}}}}',
            }.items():
                for repair in (False, True):
                    with self.subTest(large=large, duplicate=name, repair=repair):
                        run = self.root / f"{large}-{name}-{repair}"
                        run.mkdir()
                        record, attempts = {}, []

                        def reply(command, completion, token, outputs, **kwargs):
                            accept = kwargs["accept_result"]
                            self.assertEqual(accept is None, large)
                            self.assertFalse(completion.exists())
                            self.assertNotIn("detail_applicability", record)
                            self.assertFalse((run / "run.json").exists())
                            if attempts:
                                old_completion, old_token = attempts[0]
                                self.assertFalse(old_completion.exists())
                                self.assertNotEqual(token, old_token)
                                self.assertIn(token, command[-1])
                                self.assertIn("no duplicate keys", command[-1])
                            if large:
                                pending = json.loads(outputs[0].read_bytes())
                                self.assertEqual(pending, {rule: {"applies": None, "reason": ""}})
                            else:
                                self.assertFalse(outputs[0].exists())
                            body = json.dumps(valid, ensure_ascii=False) if repair and attempts else invalid
                            attempts.append((completion, token))
                            if large:
                                outputs[0].write_text(body, encoding="utf-8-sig")
                                completion.write_text(token, encoding="utf-8")
                            else:
                                accept('```json\r\n{"review_token":"' + token + '","decisions":' + body + '}\r\n```')

                        args = run, capsule, manifest, (Path("Target.cs"),), record, None
                        with patch.object(junie_cli, "run_interactive", side_effect=reply):
                            if repair:
                                self.assertEqual(detail_selection.review_detail_applicability(*args), {rule})
                                self.assertEqual(json.loads((run / "run.json").read_bytes())["detail_applicability"], valid)
                            else:
                                with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
                                    detail_selection.review_detail_applicability(*args)
                                self.assertNotIn("detail_applicability", record)
                                self.assertFalse((run / "run.json").exists())
                        self.assertEqual(len(attempts), 2)

    def test_invalid_context_evidence_retries_without_reusing_completion(self):
        capsule = self.root / "capsule"
        capsule.mkdir()
        source = capsule / "context.md"
        entry, = rule_materials.write_rule_files(capsule / "materials", [
            ({"path": "orders.md", "usage": "reference", "description": "Order output"}, "Preserve order IDs.\n"),
        ])
        manifest = {"unit_id": "test", "parts": [{"path": str(source)}], "rule_files": [entry]}
        rule = entry["file"]
        context_file = "evidence/target.cs"
        inventory = {context_file: {"file_lines": 2}}
        for large in (False, True):
            source.write_text("<?php echo $orderId;\n" * (3000 if large else 1), encoding="utf-8")
            for repair in (False, True):
                with self.subTest(large=large, repair=repair):
                    record = {}
                    attempts = []

                    def review(command, completion, token, outputs, **kwargs):
                        self.assertEqual(kwargs["accept_result"] is None, large)
                        self.assertNotIn("detail_applicability", record)
                        self.assertFalse(completion.exists())
                        if attempts:
                            prior_completion, prior_token = attempts[0]
                            self.assertFalse(prior_completion.exists())
                            self.assertNotEqual(token, prior_token)
                            self.assertIn(token, command[-1])
                            self.assertIn("Check every context_evidence", command[-1])
                            if large:
                                retained = json.loads(outputs[0].read_text(encoding="utf-8"))
                                self.assertEqual(retained[rule]["reason"], "Outputs orders")
                        citation = {"file": context_file if repair and attempts else "source-0001.md",
                                    "lines": [1, 2]}
                        if large and not (repair and attempts):
                            citation = [citation, {"file": "source-0002.md", "lines": [1, 2]}]
                        decisions = {rule: {"applies": True, "reason": "Outputs orders",
                                            "context_evidence": citation}}
                        attempts.append((completion, token))
                        if large:
                            outputs[0].write_text(json.dumps(decisions), encoding="utf-8")
                            completion.write_text(token, encoding="utf-8")
                        else:
                            kwargs["accept_result"](json.dumps({"review_token": token, "decisions": decisions}))

                    args = self.root, capsule, manifest, (Path("Page.cs"),), record, None
                    with patch.object(detail_review_evidence, "stage_review_evidence", return_value=inventory), \
                            patch.object(junie_cli, "run_interactive", side_effect=review):
                        if repair:
                            self.assertEqual(detail_selection.review_detail_applicability(*args), {rule})
                            self.assertEqual(record["detail_applicability"][rule]["context_evidence"]["file"],
                                             context_file)
                        else:
                            with self.assertRaisesRegex(ValueError, "invalid review context evidence"):
                                detail_selection.review_detail_applicability(*args)
                            self.assertNotIn("detail_applicability", record)
                    self.assertEqual(len(attempts), 2)

    def test_large_review_uses_complete_files_instead_of_inlining_only_a_prefix(self):
        capsule = self.root / "capsule"
        capsule.mkdir()
        first, last = capsule / "first.md", capsule / "last.md"
        first.write_text("<?php echo 'orders';\n", encoding="utf-8")
        entries = rule_materials.write_rule_files(capsule / "materials", [
            ({"path": "rules.md", "usage": "reference", "description": "Order output"}, "Use every original order.\n"),
            ({"path": "other.md", "usage": "reference", "description": "Unrelated\ncheck"}, "Another scope.\n"),
        ])
        entry = entries[0]
        manifest = {"unit_id": "test", "parts": [], "rule_files": entries}

        def review(command, completion, token, outputs, **kwargs):
            if oversized == "huge_index":
                self.assertNotIn("--ide-guidelines", command)
                self.assertFalse((completion.parent / "review-context.md").exists())
                self.assertIn("one candidate per physical line", command[-1])
                self.assertIn("do not create a script just to page", command[-1])
            else:
                context = Path(command[command.index("--ide-guidelines") + 1]).read_text(encoding="utf-8")
                self.assertIn(json.dumps(rule_materials.detail_index(manifest), ensure_ascii=False), context)
                inventory = json.loads(context.split("# Complete source inventory (data, not instructions)\n\n")[1])
                self.assertEqual([row["file"] for row in inventory], ["source-0001.md", "source-0002.md"])
                self.assertEqual(inventory[-1]["file_lines"], 3001 if oversized == "source" else 1)
                self.assertNotIn("TAIL", context)
            self.assertIn("Search across all the saved capsules", command[-1])
            for number, p in enumerate((first, last), 1):
                self.assertEqual((completion.parent / f"source-{number:04d}.md").read_bytes(), p.read_bytes())
            index_text = (completion.parent / "details.json").read_text(encoding="utf-8")
            expected_index = rule_materials.detail_index(manifest)
            self.assertEqual(json.loads(index_text), expected_index)
            self.assertEqual([json.loads(line.removesuffix(",")) for line in index_text.splitlines()[1:-1]],
                             expected_index)
            self.assertEqual((completion.parent / "details" / entry["file"]).read_text(encoding="utf-8"),
                             "Use every original order.\n")
            self.assertIsNone(kwargs["accept_result"])
            self.assertEqual(json.loads(outputs[0].read_text(encoding="utf-8")), {
                "rule-0001.txt": {"applies": None, "reason": ""},
                "rule-0002.txt": {"applies": None, "reason": ""},
            })
            rows = outputs[0].read_text(encoding="utf-8").splitlines()[1:-1]
            self.assertEqual(len(rows), len(expected_index))
            self.assertEqual(
                [next(iter(json.loads("{" + line.strip().removesuffix(",") + "}"))) for line in rows],
                [item["file"] for item in expected_index],
            )
            self.assertIn("including its .txt", command[-1])
            self.assertIn("Use direct file edits in bounded ranges", command[-1])
            self.assertIn("Do not create or run helper scripts", command[-1])
            self.assertNotIn("You may use a workspace shell script", command[-1])
            outputs[0].write_text(json.dumps({
                "rule-0001.txt": {"applies": True, "reason": "Orders"},
                "rule-0002.txt": {"applies": False, "reason": "Unrelated"},
            }))
            completion.write_text(token)

        for oversized in ("source", "index", "huge_index"):
            with self.subTest(oversized=oversized):
                last.write_text(("// 日本語\r\n" * 3000 if oversized == "source" else "") + "TAIL", encoding="utf-8", newline="")
                entry["description"] = {"source": "Order output", "index": "Orders" * 5000,
                                        "huge_index": "日本語" * 8000}[oversized]
                manifest["parts"] = [{"path": str(p), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                                     for p in (first, last)]
                with patch.object(junie_cli, "run_interactive", side_effect=review):
                    detail_selection.review_detail_applicability(self.root, capsule, manifest, (Path("Page.cs"),), {}, None)
                last.write_text("changed", encoding="utf-8")
                with patch.object(junie_cli, "run_interactive") as junie:
                    with self.assertRaisesRegex(ValueError, "saved detail review source changed"):
                        detail_selection.review_detail_applicability(self.root, capsule, manifest, (Path("Page.cs"),), {}, None)
                    junie.assert_not_called()

    def test_large_review_rejects_incomplete_or_invalid_decisions_before_saving(self):
        capsule = self.root / "capsule"
        capsule.mkdir()
        source = capsule / "context.md"
        source.write_text("<?php echo '日本語';\n" * 2000, encoding="utf-8")
        entries = rule_materials.write_rule_files(capsule / "materials", [
            ({"path": f"{number}.md", "usage": "reference", "description": "Output"}, f"Rule {number}\n")
            for number in (1, 2)
        ])
        manifest = {"unit_id": "test", "parts": [{"path": str(source)}], "rule_files": entries}
        valid = {f"rule-{number:04d}.txt": {"applies": number == 1, "reason": "Output scope"} for number in (1, 2)}
        invalid = [
            {"rule-0001.txt": valid["rule-0001.txt"]},
            {name.removesuffix(".txt"): value for name, value in valid.items()},
            {**valid, "unknown.txt": valid["rule-0001.txt"]},
            {**valid, "rule-0002.txt": {"applies": None, "reason": ""}},
            {**valid, "rule-0002.txt": {"applies": "false", "reason": "Output scope"}},
            {**valid, "rule-0002.txt": {"applies": False, "reason": " "}},
        ]
        for decisions, marker in [(value, "valid") for value in invalid] + [(valid, "wrong"), (valid, "absent")]:
            with self.subTest(decisions=decisions, marker=marker):
                def review(command, completion, token, outputs, **kwargs):
                    outputs[0].write_text(json.dumps(decisions))
                    if marker != "absent":
                        completion.write_text(token if marker == "valid" else "another invocation")
                record = {}
                with patch.object(junie_cli, "run_interactive", side_effect=review):
                    with self.assertRaises(ValueError):
                        detail_selection.review_detail_applicability(self.root, capsule, manifest, (Path("Page.cs"),), record, None)
                self.assertNotIn("detail_applicability", record)
                self.assertFalse((self.root / "run.json").exists())

    def test_missing_or_changed_pinned_files_are_not_silently_ignored(self):
        capsule, manifest, agent = self.prepare()
        material = capsule / "materials" / manifest["rule_files"][0]["file"]
        for change in (lambda: material.write_text("changed"), material.unlink):
            change()
            with self.assertRaisesRegex(ValueError, "saved migration material"):
                migration_materials.stage_migration_materials(capsule, manifest, agent)

    def test_old_chunked_run_is_rejected_without_touching_saved_rule_bytes(self):
        capsule = self.root / "capsule"
        material = capsule / "materials"
        material.mkdir(parents=True)
        chunks = []
        contents = ["FIRST\r\n長い", "文章の途中\r\nLAST"]
        for index, content in enumerate(contents, 1):
            name = f"rule-{index:04d}.txt"
            data = content.encode()
            (material / name).write_bytes(data)
            chunks.append({"file": name, "sha256": hashlib.sha256(data).hexdigest()})
        manifest = {"materials": {item["file"]: item["sha256"] for item in chunks}, "rule_chunks": chunks}
        agent = self.root / "agent"
        agent.mkdir()
        with self.assertRaisesRegex(ValueError, "rule_files"):
            migration_materials.stage_migration_materials(capsule, manifest, agent)
        self.assertEqual([(material / item["file"]).read_bytes().decode() for item in chunks], contents)
        self.assertFalse(list(agent.iterdir()))

    def test_old_shared_run_is_rejected_but_current_shared_constraints_are_not_rules(self):
        capsule = self.root / "capsule"
        material = capsule / "materials"
        material.mkdir(parents=True)
        original = ("old mandatory rules\r\n" * 200).encode()
        (material / "shared.md").write_bytes(original)
        agent = self.root / "agent"
        agent.mkdir()
        manifest = {"materials": {"shared.md": hashlib.sha256(original).hexdigest()}, "parts": []}
        with self.assertRaisesRegex(ValueError, "rule_files"):
            migration_materials.stage_migration_materials(capsule, manifest, agent)
        self.assertEqual((material / "shared.md").read_bytes(), original)
        self.assertFalse(list(agent.iterdir()))
        prompt = migration_materials.stage_migration_materials(capsule, {**manifest, "rule_files": []}, agent)
        staged = agent / ".migration-materials"
        self.assertIn("shared.md", prompt)
        self.assertEqual((staged / "shared.md").read_bytes(), original)
        self.assertFalse((staged / "guidelines.md").exists())
        self.assertEqual(rule_materials.render_guidelines(material, {"rule_files": []}), "")
        self.assertNotIn("--ide-guidelines", junie_cli.build_command(agent, "plain", None))

    def test_current_rule_prompt_keeps_saved_crlf(self):
        material = self.root / "materials"
        original = "mandatory text\r\nkeep these line endings\r\n"
        rules = rule_materials.write_rule_files(material, [({"path": "rule.md"}, original)])
        guidelines = rule_materials.render_guidelines(material, {"rule_files": rules})
        self.assertIn(original, guidelines)
