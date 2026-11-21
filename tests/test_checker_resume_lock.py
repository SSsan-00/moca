from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
from threading import Event
from unittest.mock import patch

from fixtures import SavedRunFixture
from migration_harness import workflow
from migration_harness.editor_queue import editor_queue
from migration_harness.worktree_targets import save_target_edit, worktree_source_lock


class CheckerResumeLockTests(SavedRunFixture):
    def setUp(self):
        super().setUp()
        record = self.record()
        self.receipt = {"inputs": {"source": "fixture"}, "artifacts": {"bin": "fixture"}}
        record.update(checker_build=self.receipt, verification={})
        self.save_record(record)

    def test_run_b_save_completes_during_run_a_receipt_scan(self):
        scan_started, release_scan = Event(), Event()
        run_b = self.worktree.with_name("second-worktree")
        run_b.mkdir()
        target_b = run_b / "Example.cs"
        target_b.write_bytes(b"class Before {}\n")
        edit = {"target": target_b.name, "version": sha256(target_b.read_bytes()).hexdigest(),
                "content": "class Saved {}\n"}

        def slow_inputs(worktree, project):
            self.assertEqual(worktree, self.worktree)
            scan_started.set()
            if not release_scan.wait(8):
                raise TimeoutError("test did not release receipt scan")
            return self.receipt["inputs"]

        with (
            patch.object(workflow, "build_inputs", side_effect=slow_inputs),
            patch.object(workflow, "build_artifacts", return_value=self.receipt["artifacts"]),
            ThreadPoolExecutor(max_workers=2) as threads,
        ):
            plan = threads.submit(workflow.plan_checker_resume, self.config, self.run_id)
            try:
                self.assertTrue(scan_started.wait(4))
                saved = threads.submit(save_target_edit, run_b, (Path(target_b.name),), edit)
                saved.result(timeout=4)
                self.assertEqual(target_b.read_text(encoding="utf-8-sig"), "class Saved {}\n")
                self.assertFalse(plan.done())
            finally:
                release_scan.set()
            self.assertEqual(plan.result(timeout=4)["build_receipt"], self.receipt)

    def test_aliases_use_the_same_lock_but_unrelated_worktrees_do_not(self):
        self.assertIs(worktree_source_lock(self.worktree), worktree_source_lock(
            self.worktree.parent / "." / self.worktree.name,
        ))
        self.assertIsNot(worktree_source_lock(self.worktree), worktree_source_lock(
            self.worktree.with_name("second-worktree"),
        ))

    def test_checker_rejects_receipt_drift_and_new_same_run_edit_before_launch(self):
        result = {"run_path": str(self.run_path), "worktree_path": str(self.worktree)}
        plan = {"checker_command": self.config["checker_command"], "edit_revision": None,
                "build_receipt": self.receipt}
        with (
            patch.object(workflow, "_check_build_receipt", return_value={"inputs": {}, "artifacts": {}}),
            patch.object(workflow, "_run_checker") as checker,
        ):
            with self.assertRaisesRegex(ValueError, "再開計画"):
                workflow._resume_checker(self.config, self.config["units"][0], result, self.record(),
                                         plan, "test")
            with editor_queue(self.run_path) as queue:
                queue["edit_revision"] = "changed"
            with self.assertRaisesRegex(ValueError, "計画後"):
                workflow._resume_checker(self.config, self.config["units"][0], result, self.record(),
                                         plan, "test")
            checker.assert_not_called()
            self.assertEqual(self.record()["checker_build"], self.receipt)
