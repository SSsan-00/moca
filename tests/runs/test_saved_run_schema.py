from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import subprocess
import tomllib
import unittest
from unittest.mock import patch
import uuid

from tests.support.fixtures import HarnessCase, SavedRunFixture, prepare_fixture
from migration_harness import execution, workflow
from migration_harness.config import load_config
from migration_harness.junie import junie_cli
from migration_harness.runs import editor_queue, resume_plan, run_records
from migration_harness.web.diff_viewer import save_workspace, snapshot_bytes


class SavedRunSchemaTests(SavedRunFixture):
    def test_current_single_target_reopens_and_legacy_target_shapes_are_preserved_on_rejection(self):
        record = self.record()
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

        for remove, added, diagnostic in (
            ("targets", {}, "targets"),
            ("target_hashes", {"target_sha256": sha256(self.target.read_bytes()).hexdigest()}, "target_hashes"),
            ("junie_max_steps", {}, "junie_max_steps"),
            ("junie_model", {}, "junie_model"),
            ("current_capsule", {}, "current_capsule"),
        ):
            with self.subTest(field=remove):
                original = self.target.read_bytes()
                old = {key: value for key, value in record.items() if key != remove} | added
                self.save_record(old)
                old_bytes = self.record_path.read_bytes()
                with patch.object(execution, "run_process", side_effect=AssertionError("unexpected new process")):
                    with self.assertRaisesRegex(ValueError, diagnostic):
                        workflow.resume_with_junie(self.config, self.run_id)
                self.assertEqual(self.record_path.read_bytes(), old_bytes)
                self.assertEqual(self.target.read_bytes(), original)

    def test_used_reference_snapshot_is_not_recreated_for_a_saved_run(self):
        snapshot = self.run_path / "capsule" / "target-context"
        saved_bytes = self.record_path.read_bytes()
        target_bytes = self.target.read_bytes()
        (snapshot / "manifest.json").unlink()
        snapshot.rmdir()
        with patch.object(execution, "run_process", side_effect=AssertionError("unexpected new process")):
            with self.assertRaisesRegex(ValueError, "固定した参考資料"):
                workflow.resume_with_junie(self.config, self.run_id)
        self.assertEqual(self.record_path.read_bytes(), saved_bytes)
        self.assertEqual(self.target.read_bytes(), target_bytes)
        self.assertFalse(snapshot.exists())

    def test_saved_reference_capture_without_original_policy_cannot_resume(self):
        manifest_path = self.run_path / "capsule" / "target-context" / "manifest.json"
        saved = json.loads(manifest_path.read_bytes())
        record_bytes = self.record_path.read_bytes()
        for field in ("policy", "roots"):
            with self.subTest(field=field):
                old = {key: value for key, value in saved.items() if key != field}
                manifest_path.write_text(json.dumps(old), encoding="utf-8")
                before = manifest_path.read_bytes()
                with patch.object(execution, "run_process", side_effect=AssertionError("unexpected new process")):
                    with self.assertRaisesRegex(ValueError, "元の設定・起点・版"):
                        workflow.resume_with_junie(self.config, self.run_id)
                self.assertEqual(manifest_path.read_bytes(), before)
                self.assertEqual(self.record_path.read_bytes(), record_bytes)

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


