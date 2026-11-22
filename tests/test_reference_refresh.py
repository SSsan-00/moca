import codecs
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from migration_harness import source_context, workflow
from migration_harness.worktree_targets import target_hash


class ReferenceRefreshTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.run = Path(folder.name).resolve() / "run"
        self.worktree = self.run.parent / "worktree"
        self.worktree.mkdir()
        self.agent = self.run / "agent-workspace"
        self.agent.mkdir(parents=True)
        self.capsule = self.run / "capsule"
        self.target = Path("Page.cs")
        self.targets = (self.target,)
        self.ref = Path("refs/Example.cs")
        self.base_text = "start\nold reference behavior\none\ntwo\nthree\nfour\nfive\nold label\nend\n"
        self.put(self.target, self.base_text)
        self.put(self.target, self.base_text, root=self.agent)
        self.put(self.ref, "old reference")
        source_context.stage_target_context(
            self.worktree, self.targets, self.capsule, self.agent,
            policy={"roots": [], "folders": ["refs"]},
        )
        before = {self.target.as_posix(): target_hash(self.worktree / self.target)}
        self.record = {"target": self.target.as_posix(), "status": "interrupted", "completed_capsules": 1,
                       "target_hashes": before.copy(), "build_exit_code": 0, "checker_exit_code": 0,
                       "capsule_checkpoints": {"1": workflow._save_capsule_checkpoint(
                           self.run, self.worktree, self.targets, before)}}
        self.addCleanup(patch.stopall)
        patch.object(workflow, "target_paths", return_value=self.targets).start()
        patch.object(workflow, "load_capsule_manifest", return_value=(self.capsule, {})).start()

    def put(self, relative, value, *, root=None):
        path = (root or self.worktree) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(codecs.BOM_UTF8 + value.encode("utf-8"))

    def refresh(self):
        workflow._refresh_target_context_on_resume({}, {}, self.run, self.worktree, self.record,
                                                   migration_complete=False)

    def propose(self, *args):
        proposal = self.run / "reference-refresh-proposal"
        self.put(self.target, self.base_text.replace("old reference behavior", "new reference behavior"), root=proposal)
        return proposal

    def test_unmodified_reference_skips_junie_even_if_timestamp_changed(self):
        (self.worktree / self.ref).touch()
        with patch.object(workflow, "_propose_reference_refresh") as propose:
            self.refresh()
        propose.assert_not_called()
        self.assertIn("build_exit_code", self.record)

    def test_changed_reference_merges_unrelated_edits_and_preserves_versions(self):
        self.put(self.ref, "new reference")
        self.put(self.target, self.base_text.replace("old label", "operator label"))
        with patch.object(workflow, "_propose_reference_refresh", side_effect=self.propose) as propose:
            self.refresh()
            self.refresh()
        self.assertEqual(propose.call_count, 1)
        text = (self.worktree / self.target).read_text(encoding="utf-8-sig")
        self.assertEqual(text, self.base_text.replace("old label", "operator label")
                         .replace("old reference behavior", "new reference behavior"))
        op = self.record["reference_refresh_history"][0]
        preserved = workflow._verified_target_snapshot(self.run, op["adoption"]["backup"])
        self.assertIn("operator label", preserved["Page.cs"].read_text(encoding="utf-8-sig"))
        self.assertEqual(self.record["completed_capsules"], 1)
        self.assertNotIn("checker_exit_code", self.record)
        self.assertEqual((self.agent / self.ref).read_text(encoding="utf-8-sig"), "new reference")
        self.assertEqual((self.capsule / "target-context/files" / self.ref).read_text(encoding="utf-8-sig"),
                         "old reference")

    def test_overlap_keeps_csharp_unchanged_and_shows_proposed_diff(self):
        self.put(self.ref, "new reference")
        hand = self.base_text.replace("old reference behavior", "operator behavior")
        self.put(self.target, hand)
        with patch.object(workflow, "_propose_reference_refresh", side_effect=self.propose):
            with self.assertRaisesRegex(ValueError, "手修正と競合.*Page.cs"):
                self.refresh()
        self.assertEqual((self.worktree / self.target).read_text(encoding="utf-8-sig"), hand)
        self.assertIn("+new reference behavior",
                      (self.run / "reference-refresh-proposal/変更差分.diff").read_text(encoding="utf-8"))
        self.assertIn("reference_refresh", self.record)
        self.assertNotIn("checker_exit_code", self.record)

    def test_overlap_already_recorded_in_a_checkpoint_is_protected(self):
        self.put(self.ref, "new reference")
        with workflow.editor_queue(self.run) as queue:
            workflow.record_operator_edit(queue, "Page.cs", "previous operator wording\n", "old reference behavior\n")
        with patch.object(workflow, "_propose_reference_refresh", side_effect=self.propose):
            with self.assertRaisesRegex(ValueError, "手修正と競合.*Page.cs"):
                self.refresh()
        self.assertEqual((self.worktree / self.target).read_text(encoding="utf-8-sig"), self.base_text)

    def test_stopped_draft_is_retained_but_restarted_from_saved_output(self):
        before = self.record["target_hashes"].copy()
        self.put(self.target, "unfinished old-reference draft", root=self.agent)
        draft = workflow._save_target_snapshot(
            self.run, {"Page.cs": self.agent / self.target}, before, workspace_unchanged=True,
        )
        self.record["stopped_migration_draft"] = draft
        self.put(self.ref, "new reference")
        with patch.object(workflow, "_propose_reference_refresh", side_effect=self.propose):
            self.refresh()
        history = self.record["reference_refresh_history"][0]
        self.assertEqual(history["archived_draft"], draft)
        self.assertIn("unfinished", workflow._verified_target_snapshot(self.run, draft)["Page.cs"].read_text(
            encoding="utf-8-sig"))
        self.assertNotIn("stopped_migration_draft", self.record)
        self.assertIn("new reference behavior", (self.worktree / self.target).read_text(encoding="utf-8-sig"))

    def test_proposal_reads_both_reference_versions_and_uses_normal_completion(self):
        self.put(self.ref, "new reference")
        self.put(self.target, self.base_text.replace("old label", "operator label"))

        def run_model(config, record_path, record, phase, guidelines, root, inputs,
                      command, completion, token, paths, **kwargs):
            self.assertEqual(phase, "reference_refresh")
            self.assertEqual((root / self.ref).read_text(encoding="utf-8-sig"), "new reference")
            previous = root / ".migration-materials/previous-references" / self.ref
            self.assertEqual(previous.read_text(encoding="utf-8-sig"), "old reference")
            human = root / ".migration-materials/current-targets" / self.target
            self.assertIn("operator label", human.read_text(encoding="utf-8-sig"))
            paths[0].write_text(self.base_text.replace("old reference behavior", "new reference behavior"),
                                encoding="utf-8-sig")
            completion.write_text(token, encoding="utf-8")

        with patch.object(workflow, "load_capsule_manifest", return_value=(self.capsule, {"parts": [], "rule_files": []})), \
             patch.object(workflow.junie_cli, "build_command", return_value=[]), \
             patch.object(workflow, "_run_budgeted_junie", side_effect=run_model) as model:
            workflow._refresh_target_context_on_resume({"junie_max_steps": 10}, {}, self.run, self.worktree, self.record,
                                                       migration_complete=False)
        model.assert_called_once()
        self.assertIn("operator label", (self.worktree / self.target).read_text(encoding="utf-8-sig"))

    def test_addition_deletion_and_move_are_compared_under_original_policy(self):
        moved = Path("refs/Moved.cs")
        (self.worktree / self.ref).rename(self.worktree / moved)
        self.put(Path("Other.cs"), "not a reference")
        with patch.object(workflow, "_propose_reference_refresh", side_effect=self.propose):
            self.refresh()
        self.assertFalse((self.agent / self.ref).exists())
        self.assertTrue((self.agent / moved).is_file())
        self.assertFalse((self.agent / "Other.cs").exists())
        self.assertEqual(self.record["reference_refresh_history"][0]["changes"],
                         {"refs/Example.cs": "削除", "refs/Moved.cs": "追加"})

    def test_completed_migration_revalidates_after_reference_update(self):
        self.put(self.ref, "new reference")
        self.record["status"] = "checker_succeeded"
        result = {"run_id": "example"}
        with patch.object(workflow, "load_saved_run", return_value=(self.run, self.worktree, self.record, {})), \
             patch.object(workflow, "_load_resume_context", return_value=(result, True, False, False)), \
             patch.object(workflow, "can_resume_run", return_value=True), \
             patch.object(workflow, "_propose_reference_refresh", side_effect=self.propose), \
             patch.object(workflow, "_verify_worktree", return_value={"status": "checker_succeeded"}) as verify:
            result = workflow._resume_with_junie({"checker_command": ["checker"]}, "example")
        verify.assert_called_once()
        self.assertNotIn("checker_exit_code", verify.call_args.args[-1])
        self.assertTrue(verify.call_args.args[-1]["checker_required"])
        self.assertEqual(result["status"], "checker_succeeded")
