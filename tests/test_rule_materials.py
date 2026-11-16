import hashlib
import json
from pathlib import Path
from unittest.mock import patch

from fixtures import HarnessCase
from migration_harness import junie_cli, rule_materials, workflow


class RuleMaterialsTests(HarnessCase):
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
        self.original = "FIRST RULE\r\n" + "保持する😀" * 1600 + "\r\nLAST RULE\r\n"
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
        self.assertIn(self.original.replace("\r\n", "\r\n     2 | ", 1).split("LAST RULE")[0].encode(), content)
        self.assertIn(b"LAST RULE", content)
        self.assertIn(content, (agent / ".migration-materials/guidelines.md").read_bytes())
        self.assertEqual(len(manifest["parts"]), 1)
        self.assertFalse((agent / "migration-read-rules.py").exists())
        command = junie_cli.build_command(agent, "migrate", 7, guidelines=agent / ".migration-materials/guidelines.md")
        self.assertEqual(command[-2:], ["--prompt", "migrate"])
        self.assertNotIn("FIRST RULE", " ".join(command))  # Windowsの引数には本文を展開しない。
        self.assertIn("--ide-guidelines", command)
        (self.project / "rules.md").unlink()
        (agent / ".migration-materials/guidelines.md").write_text("changed")
        workflow._stage_migration_materials(capsule, manifest, agent, repair=True)
        self.assertIn(content, (agent / ".migration-materials/guidelines.md").read_bytes())

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
