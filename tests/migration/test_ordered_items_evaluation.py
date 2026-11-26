import html
import json
from pathlib import Path
import subprocess
import sys
import unittest


APP = Path(__file__).resolve().parents[2] / "evaluations" / "ordered-items" / "app"


class OrderedItemsEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.expected = json.loads((APP / "expected.json").read_text(encoding="utf-8"))

    def score(self, values):
        items = "".join(f"<li>{html.escape(value)}</li>" for value in values)
        return subprocess.run(
            [sys.executable, str(APP / "check_ordered_items.py"), "--html-stdin"],
            input=f"<!doctype html><ul>{items}</ul>".encode("utf-8"),
            capture_output=True, timeout=10,
        )

    def test_correct_output_passes(self):
        result = self.score(self.expected)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout + result.stderr, b"")

    def test_incorrect_sequences_fail(self):
        cases = {
            "changed value": ["Different label", *self.expected[1:]],
            "swapped order": [self.expected[1], self.expected[0], *self.expected[2:]],
            "missing duplicate": self.expected[1:],
            "double escaping": [value.replace("&", "&amp;") for value in self.expected],
        }
        self.assertIn(self.expected[0], cases["missing duplicate"])
        for case, values in cases.items():
            with self.subTest(case=case):
                result = self.score(values)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(b"FIXTURE_SEQUENCE Pages/OrderedItems.cshtml:1 error:", result.stdout)
