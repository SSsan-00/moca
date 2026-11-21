import hashlib
import json
from pathlib import Path
from unittest.mock import patch

from fixtures import HarnessCase
from migration_harness import junie_cli, rule_materials, workflow


class RuleMaterialsTests(HarnessCase):
    def test_operator_rule_review_replaces_false_decision_and_checks_evidence_version(self):
        from migration_harness.rule_review import review_rules

        (self.project / "source.php").write_text("<?php echo 42;", encoding="utf-8")
        (self.project / "Page.cs").write_text("class Page {}", encoding="utf-8")
        (self.project / "details.md").write_text("Preface\n" + "x\n" * 1000 + "Keep the final exception.\n", encoding="utf-8")
        self.commit_project()
        config = self.config()
        config["units"][0]["inputs"].append({
            "path": "details.md", "role": "migration_rules", "usage": "reference", "description": "Exception",
            "encoding": "utf-8", "start_line": 1, "end_line": None, "shared": True,
        })
        result = workflow.prepare_run(config, "sample")
        run_id = result["run_id"]
        run_path = Path(result["run_path"])
        record_path = run_path / "run.json"
        record = json.loads(record_path.read_bytes())
        rule = "rule-0001.txt"
        record["detail_applicability"] = {rule: {"applies": False, "reason": "Missed exception"}}
        workflow.write_run_record(record_path, record)
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
        capsule, manifest = workflow.load_capsule_manifest(run_path)
        agent = run_path / "agent-workspace"
        agent.mkdir()
        saved = json.loads(record_path.read_bytes())
        selected = rule_materials.effective_details(manifest, saved)
        workflow._stage_migration_materials(capsule, manifest, agent, selected=selected)
        self.assertIn("Keep the final exception.",
                      (agent / ".migration-materials/guidelines.md").read_text(encoding="utf-8"))
        updated = Path(result["worktree_path"]) / next(iter(report["targets"]))
        updated.write_text("class Changed {}", encoding="utf-8")
        self.assertIsNone(review_rules(config, run_id)["rules"][0]["independent_verification"])
        (run_path / row["snapshot"]).write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "saved rule changed"):
            review_rules(config, run_id)

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
        result = workflow.prepare_run(config, "sample")
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
        result = workflow.prepare_run(config, "sample")
        capsule = Path(result["capsule_path"])
        manifest = json.loads((capsule / "manifest.json").read_bytes())
        agent = Path(result["run_path"]) / "agent-workspace"
        agent.mkdir()
        workflow._stage_migration_materials(capsule, manifest, agent)
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
        workflow._stage_migration_materials(capsule, manifest, agent, repair=True)
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
        result = workflow.prepare_run(config, "sample")
        capsule, manifest = workflow.load_capsule_manifest(Path(result["run_path"]))
        records = manifest["rule_files"]
        selected = frozenset({records[1]["file"]})
        agent = Path(result["run_path"]) / "agent-workspace"
        agent.mkdir()
        workflow._stage_migration_materials(capsule, manifest, agent, selected=selected)
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
        for old in (before, {**before, "rule_guidelines_format": "unknown-future-format"}):
            rendered = rule_materials.render_guidelines(capsule / "materials", old, selected=selected)
            self.assertIn(numbered.decode(), rendered)
            self.assertNotIn("lines 1-5 (.migration-materials/rule-0001.txt)", rendered)
        workflow._stage_migration_materials(capsule, manifest, agent, repair=True, selected=selected)
        self.assertEqual((agent / ".migration-materials/guidelines.md").read_bytes().decode(), inline)

    def test_compact_format_does_not_increase_short_rule_input(self):
        material = self.root / "materials"
        short = "## migration_rules — `rules.md` lines 1-1\n\n```text\n     1 | 最初で最後\n```\n\n"
        entries = rule_materials.write_rule_files(material, [({"path": "rules.md"}, short)])
        self.assertEqual(
            rule_materials.render_guidelines(material, {
                "rule_files": entries, "rule_guidelines_format": rule_materials.COMPACT_RULE_GUIDELINES,
            }),
            rule_materials.render_guidelines(material, {"rule_files": entries}),
        )

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
                workflow._run_budgeted_junie(settings, root / "run.json", record, "migration", None, root, [],
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
                workflow._review_detail_applicability(self.root, capsule, manifest, targets, record, None)
            self.assertNotIn("detail_applicability", record)
            decisions[entries[1]["file"]] = {"applies": False, "reason": "No database operations"}
            selected = workflow._review_detail_applicability(self.root, capsule, manifest, targets, record, None)
            saved = json.loads((self.root / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(workflow._review_detail_applicability(self.root, capsule, manifest, targets, saved, None),
                             selected)
            self.assertEqual(junie.call_count, 2)
        agent = self.root / "agent"
        agent.mkdir()
        workflow._stage_migration_materials(capsule, manifest, agent, targets=targets)
        workflow._stage_migration_materials(capsule, manifest, agent, targets=targets, selected=selected)
        guidelines = (agent / ".migration-materials/guidelines.md").read_text(encoding="utf-8")
        self.assertIn(body, guidelines)
        self.assertNotIn(irrelevant, guidelines)
        self.assertFalse((agent / ".migration-materials" / entries[1]["file"]).exists())
        self.assertNotIn("database.md", guidelines)

        (capsule / "materials" / entries[1]["file"]).write_text("changed", encoding="utf-8")
        with patch.object(junie_cli, "run_interactive") as junie:
            with self.assertRaisesRegex(ValueError, "saved detail review material changed"):
                workflow._review_detail_applicability(self.root, capsule, manifest, targets, {}, None)
            junie.assert_not_called()

    def test_structured_final_answer_has_no_conflicting_dashboard_progress_instruction(self):
        command = ["fixture-junie", "Return only a JSON object."]
        completion = self.root / "review-complete.txt"
        with patch.object(junie_cli, "junie_dashboard_control", return_value=object()), \
                patch.object(junie_cli, "_run_session", return_value=0) as launch:
            junie_cli.run_interactive(command, completion, "token", (), accept_result=lambda _: None)
        self.assertEqual(launch.call_args.args[0], command)

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
        for should_complete in (True, False):
            with self.subTest(should_complete=should_complete):
                record = {"status": "junie_running"}
                attempts = []

                def reply(command, completion, token, outputs, **kwargs):
                    attempts.append(command[-1])
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
                        self.assertEqual(workflow._review_detail_applicability(*args), {rule})
                    else:
                        with self.assertRaisesRegex(ValueError, "JSON形式"):
                            workflow._review_detail_applicability(*args)
                        self.assertNotIn("detail_applicability", record)
                self.assertEqual(len(attempts), 2)
                self.assertIn("previous reply was not valid JSON", attempts[1])

    def test_large_review_uses_complete_files_instead_of_inlining_only_a_prefix(self):
        capsule = self.root / "capsule"
        capsule.mkdir()
        first, last = capsule / "first.md", capsule / "last.md"
        first.write_text("<?php echo 'orders';\n", encoding="utf-8")
        entry, = rule_materials.write_rule_files(capsule / "materials", [
            ({"path": "rules.md", "usage": "reference", "description": "Order output"}, "Use every original order.\n"),
        ])
        manifest = {"unit_id": "test", "parts": [], "rule_files": [entry]}

        def review(command, completion, token, outputs, **kwargs):
            if oversized == "huge_index":
                self.assertNotIn("--ide-guidelines", command)
                self.assertFalse((completion.parent / "review-context.md").exists())
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
            saved_index = json.loads((completion.parent / "details.json").read_text(encoding="utf-8"))
            self.assertEqual(saved_index[0]["when_to_read"], entry["description"])
            self.assertEqual((completion.parent / "details" / entry["file"]).read_text(encoding="utf-8"),
                             "Use every original order.\n")
            self.assertIsNone(kwargs["accept_result"])
            self.assertEqual(json.loads(outputs[0].read_text(encoding="utf-8")), {
                "rule-0001.txt": {"applies": None, "reason": ""},
            })
            self.assertIn("including its .txt", command[-1])
            outputs[0].write_text(json.dumps({"rule-0001.txt": {"applies": True, "reason": "Orders"}}))
            completion.write_text(token)

        for oversized in ("source", "index", "huge_index"):
            with self.subTest(oversized=oversized):
                last.write_text(("// 日本語\r\n" * 3000 if oversized == "source" else "") + "TAIL", encoding="utf-8", newline="")
                entry["description"] = {"source": "Order output", "index": "Orders" * 5000,
                                        "huge_index": "日本語" * 8000}[oversized]
                manifest["parts"] = [{"path": str(p), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                                     for p in (first, last)]
                with patch.object(junie_cli, "run_interactive", side_effect=review):
                    workflow._review_detail_applicability(self.root, capsule, manifest, (Path("Page.cs"),), {}, None)
                last.write_text("changed", encoding="utf-8")
                with patch.object(junie_cli, "run_interactive") as junie:
                    with self.assertRaisesRegex(ValueError, "saved detail review source changed"):
                        workflow._review_detail_applicability(self.root, capsule, manifest, (Path("Page.cs"),), {}, None)
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
                        workflow._review_detail_applicability(self.root, capsule, manifest, (Path("Page.cs"),), record, None)
                self.assertNotIn("detail_applicability", record)
                self.assertFalse((self.root / "run.json").exists())

    def test_missing_or_changed_pinned_files_are_not_silently_ignored(self):
        capsule, manifest, agent = self.prepare()
        material = capsule / "materials" / manifest["rule_files"][0]["file"]
        for change in (lambda: material.write_text("changed"), material.unlink):
            change()
            with self.assertRaisesRegex(ValueError, "saved migration material"):
                workflow._stage_migration_materials(capsule, manifest, agent)

    def test_old_chunked_run_joins_pinned_text_without_new_receipts(self):
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
        workflow._stage_migration_materials(capsule, manifest, agent)
        self.assertIn("".join(contents), (agent / ".migration-materials/guidelines.md").read_bytes().decode())
        self.assertFalse((agent / "migration-read-rules.py").exists())

    def test_old_shared_run_and_no_rules(self):
        capsule = self.root / "capsule"
        material = capsule / "materials"
        material.mkdir(parents=True)
        original = ("old mandatory rules\r\n" * 200).encode()
        (material / "shared.md").write_bytes(original)
        agent = self.root / "agent"
        agent.mkdir()
        manifest = {"materials": {"shared.md": hashlib.sha256(original).hexdigest()}, "parts": []}
        workflow._stage_migration_materials(capsule, manifest, agent)
        self.assertIn(original, (agent / ".migration-materials/guidelines.md").read_bytes())
        self.assertEqual(rule_materials.render_guidelines(material, {"rule_files": []}), "")
        self.assertNotIn("--ide-guidelines", junie_cli.build_command(agent, "plain", None))
