from unittest.mock import patch

from tests.support.fixtures import SavedRunFixture
from migration_harness import execution, workflow
from migration_harness.runs.editor_queue import add_prompt, editor_queue


class VerificationFollowupTests(SavedRunFixture):
    def test_queued_edit_and_instruction_go_back_to_migration_before_any_build(self):
        unit = self.config["units"][0]
        result = {"run_path": str(self.run_path), "worktree_path": str(self.worktree)}
        record = self.record()
        before = self.target.read_text(encoding="utf-8")
        with editor_queue(self.run_path) as queue:
            queue["edits"][self.target.name] = {
                "id": "manual-edit", "base": before, "content": "class Edited {}\n", "status": "queued",
            }
            add_prompt(queue, {"text": "Keep the operator edit."})

        with (
            patch.object(execution, "run_process") as process,
            patch.object(workflow, "_run_prepared_with_junie", return_value={"status": "editor_checkpoint"}) as resume,
        ):
            outcome = workflow._verify_worktree(self.config, unit, result, record)

        self.assertEqual(outcome, {"status": "editor_checkpoint"})
        resume.assert_called_once_with(self.config, unit, self.run_path, result, record, resuming=True, followup_only=True)
        process.assert_not_called()
        self.assertEqual(self.target.read_text(encoding="utf-8"), before)
        with editor_queue(self.run_path) as queue:
            self.assertEqual(queue["prompts"][0]["status"], "queued")
            self.assertIn(self.target.name, queue["edits"])
