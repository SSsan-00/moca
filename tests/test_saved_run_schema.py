from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import subprocess
import tomllib
import unittest
from unittest.mock import patch
import uuid

from fixtures import SavedRunFixture
from migration_harness import execution, workflow
from migration_harness.config import load_config
from migration_harness.runs import editor_queue, resume_plan, run_records
from migration_harness.web.diff_viewer import save_workspace, snapshot_bytes


class SavedRunSchemaTests(SavedRunFixture):
    def test_legacy_single_target_reopens_and_resumes_without_rewriting(self):
        record = self.record()
        for field in ("targets", "verification", "junie_model_revision"):
            record.pop(field, None)
        self.save_record(record)
        before = self.record_path.read_bytes()
        run_path, saved = run_records.load_dashboard_record(self.config, self.run_id)
        self.assertEqual(run_path, self.run_path)
        self.assertEqual(saved, record)
        self.assertTrue(resume_plan.can_resume_run(self.config, saved))
        resolved, complete, _, _ = resume_plan.load_resume_context(
            self.config, self.run_id, run_path, self.worktree, saved, self.config["units"][0], saved["status"],
        )
        self.assertTrue(complete)
        self.assertEqual(resolved["capsule_count"], 1)
        result = workflow.migration_ledger(self.config, self.run_id)
        self.assertEqual(result["completion"], {"complete": False, "verification_passed": False, "total": 1})
        self.assertEqual(self.record_path.read_bytes(), before)

    def test_malformed_records_fail_closed_before_resume_and_keep_original(self):
        record = self.record()
        for name, raw in (
            ("partial JSON", b'{"status":'),
            ("array", b"[]"),
            ("duplicate field", b'{"status":"interrupt","status":"building"}'),
            ("nonfinite number", b'{"status":"interrupted","extra":NaN}'),
            ("missing status", json.dumps({key: value for key, value in record.items() if key != "status"}).encode()),
            ("wrong progress type", json.dumps({**record, "completed_capsules": True}).encode()),
            ("wrong exit type", json.dumps({**record, "build_exit_code": False}).encode()),
            ("unknown schema", json.dumps({**record, "schema_version": 99}).encode()),
            ("different run", json.dumps({**record, "run_id": str(uuid.uuid4())}).encode()),
            ("different primary target", json.dumps({**record, "target": "Other.cs", "targets": [self.target.name]}).encode()),
        ):
            with self.subTest(name=name):
                self.record_path.write_bytes(raw)
                with patch.object(execution, "run_process", side_effect=AssertionError("unexpected new process")):
                    with self.assertRaises(ValueError):
                        workflow.resume_with_junie(self.config, self.run_id)
                self.assertEqual(self.record_path.read_bytes(), raw)
                self.assertEqual(self.target.read_bytes().replace(b"\r\n", b"\n"), b"class Original {}\n")
        self.record_path.unlink()
        scratch = self.record_path.with_suffix(".json.tmp")
        scratch.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "run record is missing"):
            run_records.load_saved_run(self.config, self.run_id)
        self.assertFalse(self.record_path.exists())
        self.assertEqual(json.loads(scratch.read_bytes()), record)

    def test_invalid_progress_or_unit_is_not_an_implicit_recovery(self):
        record = self.record()
        self.save_record({**record, "context_capsules": 2})
        with self.assertRaisesRegex(ValueError, "inconsistent capsule counts"):
            workflow.resume_with_junie(self.config, self.run_id)
        self.save_record(record)
        manifest = self.run_path / "capsule" / "manifest.json"
        contents = json.loads(manifest.read_bytes())
        manifest.write_text(json.dumps({**contents, "unit_id": "different"}), encoding="utf-8")
        before = manifest.read_bytes()
        with self.assertRaisesRegex(ValueError, "migration unit"):
            workflow.resume_with_junie(self.config, self.run_id)
        with self.assertRaisesRegex(ValueError, "migration unit"):
            workflow.migration_ledger(self.config, self.run_id)
        self.assertEqual(manifest.read_bytes(), before)
        self.assertEqual(self.record(), record)


    def test_comparison_keeps_valid_v1_bytes_and_refuses_a_different_version_or_run(self):
        snapshot = {"kind": "diff-viewer-workspace", "version": 1, "snapshot": {"runId": self.run_id},
                    "workspace": {"leftText": "<?php", "rightText": "class Fixture {}"}}
        good = save_workspace(self.run_path, snapshot)
        path = self.run_path / "review-snapshots" / f"{good}.json"
        self.assertEqual(snapshot_bytes(self.config, self.run_id, good), path.read_bytes())
        for entry in ({**snapshot, "version": 99}, {**snapshot, "version": True},
                      {**snapshot, "workspace": {"rightText": "class Fixture {}"}},
                      {**snapshot, "snapshot": {"runId": str(uuid.uuid4())}}):
            with self.subTest(entry=entry):
                bad = save_workspace(self.run_path, entry)
                broken = self.run_path / "review-snapshots" / f"{bad}.json"
                before = broken.read_bytes()
                with self.assertRaises(ValueError):
                    snapshot_bytes(self.config, self.run_id, bad)
                self.assertEqual(broken.read_bytes(), before)


class SavedEditorSchemaTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(TemporaryDirectory())
        self.root = Path(folder)
        self.run = self.root / "run"
        self.run.mkdir()
        self.worktree = self.root / "worktree"
        self.worktree.mkdir()
        self.name = "Page.cs"
        self.target = self.worktree / self.name
        self.target.write_bytes(b"new\n")
        self.history = self.run / "editor.json"

    def test_legacy_save_receipt_and_missing_history_remain_compatible(self):
        with editor_queue.editor_queue(self.run) as history:
            self.assertEqual(history, {"edits": {}, "prompts": []})
        self.assertFalse(self.history.exists())
        digest = lambda text: sha256(text.encode("utf-8")).hexdigest()
        identifier = "old-request"
        receipt = {"target": self.name, "base": digest("old\n"), "draft": digest("new\n"), "after": digest("new\n")}
        self.history.write_text(json.dumps({"edits": {}, "prompts": [], "immediate_save_requests": {identifier: receipt}}),
                                encoding="utf-8")
        before = self.history.read_bytes()
        with editor_queue.editor_queue(self.run) as history:
            intent = editor_queue.editor_save_intent(self.name, "old\n", "new\n", None, False)
            self.assertTrue(editor_queue.replayed_editor_save(
                history, None, self.target.read_bytes(), "new\n", identifier, intent,
            ))
        self.assertEqual(self.history.read_bytes(), before)

    def test_broken_history_or_pending_journal_keeps_both_versions(self):
        valid = {"edits": {}, "prompts": []}
        for record in ({}, {**valid, "prompts": {}}, {**valid, "schema_version": 99},
                       {**valid, "latest_save_requests": {self.name: "missing"}},
                       {**valid, "edits": {self.name: {"id": "x", "status": "queued", "base": "valid"}}}):
            with self.subTest(record=record):
                self.history.write_text(json.dumps(record), encoding="utf-8")
                before = self.history.read_bytes()
                with self.assertRaises(ValueError), editor_queue.editor_queue(self.run):
                    self.fail("invalid editor history was made visible")
                self.assertEqual(self.history.read_bytes(), before)
        self.history.write_text(json.dumps(valid), encoding="utf-8")
        before = self.history.read_bytes()
        journal = self.run / editor_queue.EDITOR_SAVE_PENDING
        invalid = {"target": self.name, "before": None, "after": sha256(self.target.read_bytes()).hexdigest(),
                   "history_before": sha256(before).hexdigest(), "history_after": {**valid, "prompts": {}}}
        journal.write_text(json.dumps(invalid), encoding="utf-8")
        saved_journal = journal.read_bytes()
        with self.assertRaises(ValueError), editor_queue.editor_queue(self.run, recover=(self.worktree, (Path(self.name),))):
            self.fail("invalid journal should stop before editing")
        self.assertEqual(self.history.read_bytes(), before)
        self.assertEqual(journal.read_bytes(), saved_journal)
        self.assertEqual(self.target.read_bytes(), b"new\n")

    def test_editor_temporary_file_without_previous_record_is_not_empty_history(self):
        temporary = self.history.with_suffix(".json.tmp")
        temporary.write_bytes(b'{"edits":')
        with self.assertRaisesRegex(ValueError, "保存途中"), editor_queue.editor_queue(self.run):
            self.fail("temporary history should not be used")
        self.assertEqual(temporary.read_bytes(), b'{"edits":')
        self.assertFalse(self.history.exists())


class SavedConfigSchemaTests(unittest.TestCase):
    def test_legacy_config_can_be_read_and_bad_versions_or_types_are_not_rewritten(self):
        base = ('[project]\nroot="app"\nworktree_root="trees"\nrun_root="runs"\n'
                '[[units]]\nid="example"\nsource="before.inc"\ntarget="Page.cs"\n')
        with TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "--quiet", str(root / "app")], check=True)
            path = root / "harness.toml"
            path.write_text(base, encoding="utf-8")
            self.assertEqual(load_config(path)["units"][0]["target"], "Page.cs")
            for invalid, expected in (
                ("schema_version = 99\n" + base, ValueError),
                (base.replace('target="Page.cs"', "target=false"), ValueError),
                (base.replace('target="Page.cs"', 'target="Page.cs'), tomllib.TOMLDecodeError),
            ):
                path.write_text(invalid, encoding="utf-8")
                before = path.read_bytes()
                with self.assertRaises(expected):
                    load_config(path)
                self.assertEqual(path.read_bytes(), before)
