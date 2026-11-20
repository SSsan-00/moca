import json
from pathlib import Path
import tempfile
import unittest

from migration_harness.capsules import prepare_context
from migration_harness.context_handoff import context_handoff, handoff_prompt, record_handoff


class ContextHandoffTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.run = self.root / "run"
        (self.root / "demo.php").write_text("<?php\necho 42;\n", encoding="utf-8")
        self.target = self.root / "Demo.cs"
        self.target.write_text("class Demo {}", encoding="utf-8")
        self.targets = (Path("Demo.cs"),)
        prepare_context({"project_root": str(self.root), "max_context_chars": 1_000}, {
            "id": "unit", "target": "Demo.cs", "inputs": [{
                "role": "php", "path": "demo.php", "start_line": 1, "end_line": None,
                "encoding": "utf-8", "shared": False,
            }],
        }, self.run)
        self.record = self.run / "run.json"
        self.record.write_text('{"completed_capsules":0, "verification_passed":false}', encoding="utf-8")

    def add(self, note="check failed-state return", supersedes=None):
        return record_handoff(self.run, self.root, self.targets, {},
                              "php-0001", 2, 2, note, supersedes=supersedes)

    def test_replay_and_correction_keep_history_and_never_change_progress(self):
        old = self.add()
        self.assertEqual(self.add(), old)
        replacement = self.add("check both return paths", supersedes=old["id"])
        restored = context_handoff(self.run, self.root, self.targets, {})
        self.assertEqual([replacement["id"], old["id"]], [row["id"] for row in restored])
        self.assertEqual(["pending", "superseded"], [row["state"] for row in restored])
        prompt = handoff_prompt(self.run, self.root, self.targets, {}, through=1)
        self.assertIn("check both return paths", prompt)
        self.assertNotIn("check failed-state return", prompt)
        self.assertEqual({"completed_capsules": 0, "verification_passed": False}, json.loads(self.record.read_text()))

    def test_source_target_and_rule_changes_disable_hint(self):
        self.add()
        self.assertEqual("", handoff_prompt(self.run, self.root, self.targets, {
            "detail_overrides": {"detail": {"applies": False}},
        }))
        self.target.write_text("class Edited {}", encoding="utf-8")
        self.assertEqual("", handoff_prompt(self.run, self.root, self.targets, {}))
        self.target.write_text("class Demo {}", encoding="utf-8")
        (self.run / "capsule/php-sources/0001.php").write_text("corrupt", encoding="utf-8")
        self.assertEqual("", handoff_prompt(self.run, self.root, self.targets, {}))

    def test_missing_and_corrupt_legacy_handoff_does_not_prevent_work(self):
        self.assertEqual("", handoff_prompt(self.run, self.root, self.targets, {}))
        (self.run / "context-handoff.json").write_text("invalid", encoding="utf-8")
        self.assertIn("unreadable", handoff_prompt(self.run, self.root, self.targets, {}))
