from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from migration_harness.repair_context import diagnostic_php_ranges


class DiagnosticPhpRangeTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        self.capsule = root / "capsule"
        self.capsule.mkdir()
        self.worktree = root / "worktree"
        self.worktree.mkdir()
        self.php = self.capsule / "php-001.php"
        self.php.write_bytes(b"<?php\r\n$count = 1;\r\necho $count;\r\n")
        self.target = Path("Pages/A A.cshtml.cs")
        self.target_digest = sha256(b"fake target").hexdigest()
        self.manifest = {
            "parts": [{"index": 1, "sources": [
                {"path": "src/a.php", "start_line": 2, "end_line": 3, "encoding": "utf-8"},
            ]}],
            "php_sources": [{
                "path": "src/a.php", "snapshot": self.php.name, "encoding": "utf-8",
                "sha256": sha256(self.php.read_bytes()).hexdigest(),
            }],
        }
        mapping = {"path": self.target.as_posix(), "start_line": 9, "end_line": 13,
                   "sha256": self.target_digest, "candidate_snapshot": "fixture", "anchors": 2}
        self.record = {
            "completed_capsules": 1,
            "migration_mappings": {"1:0": mapping},
            "migration_reviews": {"1:0": {
                "state": "confirmed", "mapping": deepcopy(mapping),
                "target_hashes": {self.target.as_posix(): self.target_digest},
            }},
        }

    def hints(self, diagnostic=None, *, kind="build", digest=None):
        return diagnostic_php_ranges(
            self.capsule, self.manifest, self.record, self.worktree, (self.target,),
            {self.target.as_posix(): digest or self.target_digest},
            diagnostic if diagnostic is not None else f"{self.target.as_posix()}(10,5): CS001: Example", kind,
        )

    def test_build_and_checker_diagnostics_link_only_to_fixed_confirmed_php_range(self):
        for diagnostic, kind in (
            (None, "build"), (f"{self.worktree / self.target}(13): RZ123: Current Razor", "build"),
            (f"CHECK {self.target.as_posix()}:9:2 warning: Sample checker", "checker"),
        ):
            with self.subTest(diagnostic=diagnostic):
                hint, = self.hints(diagnostic, kind=kind)
                self.assertEqual((hint["id"], hint["path"], hint["start_line"], hint["end_line"]),
                                 ("php-0001", "src/a.php", 2, 3))
                self.assertEqual(hint["range_id"], "1:1")
                self.assertEqual(hint["sha256"], self.manifest["php_sources"][0]["sha256"])
                self.assertEqual(hint["target_sha256"], self.target_digest)
                self.assertEqual(hint["relationship"], "operator-confirmed-mapping-navigation-only")
                self.assertEqual(hint["file"], ".migration-materials/source-0001.md")

    def test_unknown_stale_unconfirmed_changed_mapping_and_outside_path_do_not_guess(self):
        before = deepcopy(self.record)
        for diagnostic in (
            f"{self.target.name}(10,5): CS001: Ambiguous basename",
            f"{self.worktree.parent / 'outside' / self.target}(10,5): CS001: Other codebase",
            f"{self.target.as_posix()}(99,5): CS001: Line outside confirmed range",
        ):
            with self.subTest(diagnostic=diagnostic):
                self.assertEqual(self.hints(diagnostic), [])
        self.assertEqual(self.hints(digest="changed target"), [])
        self.record["migration_reviews"]["1:0"]["state"] = "needs_review"
        self.assertEqual(self.hints(), [])
        self.record = deepcopy(before)
        self.record["migration_mappings"]["1:0"]["end_line"] += 1
        self.assertEqual(self.hints(), [])
        self.record = deepcopy(before)
        self.record["completed_capsules"] = 0
        self.assertEqual(self.hints(), [])

    def test_overlapping_candidate_and_corrupt_or_legacy_source_do_not_reassign_evidence(self):
        source = self.manifest["parts"][0]["sources"][0]
        self.manifest["parts"][0]["sources"].append(dict(source))
        self.record["migration_mappings"]["1:1"] = deepcopy(self.record["migration_mappings"]["1:0"])
        self.assertEqual(self.hints(), [])
        del self.record["migration_mappings"]["1:1"]
        source["character_slices"] = [{"line": 2}]
        self.assertEqual(self.hints(), [])
        del source["character_slices"]
        source["end_line"] = 4
        self.assertEqual(self.hints(), [])
        source["end_line"] = 3
        self.php.write_bytes(b"modified")
        self.assertEqual(self.hints(), [])
        del self.manifest["php_sources"]
        self.assertEqual(self.hints(), [])
