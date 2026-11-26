from hashlib import sha256
import json
from pathlib import Path
from unittest.mock import patch

from fixtures import HarnessCase, prepare_fixture
from migration_harness.config import load_config
from migration_harness.runs import run_records, static_migration_evidence as evidence
from migration_harness.runs.migration_ledger import migration_ledger


_SOURCES = {
    "rename.php": "<?php\nfunction old_name() { return LegacyBilling::NormalizeOrder('ORDER'); }\n",
    "split.php": "<?php\nclass LegacySplit {\n    public function combined() { return 'audit'; }\n}\n",
    "merge-a.php": "<?php\nfunction old_a() { return 'a'; }\n",
    "merge-b.php": "<?php\nfunction old_b() { return 'b'; }\n",
    "missing.php": "<?php\nfunction WriteAudit() { return 'CASE_REQUIRED'; }\n",
    "dynamic.php": "<?php\nfunction Dispatch($callback) { return $callback(); }\n",
    "excluded.php": "<?php\nconst RETIRE = 5;\n",
    "unindexed.php": "<div>old</div>\n<?php echo 'server side'; ?>\n",
    "razor.php": "<?php\nfunction Render() { return 'HTML'; }\n",
    "anchor.php": "<?php\nfunction RawCandidate() { return 'a'; }\n",
}
_TARGETS = {
    "Page.cs": """class Page
{
    string Renamed() => "ORDER";
    string Left() => "a";
    string Consolidated() => "b";
    string Dispatch() => "present";
    string WriteAudit() => "CASE_WRONG";
    // CASE_REQUIRED is not a string literal.
}
""",
    "View.cshtml": '@functions { string Right() => "HTML"; }\n',
    "View.cshtml.cs": "class ViewModel {}\n",
}


