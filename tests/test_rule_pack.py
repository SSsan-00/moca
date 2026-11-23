import importlib.util
import hashlib
import json
import tempfile
import tomllib
import unittest
from unittest.mock import patch
from pathlib import Path

from fixtures import HarnessCase
from migration_harness import workflow
from migration_harness.config import load_config


spec = importlib.util.spec_from_file_location("rule_pack", Path(__file__).resolve().parents[1] / "tools/rule_pack.py")
rule_pack = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rule_pack)


class RulePackTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.pack = Path(self.temporary.name) / "ルール & rules"
        (self.pack / "sources").mkdir(parents=True)
        self.original = b"\xef\xbb\xbf" + "# 共通規約\r\n値を推測しない。\r\n\r\n## DB\r\n例外：参照専用では更新しない。\r\n```sql\r\nSELECT 'A & B';\r\n```".encode()
        (self.pack / "sources/mixed.md").write_bytes(self.original)
        self.plan = {"documents": [
            {"path": "Agent.md", "title": "共通の必須事項", "parts": [{"source": "mixed.md", "start": 1, "end": 3}]},
            {"path": "details/database.md", "title": "DBの詳細", "description": "DB読み書きを移行・修正するとき",
             "parts": [{"source": "mixed.md", "start": 4, "end": 8}]},
        ]}
        rule_pack.snapshot(self.pack)
        self.save_plan()

    def save_plan(self):
        (self.pack / "plan.json").write_text(json.dumps(self.plan, ensure_ascii=False), encoding="utf-8")

    def test_preserves_original_bytes_and_text_including_exceptions_and_code(self):
        rule_pack.build(self.pack)
        rule_pack.verify(self.pack)
        self.assertEqual((self.pack / "sources/mixed.md").read_bytes(), self.original)
        content = (self.pack / "generated/details/database.md").read_bytes()
        self.assertIn("## DB\r\n例外：参照専用では更新しない。\r\n```sql\r\nSELECT 'A & B';\r\n```".encode(), content)
        rules = tomllib.loads((self.pack / "generated/rules.toml").read_text(encoding="utf-8"))["project"]["migration_rules"]
        self.assertEqual([item["usage"] for item in rules], ["required", "reference"])
        self.assertEqual(Path(rules[0]["path"]), self.pack / "generated/Agent.md")
        self.assertEqual(rules[1]["description"], self.plan["documents"][1]["description"])
        for item in rules:
            self.assertTrue(Path(item["path"]).is_file())
        coverage = json.loads((self.pack / "generated/coverage.json").read_text(encoding="utf-8"))
        self.assertEqual(sum(p["end"] - p["start"] + 1 for p in coverage["placements"]), 8)

    def test_old_packs_still_verify_and_new_format_keeps_the_same_original_excerpts(self):
        legacy = rule_pack.render(self.pack)
        # 更新前に生成した案内のバイト列。既存PACKを「改変あり」と誤検知させない。
        self.assertEqual(hashlib.sha256(legacy["Agent.md"]).hexdigest(),
                         "08049c76ff6930143ff6bbd5afa9a7e4154adebb07e271f6a7d64d0f7eff6870")
        rule_pack.build(self.pack)
        rule_pack.verify(self.pack)
        self.plan["format_version"] = 2
        self.save_plan()
        current = rule_pack.render(self.pack)
        self.assertNotEqual(current["Agent.md"], legacy["Agent.md"])
        for name in ("details/database.md", "coverage.json", "rules.toml"):
            self.assertEqual(current[name], legacy[name])
        self.assertIn("# 共通規約\r\n値を推測しない。\r\n\r\n".encode(), current["Agent.md"])
        self.assertEqual((self.pack / "sources/mixed.md").read_bytes(), self.original)
        with self.assertRaisesRegex(ValueError, "出力の変更"):
            rule_pack.verify(self.pack)
        for version in (0, 3, True, "2"):
            self.plan["format_version"] = version
            self.save_plan()
            with self.subTest(version=version), self.assertRaisesRegex(ValueError, "format_version"):
                rule_pack.render(self.pack)

    def test_missing_and_overlapping_lines_block_generation_without_partial_output(self):
        for start, message in [(5, "未配置"), (3, "重複")]:
            with self.subTest(start=start):
                self.plan["documents"][1]["parts"][0]["start"] = start
                self.save_plan()
                with self.assertRaisesRegex(ValueError, message):
                    rule_pack.build(self.pack)
                self.assertFalse((self.pack / "generated").exists())

    def test_source_changes_and_additions_are_detected(self):
        path = self.pack / "sources/mixed.md"
        path.write_bytes(self.original + b"NEW RULE")
        with self.assertRaisesRegex(ValueError, "原文の変更"):
            rule_pack.build(self.pack)
        path.write_bytes(self.original)
        (self.pack / "sources/forgotten.txt").write_text("do not omit this", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "原文の変更"):
            rule_pack.build(self.pack)

    def test_generated_documents_and_coverage_cannot_be_falsified(self):
        rule_pack.build(self.pack)
        for name in ("details/database.md", "Agent.md", "coverage.json", "rules.toml"):
            path = self.pack / "generated" / name
            original = path.read_bytes()
            path.write_bytes(b"summary only")
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "出力の変更"):
                rule_pack.verify(self.pack)
            path.write_bytes(original)
        path.unlink()
        with self.assertRaisesRegex(ValueError, "出力の変更"):
            rule_pack.verify(self.pack)

    def test_existing_snapshot_and_output_are_not_overwritten(self):
        with self.assertRaises(FileExistsError):
            rule_pack.snapshot(self.pack)
        rule_pack.build(self.pack)
        original = (self.pack / "generated/Agent.md").read_bytes()
        with self.assertRaisesRegex(ValueError, "既にあります"):
            rule_pack.build(self.pack)
        self.assertEqual((self.pack / "generated/Agent.md").read_bytes(), original)

    def test_invalid_plans_do_not_escape_the_pack_or_hide_details(self):
        for path, description, start, end in [
            ("../outside.md", "DB処理", 4, 8), ("Agent.md", "DB処理", 4, 8),
            ("details/database.md", "", 4, 8), ("details/database.md", "DB処理", 4, 9),
            ("details/database.md", "DB処理", True, 8),
        ]:
            with self.subTest(path=path, start=start, end=end):
                document = self.plan["documents"][1]
                document.update(path=path, description=description)
                document["parts"][0].update(start=start, end=end)
                self.save_plan()
                with self.assertRaises(ValueError):
                    rule_pack.build(self.pack)
                self.assertFalse((self.pack / "generated").exists())

    def test_unsupported_sources_are_not_silently_discarded(self):
        (self.pack / "sources/unread.pdf").write_bytes(b"%PDF-1.7")
        with self.assertRaisesRegex(ValueError, "Markdownかテキスト"):
            rule_pack.build(self.pack)

    def test_unreadable_folders_cannot_disappear_from_inventory_or_verification(self):
        rule_pack.build(self.pack)
        original_iterdir = Path.iterdir
        for directory in ("sources", "generated"):
            blocked = self.pack / directory / "unreadable"
            blocked.mkdir()
            (blocked / "rule.md").write_text("keep this", encoding="utf-8")

            def fail_enumeration(path):
                if path == blocked:
                    raise PermissionError("cannot list rules")
                return original_iterdir(path)

            action = rule_pack.source_files if directory == "sources" else rule_pack.verify
            with self.subTest(directory=directory), patch.object(Path, "iterdir", fail_enumeration):
                with self.assertRaises(PermissionError):
                    action(self.pack)
            (blocked / "rule.md").unlink()
            blocked.rmdir()

    def test_windows_reserved_output_names_are_rejected_before_writing(self):
        for name in ("con", "prn", "aux", "nul", "com1", "com9", "lpt1", "lpt9"):
            self.plan["documents"][1]["path"] = f"details/{name}.md"
            self.save_plan()
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "Windowsの予約名"):
                rule_pack.build(self.pack)
            self.assertFalse((self.pack / "generated").exists())

    def test_toml_escapes_control_characters_and_preserves_unicode(self):
        description = 'DBの処理\t"query"\x7f 📝'
        self.plan["documents"][1]["description"] = description
        self.save_plan()
        # Windowsのエディターが保存するBOM付きJSONも受け付ける。
        path = self.pack / "plan.json"
        path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
        rule_pack.build(self.pack)
        rule_pack.verify(self.pack)
        rules = tomllib.loads((self.pack / "generated/rules.toml").read_text(encoding="utf-8"))
        self.assertEqual(rules["project"]["migration_rules"][1]["description"], description)

    def test_symlinked_inputs_and_outputs_are_rejected(self):
        target = self.pack / "sources/mixed.md"
        link = self.pack / "sources/link.md"
        try:
            link.symlink_to(target)
        except OSError:
            self.skipTest("symlinks unavailable")
        with self.assertRaisesRegex(ValueError, "リンク"):
            rule_pack.build(self.pack)
        link.unlink()
        rule_pack.build(self.pack)
        link = self.pack / "generated/details/database.md"
        link.unlink()
        link.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "リンク"):
            rule_pack.verify(self.pack)


