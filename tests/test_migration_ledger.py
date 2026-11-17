import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from fixtures import HarnessCase
from migration_harness import run_records, workflow
from migration_harness.diff_viewer import capture_workspace, save_workspace


class LedgerReviewTest(HarnessCase):
    """判断の記録と対応候補の再確認を台帳から検証する。"""

    def setUp(self):
        """一つのPHP範囲と対応先を持つRunを準備する。"""
        super().setUp()
        (self.project / "source.php").write_text("<?php function label() {}\n", encoding="utf-8")
        (self.project / "Page.cs").write_text("public void Label() {}\n", encoding="utf-8")
        (self.project / "App.csproj").write_text('<Project Sdk="Microsoft.NET.Sdk" />\n', encoding="utf-8")
        (self.project / "Dependency.cs").write_text("class Dependency {}\n", encoding="utf-8")
        (self.project / ".gitignore").write_text("bin/\nobj/\nlocal/\n", encoding="utf-8")
        self.commit_project()
        self.settings = self.config()
        self.settings["units"][0]["inputs"][0]["symbol"] = "label"
        prepared = workflow.prepare_run(self.settings, "sample")
        self.run_id = prepared["run_id"]
        self.record_path = Path(prepared["run_path"]) / "run.json"
        self.target = Path(prepared["worktree_path"]) / "Page.cs"

    def completion(self):
        return workflow.migration_ledger(self.settings, self.run_id)["completion"]

    def prepare_for_verification(self):
        record = json.loads(self.record_path.read_bytes())
        record.update(status="junie_finished", completed_capsules=1)
        run_records.write_run_record(self.record_path, record)
        workflow.review_migration_ranges(
            self.settings, self.run_id, ["1:1"], "confirmed", "Reviewed",
            confirmed_targets=[self.target.name],
        )

    def verify(self, exit_code=0):
        run_process = subprocess.run

        def build_or_run(command, **kwargs):
            if command[:2] != ["dotnet", "build"]:
                return run_process(command, **kwargs)
            output = self.target.parent / "obj" / "build-output"
            output.parent.mkdir(exist_ok=True)
            with output.open("ab") as stream:
                stream.write(b"another build\n")
            return subprocess.CompletedProcess(command, exit_code)

        with patch.object(workflow.subprocess, "run", side_effect=build_or_run):
            return workflow.resume_with_junie(self.settings, self.run_id)

    def source(self):
        """台帳の対象範囲を、毎回ディスクの最新状態から取得する。"""
        return workflow.migration_ledger(self.settings, self.run_id)["capsules"][0]["sources"][0]

    def test_checker_success_sentence_counts_as_current_verification(self):
        self.settings["checker_command"] = [
            sys.executable, "-X", "utf8", "-c",
            "import sys; print('特に気になる点はありません。'); sys.exit(1)",
        ]
        self.prepare_for_verification()
        self.assertEqual(self.verify()["status"], "checker_succeeded")
        self.assertTrue(self.completion()["complete"])
        self.target.write_text("public void Changed() {}\n", encoding="utf-8")
        self.assertFalse(self.completion()["complete"])

    # 移行先の編集後も判断理由を保持し、従来の確認を失効させることを検証する。
    def test_keeps_review_reason_and_flags_stale_after_edit(self):
        record = json.loads(self.record_path.read_text(encoding="utf-8"))
        record.update(status="build_succeeded", completed_capsules=1)
        run_records.write_run_record(self.record_path, record)
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

    def test_anchor_usage_keeps_reviews_written_by_another_cli(self):
        self.prepare_for_verification()
        self.verify()
        run_path = self.record_path.parent
        stale_record = json.loads(self.record_path.read_bytes())
        review_command = [
            sys.executable, "-m", "migration_harness.cli", "ledger-review",
            "--config", self.settings["config_path"], "--run-id", self.run_id, "--range", "1:1",
        ]
        report = run_records.junie_usage_recorder(run_path, "anchors")
        report([18, 4, 5, 3, 1])
        subprocess.run([
            *review_command, "--state", "confirmed", "--reason", "Confirmed again", "--target", self.target.name,
        ], check=True, capture_output=True)
        report([1, 0, 0, 0, 1])
        self.assertEqual(self.source()["review"], {
            "state": "confirmed", "reason": "Confirmed again", "targets": [self.target.name],
        })
        self.assertTrue(self.completion()["complete"])
        subprocess.run([
            *review_command, "--state", "needs_review", "--reason", "Different result",
        ], check=True, capture_output=True)
        report([2, 1, 0, 0, 1])

        hashes = {}
        _, payload = capture_workspace(self.settings, self.run_id, target_hashes=hashes)
        workspace = payload["workspace"]
        workspace["anchors"]["manualAnchors"] = [{"leftLineNo": 0, "rightLineNo": 0}]
        snapshot = save_workspace(run_path, payload)
        workflow.record_migration_candidates(self.settings, self.run_id, snapshot, workspace, hashes)
        run_records.write_run_record(self.record_path, stale_record)

        source = self.source()
        self.assertEqual(source["review"], {"state": "needs_review", "reason": "Different result"})
        self.assertEqual(source["mapping"]["snapshot_id"], snapshot)
        self.assertFalse(self.completion()["complete"])
        saved = json.loads(self.record_path.read_bytes())
        self.assertEqual(saved["token_usage"], [{
            "phase": "anchors", "capsule": None, "input": 21, "output": 5,
            "cache_read": 5, "cache_write": 3, "responses": 3,
        }])

    def test_verification_and_ledger_do_not_scan_non_target_files(self):
        self.prepare_for_verification()
        original_open = Path.open
        original_run = subprocess.run

        def open_file(path, mode="r", *args, **kwargs):
            if "r" in mode and path.is_relative_to(self.target.parent):
                self.assertEqual(path, self.target, f"Unexpected worktree read: {path}")
            return original_open(path, mode, *args, **kwargs)

        def run_command(command, **kwargs):
            self.assertNotEqual(command[0], "git", "Verification must not scan the worktree with Git")
            return original_run(command, **kwargs)

        with (
            patch.object(Path, "open", open_file),
            patch.object(workflow.subprocess, "run", side_effect=run_command),
        ):
            self.assertEqual(self.verify()["status"], "build_succeeded")
            self.assertTrue(self.completion()["complete"])

    def test_non_target_files_are_assumed_unchanged_until_explicit_reverification(self):
        self.prepare_for_verification()
        self.verify()
        self.assertTrue(self.completion()["complete"])
        worktree = self.target.parent
        project = worktree / "App.csproj"
        dependency = worktree / "Dependency.cs"
        added = worktree / "Untracked.cs"
        renamed = worktree / "Renamed.cs"
        ignored_source = worktree / "local" / "Local.cs"
        ignored_source.parent.mkdir()
        for edit in (
            lambda: project.write_text('<Project Sdk="Microsoft.NET.Sdk">\n</Project>\n', encoding="utf-8"),
            lambda: dependency.write_text("class Dependency { }\n", encoding="utf-8"),
            lambda: added.write_text("class Untracked {}\n", encoding="utf-8"),
            lambda: added.rename(renamed),
            renamed.unlink,
            dependency.unlink,
            lambda: ignored_source.write_text("class Local {}\n", encoding="utf-8"),
            ignored_source.unlink,
        ):
            with self.subTest(edit=edit):
                edit()
                self.assertEqual(self.completion(), {
                    "complete": True, "verification_passed": True, "reviewed": 1, "total": 1,
                })

        (worktree / "obj" / "Generated.cs").write_text("ignored change", encoding="utf-8")
        self.assertTrue(self.completion()["complete"])
        self.assertEqual(self.source()["review"]["state"], "confirmed")

    def test_checker_and_hook_changes_are_applied_on_reverification(self):
        worktree = self.target.parent
        ignored = worktree / "obj"
        ignored.mkdir()
        checker = ignored / "check.py"
        checker.write_text("pass\n", encoding="utf-8")
        hook = self.root / "after_build.py"
        hook.write_text("pass\n", encoding="utf-8")
        self.settings.update(
            checker_command=[sys.executable, "obj/check.py"],
            scripts={"after_build": [[sys.executable, str(hook)]]},
        )
        self.prepare_for_verification()
        self.verify()
        self.assertTrue(self.completion()["complete"])
        checker.write_text("raise SystemExit(1)\n", encoding="utf-8")
        self.assertTrue(self.completion()["verification_passed"])
        self.assertEqual(self.verify()["status"], "checker_needs_review")
        self.assertFalse(self.completion()["complete"])
        checker.write_text("# fixed\n", encoding="utf-8")
        self.verify()
        self.assertTrue(self.completion()["complete"])
        hook.write_text("# edited\n", encoding="utf-8")
        self.assertTrue(self.completion()["verification_passed"])
        self.verify()
        self.assertTrue(self.completion()["complete"])

        self.settings["build_project"] = "App.csproj"
        self.assertFalse(self.completion()["verification_passed"])
        self.verify()
        self.assertTrue(self.completion()["complete"])
        self.settings["scripts"] = {}
        self.assertFalse(self.completion()["verification_passed"])
        self.verify()
        self.assertTrue(self.completion()["complete"])
        self.settings["checker_command"] = None
        self.assertFalse(self.completion()["verification_passed"])

    def test_saved_cli_checker_is_used_with_toml_only_when_reading_ledger(self):
        self.settings.update(checker_from_cli=True, checker_command=[sys.executable, "-c", "pass"])
        self.prepare_for_verification()
        self.verify()
        self.settings.pop("checker_from_cli")
        self.settings["checker_command"] = None
        self.assertTrue(self.completion()["complete"])
        self.settings["checker_command"] = [sys.executable, "-c", "# different command"]
        self.assertFalse(self.completion()["verification_passed"])

    def test_completion_requires_verification_record_but_no_worktree_fingerprint(self):
        self.prepare_for_verification()
        self.verify()
        record = json.loads(self.record_path.read_bytes())
        self.assertTrue(self.completion()["complete"])
        self.assertNotIn("input_fingerprint", record["verification"])
        with patch.object(workflow.subprocess, "run", side_effect=OSError("Git unavailable")):
            self.assertTrue(self.completion()["complete"])
        # 旧Runに残る全体ハッシュも再計算しない。
        record["verification"]["input_fingerprint"] = "legacy-worktree-fingerprint"
        run_records.write_run_record(self.record_path, record)
        self.assertTrue(self.completion()["complete"])
        del record["verification"]
        run_records.write_run_record(self.record_path, record)
        self.assertFalse(self.completion()["verification_passed"])
        self.verify()
        self.assertTrue(self.completion()["complete"])

    def test_cli_override_is_reused_only_with_original_toml_default(self):
        checker = self.target.parent / "override.py"
        checker.write_text("pass\n", encoding="utf-8")
        default = [sys.executable, "default.py"]
        override = [sys.executable, "override.py"]
        alternate = [sys.executable, "alternate.py"]
        self.settings.update(checker_from_cli=True, checker_command=override, toml_checker_command=default)
        self.prepare_for_verification()
        self.verify()

        self.settings.pop("checker_from_cli")
        self.settings.pop("toml_checker_command")
        self.settings["checker_command"] = default
        self.assertTrue(self.completion()["complete"])
        self.settings["checker_command"] = alternate
        self.assertFalse(self.completion()["verification_passed"])
        self.settings.update(checker_from_cli=True, checker_command=alternate, toml_checker_command=default)
        self.assertFalse(self.completion()["verification_passed"])
        self.settings.pop("checker_from_cli")
        self.settings["checker_command"] = default
        checker.write_text("# changed checker\n", encoding="utf-8")
        self.assertTrue(self.completion()["verification_passed"])

    def test_hooks_run_without_scanning_non_target_build_inputs(self):
        change = [sys.executable, "-c", "open('Dependency.cs', 'w').write('class HookEdit {}')"]
        restore = [sys.executable, "-c", "open('Dependency.cs', 'w').write('class Dependency {}\\n')"]
        self.settings.update(
            checker_command=[sys.executable, "-c", "pass"],
            scripts={"before_checker": [change], "after_checker": [restore]},
        )
        self.prepare_for_verification()
        self.assertEqual(self.verify()["status"], "checker_succeeded")
        self.assertTrue(self.completion()["verification_passed"])
        self.assertEqual((self.target.parent / "Dependency.cs").read_text(), "class Dependency {}\n")