class StaticMigrationEvidenceTests(HarnessCase):
    def setUp(self):
        super().setUp()
        for filename, text in (_SOURCES | _TARGETS).items():
            (self.project / filename).write_text(text, encoding="utf-8", newline="\n")
        self.commit_project()
        settings = self.root / "test.toml"
        prefix = '''[project]
root = "app"
worktree_root = "trees"
run_root = "runs"
max_context_chars = 5000

[[units]]
id = "sample"
targets = ["Page.cs", "View.cshtml", "View.cshtml.cs"]
static_evidence = "evidence.json"
'''
        inputs = "".join(
            f'\n[[units.inputs]]\npath = "{path}"\nrole = "php"\nencoding = "utf-8"\n'
            f'start_line = 1\nend_line = {len(text.splitlines())}\n'
            for path, text in _SOURCES.items()
        )
        settings.write_text(prefix + inputs, encoding="utf-8")
        self.config = load_config(settings)
        run = prepare_fixture(self.config, "sample")
        self.run_id = run["run_id"]
        self.run_path = Path(run["run_path"])
        self.tree = Path(run["worktree_path"])
        self.record_path = self.run_path / "run.json"
        self.manifest = json.loads((self.run_path / "capsule" / "manifest.json").read_bytes())
        self.parts = {
            source["path"]: {
                "id": f"{part['index']}:{index}", "path": source["path"],
                "start_line": source["start_line"], "end_line": source["end_line"],
                "sha256": sha256((self.project / source["path"]).read_bytes()).hexdigest(),
            }
            for part in self.manifest["parts"] for index, source in enumerate(part["sources"], 1)
            if source["path"] in _SOURCES
        }
        self.update_record(status="junie_finished", completed_capsules=len(self.manifest["parts"]))
        self.plan()

    def update_record(self, **changes):
        run_records.write_run_record(self.record_path, json.loads(self.record_path.read_bytes()) | changes)

    def plan(self, *entries):
        (self.project / "evidence.json").write_text(json.dumps({"version": 1, "entries": entries}), encoding="utf-8")

    def scope(self, path, start, end=None):
        return {"path": path, "start_line": start, "end_line": end or start,
                "sha256": sha256((self.tree / path).read_bytes()).hexdigest()}

    def claim(self, name, kind, **details):
        return {"source": self.parts[name], "kind": kind, "reason": "fixture reviewed expectation", **details}

    def report(self):
        report = evidence.migration_evidence(self.config, self.run_id)
        return report, {row["path"]: row for row in report["ranges"]}

    def test_real_roslyn_distinguishes_omission_rename_split_merge_and_unresolved(self):
        shared = self.scope("Page.cs", 5)
        self.plan(
            self.claim("rename.php", "mapping", origin="human", targets=[self.scope("Page.cs", 3)]),
            self.claim("rename.php", "require", targets=[{"path": "Page.cs"}], fact={"kind": "method", "value": "Renamed"}),
            self.claim("split.php", "mapping", origin="human", targets=[self.scope("Page.cs", 4), self.scope("View.cshtml", 1)]),
            self.claim("merge-a.php", "mapping", origin="human", targets=[shared]),
            self.claim("merge-b.php", "mapping", origin="ai", targets=[shared]),
            self.claim("missing.php", "require", targets=[{"path": "Page.cs"}], fact={"kind": "literal", "value": "CASE_REQUIRED"}),
            self.claim("missing.php", "require", targets=[{"path": "Page.cs"}], fact={"kind": "method", "value": "AppendAuditLog"}),
            self.claim("dynamic.php", "require", targets=[{"path": "Page.cs"}], fact={"kind": "method", "value": "Dispatch"}),
            self.claim("excluded.php", "exclude"),
            self.claim("razor.php", "require", targets=[{"path": "View.cshtml"}], fact={"kind": "method", "value": "Right"}),
        )
        key, _, index = self.parts["anchor.php"]["id"].partition(":")
        self.update_record(migration_mappings={f"{key}:{int(index) - 1}": self.scope("Page.cs", 4) | {"candidate_snapshot": "candidate"}})
        before = self.record_path.read_bytes()
        complete = migration_ledger(self.config, self.run_id)["completion"]
        (self.project / "rename.php").write_text("a user has changed the live input", encoding="utf-8")
        report, rows = self.report()
        self.assertEqual(report["status"], "ready")
        self.assertEqual(set(rows), set(_SOURCES))
        self.assertEqual(report["summary"], {"verified": 2, "mismatch": 1, "review_candidate": 4, "unverified": 2, "excluded": 1})
        for name, status in {
            "rename.php": "verified", "split.php": "review_candidate", "merge-a.php": "review_candidate",
            "merge-b.php": "review_candidate", "missing.php": "mismatch", "dynamic.php": "unverified",
            "excluded.php": "excluded", "unindexed.php": "unverified", "razor.php": "verified",
            "anchor.php": "review_candidate",
        }.items():
            self.assertEqual(rows[name]["status"], status, name)
            self.assertEqual(rows[name]["source_sha256"], self.parts[name]["sha256"])
        self.assertEqual({target["origin"] for target in rows["rename.php"]["targets"]}, {"human", "mechanical"})
        self.assertEqual({target["path"] for target in rows["split.php"]["targets"]}, {"Page.cs", "View.cshtml"})
        self.assertIn("class", {structure["kind"] for structure in rows["split.php"]["structure"]})
        self.assertEqual([target["origin"] for target in rows["merge-b.php"]["targets"]], ["ai"])
        self.assertEqual(rows["merge-a.php"]["targets"][0] | {"origin": "ai"}, rows["merge-b.php"]["targets"][0])
        self.assertEqual([target["origin"] for target in rows["anchor.php"]["targets"]], ["anchor"])
        self.assertEqual(sum("指定範囲" in item for item in rows["missing.php"]["reasons"]), 2)
        self.assertTrue(any(item["kind"] == "dynamic" for item in rows["dynamic.php"]["structure"]))
        self.assertEqual(self.record_path.read_bytes(), before)
        self.assertEqual(migration_ledger(self.config, self.run_id)["completion"], complete)

    def test_stale_spans_incomplete_syntax_and_required_razor_absence_remain_unverified(self):
        declaration = self.claim("rename.php", "require", targets=[{"path": "Page.cs"}],
                                 fact={"kind": "method", "value": "Renamed"})
        self.plan(
            declaration,
            self.claim("split.php", "mapping", origin="human", targets=[self.scope("Page.cs", 4)]),
            self.claim("razor.php", "require", targets=[{"path": "View.cshtml"}], fact={"kind": "method", "value": "Missing"}),
        )
        (self.tree / "Page.cs").write_text("class Broken { void Renamed(", encoding="utf-8")
        _, rows = self.report()
        self.assertEqual([rows[name]["status"] for name in ("rename.php", "split.php", "razor.php")], ["unverified"] * 3)
        self.assertTrue(rows["razor.php"]["targets"])
        self.assertFalse(rows["split.php"]["targets"])
        self.plan(declaration | {"source": declaration["source"] | {"start_line": 2}})
        _, rows = self.report()
        self.assertEqual(rows["rename.php"]["status"], "unverified")
        self.assertIn("原文版・範囲", rows["rename.php"]["reasons"][0])

    def test_existing_approved_mapping_coordinates_do_not_survive_a_target_byte_edit(self):
        same_scope = self.scope("Page.cs", 3)
        self.plan(self.claim("rename.php", "mapping", origin="human", targets=[same_scope]))
        key, _, index = self.parts["anchor.php"]["id"].partition(":")
        self.update_record(migration_mappings={f"{key}:{int(index) - 1}": same_scope})
        _, rows = self.report()
        self.assertEqual([rows[path]["status"] for path in ("rename.php", "anchor.php")], ["review_candidate"] * 2)
        target = self.tree / "Page.cs"
        target.write_bytes(target.read_bytes().replace(b'"ORDER"', b'"EDITED"'))
        _, rows = self.report()
        for path in ("rename.php", "anchor.php"):
            self.assertEqual(rows[path]["status"], "unverified")
            self.assertFalse(rows[path]["targets"])
        saved = next(row for part in migration_ledger(self.config, self.run_id)["capsules"] for row in part["sources"]
                     if row["path"] == "anchor.php")
        self.assertFalse(saved["mapping"]["valid"])
        self.assertEqual(saved["mapping"]["start_line"], same_scope["start_line"])

    def test_unconfigured_targets_invalid_plans_or_active_runs_do_not_scan_targets(self):
        self.plan(self.claim("missing.php", "require", targets=[{"path": "../forbidden.cs"}],
                             fact={"kind": "literal", "value": "CASE_REQUIRED"}))
        with patch.object(evidence.roslyn_syntax, "collect_static_evidence", side_effect=AssertionError("must not run SDK")):
            report, rows = self.report()
            self.assertEqual(report["status"], "unverified")
            self.assertEqual(rows["missing.php"]["status"], "unverified")
            self.plan()
            self.update_record(status="building")
            _, rows = self.report()
            self.assertEqual(set(row["status"] for row in rows.values()), {"unverified"})
            self.update_record(status="prepared", completed_capsules=0)
            _, rows = self.report()
            self.assertEqual(set(row["status"] for row in rows.values()), {"unverified"})

    def test_legacy_or_corrupt_pins_still_list_the_entire_source_assignment(self):
        path = self.run_path / "capsule" / "manifest.json"
        pins = self.manifest.pop("php_sources")
        path.write_text(json.dumps(self.manifest), encoding="utf-8")
        with patch.object(evidence.roslyn_syntax, "collect_static_evidence", side_effect=AssertionError("no source pins")):
            report, rows = self.report()
        self.assertEqual(set(rows), set(_SOURCES))
        self.assertEqual(report["summary"]["unverified"], len(_SOURCES))
        self.manifest["php_sources"] = pins
        path.write_text(json.dumps(self.manifest), encoding="utf-8")
        pin = next(row for row in pins if row["path"] == "rename.php")
        (self.run_path / "capsule" / pin["snapshot"]).write_bytes(b"<?php\nfunction fake() {}\n")
        _, rows = self.report()
        self.assertIsNone(rows["rename.php"]["source_sha256"])
        self.assertEqual(rows["rename.php"]["status"], "unverified")
