import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from tests.support.fixtures import HarnessCase, prepare_fixture
from migration_harness.web import diff_viewer, review_anchors
from migration_harness.junie import junie_cli
from migration_harness.runs import run_records
from migration_harness import workflow
from migration_harness.web.diff_viewer import capture_workspace, create_snapshot, snapshot_bytes


class LiveComparisonTests(HarnessCase):
    def test_managed_worktree_target_is_read_once_per_capture(self):
        (self.project / "source.php").write_text("<?php echo 42;\n", encoding="utf-8")
        data = "class Original {}\r\n".encode("utf-8-sig")
        (self.project / "Page.cs").write_bytes(data)
        self.commit_project()
        config = self.config()
        result = prepare_fixture(config, "sample")
        target = Path(result["worktree_path"]) / "Page.cs"
        original_open = Path.open

        for hashes in (None, {}):
            with self.subTest(return_hashes=hashes is not None):
                reads = []

                def open_file(path, mode="r", *args, **kwargs):
                    if path == target and "r" in mode:
                        reads.append(path)
                    return original_open(path, mode, *args, **kwargs)

                with patch.object(Path, "open", open_file):
                    _, payload = capture_workspace(config, result["run_id"], target_hashes=hashes)
                self.assertEqual(reads, [target])
                self.assertEqual(payload["workspace"]["rightText"], "class Original {}\n")
                if hashes is not None:
                    self.assertEqual(hashes, {"Page.cs": hashlib.sha256(data).hexdigest()})

    def test_live_draft_is_captured_without_changing_worktree_or_anchor_inputs(self):
        (self.project / "source.php").write_text("<?php echo 42;\n", encoding="utf-8")
        (self.project / "Page.cs").write_text("class Original {}", encoding="utf-8")
        self.commit_project()
        config = self.config()
        result = prepare_fixture(config, "sample")
        run_path, worktree, record, _ = run_records.load_saved_run(config, result["run_id"])
        agent = run_path / "agent-workspace"
        agent.mkdir()
        (agent / "Page.cs").write_text("class SavedDraft {}", encoding="utf-8")
        record["status"] = "junie_running"
        run_records.write_run_record(run_path / "run.json", record)
        snapshot = create_snapshot(config, result["run_id"])
        payload = json.loads(snapshot_bytes(config, result["run_id"], snapshot))
        self.assertEqual(payload["workspace"]["rightText"], "class SavedDraft {}")
        self.assertEqual(payload["snapshot"]["draftFiles"], ["Page.cs"])
        self.assertEqual((worktree / "Page.cs").read_text(), "class Original {}")
        _, anchor_input = capture_workspace(config, result["run_id"])
        self.assertEqual(anchor_input["workspace"]["rightText"], "class Original {}")
        (agent / "Page.cs").write_text("class LaterDraft {}", encoding="utf-8")
        self.assertEqual(json.loads(snapshot_bytes(config, result["run_id"], snapshot)), payload)
        record["status"] = "build_succeeded"
        run_records.write_run_record(run_path / "run.json", record)
        _, finished = capture_workspace(config, result["run_id"], include_draft=True)
        self.assertEqual(finished["workspace"]["rightText"], "class Original {}")

    def test_source_can_be_viewed_before_target_is_created(self):
        (self.project / "source.php").write_text("<?php echo 42;\n", encoding="utf-8")
        self.commit_project()
        config = self.config()
        result = prepare_fixture(config, "sample")
        _, payload = capture_workspace(config, result["run_id"], include_draft=True)
        self.assertIn("42", payload["workspace"]["leftText"])
        self.assertEqual(payload["workspace"]["rightText"], "")
        self.assertEqual(payload["snapshot"]["missingFiles"], ["Page.cs"])

    def test_capture_checks_pending_application_after_acquiring_source_lock(self):
        (self.project / "source.php").write_text("<?php echo 42;\n", encoding="utf-8")
        (self.project / "Page.cs").write_text("class Original {}", encoding="utf-8")
        self.commit_project()
        config = self.config()
        result = prepare_fixture(config, "sample")
        run_path, worktree, record, _ = run_records.load_saved_run(config, result["run_id"])

        @contextmanager
        def application_failed_while_waiting():
            (worktree / "Page.cs").write_text("class PartlyApplied {}", encoding="utf-8")
            record["target_apply_pending"] = {"Page.cs": {"before": "old", "after": "new"}}
            run_records.write_run_record(run_path / "run.json", record)
            yield

        with patch.object(diff_viewer, "worktree_source_lock", side_effect=lambda _: application_failed_while_waiting()):
            with self.assertRaisesRegex(ValueError, "反映が途中"):
                create_snapshot(config, result["run_id"])
        self.assertFalse((run_path / "review-snapshots").exists())

    def test_anchors_reject_modified_numbered_inputs(self):
        (self.project / "source.php").write_text("<?php echo 42;\n", encoding="utf-8")
        (self.project / "Page.cs").write_text("class Original {}", encoding="utf-8")
        self.commit_project()
        config = self.config()
        config["diff_viewer_html"] = "viewer.html"
        result = prepare_fixture(config, "sample")

        for name in ("left.txt", "right.txt", "guide.json"):
            with self.subTest(name=name):
                def junie(command, completion, token, targets, *, report_usage=None, prefer_task=False):
                    proposal, = targets
                    (proposal.parent / name).write_text("changed input", encoding="utf-8")
                    proposal.write_text('[{"leftLineNo": 1, "rightLineNo": 1}]', encoding="utf-8")
                    completion.write_text(token, encoding="ascii")

                with (
                    patch.object(junie_cli, "require_interactive_cli"),
                    patch.object(junie_cli, "run_interactive", side_effect=junie),
                ):
                    with self.assertRaisesRegex(ValueError, "アンカー作成中に入力"):
                        review_anchors.generate_anchors(config, result["run_id"])
                self.assertFalse((Path(result["run_path"]) / "review-snapshots").exists())

    def test_capture_rejects_target_appearing_during_read(self):
        (self.project / "source.php").write_text("<?php echo 42;\n", encoding="utf-8")
        self.commit_project()
        config = self.config()
        result = prepare_fixture(config, "sample")
        run_path, worktree, record, _ = run_records.load_saved_run(config, result["run_id"])
        agent = run_path / "agent-workspace"
        agent.mkdir()
        read_pinned = diff_viewer.read_pinned_php
        for destination in (worktree, agent):
            with self.subTest(destination=destination.name):
                record["status"] = "junie_running"
                run_records.write_run_record(run_path / "run.json", record)

                def create_target(capsule_path, source):
                    content = read_pinned(capsule_path, source)
                    (destination / "Page.cs").write_text("class New {}", encoding="utf-8")
                    return content

                with patch.object(diff_viewer, "read_pinned_php", side_effect=create_target):
                    with self.assertRaisesRegex(ValueError, "取得中にソースが変更"):
                        create_snapshot(config, result["run_id"])
                self.assertFalse((run_path / "review-snapshots").exists())
                # 次のケースでは、既存のworktreeから新しい下書きへの切替を検証する。
                (worktree / "Page.cs").write_text("class Previous {}", encoding="utf-8")

    def test_capture_rejects_draft_state_changing_during_read(self):
        (self.project / "source.php").write_text("<?php echo 42;\n", encoding="utf-8")
        (self.project / "Page.cs").write_text("class Original {}", encoding="utf-8")
        self.commit_project()
        config = self.config()
        result = prepare_fixture(config, "sample")
        run_path, _, record, _ = run_records.load_saved_run(config, result["run_id"])
        agent = run_path / "agent-workspace"
        agent.mkdir()
        (agent / "Page.cs").write_text("class Draft {}", encoding="utf-8")
        record["status"] = "junie_running"
        run_records.write_run_record(run_path / "run.json", record)
        read_pinned = diff_viewer.read_pinned_php

        def finish_run(capsule_path, source):
            content = read_pinned(capsule_path, source)
            record["status"] = "build_succeeded"
            run_records.write_run_record(run_path / "run.json", record)
            return content

        with patch.object(diff_viewer, "read_pinned_php", side_effect=finish_run):
            with self.assertRaisesRegex(ValueError, "取得中にソースが変更"):
                create_snapshot(config, result["run_id"])
        self.assertFalse((run_path / "review-snapshots").exists())

    def test_managed_replacement_during_capture_does_not_publish_hashes(self):
        (self.project / "source.php").write_text("<?php echo 42;\n", encoding="utf-8")
        (self.project / "Page.cs").write_text("class Original {}", encoding="utf-8")
        self.commit_project()
        config = self.config()
        result = prepare_fixture(config, "sample")
        target = Path(result["worktree_path"]) / "Page.cs"
        read_bytes = Path.read_bytes
        hashes = {"existing": "unchanged"}

        def edit_after_read(path):
            content = read_bytes(path)
            if path == target:
                before = path.stat()
                replacement = path.with_suffix(".tmp")
                replacement.write_bytes(b"class Replaced {}")
                os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
                replacement.replace(path)
            return content

        with patch.object(Path, "read_bytes", edit_after_read):
            with self.assertRaisesRegex(ValueError, "取得中にソースが変更"):
                capture_workspace(config, result["run_id"], target_hashes=hashes)
        self.assertEqual(hashes, {"existing": "unchanged"})

    def test_live_source_changes_are_rejected_when_windows_metadata_is_unchanged(self):
        (self.project / "source.php").write_bytes(b"<?php echo 42;\r\n")
        (self.project / "Page.cs").write_bytes(b"class Original {}")
        self.commit_project()
        config = self.config()
        result = prepare_fixture(config, "sample")
        run_path, worktree, record, _ = run_records.load_saved_run(config, result["run_id"])
        agent = run_path / "agent-workspace"
        agent.mkdir()
        (agent / "Page.cs").write_bytes(b"class Original {}")
        read_bytes, file_version = Path.read_bytes, diff_viewer._file_version

        for source_kind in ("draft", "legacy_php"):
            with self.subTest(source_kind=source_kind):
                record["status"] = "junie_running" if source_kind == "draft" else "prepared"
                run_records.write_run_record(run_path / "run.json", record)
                trigger = (agent if source_kind == "draft" else worktree) / "Page.cs"
                changed = trigger
                replacement = b"class Replaced {}"
                if source_kind == "legacy_php":
                    manifest_path = run_path / "capsule" / "manifest.json"
                    manifest = json.loads(manifest_path.read_bytes())
                    manifest.pop("php_sources")
                    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                    changed = self.project / "source.php"
                    replacement = b"<?php echo 43;\r\n"
                before = changed.stat()
                original_version = file_version(changed)
                edits = []

                def windows_version(path):
                    version = file_version(path)
                    # Windowsのctimeは作成日時なので、内容を書き換えても変わらない。
                    return (*version[:-1], before.st_ctime_ns) if path == changed and version else version

                def edit_after_read(path):
                    content = read_bytes(path)
                    if path == trigger:
                        changed.write_bytes(replacement)
                        os.utime(changed, ns=(before.st_atime_ns, before.st_mtime_ns))
                        self.assertEqual(windows_version(changed), original_version)
                        edits.append(changed)
                    return content

                with (
                    patch.object(diff_viewer, "_file_version", side_effect=windows_version),
                    patch.object(Path, "read_bytes", edit_after_read),
                ):
                    # 通常比較でも、ハッシュの返却を要求せずに内容の整合性を検証する。
                    with self.assertRaisesRegex(ValueError, "取得中にソースが変更"):
                        create_snapshot(config, result["run_id"])
                self.assertEqual(edits, [changed])
                self.assertFalse((run_path / "review-snapshots").exists())
