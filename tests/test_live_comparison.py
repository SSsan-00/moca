import json

from fixtures import HarnessCase
from migration_harness import workflow
from migration_harness.diff_viewer import capture_workspace, create_snapshot, snapshot_bytes


class LiveComparisonTests(HarnessCase):
    def test_live_draft_is_captured_without_changing_worktree_or_anchor_inputs(self):
        (self.project / "source.php").write_text("<?php echo 42;\n", encoding="utf-8")
        (self.project / "Page.cs").write_text("class Original {}", encoding="utf-8")
        self.commit_project()
        config = self.config()
        result = workflow.prepare_run(config, "sample")
        run_path, worktree, record, _ = workflow._load_saved_run(config, result["run_id"])
        agent = run_path / "agent-workspace"
        agent.mkdir()
        (agent / "Page.cs").write_text("class SavedDraft {}", encoding="utf-8")
        record["status"] = "junie_running"
        workflow._write_run_record(run_path / "run.json", record)
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
        workflow._write_run_record(run_path / "run.json", record)
        _, finished = capture_workspace(config, result["run_id"], include_draft=True)
        self.assertEqual(finished["workspace"]["rightText"], "class Original {}")

    def test_source_can_be_viewed_before_target_is_created(self):
        (self.project / "source.php").write_text("<?php echo 42;\n", encoding="utf-8")
        self.commit_project()
        config = self.config()
        result = workflow.prepare_run(config, "sample")
        _, payload = capture_workspace(config, result["run_id"], include_draft=True)
        self.assertIn("42", payload["workspace"]["leftText"])
        self.assertEqual(payload["workspace"]["rightText"], "")
        self.assertEqual(payload["snapshot"]["missingFiles"], ["Page.cs"])
