import hashlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tests.support.build_receipt_fixtures import saved_build_receipt
from tests.support.fixtures import SavedRunFixture
from migration_harness.errors import CheckerRepairBlocked
from migration_harness.runs import stopped_repair, target_snapshots
from migration_harness.verification import build_cycle, build_receipt, repairs


def digest(value):
    return hashlib.sha256(value).hexdigest()


class TargetSnapshotCompatibilityTests(unittest.TestCase):
    def test_legacy_numbered_files_follow_the_manifest_not_reserialized_run_order(self):
        # ddb72be / 375687a persisted optional missing entries in a versionless manifest.
        with TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            identifier = "stopped-repair-" + "1" * 32
            snapshot = root / identifier
            snapshot.mkdir()
            targets = {"Z.cs": digest(b"unfinished Z"), "Mid.cs": None, "A.cs": digest(b"edited A")}
            legacy = {"snapshot": identifier, "base": {**targets, "Unchanged.cs": digest(b"unchanged")},
                      "targets": targets, "workspace_unchanged": True}
            manifest = snapshot / "draft.json"
            manifest.write_text(json.dumps(legacy), encoding="utf-8")
            (snapshot / "0.draft").write_bytes(b"unfinished Z")
            (snapshot / "2.draft").write_bytes(b"edited A")
            from_run = {**json.loads(json.dumps(legacy, sort_keys=True)), "junie_memos": []}
            before = manifest.read_bytes()
            files = target_snapshots.verified_snapshot_files(root, from_run)
            self.assertEqual(list(files), list(targets))
            self.assertEqual(files["Z.cs"].read_bytes(), b"unfinished Z")
            self.assertFalse(files["Mid.cs"].exists())
            self.assertEqual(files["A.cs"].read_bytes(), b"edited A")
            self.assertEqual(manifest.read_bytes(), before)

    def test_current_snapshot_refuses_unusable_metadata_and_corrupted_manifest_without_writing(self):
        with TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            source = root / "fixture.cs"
            source.write_bytes(b"class Fixture {}")
            saved = target_snapshots.save_target_snapshot(
                root, {"Fixture.cs": source}, {"Fixture.cs": None}, workspace_unchanged=True,
                stopped_snapshot="stopped-repair-" + "2" * 32,
                base_snapshot="stopped-repair-" + "3" * 32,
            )
            manifest = root / saved["snapshot"] / "draft.json"
            snapshot = root / saved["snapshot"] / "0.draft"
            persisted = manifest.read_bytes()
            self.assertEqual(target_snapshots.verified_snapshot_files(root, saved), {"Fixture.cs": snapshot})
            for invalid in (
                "unfinished", {**saved, "workspace_unchanged": 1}, {**saved, "snapshot": None},
                {**saved, "targets": {"Fixture.cs": False}}, {**saved, "base": {}},
                {**saved, "base_snapshot": saved["snapshot"]}, {**saved, "schema_version": 42},
            ):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    target_snapshots.verified_snapshot_files(root, invalid)
                self.assertEqual(manifest.read_bytes(), persisted)
            for broken in (
                b'{"snapshot":', b"[]", b"\xff\xff",
                json.dumps({**saved, "schema_version": 42}).encode(),
                json.dumps({**saved, "workspace_unchanged": 1}).encode(),
                json.dumps({**saved, "snapshot": "stopped-repair-" + "4" * 32}).encode(),
                persisted[:-1] + b', "workspace_unchanged": true}',
            ):
                with self.subTest(broken=broken):
                    manifest.write_bytes(broken)
                    with self.assertRaises(ValueError):
                        target_snapshots.verified_snapshot_files(root, saved)
                    self.assertEqual(manifest.read_bytes(), broken)
                    self.assertEqual(snapshot.read_bytes(), b"class Fixture {}")
            manifest.write_bytes(persisted)
            snapshot.unlink()
            with self.assertRaisesRegex(ValueError, "内容が一致しません"):
                target_snapshots.verified_snapshot_files(root, saved)
            self.assertEqual(manifest.read_bytes(), persisted)


class SavedProofRecoveryTests(SavedRunFixture):
    def test_legacy_agent_draft_is_preserved_and_malformed_draft_stops_without_sync(self):
        agent = self.run_path / "agent-workspace"
        agent.mkdir()
        draft = agent / self.target.name
        draft.write_bytes(b"class Unfinished {}")
        before = self.target.read_bytes()
        version = {self.target.name: digest(before)}
        pending = {self.target.name: digest(draft.read_bytes())}
        targets = (Path(self.target.name),)
        self.save_record({**self.record(), "repair_pending": {"draft": {"base": version, "targets": pending}}})
        result = repairs._sync_repair_workspace(self.run_path, self.worktree, agent, targets, targets,
                                                self.record()["repair_pending"])
        self.assertTrue(result[3])
        for invalid in ([], {"base": version}, {"base": version, "targets": pending, "snapshot": 1}):
            with self.subTest(invalid=invalid):
                record = {**self.record(), "repair_pending": {"draft": invalid}}
                self.save_record(record)
                persisted = self.record_path.read_bytes()
                with self.assertRaisesRegex(CheckerRepairBlocked, "下書き|snapshot"):
                    repairs._sync_repair_workspace(self.run_path, self.worktree, agent, targets, targets,
                                                   self.record()["repair_pending"])
                self.assertEqual(self.target.read_bytes(), before)
                self.assertEqual(draft.read_bytes(), b"class Unfinished {}")
                self.assertEqual(self.record_path.read_bytes(), persisted)

    def test_invalid_snapshot_cannot_make_a_restore_plan(self):
        before = self.target.read_bytes()
        record = {**self.record(), "repair_pending": {"draft": ["interrupted record"]}}
        self.save_record(record)
        persisted = self.record_path.read_bytes()
        with self.assertRaisesRegex(ValueError, "snapshot"):
            stopped_repair.plan_stopped_repair(self.config, self.run_id)
        self.assertEqual(self.target.read_bytes(), before)
        self.assertEqual(self.record_path.read_bytes(), persisted)

    def test_invalid_previous_receipt_does_not_stop_an_explicit_rebuild(self):
        unit = self.config["units"][0]
        result = {"worktree_path": str(self.worktree)}
        inputs = saved_build_receipt()["inputs"]
        (self.worktree / "bin").mkdir()
        with patch.object(build_receipt, "build_inputs", return_value=inputs), patch.object(
            build_receipt, "build_artifacts",
        ) as scan, patch.object(build_cycle, "_build_worktree", return_value={"status": "build_succeeded"}) as build:
            verified, _, certifiable = build_cycle.execute_verification_build(
                self.config, unit, result, self.record(), self.run_path, self.worktree, (Path(self.target.name),),
                expected_targets=None, previous_receipt={"artifacts": None}, configured_checker=None, command=None,
                scripts={}, input_files={}, request={"intent": "build"},
            )
        self.assertEqual(verified["status"], "build_succeeded")
        self.assertIsNone(certifiable)
        scan.assert_not_called()
        build.assert_called_once()
