import codecs
import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from fixtures import SavedRunFixture
from migration_harness.context import source_context
from migration_harness import workflow
from migration_harness.web import dashboard
from migration_harness.runs.worktree_targets import target_hash


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

    def test_reference_refresh_does_not_supply_manually_excluded_target_rule(self):
        materials = self.capsule / "materials"
        materials.mkdir()
        name = "rule-0001.txt"
        marker = "Exclude this Page.cs rule from the reference refresh."
        (materials / name).write_text(marker, encoding="utf-8")
        entry = {"file": name, "path": "Rules/Target.md", "usage": "reference",
                 "description": "For Page.cs", "sha256": target_hash(materials / name)}
        (materials / "rules.json").write_text(json.dumps([entry]), encoding="utf-8")
        manifest = {"rule_files": [entry], "materials": {
            item: target_hash(materials / item) for item in (name, "rules.json")}}
        self.record["detail_overrides"] = {name: {"applies": False}}
        self.put(self.ref, "updated reference")
        pending = {"previous": "target-context", "changes": {self.ref.as_posix(): "changed"}}

        def finish(_config, _record_path, _record, _stage, guidelines, proposal, _inputs,
                   _command, completion, token, _targets, **_kwargs):
            self.assertFalse((proposal / ".migration-materials" / name).exists())
            if guidelines is not None:
                self.assertNotIn(marker, guidelines.read_text(encoding="utf-8"))
            completion.write_text(token, encoding="utf-8")

        with (patch.object(workflow, "_review_detail_applicability", return_value=frozenset()),
              patch.object(workflow, "_stage_source_excerpts", return_value=[]),
              patch.object(workflow.junie_cli, "build_command", return_value=[]),
              patch.object(workflow, "_run_budgeted_junie", side_effect=finish) as launch):
            workflow._propose_reference_refresh(
                {"junie_max_steps": 10}, self.run, self.capsule, manifest, self.worktree, self.targets,
                self.record, pending, {self.target.as_posix(): self.agent / self.target})
        launch.assert_called_once()

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

    def test_later_reference_change_checks_conflict_after_previous_merge_kept_hand_edit(self):
        self.put(self.ref, "revision B")
        self.put(self.target, self.base_text.replace("old label", "operator label"))
        with patch.object(workflow, "_propose_reference_refresh", side_effect=self.propose):
            self.refresh()
        merged = (self.worktree / self.target).read_text(encoding="utf-8-sig")
        self.put(self.target, merged.replace("new reference behavior", "operator behavior"))
        before = (self.worktree / self.target).read_bytes()
        self.put(self.ref, "revision C")

        def propose_again(*_):
            proposal = self.run / "reference-refresh-C"
            pure = self.base_text.replace("old reference behavior", "revision C")
            self.put(self.target, pure, root=proposal)
            return proposal

        with patch.object(workflow, "_propose_reference_refresh", side_effect=propose_again) as model:
            with self.assertRaisesRegex(ValueError, "手修正と競合"):
                self.refresh()
        model.assert_called_once()
        self.assertEqual((self.worktree / self.target).read_bytes(), before)

    def test_unrecorded_junie_draft_is_still_not_overwritten(self):
        self.put(self.ref, "revision B")
        self.put(self.target, "unsaved Junie draft", root=self.agent)
        with patch.object(workflow, "_propose_reference_refresh") as model:
            with self.assertRaisesRegex(ValueError, "保存されていない Junie"):
                self.refresh()
        model.assert_not_called()
        self.assertEqual((self.agent / self.target).read_text(encoding="utf-8-sig"), "unsaved Junie draft")
        self.assertEqual((self.worktree / self.target).read_text(encoding="utf-8-sig"), self.base_text)

    def test_resume_replaces_conflicted_reference_revision_with_latest(self):
        self.put(self.ref, "revision B")
        self.put(self.target, self.base_text.replace("old reference behavior", "operator behavior"))
        observed = []

        def propose(*_):
            latest = source_context.target_context_snapshot(self.capsule) / "files" / self.ref
            revision = latest.read_text(encoding="utf-8-sig")
            observed.append(revision)
            output = self.run / f"reference-refresh-{len(observed)}"
            self.put(self.target, self.base_text.replace("old reference behavior", revision), root=output)
            return output

        with patch.object(workflow, "_propose_reference_refresh", side_effect=propose):
            with self.assertRaisesRegex(ValueError, "手修正と競合"):
                self.refresh()
            previous = self.record["reference_refresh"]["snapshot"]
            self.put(self.ref, "revision C")
            self.put(self.target, self.base_text.replace("old label", "operator label"))
            self.refresh()
        self.assertEqual(observed, ["revision B", "revision C"])
        self.assertIn("revision C", (self.worktree / self.target).read_text(encoding="utf-8-sig"))
        self.assertIn("operator label", (self.worktree / self.target).read_text(encoding="utf-8-sig"))
        self.assertIn(previous, self.record["reference_refresh_history"][0]["superseded_snapshots"])
        self.assertNotIn("checker_exit_code", self.record)

    def test_reverting_conflicted_reference_reuses_original_without_overwriting_csharp(self):
        self.put(self.ref, "revision B")
        changed = self.base_text.replace("old reference behavior", "operator behavior")
        self.put(self.target, changed)
        with patch.object(workflow, "_propose_reference_refresh", side_effect=self.propose) as propose:
            with self.assertRaisesRegex(ValueError, "手修正と競合"):
                self.refresh()
            self.put(self.ref, "old reference")
            self.refresh()
        self.assertEqual(propose.call_count, 1)
        self.assertEqual(source_context.target_context_snapshot(self.capsule).name, "target-context")
        self.assertEqual((self.worktree / self.target).read_text(encoding="utf-8-sig"), changed)
        self.assertNotIn("checker_exit_code", self.record)

    def test_recovery_finishes_durable_adoption_and_checks_latest_references(self):
        original_apply = workflow.apply_agent_targets
        observed = []

        def propose(*_):
            latest = source_context.target_context_snapshot(self.capsule) / "files" / self.ref
            revision = latest.read_text(encoding="utf-8-sig")
            observed.append(revision)
            output = self.run / f"reference-refresh-{len(observed)}"
            self.put(self.target, self.base_text.replace("old reference behavior", revision), root=output)
            return output

        self.put(self.ref, "revision B")
        with patch.object(workflow, "_propose_reference_refresh", side_effect=propose):
            with patch.object(workflow, "apply_agent_targets", side_effect=OSError("simulated interruption")):
                with self.assertRaisesRegex(OSError, "simulated interruption"):
                    self.refresh()
            self.assertIn("adoption", self.record["reference_refresh"])
            self.put(self.ref, "revision C")
            with patch.object(workflow, "apply_agent_targets", wraps=original_apply) as apply:
                self.refresh()
            self.assertEqual(apply.call_count, 2)
        self.assertEqual(observed, ["revision B", "revision C"])
        self.assertIn("revision C", (self.worktree / self.target).read_text(encoding="utf-8-sig"))
        self.assertEqual(len(self.record["reference_refresh_history"]), 2)

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


