import json
import os
import subprocess
import sys
from pathlib import Path

from fixtures import HarnessCase


class RuleReviewTests(HarnessCase):
    def test_unread_tail_and_demoted_mandatory_line_block_application(self):
        config = self.root / "settings.toml"
        original = b'''[project]
root = "app"
worktree_root = "trees"
run_root = "runs"
[[project.migration_rules]]
path = "details.md"
usage = "reference"
description = "View rules"
[[units]]
id = "one"
source = "source.php"
target = "One.cs"
'''
        config.write_bytes(original)
        detail = self.project / "details.md"
        first = "Applies only to views."
        last = "Every migration must preserve correlation IDs."
        detail.write_text(first + "\n" + "x" * 2700 + "\n" + last + "\n", encoding="utf-8")
        pack = self.root / "review"
        candidate = pack / "proposed.toml"
        tool = Path(__file__).resolve().parents[1] / "tools/rule_review.py"

        def run(*arguments):
            return subprocess.run(
                [sys.executable, str(tool), *map(str, arguments)],
                capture_output=True, encoding="utf-8",
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            )

        self.assertEqual(run("start", pack, config).returncode, 0)
        self.assertEqual(run("read", pack, 1).returncode, 0)
        candidate.write_bytes(original)
        review = {"1": {"end": "x" * 2700, "note": "View details.", "required_lines": []}}
        review_path = pack / "reading-review.json"
        review_path.write_text(json.dumps(review), encoding="utf-8")
        self.assertIn("未確認のページ: 2", run("apply", pack, candidate).stderr)
        self.assertEqual(config.read_bytes(), original)

        self.assertIn(last, run("read", pack, 2).stdout)
        review["2"] = {"end": last, "note": "Applies project-wide.", "required_lines": [3]}
        review_path.write_text(json.dumps(review), encoding="utf-8")
        self.assertEqual(run("check", pack).returncode, 0)
        self.assertIn("必須登録に原文がありません", run("apply", pack, candidate).stderr)
        self.assertEqual(config.read_bytes(), original)

        candidate.write_bytes(original.replace(b'usage = "reference"', b'usage = "required"'))
        detail.write_text(first + "\n" + last, encoding="utf-8")
        self.assertIn("変更されました", run("apply", pack, candidate).stderr)
        self.assertEqual(config.read_bytes(), original)
        detail.write_text(first + "\n" + "x" * 2700 + "\n" + last + "\n", encoding="utf-8")
        result = run("apply", pack, candidate)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(config.read_bytes(), candidate.read_bytes())
        self.assertEqual((pack / "config.before.toml").read_bytes(), original)
