from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from migration_harness.context.capsules import prepare_context
from migration_harness.verification.grounded_evidence import bounded_evidence_snapshot, grounded_diagnostic, grounded_evidence


class GroundedEvidenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.worktree = Path(temporary.name).resolve()
        self.run = self.worktree / "run"
        self.php = self.worktree / "source.php"
        self.php.write_bytes(b"<?php\r\nfunction total() { return 42; }\r\n// outside assignment\r\n")
        prepare_context({"project_root": str(self.worktree), "max_context_chars": 1000}, {
            "id": "sample", "target": "Sample.cs", "inputs": [{
                "role": "php", "path": "source.php", "start_line": 1, "end_line": 2,
                "encoding": "utf-8", "shared": False,
            }],
        }, self.run)
        self.targets = (Path("Sample.cs"),)
        self.target = self.worktree / self.targets[0]
        self.target.write_bytes(b"\xef\xbb\xbfclass Sample { int total() => 0; }\r\n")
        self.hashes = {"Sample.cs": sha256(self.target.read_bytes()).hexdigest()}
        self.row = {
            "source_id": "php-0001", "start_line": 2, "end_line": 2,
            "excerpt": "function total() { return 42; }\r\n",
            "target": "Sample.cs", "target_start_line": 1, "target_end_line": 1,
            "target_excerpt": "class Sample { int total() => 0; }\r\n",
        }

    def verify(self, *rows, **kwargs):
        return grounded_evidence(self.run, self.worktree, self.targets, list(rows),
                                 expected_target_hashes=self.hashes, **kwargs)

    def diagnose(self, *lines):
        return grounded_diagnostic(self.run, self.worktree, self.targets,
                                   expected_target_hashes=self.hashes, phase="after_build", evidence_lines=lines)

    def test_exact_pinned_lines_version_and_packet(self):
        snapshot = bounded_evidence_snapshot(self.run, self.worktree, self.targets, expected_target_hashes=self.hashes)
        verified, = self.verify(self.row, snapshot=snapshot)
        self.assertEqual(self.hashes["Sample.cs"], verified["target_sha256"])
        self.assertEqual(sha256(self.php.read_bytes()).hexdigest(), verified["source_sha256"])
        self.php.write_bytes(b"modified live PHP")
        self.assertEqual([verified], self.verify(self.row, snapshot=snapshot))
        self.assertNotIn("outside assignment", json.dumps(snapshot))
        with self.assertRaisesRegex(ValueError, "not supplied"):
            self.verify(self.row, snapshot={**snapshot, "sources": []})

    def test_rejects_missing_changed_target_and_outside_assigned_range(self):
        with self.assertRaisesRegex(ValueError, "range"):
            self.verify({**self.row, "start_line": 3, "end_line": 3, "excerpt": "// outside assignment\r\n"})
        with self.assertRaisesRegex(ValueError, "citation"):
            self.verify({**self.row, "target": "../Other.cs"})
        with self.assertRaisesRegex(ValueError, "fixed text"):
            self.verify({**self.row, "excerpt": self.row["excerpt"].replace("\r", "")})
        self.target.write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "saved version"):
            self.verify(self.row)

    def test_uses_text_only_validates_strict_json_and_has_no_environment_side_effects(self):
        name = "migration_harness.verification.checker_advisory.run_text_advisory"
        with patch(name, return_value=json.dumps({"citations": [self.row]})) as acp:
            result = self.diagnose("Total mismatch in Sample.cs")
        self.assertEqual("grounded", result["status"])
        self.assertIn("Do not access files", acp.call_args.args[0])
        self.assertIn("total() => 0", acp.call_args.args[0])
        with patch(name, return_value='{"citations":[],"citations":[]}') as acp:
            self.assertEqual("unavailable", self.diagnose("Total mismatch")["status"])
            self.assertEqual("unclear", self.diagnose("Socket connection failed")["status"])
        self.assertEqual(1, acp.call_count)

    def test_rechecks_target_after_the_text_only_session(self):
        def replace_before_reply(_):
            self.target.write_text("class Newer {}", encoding="utf-8")
            return json.dumps({"citations": [self.row]})

        with patch("migration_harness.verification.checker_advisory.run_text_advisory", side_effect=replace_before_reply):
            self.assertEqual("unavailable", self.diagnose("Total mismatch in Sample.cs")["status"])

    def test_sidecar_with_changed_pinned_source_is_never_sent(self):
        (self.run / "capsule/php-sources/0001.php").write_bytes(b"changed source")
        with patch("migration_harness.verification.checker_advisory.run_text_advisory") as acp:
            self.assertEqual("unavailable", self.diagnose("Total mismatch")["status"])
        acp.assert_not_called()

    def test_rejects_same_unnamed_target_excerpt_in_multiple_approved_targets(self):
        mirror = self.worktree / "Other.cs"
        mirror.write_bytes(self.target.read_bytes())
        self.targets += (Path(mirror.name),)
        self.hashes[mirror.name] = self.hashes[self.target.name]
        with patch("migration_harness.verification.checker_advisory.run_text_advisory",
                   return_value=json.dumps({"citations": [self.row]})):
            self.assertEqual("unclear", self.diagnose("Total mismatch")["status"])
            self.assertEqual("grounded", self.diagnose("Total mismatch in Sample.cs")["status"])
