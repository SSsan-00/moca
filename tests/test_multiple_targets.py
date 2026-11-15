import json
from pathlib import Path
from unittest.mock import patch

from fixtures import HarnessCase
from migration_harness import workflow
from migration_harness.diff_viewer import capture_workspace


class MultipleTargetsTest(HarnessCase):
    """複数のRazorファイルの反映、中断、承認を一つのRunで検証する。"""

    targets = ("Page.cshtml.cs", "Page.cshtml")

    def setUp(self):
        """両方の移行先を同じworktreeに持つRunを準備する。"""
        super().setUp()
        (self.project / "source.php").write_text("<?php echo 1;\n", encoding="utf-8")
        for name in self.targets:
            (self.project / name).write_text(f"original {name}", encoding="utf-8")
        self.commit_project()
        self.settings = self.config('targets = ["Page.cshtml.cs", "Page.cshtml"]')
        self.prepared = workflow.prepare_run(self.settings, "sample")
        self.run_path = Path(self.prepared["run_path"])
        self.worktree = Path(self.prepared["worktree_path"])
        self.record_path = self.run_path / "run.json"

    def record(self):
        """移行先の復旧状況を、ディスク上のRun記録から確認する。"""
        return json.loads(self.record_path.read_bytes())

    def edit_with_junie(self, *, complete=True, outside=False):
        """Junieの編集結果と完了記録を、テストの条件に合わせて用意する。"""
        def run(_args, completion, token, targets):
            """Junieに相当する処理として、両方の移行先へ変更を保存する。"""
            self.assertEqual(tuple(path.name for path in targets), self.targets)
            for path in targets:
                path.write_text(f"migrated {path.name}", encoding="utf-8")
            if outside:
                (completion.parent / "unlisted.cs").write_text("unlisted", encoding="utf-8")
            if complete:
                completion.write_text(token, encoding="ascii")
            return 0
        return run

    def resume(self, run):
        """対話端末だけを差し替えて通常の再開経路を呼ぶ。"""
        with patch.object(workflow, "_validate_junie_execution"), patch.object(workflow, "_run_interactive_junie", run):
            return workflow.resume_with_junie(self.settings, self.prepared["run_id"])

    # 中断時の編集が残り、再開後に両方のファイルを比較できることを検証する。
    def test_partial_edits_are_kept_and_both_targets_are_compared(self):
        paused = self.resume(self.edit_with_junie(complete=False))
        self.assertEqual(paused["status"], "interrupted")
        self.assertEqual(paused["completed_capsules"], 0)
        with patch.object(workflow, "_verify_worktree", wraps=lambda _config, _unit, result, _record: result) as verify:
            done = self.resume(self.edit_with_junie())
        verify.assert_called_once()
        self.assertEqual(done["completed_capsules"], 1)
        self.assertEqual(set(self.record()["target_hashes"]), set(self.targets))
        _, snapshot = capture_workspace(self.settings, self.prepared["run_id"])
        self.assertEqual([segment["fileName"] for segment in snapshot["workspace"]["rightSegments"]], list(self.targets))

    # 主移行先以外の外部編集も検出して上書きを防ぐことを検証する。
    def test_rejects_worktree_edits_to_second_file_before_resuming(self):
        self.resume(self.edit_with_junie(complete=False))
        (self.worktree / self.targets[1]).write_text("someone else's edits", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "worktree targets changed"):
            self.resume(self.edit_with_junie())

    # 許可外の変更があれば、許可済みファイルも反映する前に停止することを検証する。
    def test_rejects_changes_to_unlisted_files_before_publishing_any_target(self):
        with self.assertRaisesRegex(ValueError, "outside the configured targets"):
            self.resume(self.edit_with_junie(outside=True))
        for name in self.targets:
            self.assertEqual((self.worktree / name).read_text(encoding="utf-8"), f"original {name}")
        self.assertEqual(self.record()["completed_capsules"], 0)

    # 途中の反映を復旧したうえで、カプセルの完了を改めて判定することを検証する。
    def test_recovers_partial_application_without_counting_the_capsule(self):
        real_replace = Path.replace

        def fail_second_file(source, target):
            """二つ目の移行先を置き換える箇所でだけ障害を起こす。"""
            if Path(target).name == self.targets[1]:
                raise OSError("fixture failure applying second target")
            return real_replace(source, target)

        with patch.object(Path, "replace", fail_second_file), self.assertRaisesRegex(OSError, "fixture failure"):
            self.resume(self.edit_with_junie())
        self.assertIn("target_apply_pending", self.record())
        self.assertEqual(self.record()["completed_capsules"], 0)
        self.assertEqual((self.worktree / self.targets[1]).read_text(encoding="utf-8"), f"original {self.targets[1]}")

        def resume_junie(args, completion, token, targets):
            """Junieの再開前に二つ目まで復旧していることを確認する。"""
            self.assertEqual((self.worktree / self.targets[1]).read_text(encoding="utf-8"), f"migrated {self.targets[1]}")
            return self.edit_with_junie()(args, completion, token, targets)

        with patch.object(workflow, "_verify_worktree", wraps=lambda _config, _unit, result, _record: result):
            self.resume(resume_junie)
        self.assertNotIn("target_apply_pending", self.record())
        self.assertEqual(self.record()["completed_capsules"], 1)

    # 反映失敗後の外部編集を、復旧記録で上書きしないことを検証する。
    def test_failed_application_does_not_overwrite_an_external_edit(self):
        real_replace = Path.replace

        def fail_second_file(source, target):
            """二つ目の移行先を置き換える箇所でだけ障害を起こす。"""
            if Path(target).name == self.targets[1]:
                raise OSError("fixture failure applying second target")
            return real_replace(source, target)

        with patch.object(Path, "replace", fail_second_file), self.assertRaises(OSError):
            self.resume(self.edit_with_junie())
        other = self.worktree / self.targets[1]
        other.write_text("external edit", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "external change"):
            self.resume(self.edit_with_junie())
        self.assertEqual(other.read_text(encoding="utf-8"), "external edit")

    # 片方を編集しても、もう一方の指摘の承認は有効なことを検証する。
    def test_acceptance_is_linked_to_each_reviewer_file(self):
        self.resume(self.edit_with_junie(complete=False))
        record = self.record()
        hashes = record["target_hashes"]
        record.update(status="checker_needs_review", checker_exit_code=1, review_file_hashes=hashes)
        workflow._write_run_record(self.record_path, record)
        (self.run_path / "checker.log").write_text(
            "".join(f"RZ1 {name}:1 warning: sample\n" for name in self.targets), encoding="utf-8",
        )
        run_id = self.prepared["run_id"]
        report = workflow.review_checker(self.settings, run_id, None, None)
        self.assertEqual(len(report["findings"]), 2)
        for finding_id, _, _ in report["findings"]:
            workflow.review_checker(self.settings, run_id, finding_id, "Fixture decision")
        self.assertTrue(all(accepted for _, _, accepted in workflow.review_checker(self.settings, run_id, None, None)["findings"]))
        (self.worktree / self.targets[1]).write_text("external edit", encoding="utf-8")
        report = workflow.review_checker(self.settings, run_id, None, None)
        self.assertEqual(sum(accepted for _, _, accepted in report["findings"]), 1)