class PreparedRunCanonicalTargetsTests(HarnessCase):
    def test_current_prepare_then_resume_pins_first_references_and_stores_only_target_map(self):
        (self.project / "source.php").write_text("<?php echo 1;\n", encoding="utf-8")
        self.commit_project()
        settings = ('target = "Page.cs"', 'targets = ["Page.cshtml.cs", "Page.cshtml"]')
        for target in settings:
            with self.subTest(target=target):
                config = self.config(target)
                names = config["units"][0]["targets"]
                prepared = prepare_fixture(config, "sample")
                run = Path(prepared["run_path"])
                record_path = run / "run.json"
                record = json.loads(record_path.read_bytes())
                self.assertEqual(record["targets"], names)
                self.assertNotIn("target_hashes", record)
                self.assertIsNone(record["junie_max_steps"])
                self.assertFalse((run / "capsule" / "target-context").exists())

                def migrate(command, completion, token, targets, **kwargs):
                    self.assertEqual([path.name for path in targets], names)
                    for path in targets:
                        path.write_text("<p>fixture</p>\n" if path.suffix == ".cshtml" else "class Fixture {}\n",
                                        encoding="utf-8")
                    completion.write_text(token, encoding="utf-8")
                    return 0

                with patch.object(junie_cli, "require_interactive_cli"), patch.object(
                    junie_cli, "run_interactive", side_effect=migrate,
                ) as launch, patch.object(workflow, "_verify_worktree", return_value={"status": "build_succeeded"}):
                    self.assertEqual(workflow.resume_with_junie(config, prepared["run_id"])["status"], "build_succeeded")
                launch.assert_called_once()
                record = json.loads(record_path.read_bytes())
                self.assertTrue(record["target_context_initialized"])
                worktree = Path(record["worktree_path"])
                self.assertEqual(record["target_hashes"], {
                    name: sha256((worktree / name).read_bytes()).hexdigest() for name in names
                })
                self.assertNotIn("target_sha256", record)


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

    def test_missing_history_is_new_but_legacy_receipts_cannot_be_replayed(self):
        with editor_queue.editor_queue(self.run) as history:
            self.assertEqual(history, {"edits": {}, "prompts": []})
        self.assertFalse(self.history.exists())
        intent = editor_queue.editor_save_intent(self.name, "old\n", "new\n", None, False)
        receipt = {key: intent[key] for key in ("target", "base", "draft")}
        receipt["after"] = sha256(self.target.read_bytes()).hexdigest()
        for field, identifier in (("immediate_save_requests", "a" * 32), ("immediate_saves", self.name)):
            with self.subTest(field=field):
                data = json.dumps({"edits": {}, "prompts": [], field: {identifier: receipt}}).encode()
                self.history.write_bytes(data)
                with self.assertRaisesRegex(ValueError, "旧形式"), editor_queue.editor_queue(self.run):
                    self.fail("legacy-only receipt was made visible")
                self.assertEqual(self.history.read_bytes(), data)
                self.assertEqual(self.target.read_bytes(), b"new\n")

    def test_mixed_journal_uses_only_current_receipt_and_keeps_old_fields_as_recorded(self):
        current_id, old_id = "2" * 32, "1" * 32
        original = {"edits": {}, "prompts": []}
        intent = editor_queue.editor_save_intent(self.name, "old\n", "new\n", None, False)
        digest = sha256(self.target.read_bytes()).hexdigest()
        legacy = {"immediate_save_requests": {old_id: {"target": self.name, "base": intent["base"],
                                                        "draft": intent["draft"], "after": digest}}}
        modern = {"save_requests": {current_id: {**intent, "after": digest}},
                  "latest_save_requests": {self.name: current_id}}
        before = json.dumps(original).encode()
        self.history.write_bytes(before)
        journal = self.run / editor_queue.EDITOR_SAVE_PENDING
        record = {"target": self.name, "before": None, "after": digest, "history_before": sha256(before).hexdigest()}
        for evidence in ({}, legacy, {**modern, "latest_save_requests": {}}, {**modern, "save_requests": {
                current_id: {**intent, "after": sha256(b"later").hexdigest()}}}):
            data = json.dumps({**record, "history_after": {**original, **evidence}}).encode()
            journal.write_bytes(data)
            with self.subTest(evidence=evidence), self.assertRaises(ValueError), editor_queue.editor_queue(
                    self.run, recover=(self.worktree, (Path(self.name),))):
                self.fail("journal not supported by the current receipt must not recover")
            self.assertEqual(journal.read_bytes(), data)
            self.assertEqual(self.history.read_bytes(), before)
            self.assertEqual(self.target.read_bytes(), b"new\n")
        mixed = {**original, **legacy, **modern}
        journal.write_text(json.dumps({**record, "history_after": mixed}), encoding="utf-8")
        with editor_queue.editor_queue(self.run, recover=(self.worktree, (Path(self.name),))) as recovered:
            self.assertEqual(recovered, mixed)
            args = recovered, None, self.target.read_bytes(), "new\n"
            self.assertTrue(editor_queue.replayed_editor_save(*args, current_id, intent))
            with self.assertRaisesRegex(ValueError, "旧形式"):
                editor_queue.replayed_editor_save(*args, old_id, intent)
        self.assertEqual(json.loads(self.history.read_bytes()), mixed)
        self.assertFalse(journal.exists())

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


    def test_first_save_recovers_from_valid_journal_without_reading_temporary_history(self):
        temporary = self.history.with_suffix(".json.tmp")
        temporary.write_bytes(b'{"edits":')
        journal = self.run / editor_queue.EDITOR_SAVE_PENDING
        old = b'{"edits": {}, "prompts": []}'
        persisted = {"edits": {}, "prompts": [{"id": "saved", "text": "Keep the edit.", "status": "queued"}]}
        identifier = "c" * 32
        intent = editor_queue.editor_save_intent(self.name, "old\n", "new\n", None, False)
        persisted.update(save_requests={identifier: {**intent, "after": sha256(self.target.read_bytes()).hexdigest()}},
                         latest_save_requests={self.name: identifier})
        journal.write_text(json.dumps({
            "target": self.name, "before": sha256(b"old\n").hexdigest(),
            "after": sha256(self.target.read_bytes()).hexdigest(),
            "history_before": sha256(old).hexdigest(), "history_after": persisted,
        }), encoding="utf-8")
        before = journal.read_bytes()
        with self.assertRaises(ValueError), editor_queue.editor_queue(self.run):
            self.fail("history-only read cannot perform recovery")
        self.assertEqual(journal.read_bytes(), before)
        self.assertFalse(self.history.exists())
        self.assertEqual(temporary.read_bytes(), b'{"edits":')
        with editor_queue.editor_queue(self.run, recover=(self.worktree, (Path(self.name),))) as recovered:
            self.assertEqual(recovered, persisted)
        self.assertEqual(json.loads(self.history.read_bytes()), persisted)
        self.assertFalse(journal.exists() or temporary.exists())
        self.assertEqual(self.target.read_bytes(), b"new\n")


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
