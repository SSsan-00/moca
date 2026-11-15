import json
from pathlib import Path

from fixtures import HarnessCase
from migration_harness import workflow
from migration_harness.diff_viewer import capture_workspace, save_workspace


class LedgerReviewTest(HarnessCase):
    """判断の記録と対応候補の再確認を台帳から検証する。"""

    def setUp(self):
        """一つのPHP範囲と対応先を持つRunを準備する。"""
        super().setUp()
        (self.project / "source.php").write_text("<?php function label() {}\n", encoding="utf-8")
        (self.project / "Page.cs").write_text("public void Label() {}\n", encoding="utf-8")
        self.commit_project()
        self.settings = self.config()
        self.settings["units"][0]["inputs"][0]["symbol"] = "label"
        prepared = workflow.prepare_run(self.settings, "sample")
        self.run_id = prepared["run_id"]
        self.record_path = Path(prepared["run_path"]) / "run.json"
        self.target = Path(prepared["worktree_path"]) / "Page.cs"

    def source(self):
        """台帳の対象範囲を、毎回ディスクの最新状態から取得する。"""
        return workflow.migration_ledger(self.settings, self.run_id)["capsules"][0]["sources"][0]

    # 移行先の編集後も判断理由を保持し、従来の確認を失効させることを検証する。
    def test_keeps_review_reason_and_flags_stale_after_edit(self):
        record = json.loads(self.record_path.read_text(encoding="utf-8"))
        record.update(status="build_succeeded", completed_capsules=1)
        workflow._write_run_record(self.record_path, record)
        hashes = {}
        run, payload = capture_workspace(self.settings, self.run_id, target_hashes=hashes)
        workspace = payload["workspace"]
        workspace["anchors"]["manualAnchors"] = [{"leftLineNo": 0, "rightLineNo": 0}]
        snapshot = save_workspace(run, payload)
        workflow.record_migration_candidates(self.settings, self.run_id, snapshot, workspace, hashes)
        self.assertEqual(self.source()["symbol"], "label")
        self.assertEqual(self.source()["mapping"]["start_line"], 1)
        self.assertEqual(self.source()["mapping"]["snapshot_id"], snapshot)

        workflow.review_migration_ranges(self.settings, self.run_id, ["1:1"], "confirmed", "Compared outputs")
        self.assertEqual(self.source()["review"], {"state": "confirmed", "reason": "Compared outputs"})
        self.target.write_text("public void Changed() {}\n", encoding="utf-8")
        self.assertEqual(self.source()["review"], {"state": "stale", "reason": "Compared outputs"})
        with self.assertRaisesRegex(ValueError, "generate a new candidate"):
            workflow.review_migration_ranges(self.settings, self.run_id, ["1:1"], "confirmed", "Outdated")

    # 一括判断で無効な範囲を含めた場合に部分保存されないことを検証する。
    def test_exclusion_can_be_saved_and_invalid_batch_leaves_no_partial_changes(self):
        with self.assertRaisesRegex(ValueError, "PHP range not found"):
            workflow.review_migration_ranges(self.settings, self.run_id, ["1:1", "2:1"], "excluded", "Unused")
        self.assertNotIn("review", self.source())
        workflow.review_migration_ranges(self.settings, self.run_id, ["1:1"], "excluded", "Unused")
        self.assertEqual(self.source()["review"], {"state": "excluded", "reason": "Unused"})
        workflow.review_migration_ranges(self.settings, self.run_id, ["1:1"], "needs_review", "Check call sites")
        self.assertEqual(self.source()["review"], {"state": "needs_review", "reason": "Check call sites"})
