import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from unittest import TestCase
from unittest.mock import patch
from uuid import UUID

from migration_harness.runs import run_manager
from migration_harness.web.dashboard import Dashboard
from migration_harness.runs.run_manager import RunManager


class WorkspaceListCacheTests(TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config = {
            "project_root": str(self.root / "project"),
            "run_root": str(self.root / "runs"),
            "worktree_root": str(self.root / "trees"),
            "units": [{"id": "one", "source": "source.php", "target": "One.cs"}],
        }
        self.manager = RunManager(self.config, Dashboard())
        self.addCleanup(self.manager.close)
        self.run_id = str(UUID(int=1))
        self.path = self.root / "runs" / self.run_id / "run.json"
        self.path.parent.mkdir()
        self.record = {
            "run_id": self.run_id, "project_root": self.config["project_root"],
            "unit_id": "one", "target": "One.cs", "status": "prepared", "created_at": "01",
            "worktree_path": str(self.root / "trees" / self.run_id),
            "detail_applicability": {"doc": {"applies": True, "reason": "reason"}},
        }
        self.replace()

    def replace(self, **changes):
        self.record.update(changes)
        temporary = self.path.with_suffix(".new")
        temporary.write_text(json.dumps(self.record), encoding="utf-8")
        temporary.replace(self.path)

    def test_single_read_on_open_reuse_update_hide_usage_and_removal(self):
        with patch.object(run_manager, "load_dashboard_record", wraps=run_manager.load_dashboard_record) as load:
            self.assertEqual(self.manager.workspace()["runs"][0]["status"], "prepared")
            self.assertEqual(load.call_count, 1)
            self.assertEqual(len(self.manager.workspace()["runs"]), 1)
            self.assertEqual(load.call_count, 1)
            self.replace(status="build_succeeded", hidden_from_dashboard=True, token_usage=[{"phase": "migration"}])
            self.assertEqual(self.manager.workspace()["runs"], [])
            self.assertEqual(self.manager.workspace(include_usage=True)["runs"][0]["token_usage"], [{"phase": "migration"}])
            self.assertEqual(load.call_count, 2)
            self.replace(hidden_from_dashboard=False)
            self.assertEqual(self.manager.workspace()["runs"][0]["status"], "build_succeeded")
            self.assertEqual(load.call_count, 3)
            self.path.unlink()
            self.assertEqual(self.manager.workspace()["runs"], [])

    def test_scan_does_not_hold_condition_or_overwrite_newer_view(self):
        view = self.manager.get(self.run_id)
        loaded, release, updated = Event(), Event(), Event()
        original = run_manager.load_dashboard_record

        def slow_load(config, run_id):
            record = original(config, run_id)
            loaded.set()
            if not release.wait(5):
                raise RuntimeError("test did not release record scan")
            return record

        def publish_newer():
            with self.manager.condition:
                self.replace(status="building")
                view.publish({**view.state, "status": "building"})
                updated.set()

        result = []
        with patch.object(run_manager, "load_dashboard_record", side_effect=slow_load):
            scan = Thread(target=lambda: result.append(self.manager.workspace()))
            scan.start()
            edit = Thread(target=publish_newer)
            try:
                self.assertTrue(loaded.wait(5))
                edit.start()
                self.assertTrue(updated.wait(2), "record scan blocked an unrelated view update")
            finally:
                release.set()
                scan.join(6)
                if edit.ident is not None:
                    edit.join(6)
        self.assertEqual(result[0]["runs"][0]["status"], "building")

    def test_same_root_configuration_revalidates_targets(self):
        self.assertEqual(len(self.manager.workspace()["runs"]), 1)
        self.config["units"] = [{"id": "one", "source": "source.php", "target": "Other.cs"}]
        self.assertEqual(self.manager.workspace()["runs"], [])

    def test_resume_actions_follow_saved_gates_without_progress_change(self):
        self.config["checker_command"] = None
        self.replace(status="build_succeeded", completed_capsules=1, context_capsules=1, stage_timings={}, active_stage=None)
        initial = self.manager.workspace()["runs"][0]
        self.assertTrue(initial["resumable"])
        self.assertTrue(initial["build_resumable"])

        self.replace(checker_required=True, target_apply_pending={"state": "pending"})
        blocked = self.manager.workspace()["runs"][0]
        self.assertFalse(blocked["resumable"])
        self.assertFalse(blocked["build_resumable"])

        self.config["checker_command"] = ["checker"]
        configured = self.manager.workspace()["runs"][0]
        self.assertTrue(configured["resumable"])
        self.assertFalse(configured["build_resumable"])

        self.replace(target_apply_pending=None)
        restored = self.manager.workspace()["runs"][0]
        self.assertTrue(restored["resumable"])
        self.assertTrue(restored["build_resumable"])
        self.assertEqual(self.manager.workspace()["runs"][0]["revision"], restored["revision"])