class RulePackHarnessTests(HarnessCase):
    def test_generated_registration_supplies_entry_and_pins_details(self):
        (self.project / "source.php").write_text("<?php echo 42;\n", encoding="utf-8")
        (self.project / "Page.cs").write_text("class Page {}\n", encoding="utf-8")
        self.commit_project()
        self.config()
        pack = self.root / "harness/rule-workspace/project"
        (pack / "sources").mkdir(parents=True)
        (pack / "sources/rules.md").write_text("MANDATORY: keep original values.\nDETAIL: SQL rollback rules.\n", encoding="utf-8")
        rule_pack.snapshot(pack)
        (pack / "plan.json").write_text(json.dumps({"format_version": 2, "documents": [
            {"path": "Agent.md", "title": "Common", "parts": [{"source": "rules.md", "start": 1, "end": 1}]},
            {"path": "details/database.md", "title": "Database", "description": "SQL migration",
             "parts": [{"source": "rules.md", "start": 2, "end": 2}]},
        ]}), encoding="utf-8")
        rule_pack.build(pack)
        configuration = self.root / "test.toml"
        with configuration.open("a", encoding="utf-8") as stream:
            stream.write("\n" + (pack / "generated/rules.toml").read_text(encoding="utf-8"))
        result = workflow.prepare_run(load_config(configuration), "sample")
        capsule = Path(result["capsule_path"])
        manifest = json.loads((capsule / "manifest.json").read_text(encoding="utf-8"))
        agent = Path(result["run_path"]) / "agent-workspace"
        agent.mkdir()
        workflow._stage_migration_materials(capsule, manifest, agent)
        guidelines = (agent / ".migration-materials/guidelines.md").read_text(encoding="utf-8")
        self.assertIn("MANDATORY: keep original values.", guidelines)
        self.assertNotIn("DETAIL: SQL rollback rules.", guidelines)
        entry = next(entry for entry in manifest["rule_files"] if entry["usage"] == "reference")
        self.assertIn(entry["path"], guidelines)
        self.assertIn(".migration-materials/" + entry["file"], guidelines)
        self.assertIn("DETAIL: SQL rollback rules.", (agent / ".migration-materials" / entry["file"]).read_text(encoding="utf-8"))
        from migration_harness.rules.rule_materials import render_guidelines

        selected = render_guidelines(capsule / "materials", manifest, selected=frozenset({entry["file"]}))
        self.assertEqual(selected.count("DETAIL: SQL rollback rules."), 1)
        self.assertEqual(selected.count("MANDATORY: keep original values."), 1)
