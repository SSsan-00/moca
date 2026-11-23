import json
from unittest.mock import patch

from fixtures import HarnessCase
from migration_harness.context import capsules
from migration_harness.rules import rule_search
from migration_harness.config import load_config
from migration_harness.execution import ExecutionControl, OperationCancelled, control_scope
from migration_harness.rules.rule_materials import detail_index, selected_details


class RuleSearchTests(HarnessCase):
    def setup_rules(self, php="<?php function calculateRefund() { return 42; }"):
        self.config()
        (self.project / "source.php").write_text(php + "\n", encoding="utf-8")
        path = self.root / "test.toml"
        contents = path.read_text(encoding="utf-8").replace("end_line = 1", f"end_line = {len(php.splitlines())}", 1)
        path.write_text(contents, encoding="utf-8")
        with path.open("a", encoding="utf-8") as stream:
            for name, content in {
                "first": "calculate refund " * 20,
                "exception": "unrelated\r\n" * 31 + "calculateRefund exception\r\n",
                "japanese": "返品上限には例外があります。\r\n",
                "required": "Always apply calculateRefund rules.\r\n",
            }.items():
                (self.project / f"{name}.md").write_bytes(content.encode())
                stream.write(f'\n[[project.migration_rules]]\npath = "{name}.md"\nencoding = "utf-8"\n')
                if name != "required":
                    stream.write('usage = "reference"\ndescription = "Details"\n')
        config = load_config(path)
        return config, config["units"][0]

    def prepare(self, config, unit, name="run"):
        run = self.root / name
        capsules.prepare_context(config, unit, run)
        capsule = run / "capsule"
        manifest = json.loads((capsule / "manifest.json").read_bytes())
        counts = {}
        for row in manifest["rule_files"]:
            with (capsule / "materials" / row["file"]).open(encoding="utf-8", newline="") as stream:
                counts[row["file"]] = len(list(stream))
        return capsule, manifest, counts

    def test_exact_identifier_prioritized_and_late_lines_remain_exact(self):
        config, unit = self.setup_rules()
        capsule, manifest, counts = self.prepare(config, unit)
        hints = manifest["rule_read_order"]
        self.assertEqual([h["file"] for h in hints], ["rule-0002.txt", "rule-0001.txt"])
        first = hints[0]
        with (capsule / "materials" / first["file"]).open(encoding="utf-8", newline="") as stream:
            lines = list(stream)
        lo, hi = first["lines"]
        self.assertIn("calculateRefund exception", "".join(lines[lo - 1:hi]))
        prompt = rule_search.review_hint(manifest, counts)
        self.assertIn("full inventory", prompt)
        self.assertLess(prompt.index("rule-0002.txt"), prompt.index("rule-0001.txt"))
        self.assertEqual(len(manifest["rule_files"]), 4)
        self.assertFalse((self.root / "run" / ".rule-search").exists())
        with self.assertRaisesRegex(ValueError, "exactly one decision"):
            selected_details(detail_index(manifest), {"rule-0002.txt": {"applies": True, "reason": "held"}}, counts)

    def test_separate_php_regions_suggest_different_rules_without_quoting_source(self):
        config, unit = self.setup_rules(
            "<?php\nfunction calculateRefund() { return 'unrelated'; }"
            "\n// 返品上限\nfunction checkLimit() { return true; }"
        )
        _, manifest, counts = self.prepare(config, unit)
        self.assertEqual({hint["file"] for hint in manifest["rule_read_order"]},
                         {"rule-0001.txt", "rule-0002.txt", "rule-0003.txt"})
        prompt = rule_search.review_hint(manifest, counts)
        self.assertNotIn("calculateRefund", prompt)
        self.assertNotIn("返品上限", prompt)

    def test_japanese_comment_suggests_japanese_rule(self):
        config, unit = self.setup_rules("<?php // 返品上限")
        _, manifest, _ = self.prepare(config, unit)
        self.assertEqual([h["file"] for h in manifest["rule_read_order"]], ["rule-0003.txt"])

    def test_no_query_and_no_match_leave_full_inventory(self):
        for name, php in {"empty": "<?php echo 42;", "no-match": "<?php function xyzAbc() {}"}.items():
            with self.subTest(name=name):
                if name == "empty":
                    config, unit = self.setup_rules(php)
                else:
                    (self.project / "source.php").write_text(php, encoding="utf-8")
                _, manifest, counts = self.prepare(config, unit, name)
                self.assertEqual(rule_search.review_hint(manifest, counts), "")
                self.assertNotIn("rule_read_order", manifest)
                self.assertEqual(len(detail_index(manifest)), 3)

    def test_only_search_failure_falls_back(self):
        config, unit = self.setup_rules()
        with patch.object(rule_search, "_MAX_RULE_CHARS", 1), patch.object(rule_search, "report_activity") as log:
            _, manifest, _ = self.prepare(config, unit)
        log.assert_called_once()
        self.assertNotIn("rule_read_order", manifest)
        self.assertEqual([row["usage"] for row in manifest["rule_files"]].count("required"), 1)
        (self.project / "required.md").unlink()
        with self.assertRaises((ValueError, OSError)):
            self.prepare(config, unit, "missing")

    def test_search_does_not_swallow_cancellation(self):
        control = ExecutionControl(lambda _: None)
        control.cancelled.set()
        with control_scope(control), self.assertRaises(OperationCancelled):
            rule_search.prepare_rule_hint([((), [(1, "calculateRefund")])], [({}, "irrelevant")], [{"usage": "reference"}])

    def test_new_run_ranks_new_version_but_saved_run_keeps_hints(self):
        config, unit = self.setup_rules()
        _, manifest, counts = self.prepare(config, unit)
        prompt = rule_search.review_hint(manifest, counts)
        (self.project / "exception.md").write_text("none", encoding="utf-8")
        _, fresh, _ = self.prepare(config, unit, "fresh")
        self.assertEqual([h["file"] for h in fresh["rule_read_order"]], ["rule-0001.txt"])
        self.assertEqual(rule_search.review_hint(manifest, counts), prompt)
        manifest["rule_read_order"][0]["sha256"] = "0" * 64
        self.assertEqual(rule_search.review_hint(manifest, counts), "")