class ReferenceConflictEditorTests(SavedRunFixture):
    def saved_targets(self, *, complete=False, multi=False, stopped=False):
        self.targets = (Path("Example.cs"), Path("Second.cs")) if multi else (Path("Example.cs"),)
        self.config["units"][0]["targets"] = [path.as_posix() for path in self.targets]
        self.agent = self.run_path / "agent-workspace"
        self.agent.mkdir()
        self.capsule = self.run_path / "capsule"
        self.reference = self.worktree / "refs" / "Library.cs"
        self.reference.parent.mkdir()
        self.reference.write_text("reference A", encoding="utf-8")
        source_context.stage_target_context(self.worktree, self.targets, self.capsule, self.agent,
                                           policy={"roots": [], "folders": ["refs"]})
        self.base = "start\nreference A\n1\n2\n3\n4\nold label\nend\n"
        self.human = self.base.replace("reference A", "manual behavior").replace("old label", "keep this hand edit")
        self.resolved = self.human.replace("manual behavior", "resolved for reference B")
        for index, path in enumerate(self.targets):
            text = self.base if not index else "second baseline\n"
            for root in (self.worktree, self.agent):
                (root / path).write_bytes(text.encode("utf-8-sig"))
        hashes = {path.as_posix(): target_hash(self.worktree / path) for path in self.targets}
        record = self.record()
        record.update(targets=list(hashes), target_hashes=hashes, capsule_checkpoints={"1": workflow._save_capsule_checkpoint(
            self.run_path, self.worktree, self.targets, hashes,
        )})
        if not complete:
            manifest_path = self.capsule / "manifest.json"
            manifest = json.loads(manifest_path.read_bytes())
            second = self.capsule / "second.txt"
            second.write_bytes(b"second capsule")
            manifest["parts"].append({"index": 2, "path": str(second), "chars": 14})
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            record.update(status="interrupted", current_capsule=2, context_capsules=2)
        (self.worktree / self.targets[0]).write_bytes(self.human.encode("utf-8-sig"))
        with workflow.editor_queue(self.run_path) as queue:
            workflow.record_operator_edit(queue, "Example.cs", self.base, self.human)
        if stopped:
            (self.agent / self.targets[0]).write_bytes(b"unfinished migration draft")
            record["stopped_migration_draft"] = workflow._save_target_snapshot(
                self.run_path, {path.as_posix(): self.agent / path for path in self.targets},
                {path.as_posix(): target_hash(self.worktree / path) for path in self.targets},
                workspace_unchanged=True,
            )
        self.save_record(record)
        self.reference.write_text("reference B", encoding="utf-8")

    def propose(self, *_):
        proposal = self.run_path / "fixture-proposal"
        proposal.mkdir(exist_ok=True)
        (proposal / self.targets[0]).write_text(self.base.replace("reference A", "model choice"), encoding="utf-8-sig")
        if len(self.targets) > 1:
            (proposal / self.targets[1]).write_text("second follows reference B\n", encoding="utf-8-sig")
        return proposal

    def open_editor(self):
        view = dashboard.Dashboard()
        view.observe(self.record())
        view.ready(self.config, self.record())
        output = io.StringIO()
        with redirect_stdout(output):
            server = view.server(self.config, b"", b"")
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)
        url = output.getvalue().splitlines()[0].removeprefix("Progress dashboard: ")
        parsed = urlsplit(url)

        def editor(contents=None):
            path = url + "editor"
            if contents is None:
                path += "?" + urlencode({"run_id": self.run_id, "target": "Example.cs"})
            data = None if contents is None else json.dumps(contents).encode("utf-8")
            headers = {"Content-Type": "application/json", "Origin": f"{parsed.scheme}://{parsed.netloc}"}
            with urlopen(Request(path, data, headers), timeout=5) as reply:
                return reply.status, json.load(reply)

        return editor

    def save_resolution(self):
        editor = self.open_editor()
        _, before = editor()
        status, after = editor({"kind": "save", "run_id": self.run_id, "target": "Example.cs",
                                "base": before["content"], "content": self.resolved,
                                "edit_id": (before.get("edit") or {}).get("id")})
        self.assertEqual(status, 200)
        self.assertEqual(after["save_timing"], "resume")
        self.assertTrue(after["queued"])
        self.assertEqual(after["edit"]["content"], self.resolved)
        return after

    def resume_fixture(self):
        return workflow.resume_with_junie(self.config, self.run_id)

    def test_http_resolution_continues_partial_run_and_preserves_old_reference_and_stopped_draft(self):
        self.saved_targets(stopped=True)
        stopped = self.record()["stopped_migration_draft"]

        def migrate(command, completion, token, targets, **kwargs):
            self.assertIn("context segment 2 of 2", command[-1])
            self.assertEqual(targets[0].read_text(encoding="utf-8-sig"), self.resolved)
            completion.write_text(token, encoding="utf-8")
            return 0

        with (patch.object(workflow, "_propose_reference_refresh", side_effect=self.propose) as model,
              patch.object(workflow.junie_cli, "require_interactive_cli"),
              patch.object(workflow.junie_cli, "run_interactive", side_effect=migrate) as continuation,
              patch.object(workflow, "_verify_worktree", side_effect=lambda config, unit, result, record: result)):
            for _ in range(2):
                with self.assertRaisesRegex(ValueError, "手修正と競合"):
                    self.resume_fixture()
            self.assertEqual(self.target.read_text(encoding="utf-8-sig"), self.human)
            self.save_resolution()
            self.assertEqual(self.resume_fixture()["completed_capsules"], 2)
        model.assert_called_once()
        continuation.assert_called_once()
        history = self.record()["reference_refresh_history"][0]
        old = workflow._verified_target_snapshot(self.run_path, history["adoption"]["backup"])
        self.assertEqual(old["Example.cs"].read_text(encoding="utf-8-sig"), self.human)
        self.assertEqual(history["archived_draft"], stopped)
        self.assertEqual(workflow._verified_target_snapshot(self.run_path, stopped)["Example.cs"].read_bytes(),
                         b"unfinished migration draft")
        self.assertEqual((self.capsule / "target-context/files/refs/Library.cs").read_text(encoding="utf-8"), "reference A")
        self.assertEqual((self.agent / "refs/Library.cs").read_text(encoding="utf-8"), "reference B")
        self.assertEqual(self.target.read_text(encoding="utf-8-sig"), self.resolved)
        self.assertEqual((self.agent / "Example.cs").read_text(encoding="utf-8-sig"), self.resolved)
        with workflow.editor_queue(self.run_path) as queue:
            self.assertEqual(queue["edits"], {})
            self.assertEqual(len(queue["operator_edits"]["Example.cs"]), 2)

    def test_completed_multiple_targets_resume_after_failed_adoption_without_repeating_model(self):
        self.saved_targets(complete=True, multi=True)
        with (patch.object(workflow, "_propose_reference_refresh", side_effect=self.propose) as model,
              patch.object(workflow, "_verify_worktree", return_value={"status": "verified"}) as verify):
            with self.assertRaisesRegex(ValueError, "手修正と競合"):
                self.resume_fixture()
            self.save_resolution()
            with patch.object(workflow, "apply_agent_targets", side_effect=OSError("simulated failure")):
                with self.assertRaisesRegex(OSError, "simulated failure"):
                    self.resume_fixture()
            self.assertEqual(self.target.read_text(encoding="utf-8-sig"), self.human)
            self.assertIn("adoption", self.record()["reference_refresh"])
            self.reference.write_text("reference C", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "反映を再開する前に参考資料が更新"):
                self.resume_fixture()
            self.assertEqual(self.target.read_text(encoding="utf-8-sig"), self.human)
            self.reference.write_text("reference B", encoding="utf-8")
            self.assertEqual(self.resume_fixture()["status"], "verified")
        model.assert_called_once()
        verify.assert_called_once()
        self.assertEqual(self.target.read_text(encoding="utf-8-sig"), self.resolved)
        self.assertEqual((self.worktree / "Second.cs").read_text(encoding="utf-8-sig"), "second follows reference B\n")

    def test_unrelated_pending_instruction_and_edit_stay_queued(self):
        self.saved_targets()
        with patch.object(workflow, "_propose_reference_refresh", side_effect=self.propose) as model:
            with self.assertRaisesRegex(ValueError, "手修正と競合"):
                self.resume_fixture()
            saved = self.save_resolution()
            with workflow.editor_queue(self.run_path) as queue:
                queue["prompts"].append({"id": "fixture", "text": "Check an unrelated item", "status": "queued"})
            with self.assertRaisesRegex(ValueError, "未送信の指示"):
                self.resume_fixture()
            with workflow.editor_queue(self.run_path) as queue:
                self.assertEqual(queue["edits"]["Example.cs"], saved["edit"])
                self.assertEqual(queue["prompts"], [{"id": "fixture", "text": "Check an unrelated item", "status": "queued"}])
        model.assert_called_once()
        self.assertEqual(self.target.read_text(encoding="utf-8-sig"), self.human)

    def test_reference_changed_after_staging_is_checked_before_adoption(self):
        self.saved_targets(complete=True)
        stage = workflow._record_reference_conflict_resolution

        def stage_then_replace_reference(*args):
            stage(*args)
            self.reference.write_text("reference C", encoding="utf-8")

        with (patch.object(workflow, "_propose_reference_refresh", side_effect=self.propose) as model,
              patch.object(workflow, "_verify_worktree") as verify):
            with self.assertRaisesRegex(ValueError, "手修正と競合"):
                self.resume_fixture()
            saved = self.save_resolution()
            with (patch.object(workflow, "_record_reference_conflict_resolution", side_effect=stage_then_replace_reference),
                  self.assertRaisesRegex(ValueError, "反映を再開する前に参考資料が更新")):
                self.resume_fixture()
            self.assertEqual(self.target.read_text(encoding="utf-8-sig"), self.human)
            with workflow.editor_queue(self.run_path) as queue:
                self.assertEqual(queue["edits"]["Example.cs"], saved["edit"])
            model.assert_called_once()
            verify.assert_not_called()

    def test_editor_save_error_and_request_for_another_run_do_not_adopt(self):
        self.saved_targets()
        with patch.object(workflow, "_propose_reference_refresh", side_effect=self.propose) as model:
            with self.assertRaisesRegex(ValueError, "手修正と競合"):
                self.resume_fixture()
            editor = self.open_editor()
            _, before = editor()
            action = {"kind": "save", "run_id": self.run_id, "target": "Example.cs",
                      "base": before["content"], "content": self.resolved, "edit_id": None}
            with self.assertRaises(HTTPError) as wrong_run:
                editor({**action, "run_id": "00000000-0000-0000-0000-000000000000"})
            wrong_run.exception.close()
            with (patch("migration_harness.runs.editor_queue.replace_run_record", side_effect=OSError("disk failure")),
                  self.assertRaises(HTTPError) as failed):
                editor(action)
            with failed.exception as reply:
                self.assertEqual(reply.code, 409)
                self.assertIn("disk failure", reply.read().decode("utf-8"))
            self.assertIsNone(editor()[1]["edit"])
            with self.assertRaisesRegex(ValueError, "手修正と競合"):
                self.resume_fixture()
        model.assert_called_once()
        self.assertEqual(self.target.read_text(encoding="utf-8-sig"), self.human)

    def test_new_reference_or_target_version_leaves_saved_resolution_untouched(self):
        self.saved_targets()
        with patch.object(workflow, "_propose_reference_refresh", side_effect=self.propose) as model:
            with self.assertRaisesRegex(ValueError, "手修正と競合"):
                self.resume_fixture()
            saved = self.save_resolution()
            for path in (self.reference, self.target, self.agent / "Example.cs"):
                with self.subTest(path=path):
                    before = path.read_bytes()
                    path.write_bytes(b"different version")
                    with self.assertRaisesRegex(ValueError, "版が競合時と異なります"):
                        self.resume_fixture()
                    self.assertEqual(path.read_bytes(), b"different version")
                    with workflow.editor_queue(self.run_path) as queue:
                        self.assertEqual(queue["edits"]["Example.cs"], saved["edit"])
                    path.write_bytes(before)
        model.assert_called_once()
