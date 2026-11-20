import importlib.util
import tempfile
import unittest
from pathlib import Path


spec = importlib.util.spec_from_file_location(
    "holdout", Path(__file__).resolve().parents[1] / "evaluations/rule-optimization/holdout.py",
)
holdout = importlib.util.module_from_spec(spec)
spec.loader.exec_module(holdout)


class RuleHoldoutTests(unittest.TestCase):
    def test_positive_separates_conditional_rules_but_not_related_exception(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "positive"
            holdout.prepare(root, "conditional")
            self.assertEqual(holdout.score(root, "conditional")["outcome"], "unnecessary_hold")
            outputs = {
                "required.md": holdout.REQUIRED,
                "csv.md": "\n".join((holdout.DETAIL, holdout.EXCEPTION, holdout.PRIORITY)),
                "rehearsal.md": holdout.DECOY,
            }
            for name, body in outputs.items():
                (root / "app" / name).write_bytes((body + "\n").encode())
            config = root / "harness.local.toml"
            candidate = holdout.CONFIG.replace(
                'path = "export.md"', 'path = "required.md"\n[[project.migration_rules]]\n'
                'path = "csv.md"\nusage = "reference"\ndescription = "CSV export migration or repair"',
            ).replace('path = "export-rehearsal.md"',
                      'path = "rehearsal.md"\nusage = "reference"\ndescription = "Test report examples"')
            config.write_bytes(candidate.encode())
            score = holdout.score(root, "conditional")
            self.assertEqual(score["outcome"], "correct_apply", score)
            self.assertFalse(score["skill_read_verified"])
            (root / "app" / "csv.md").write_bytes((holdout.DETAIL + "\n").encode())
            score = holdout.score(root, "conditional")
            self.assertEqual(score["outcome"], "incorrect_apply", score)
            self.assertIn("split its exception", score["failures"][0])

    def test_conflict_requires_preserving_configuration_and_skill_load_is_separate(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "negative"
            holdout.prepare(root, "conflict")
            trace = Path(temp) / "trace.log"
            trace.write_text("agent_skill_read_doc harness-optimize-rules/SKILL.md\n"
                             "agent_skill_read_doc harness-optimize-rules/references/loading-policy.md\n",
                             encoding="utf-8")
            self.assertEqual(holdout.score(root, "conflict")["outcome"], "justified_hold")
            self.assertTrue(holdout.score(root, "conflict", trace)["skill_read_verified"])
            with (root / "harness.local.toml").open("a", encoding="utf-8") as stream:
                stream.write("\n# Edited despite contradiction.")
            self.assertEqual(holdout.score(root, "conflict", trace)["outcome"], "incorrect_apply")
