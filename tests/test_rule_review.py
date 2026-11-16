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

        def run(*arguments, script=tool):
            return subprocess.run(
                [sys.executable, str(script), *map(str, arguments)],
                cwd=self.root, capture_output=True, encoding="utf-8",
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
        sources = pack / "sources"
        sources.mkdir()
        (sources / detail.name).write_bytes(detail.read_bytes())
        rule_pack = tool.with_name("rule_pack.py")
        self.assertEqual(run("snapshot", pack.name, script=rule_pack).returncode, 0)
        plan = {"documents": [
            {"path": "Agent.md", "title": "Required", "parts": [{"source": detail.name, "start": 3, "end": 3}]},
            {"path": "details/views.md", "title": "Views", "description": "When migrating views",
             "parts": [{"source": detail.name, "start": 1, "end": 2}]},
        ]}
        (pack / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
        result = run("build", pack.name, script=rule_pack)
        self.assertEqual(result.returncode, 0, result.stderr)
        rules = (pack / "generated/rules.toml").read_bytes()
        candidate.write_bytes(original[:original.index(b"[[project.migration_rules]]")] + rules
                              + original[original.index(b"[[units]]"):])
        result = run("apply", pack.name, candidate)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(config.read_bytes(), candidate.read_bytes())
        self.assertEqual((pack / "config.before.toml").read_bytes(), original)
