import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


EVALUATION = Path(__file__).resolve().parents[1] / "evaluations" / "business-php"
FIXTURE = EVALUATION / "control-flow-fixtures"
sys.path.insert(0, str(EVALUATION))
try:
    from control_flow_model import INT_MAX, INT_MIN, compare_models
finally:
    sys.path.pop(0)


def return_value(value, line=2):
    return {"op": "return", "line": line, "value": value}


class ModeledInputTests(unittest.TestCase):
    @staticmethod
    def compare(left, right, inputs, return_type="bool"):
        return compare_models(
            {"params": inputs, "return_type": return_type, "body": left},
            {"params": inputs, "return_type": return_type, "body": right},
            inputs, {"start_line": 1, "end_line": 20}, {"start_line": 1, "end_line": 20},
        )

    def test_distinguishes_each_order_and_equality_inside_open_intervals(self):
        left = [return_value(["<", ["arg", 0], ["arg", 1]])]
        right = [return_value(["!=", ["arg", 0], ["arg", 1]])]
        result = self.compare(left, right, ["int", "int"])
        self.assertEqual(result["status"], "mismatch")
        self.assertGreater(*result["witness"]["inputs"])
        self.assertEqual(self.compare(left, [return_value([">", ["arg", 1], ["arg", 0]])],
                                      ["int", "int"])["status"], "verified")

    def test_int32_boundary_at_each_endpoint(self):
        for bound, comparison, inverted in ((INT_MIN, ">", "<="), (INT_MAX, "<", ">=")):
            with self.subTest(bound=bound):
                left = [return_value([comparison, ["arg", 0], ["int", bound]])]
                right = [return_value(["not", [inverted, ["arg", 0], ["int", bound]]])]
                self.assertEqual(self.compare(left, right, ["int"])["status"], "verified")
                result = self.compare(left, [return_value(["bool", True])], ["int"])
                self.assertEqual(result["witness"]["inputs"], [bound])

    def test_unmodeled_return_mixed_types_and_world_limit_never_pass(self):
        self.assertEqual(self.compare([], [return_value(["bool", True])], [])["reason"], "non_total_return")
        with self.assertRaises(ValueError):
            self.compare([return_value(["==", ["int", 0], ["bool", False]])], [], [])
        many_constants = ["bool", False]
        for value in range(3):
            many_constants = ["||", many_constants, ["==", ["arg", 0], ["int", value]]]
        larger = [return_value(many_constants)]
        self.assertEqual(self.compare(larger, larger, ["int"] * 6)["reason"], "input_partition_limit")


class MigrationControlFlowTests(unittest.TestCase):
    def run_checker(self, plan):
        cli = EVALUATION / "compare_control_flow.py"
        result = subprocess.run(
            [sys.executable, str(cli), "--plan", str(plan), "--source-root", str(FIXTURE),
             "--target-root", str(FIXTURE)], check=False, capture_output=True, timeout=100,
        )
        self.assertTrue(result.stdout, result.stderr.decode(errors="replace"))
        return result.returncode, json.loads(result.stdout)

    def test_pinned_cli_projects_match_change_and_uncertainty_with_real_roslyn(self):
        code, result = self.run_checker(FIXTURE / "pairs.json")
        self.assertEqual(code, 1, result)
        self.assertEqual(result["status"], "mismatch")
        self.assertEqual(result["summary"], {
            "verified": 3, "mismatch": 4, "review_candidate": 1, "unverified": 7, "excluded": 1,
        })
        rows = {line["id"]: line for line in result["pairs"]}
        verified = {"inverted_boolean", "early_return", "equivalent_csharp_switch"}
        self.assertEqual({key for key, row in rows.items() if row["status"] == "verified"}, verified)
        for name, inputs, original, migrated in (
            ("boundary", [3], 1, 0), ("omitted_branch", [5], 2, 3), ("branch_order", [10], 1, 2),
        ):
            row = rows[name]
            self.assertEqual(row["witness"]["inputs"], inputs)
            self.assertEqual((row["witness"]["source"]["value"], row["witness"]["target"]["value"]),
                             (original, migrated))
            self.assertRegex(row["source"]["sha256"], r"\A[0-9a-f]{64}\Z")
            self.assertRegex(row["target"]["sha256"], r"\A[0-9a-f]{64}\Z")
            for side in ("source", "target"):
                evidence = row["witness"][side]
                self.assertTrue(evidence["branches"])
                self.assertTrue(row[side]["start_line"] <= evidence["line"] <= row[side]["end_line"])
        self.assertFalse(rows["relative_order"]["witness"]["source"]["value"])
        self.assertTrue(rows["relative_order"]["witness"]["target"]["value"])
        for name in ("php_truthiness", "php_loose_mixed", "php_short_circuit_effect", "php_loop"):
            self.assertEqual(rows[name]["reason"], "php_unsupported")
        self.assertEqual(rows["inverted_boolean"]["source"]["path"], "decisions.txt")
        self.assertEqual(rows["malformed_source"]["reason"], "php_unsupported")
        self.assertEqual(rows["malformed_target"]["reason"], "csharp_syntax_error")
        self.assertEqual(rows["malformed_target"]["diagnostic_line"], 1)
        self.assertTrue(rows["csharp_short_circuit_effect"]["reason"].startswith("csharp_"))
        self.assertEqual(rows["candidate_with_same_behavior"]["status"], "review_candidate")
        self.assertEqual(rows["intentionally_excluded"]["status"], "excluded")

    def test_stale_pin_invalid_path_or_ai_candidate_cannot_pass(self):
        plan = json.loads((FIXTURE / "pairs.json").read_bytes())
        rows = {row["id"]: row for row in plan["pairs"]}
        stale, candidate, excluded = (rows[name] for name in
                                      ("inverted_boolean", "candidate_with_same_behavior", "intentionally_excluded"))
        stale["target"]["sha256"] = "f" * 64
        with TemporaryDirectory() as folder:
            path = Path(folder) / "pairs.json"
            path.write_text(json.dumps({"version": 1, "pairs": [stale, candidate, excluded]}), encoding="utf-8")
            code, result = self.run_checker(path)
            self.assertEqual(code, 2)
            self.assertEqual([row["status"] for row in result["pairs"]],
                             ["unverified", "review_candidate", "excluded"])
            self.assertEqual(result["pairs"][0]["reason"], "pinned_version_changed")
            stale["target"]["path"] = "../Selection.cs"
            path.write_text(json.dumps({"version": 1, "pairs": [stale]}), encoding="utf-8")
            code, result = self.run_checker(path)
            self.assertEqual(code, 2)
            self.assertEqual(result["pairs"], [])
