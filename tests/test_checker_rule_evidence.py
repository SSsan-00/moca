import hashlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from migration_harness.checker_rule_evidence import MAX_RECEIPT_BYTES, checker_rule_context, parse_rule_receipt


class CheckerRuleEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.rules = {
            "rule-1.txt": {"sha256": "a" * 64, "saved_file_lines": 5},
            "rule-2.txt": {"sha256": "b" * 64, "saved_file_lines": 3},
        }
        self.result = {"rule": "rule-1.txt", "sha256": "a" * 64, "start_line": 1,
                       "end_line": 5, "status": "passed", "evidence": "shipping-check case 1"}

    def receipt(self, results, **overrides):
        return json.dumps({"schema": 1, "request_id": "current", "results": results, **overrides}).encode()

    def test_complete_explicit_evidence_confirms_only_the_tested_rule(self):
        expected = {"kind": "passed", "start_line": 1, "end_line": 5, "reference": "shipping-check case 1"}
        self.assertEqual(parse_rule_receipt(self.receipt([self.result]), "current", self.rules),
                         {"rule-1.txt": expected})

    def test_partial_not_checked_and_absent_coverage_are_not_confirmation(self):
        for results in ([], [{**self.result, "end_line": 4}], [{**self.result, "status": "not_checked"}]):
            with self.subTest(results=results):
                self.assertEqual(parse_rule_receipt(self.receipt(results), "current", self.rules), {})

    def test_stale_invalid_and_ambiguous_receipts_are_rejected(self):
        for data in (
            self.receipt([self.result], request_id="previous"),
            self.receipt([self.result], schema=True),
            self.receipt([{**self.result, "sha256": "wrong"}]),
            self.receipt([{**self.result, "rule": "unknown"}]),
            self.receipt([{**self.result, "start_line": True}]),
            self.receipt([{**self.result, "evidence": " "}]),
            self.receipt([self.result, self.result]),
            b'{"schema":1,"schema":1,"request_id":"current","results":[]}',
            b"x" * (MAX_RECEIPT_BYTES + 1),
        ):
            with self.subTest(size=len(data)), self.assertRaises(ValueError):
                parse_rule_receipt(data, "current", self.rules)

    def test_context_changes_when_a_target_or_reference_revision_changes(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            capsule, worktree = root / "capsule", root / "worktree"
            (capsule / "materials").mkdir(parents=True)
            worktree.mkdir()
            target, metadata = worktree / "Page.cs", capsule / "manifest.json"
            reference = capsule / "refs"
            reference.mkdir()
            metadata.write_bytes(b"fixed sources")
            (reference / "manifest.json").write_bytes(b"reference one")
            target.write_bytes(b"class Page {}")
            rule = b"Required rule\n"
            rule_path = capsule / "materials" / "rule-1.txt"
            rule_path.write_bytes(rule)
            manifest = {"rule_files": [{"file": rule_path.name, "usage": "required",
                                        "sha256": hashlib.sha256(rule).hexdigest()}]}
            with (
                patch("migration_harness.checker_rule_evidence.load_capsule_manifest", return_value=(capsule, manifest)),
                patch("migration_harness.checker_rule_evidence.target_context_snapshot", return_value=reference),
            ):
                before = checker_rule_context(root, worktree, {"target": target.name})
                target.write_bytes(b"class Updated {}")
                after = checker_rule_context(root, worktree, {"target": target.name})
                self.assertNotEqual(before["targets"], after["targets"])
                self.assertEqual(before["reference_version"], after["reference_version"])
                (reference / "manifest.json").write_bytes(b"reference two")
                current = checker_rule_context(root, worktree, {"target": target.name})
                self.assertNotEqual(after["reference_version"], current["reference_version"])
                rule_path.write_bytes(b"changed rule without a new snapshot")
                with self.assertRaises(ValueError):
                    checker_rule_context(root, worktree, {"target": target.name})
