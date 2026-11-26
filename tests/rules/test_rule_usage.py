import hashlib
import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

from tests.support.fixtures import HarnessCase


spec = importlib.util.spec_from_file_location("rule_usage", Path(__file__).resolve().parents[2] / "tools/rule_usage.py")
rule_usage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rule_usage)


class RuleUsageTests(HarnessCase):
    def setUp(self):
        super().setUp()
        self.path = self.root / "settings.toml"
        self.path.write_text('''[project]
root = "app"
worktree_root = "trees"
run_root = "runs"

[[project.migration_rules]]
path = "required.md"
[[project.migration_rules]]
path = "Agent.md"
usage = "reference"
description = "DBの詳細"
[[project.migration_rules]]
path = "legacy.md"
encoding = "euc-jp"
[[project.migration_rules]]
path = "legacy.md"
encoding = "euc-jp"

[[units]]
id = "one"
source = "unread.php"
target = "One.cs"
[[units]]
id = "two"
source = "unread.php"
target = "Two.cs"
''', encoding="utf-8")
        self.raw = {
            "required.md": b"\xef\xbb\xbf" + "必須\r\n".encode(),
            "Agent.md": "内容を読んで分類する。名前だけでは必須にならない。\n".encode(),
            "legacy.md": "追加の規約\r\n".encode("euc-jp"),
        }
        for name, data in self.raw.items():
            (self.project / name).write_bytes(data)

    def test_counts_effective_required_text_once_without_mutating_sources_or_configuration(self):
        original = self.path.read_bytes()
        result = rule_usage.inspect_rules(self.path)
        self.assertEqual(result["required_chars"], len("必須\r\n追加の規約\r\n"))
        self.assertEqual(len(result["rules"]), 3)
        for rule in result["rules"]:
            name = Path(rule["path"]).name
            self.assertEqual(rule["sha256"], hashlib.sha256(self.raw[name]).hexdigest())
            self.assertEqual(Path(rule["path"]).read_bytes(), self.raw[name])
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse((self.root / "trees").exists())
        self.assertFalse((self.root / "runs").exists())

    def test_cli_preserves_previous_inventory_and_missing_rule_does_not_produce_report(self):
        output = self.root / "before.json"
        with patch("sys.argv", ["rule_usage.py", str(self.path), "--output", str(output)]):
            rule_usage.main()
            original = output.read_bytes()
            self.assertEqual(json.loads(original), rule_usage.inspect_rules(self.path))
            with self.assertRaises(FileExistsError):
                rule_usage.main()
            self.assertEqual(output.read_bytes(), original)
        (self.project / "legacy.md").unlink()
        missing_output = self.root / "missing.json"
        with patch("sys.argv", ["rule_usage.py", str(self.path), "--output", str(missing_output)]):
            with self.assertRaises(FileNotFoundError):
                rule_usage.main()
        self.assertFalse(missing_output.exists())
