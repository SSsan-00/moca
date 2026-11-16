import hashlib
import json
from pathlib import Path

from fixtures import HarnessCase
from migration_harness import junie_cli, rule_materials, workflow


class RuleMaterialsTests(HarnessCase):
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
