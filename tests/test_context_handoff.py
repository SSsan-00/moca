import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fixtures import HarnessCase
from migration_harness import workflow

from migration_harness.capsules import prepare_context
from migration_harness.context_handoff import context_handoff, handoff_prompt, record_handoff


class ContextHandoffTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.run = self.root / "run"
        (self.root / "demo.php").write_text("<?php\necho 42;\n", encoding="utf-8")
        self.target = self.root / "Demo.cs"
        self.target.write_text("class Demo {}", encoding="utf-8")
        self.targets = (Path("Demo.cs"),)
        prepare_context({"project_root": str(self.root), "max_context_chars": 1_000}, {
            "id": "unit", "target": "Demo.cs", "inputs": [{
                "role": "php", "path": "demo.php", "start_line": 1, "end_line": None,
                "encoding": "utf-8", "shared": False,
            }],
        }, self.run)
        self.record = self.run / "run.json"
        self.record.write_text('{"completed_capsules":0, "verification_passed":false}', encoding="utf-8")

    def add(self, note="check failed-state return", supersedes=None):
        return record_handoff(self.run, self.root, self.targets, {},
                              "php-0001", 2, 2, note, supersedes=supersedes)

    def test_replay_and_correction_keep_history_and_never_change_progress(self):
        old = self.add()
        self.assertEqual(self.add(), old)
        replacement = self.add("check both return paths", supersedes=old["id"])
        restored = context_handoff(self.run, self.root, self.targets, {})
        self.assertEqual([replacement["id"], old["id"]], [row["id"] for row in restored])
        self.assertEqual(["pending", "superseded"], [row["state"] for row in restored])
        prompt = handoff_prompt(self.run, self.root, self.targets, {}, through=1)
        self.assertIn("check both return paths", prompt)
        self.assertNotIn("check failed-state return", prompt)
        self.assertEqual({"completed_capsules": 0, "verification_passed": False}, json.loads(self.record.read_text()))

    def test_source_target_and_rule_changes_disable_hint(self):
        self.add()
        self.assertEqual("", handoff_prompt(self.run, self.root, self.targets, {
            "detail_overrides": {"detail": {"applies": False}},
        }))
        self.target.write_text("class Edited {}", encoding="utf-8")
        self.assertEqual("", handoff_prompt(self.run, self.root, self.targets, {}))
        self.target.write_text("class Demo {}", encoding="utf-8")
        (self.run / "capsule/php-sources/0001.php").write_text("corrupt", encoding="utf-8")
        self.assertEqual("", handoff_prompt(self.run, self.root, self.targets, {}))

    def test_missing_and_corrupt_legacy_handoff_does_not_prevent_work(self):
        self.assertEqual("", handoff_prompt(self.run, self.root, self.targets, {}))
        (self.run / "context-handoff.json").write_text("invalid", encoding="utf-8")
        self.assertIn("unreadable", handoff_prompt(self.run, self.root, self.targets, {}))

    def test_junie_memos_are_versioned_bounded_and_do_not_supersede_operator(self):
        from migration_harness.context_handoff import junie_memo_request, save_junie_memo
        from migration_harness.worktree_targets import target_hash

        record = {"completed_capsules": 0, "target_hashes": {"Demo.cs": target_hash(self.target)}}
        draft, _ = junie_memo_request(self.root, "fixture")
        operator = self.add("operator lead")
        note = {"kind": "pending", "source_path": "demo.php", "start_line": 2, "end_line": 2,
                "note": "check the branch before finalizing"}
        draft.write_text(json.dumps([{**note, "supersedes": operator["id"]}]), encoding="utf-8")
        save_junie_memo(draft, self.run, record, phase="migration", through=1)
        self.assertIn("junie_memo_warning", record)
        self.assertNotIn("junie_memos", record)
        draft.write_text(json.dumps([note]), encoding="utf-8")
        save_junie_memo(draft, self.run, record, phase="migration", through=1)
        save_junie_memo(draft, self.run, record, phase="migration", through=1)
        reopened = json.loads(json.dumps(record))
        self.assertEqual(len(reopened["junie_memos"]), 1)
        prompt = handoff_prompt(self.run, self.root, self.targets, reopened)
        self.assertIn("operator lead", prompt)
        self.assertIn(note["note"], prompt)
        reopened["detail_applicability"] = {"example": {"applies": True}}
        prompt = handoff_prompt(self.run, self.root, self.targets, reopened)
        self.assertNotIn(note["note"], prompt)
        self.assertIn("Earlier unresolved items whose versions differ", prompt)
        self.target.write_text("class HumanEdited {}", encoding="utf-8")
        prompt = handoff_prompt(self.run, self.root, self.targets, record)
        self.assertNotIn(note["note"], prompt)
        self.assertIn(record["junie_memos"][0]["id"], prompt)
        draft.write_text(json.dumps([{**note, "end_line": 999}]), encoding="utf-8")
        save_junie_memo(draft, self.run, record, phase="repair")
        self.assertIn("junie_memo_warning", record)
        self.assertEqual(len(record["junie_memos"]), 1)
        self.assertEqual(record["completed_capsules"], 0)


class JunieMemoLifecycleTests(HarnessCase):
    def test_capsule_seam_passes_adopted_memo_but_does_not_save_unfinished_output(self):
        source = self.project / "source.php"
        source.write_text("<?php\nfunction calculate() {\n" + "echo 1;" + " " * 75 + "\n"
                          + ("echo 2;" + " " * 75 + "\n") * 27 + "}\n", encoding="utf-8")
        self.commit_project()
        config = self.config()
        config["units"][0]["inputs"][0]["end_line"] = None
        prompts = []

        def junie(command, completion, token, targets, **kwargs):
            prompts.append(command[-1])
            memo = completion.parent / f"handoff-{token}.json"
            memo.write_text(json.dumps([{
                "kind": "pending", "note": f"pending review {len(prompts)}", "source_id": "php-0001",
                "start_line": 2, "end_line": 2,
            }]), encoding="utf-8")
            targets[0].write_text(f"class Provisional{len(prompts)} {{}}", encoding="utf-8")
            if len(prompts) == 1:
                completion.write_text(token, encoding="utf-8")
            return 0

        with (patch.object(workflow.junie_cli, "require_interactive_cli"),
              patch.object(workflow.junie_cli, "run_interactive", side_effect=junie), redirect_stdout(io.StringIO())):
            workflow.run_with_junie(config, "sample")
        self.assertEqual(len(prompts), 2)
        self.assertIn("pending review 1", prompts[1])
        completed, = Path(config["run_root"]).glob("*/run.json")
        saved = json.loads(completed.read_bytes())
        self.assertEqual(saved["completed_capsules"], 1)
        self.assertEqual([row["note"] for row in saved["junie_memos"]], ["pending review 1"])
