import hashlib
import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tests.support.fixtures import HarnessCase, prepare_fixture
from migration_harness import workflow
from migration_harness import execution
from migration_harness.verification import pipeline as verification_pipeline
from migration_harness.rules.rule_review import review_rules
from migration_harness.verification.checker_rule_evidence import MAX_RECEIPT_BYTES, checker_rule_context, parse_rule_receipt
from migration_harness.verification import verification_files


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
                patch("migration_harness.verification.checker_rule_evidence.load_capsule_manifest", return_value=(capsule, manifest)),
                patch("migration_harness.verification.checker_rule_evidence.target_context_snapshot", return_value=reference),
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


class CheckerEvidenceVerificationTests(HarnessCase):
    def setUp(self):
        super().setUp()
        for name, content in {
            "source.php": "<?php echo 'fixture';\n", "Page.cs": "class Page {}",
            "rules.md": "Match the fixture.\n", "check.py": "# checker fixture\n",
            "expected.json": '{"value":1}\n', "hook.py": "# hook fixture\n",
        }.items():
            (self.project / name).write_text(content, encoding="utf-8")
        self.commit_project()
        self.settings = self.config()
        self.settings.update(checker_rule_evidence=True, checker_command=["python", "check.py"],
                             checker_verification_files=["expected.json"],
                             scripts={"after_checker": [["python", "hook.py"]]})
        self.unit = self.settings["units"][0]
        self.unit["inputs"].append({
            "path": "rules.md", "role": "migration_rules", "usage": "required",
            "encoding": "utf-8", "start_line": 1, "end_line": None, "shared": True,
        })
        self.run = prepare_fixture(self.settings, "sample")
        self.worktree = Path(self.run["worktree_path"])
        self.record_path = Path(self.run["run_path"]) / "run.json"

    def verify_checker(self, *, change_after_checker=False):
        record = self.saved()
        record.update(status="junie_finished", completed_capsules=record["context_capsules"])

        def process(command, **kwargs):
            if command[0] == "dotnet":
                return subprocess.CompletedProcess(command, 0)
            if command[1] == "check.py":
                inputs = kwargs["env"]
                request = json.loads(Path(inputs["MIGRATION_RULE_CHECK_REQUEST"]).read_bytes())
                name, rule = next(iter(request["rules"].items()))
                receipt = {"schema": 1, "request_id": request["request_id"], "results": [{
                    "rule": name, "sha256": rule["sha256"], "start_line": 1,
                    "end_line": rule["saved_file_lines"], "status": "passed", "evidence": "checked",
                }]}
                Path(inputs["MIGRATION_RULE_CHECK_RECEIPT"]).write_text(json.dumps(receipt), encoding="utf-8")
            else:
                self.assertEqual(command, ["python", "hook.py"])
                if change_after_checker:
                    (self.worktree / "expected.json").write_text('{"value":2}', encoding="utf-8")
            return subprocess.CompletedProcess(command, 0)

        with patch.object(execution, "run_process", side_effect=process):
            result = verification_pipeline.run_verification(self.settings, self.unit, self.run, record, process_queued_edits=self.fail)
        self.assertEqual(result["status"], "checker_succeeded")

    def saved(self):
        return json.loads(self.record_path.read_bytes())

    def report(self):
        return review_rules(self.settings, self.run["run_id"])

    def assert_checker_stale(self):
        row, = self.report()["rules"]
        self.assertEqual(row["verification_state"], "human_review_needed")
        self.assertIsNone(row["independent_verification"])
        self.assertEqual(self.saved()["checker_rule_evidence"]["results"][row["file"]]["kind"], "passed")

    def test_checker_and_hook_settings_and_input_files_invalidate_only_automatic_evidence(self):
        self.verify_checker()
        initial, = self.report()["rules"]
        self.assertEqual(initial["verification_state"], "passed")
        self.assertEqual(initial["independent_verification"]["source"], "checker")
        for name in ("check.py", "expected.json", "hook.py"):
            with self.subTest(name=name):
                path = self.worktree / name
                original = path.read_bytes()
                path.write_bytes(original + b"changed")
                self.assert_checker_stale()
                path.write_bytes(original)
                self.assertEqual(self.report()["rules"][0]["verification_state"], "passed")
        for key, value in (("checker_command", ["python", "check.py", "--other"]),
                           ("scripts", {"before_checker": [["python", "hook.py"]]})):
            with self.subTest(key=key):
                original = self.settings[key]
                self.settings[key] = value
                self.assert_checker_stale()
                self.settings[key] = original
        expected = self.worktree / "expected.json"
        expected.unlink()
        self.assert_checker_stale()
        expected.write_bytes(b"new expectation")
        self.assert_checker_stale()
        previous = self.saved()["checker_rule_evidence"]["request_id"]
        self.verify_checker()
        self.assertNotEqual(self.saved()["checker_rule_evidence"]["request_id"], previous)
        self.assertEqual(self.report()["rules"][0]["verification_state"], "passed")
        hash_file = verification_files.sha256_file

        def unreadable(path, **kwargs):
            if path == expected:
                raise PermissionError("fixture")
            return hash_file(path, **kwargs)

        with patch.object(verification_files, "sha256_file", side_effect=unreadable):
            self.assert_checker_stale()
        manual = review_rules(self.settings, self.run["run_id"], rule=initial["file"], kind="passed",
                              version=self.report()["version"], start=1, end=1, reason="manual reason",
                              reference="manual fixture evidence")
        expected.write_bytes(b"changed after manual review")
        self.assertEqual(self.report()["rules"][0]["independent_verification"],
                         manual["rules"][0]["independent_verification"])
        self.assertEqual(self.report()["rules"][0]["verification_state"], "passed")

    def test_old_and_changed_during_verification_evidence_is_not_current(self):
        self.verify_checker(change_after_checker=True)
        self.assertTrue(self.saved()["verification"]["inputs_changed_during_verification"])
        self.assert_checker_stale()
        self.verify_checker()
        saved = self.saved()
        saved["checker_rule_evidence"].pop("verification_context", None)
        self.record_path.write_text(json.dumps(saved), encoding="utf-8")
        self.assert_checker_stale()
