import codecs
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from migration_harness.runs.worktree_targets import apply_agent_targets, finish_pending_targets, target_hash
from test_run_state_models import ApplyModel, File, apply_actions


class RunStateModelTraceTests(unittest.TestCase):
    def test_apply_plan_survives_interruption_and_rejects_manual_edit(self):
        for external_edit in (False, True):
            with self.subTest(external_edit=external_edit), TemporaryDirectory() as folder:
                root = Path(folder)
                agent, worktree = root / "agent", root / "worktree"
                agent.mkdir()
                worktree.mkdir()
                targets = (Path("Page.cs"), Path("View.cshtml"))
                originals = {targets[0]: b"class Original {}", targets[1]: b"<p>original</p>"}
                drafts = {targets[0]: b"class Changed {}", targets[1]: b"<p>changed</p>"}
                for relative in targets:
                    (worktree / relative).write_bytes(originals[relative])
                    (agent / relative).write_bytes(codecs.BOM_UTF8 + drafts[relative])
                hashes = {relative.as_posix(): target_hash(worktree / relative) for relative in targets}
                record = {"target": targets[0].as_posix(), "target_hashes": dict(hashes)}
                record_path = root / "run.json"

                replace_file = Path.replace

                def interrupt_after_first(source, destination):
                    if destination == worktree / targets[1]:
                        raise OSError("interrupted adoption")
                    return replace_file(source, destination)

                with patch.object(Path, "replace", interrupt_after_first):
                    with self.assertRaisesRegex(OSError, "interrupted adoption"):
                        apply_agent_targets(agent, worktree, targets, hashes, record, record_path)

                saved = json.loads(record_path.read_bytes())
                after_first = dict(apply_actions(ApplyModel()))["save pending plan"]
                after_first = dict(apply_actions(after_first))["write 0"]
                self.assertEqual(after_first.files, (File.DRAFT, File.BASE))
                self.assertEqual(bool(saved.get("target_apply_pending")), after_first.pending)
                self.assertFalse(saved.get("target_applied"))
                self.assertEqual((worktree / targets[0]).read_bytes(), codecs.BOM_UTF8 + drafts[targets[0]])
                self.assertEqual((worktree / targets[1]).read_bytes(), originals[targets[1]])

                restarted = dict(apply_actions(after_first))["restart"]
                if external_edit:
                    manual = b"<p>hand edited</p>"
                    (worktree / targets[1]).write_bytes(manual)
                    conflict = dict(apply_actions(restarted))["external edit 1"]
                    self.assertNotIn("write 1", dict(apply_actions(conflict)))
                    self.assertNotIn("record adoption", dict(apply_actions(conflict)))
                    with self.assertRaisesRegex(ValueError, "external change"):
                        finish_pending_targets(agent, worktree, targets, saved, record_path)
                    self.assertEqual((worktree / targets[0]).read_bytes(), codecs.BOM_UTF8 + drafts[targets[0]])
                    self.assertEqual((worktree / targets[1]).read_bytes(), manual)
                    self.assertEqual(json.loads(record_path.read_bytes()), saved)
                else:
                    safely_finished = dict(apply_actions(restarted))["write 1"]
                    safely_finished = dict(apply_actions(safely_finished))["record adoption"]
                    finish_pending_targets(agent, worktree, targets, saved, record_path)
                    final = json.loads(record_path.read_bytes())
                    self.assertEqual(final.get("target_applied"), safely_finished.adopted)
                    self.assertNotIn("target_apply_pending", final)
                    for relative in targets:
                        self.assertEqual((worktree / relative).read_bytes(), codecs.BOM_UTF8 + drafts[relative])
                        self.assertEqual(final["target_hashes"][relative.as_posix()], target_hash(worktree / relative))
