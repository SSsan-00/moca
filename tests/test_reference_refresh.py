import codecs
import io
import json
import unittest
from contextlib import contextmanager, redirect_stdout
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
    def saved_targets(self, *, complete=False, multi=False, stopped=False, second_base="second baseline\n"):
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
            text = self.base if not index else second_base
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

    def open_editor(self, target="Example.cs"):
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
                path += "?" + urlencode({"run_id": self.run_id, "target": target})
            data = None if contents is None else json.dumps(contents).encode("utf-8")
            headers = {"Content-Type": "application/json", "Origin": f"{parsed.scheme}://{parsed.netloc}"}
            with urlopen(Request(path, data, headers), timeout=5) as reply:
                return reply.status, json.load(reply)

        return editor

    def save_resolution(self):
        return self.save_editor("Example.cs", self.resolved)

    def save_editor(self, target, content, *, resolve=False):
        editor = self.open_editor(target)
        _, before = editor()
        status, after = editor({"kind": "save", "run_id": self.run_id, "target": target,
                                "base": before["content"], "content": content,
                                "edit_id": (before.get("edit") or {}).get("id"), "resolve": resolve})
        self.assertEqual(status, 200)
        self.assertEqual(after["save_timing"], "resume")
        self.assertTrue(after["queued"])
        self.assertEqual(after["edit"]["content"], content)
        return after

    def resume_fixture(self):
        return workflow.resume_with_junie(self.config, self.run_id)

    def reference_and_queued_edit(self, *, during_proposal, overlap, first_failure=None):
        self.saved_targets()
        self.target.write_bytes(self.base.encode("utf-8-sig"))
        with workflow.editor_queue(self.run_path) as queue:
            queue["operator_edits"].clear()
        human = self.base.replace("reference A", "operator behavior") if overlap else self.base.replace(
            "old label", "saved human label",
        )
        final = self.resolved.replace("keep this hand edit", "old label") if overlap else human.replace(
            "reference A", "model choice",
        )
        expected_reference = "reference C" if first_failure == "new reference" else "reference B"
        observed = []

        def propose(*args):
            pinned = source_context.target_context_snapshot(self.capsule) / "files" / "refs" / "Library.cs"
            observed.append(pinned.read_text(encoding="utf-8"))
            if during_proposal and len(observed) == 1:
                self.save_editor("Example.cs", human)
                if first_failure == "new reference":
                    self.reference.write_text(expected_reference, encoding="utf-8")
                elif first_failure == "stop proposal":
                    raise ValueError("simulated interruption after editor save")
            return self.propose(*args)

        def migrate(command, completion, token, targets, **kwargs):
            self.assertIn("context segment 2 of 2", command[-1])
            pinned = Path(targets[0]).parent / "refs" / "Library.cs"
            self.assertEqual(pinned.read_text(encoding="utf-8"), expected_reference)
            self.assertEqual(targets[0].read_text(encoding="utf-8-sig"), final)
            completion.write_text(token, encoding="utf-8")
            return 0

        if not during_proposal:
            self.save_editor("Example.cs", human)
        with (patch.object(workflow, "_propose_reference_refresh", side_effect=propose) as model,
              patch.object(workflow.junie_cli, "require_interactive_cli"),
              patch.object(workflow.junie_cli, "run_interactive", side_effect=migrate) as continuation,
              patch.object(workflow, "_verify_worktree", side_effect=lambda config, unit, result, record: result) as verify):
            if first_failure:
                pattern = "見直し中に参考資料が更新" if first_failure == "new reference" else "simulated interruption"
                with self.assertRaisesRegex(ValueError, pattern):
                    self.resume_fixture()
                self.assertEqual(self.target.read_text(encoding="utf-8-sig"), self.base)
                pending = self.record()["reference_refresh"]
                self.assertNotIn("adoption", pending)
                self.assertNotIn("conflict", pending)
                self.assertEqual(self.open_editor()()[1]["edit"]["content"], human)
                continuation.assert_not_called()
            if overlap:
                for _ in range(2):
                    with self.assertRaisesRegex(ValueError, "手修正と競合"):
                        self.resume_fixture()
                self.assertEqual(self.target.read_text(encoding="utf-8-sig"), self.base)
                _, view = self.open_editor()()
                self.assertEqual(view["edit"]["content"], human)
                self.assertEqual(view["edit"]["status"], "conflict")
                pending = self.record()["reference_refresh"]["conflict"]
                proposed = workflow._verified_target_snapshot(self.run_path, pending["generated"])
                self.assertIn("model choice", proposed["Example.cs"].read_text(encoding="utf-8-sig"))
                continuation.assert_not_called()
                self.save_editor("Example.cs", final, resolve=True)
            self.assertEqual(self.resume_fixture()["completed_capsules"], 2)
            self.resume_fixture()
        self.assertEqual(observed, ["reference B", expected_reference] if first_failure else ["reference B"])
        self.assertEqual(model.call_count, len(observed))
        continuation.assert_called_once()
        self.assertEqual(verify.call_count, 2)
        self.assertEqual(self.target.read_text(encoding="utf-8-sig"), final)
        self.assertEqual(len(self.record()["reference_refresh_history"]), 1)
        with workflow.editor_queue(self.run_path) as queue:
            self.assertEqual(queue["edits"], {})
            self.assertEqual(len(queue["operator_edits"]["Example.cs"]), 1)

    def test_edit_saved_before_resume_merges_using_latest_reference(self):
        self.reference_and_queued_edit(during_proposal=False, overlap=False)

    def test_edit_during_reference_proposal_merges_using_latest_reference(self):
        self.reference_and_queued_edit(during_proposal=True, overlap=False)

    def test_overlapping_saved_edit_before_resume_requires_explicit_resolution(self):
        self.reference_and_queued_edit(during_proposal=False, overlap=True)

    def test_overlapping_edit_during_reference_proposal_requires_explicit_resolution(self):
        self.reference_and_queued_edit(during_proposal=True, overlap=True)

    def test_pending_reference_refresh_recovers_after_interrupted_proposal(self):
        self.reference_and_queued_edit(during_proposal=True, overlap=False, first_failure="stop proposal")

    def test_newer_reference_invalidates_proposal_without_losing_concurrent_edit(self):
        self.reference_and_queued_edit(during_proposal=True, overlap=False, first_failure="new reference")

    def interrupted_adoption(self, *, fail_second=False, canonical_failure=False, complete_without_edit=False):
        second_base = "work copy\n1\n2\n3\nold second label\n"
        self.saved_targets(complete=complete_without_edit, multi=fail_second, second_base=second_base)
        self.target.write_bytes(self.base.encode("utf-8-sig"))
        with workflow.editor_queue(self.run_path) as queue:
            queue["operator_edits"].clear()
        first_edit = self.base
        if not complete_without_edit:
            first_edit = first_edit.replace("old label", "first human edit")
            self.save_editor("Example.cs", first_edit)
        if fail_second:
            self.save_editor("Second.cs", second_base.replace("old second label", "second-file human edit"))
        planned = first_edit.replace("reference A", "model choice")
        relative = self.targets[1] if fail_second else self.targets[0]
        failed_path = (self.agent if canonical_failure else self.worktree) / relative
        replace = Path.replace
        failures = []

        def replace_last(path, destination):
            if destination != failed_path:
                return replace(path, destination)
            saved = self.record()
            adoption = saved["reference_refresh"]["adoption"]
            name = relative.as_posix()
            self.assertEqual(target_hash(path), adoption["after"][name])
            if canonical_failure:
                self.assertNotIn("target_apply_pending", saved)
                self.assertEqual(target_hash(destination), adoption["draft"][name])
            else:
                self.assertEqual(saved["target_apply_pending"][name],
                                 {"before": adoption["before"][name], "after": adoption["after"][name]})
                self.assertEqual(target_hash(destination), adoption["before"][name])
            failures.append(name)
            raise PermissionError("fixture sharing violation")

        with (patch.object(Path, "replace", new=replace_last),
              self.assertRaisesRegex(PermissionError, "fixture sharing violation")):
            self.resume_fixture()
        self.assertEqual(failures, [relative.as_posix()])
        self.assertEqual((self.agent / "Example.cs").read_text(encoding="utf-8-sig"), planned)
        before_first = self.base if canonical_failure or not fail_second else planned
        self.assertEqual(self.target.read_text(encoding="utf-8-sig"), before_first)
        return planned

    def check_resuming_interrupted_adoption(self, *, resave=False, fail_second=False, canonical_failure=False,
                                            legacy=False):
        observed = {}

        def migrate(command, completion, token, targets, **kwargs):
            self.assertIn("context segment 2 of 2", command[-1])
            self.assertEqual((self.agent / "refs/Library.cs").read_text(encoding="utf-8"), "reference B")
            observed.update((p.name, p.read_text(encoding="utf-8-sig")) for p in targets)
            completion.write_text(token, encoding="utf-8")
            return 0

        def propose(*args):
            proposal = self.propose(*args)
            if fail_second:
                (proposal / "Second.cs").write_text("followed reference B\n1\n2\n3\nold second label\n",
                                                    encoding="utf-8-sig")
            return proposal

        with (patch.object(workflow, "_propose_reference_refresh", side_effect=propose) as model,
              patch.object(workflow.junie_cli, "require_interactive_cli"),
              patch.object(workflow.junie_cli, "run_interactive", side_effect=migrate) as continuation,
              patch.object(workflow, "_verify_worktree", side_effect=lambda config, unit, result, record: result) as verify):
            planned = self.interrupted_adoption(fail_second=fail_second, canonical_failure=canonical_failure)
            expected = planned
            if legacy:
                record = self.record()
                record["reference_refresh"]["adoption"].pop("operator_edits_sha256")
                self.save_record(record)
            if resave:
                self.assertEqual(self.open_editor()()[1]["content"], planned)
                expected = planned.replace("\n2\n", "\nsecond human edit\n")
                self.save_editor("Example.cs", expected)
            self.assertEqual(self.resume_fixture()["completed_capsules"], 2)
            self.resume_fixture()
        model.assert_called_once()
        continuation.assert_called_once()
        self.assertEqual(verify.call_count, 2)
        self.assertEqual(observed["Example.cs"], expected)
        self.assertEqual(self.target.read_text(encoding="utf-8-sig"), expected)
        if fail_second:
            self.assertEqual(observed["Second.cs"], "followed reference B\n1\n2\n3\nsecond-file human edit\n")
        record = self.record()
        self.assertNotIn("reference_refresh", record)
        self.assertNotIn("target_apply_pending", record)
        self.assertEqual(len(record["reference_refresh_history"]), 1)
        with workflow.editor_queue(self.run_path) as queue:
            self.assertEqual(queue["edits"], {})
            changes = queue["operator_edits"]["Example.cs"]
            self.assertEqual(len(changes), 2 if resave and not legacy else 1)
            if not legacy:
                self.assertIn("+first human edit", changes[0])
            if resave:
                self.assertIn("+second human edit", changes[-1])
            if fail_second:
                self.assertEqual(len(queue["operator_edits"]["Second.cs"]), 1)
                self.assertIn("+second-file human edit", queue["operator_edits"]["Second.cs"][0])

    def test_replacement_failure_recovery_without_another_save(self):
        self.check_resuming_interrupted_adoption()

    def test_replacement_failure_recovery_keeps_superseding_http_save(self):
        self.check_resuming_interrupted_adoption(resave=True)

    def test_partially_applied_multiple_targets_keep_superseding_http_save(self):
        self.check_resuming_interrupted_adoption(resave=True, fail_second=True)

    def test_partially_written_agent_targets_keep_superseding_http_save(self):
        self.check_resuming_interrupted_adoption(resave=True, fail_second=True, canonical_failure=True)

    def test_already_interrupted_adoption_without_new_history_receipt_can_resume(self):
        self.check_resuming_interrupted_adoption(resave=True, legacy=True)

    def test_completed_run_queues_the_first_save_while_reference_adoption_is_pending(self):
        with (patch.object(workflow, "_propose_reference_refresh", side_effect=self.propose) as model,
              patch.object(workflow, "_verify_worktree", return_value={"status": "verified"}) as verify):
            planned = self.interrupted_adoption(complete_without_edit=True)
            self.assertEqual(self.open_editor()()[1]["content"], self.base)
            self.save_editor("Example.cs", self.base.replace("old label", "late human edit"))
            self.assertEqual(self.target.read_text(encoding="utf-8-sig"), self.base)
            self.assertEqual(self.resume_fixture()["status"], "verified")
            self.assertEqual(self.target.read_text(encoding="utf-8-sig"),
                             planned.replace("old label", "late human edit"))
        model.assert_called_once()
        verify.assert_called_once()
        with workflow.editor_queue(self.run_path) as queue:
            self.assertEqual(queue["edits"], {})
            self.assertEqual(len(queue["operator_edits"]["Example.cs"]), 1)

    def test_recovery_preserves_overlap_until_an_explicit_http_resolution(self):
        def migrate(_command, completion, token, targets, **kwargs):
            self.assertEqual(targets[0].read_text(encoding="utf-8-sig"), resolved)
            completion.write_text(token, encoding="utf-8")
            return 0

        with (patch.object(workflow, "_propose_reference_refresh", side_effect=self.propose) as model,
              patch.object(workflow.junie_cli, "require_interactive_cli"),
              patch.object(workflow.junie_cli, "run_interactive", side_effect=migrate) as continuation,
              patch.object(workflow, "_verify_worktree", side_effect=lambda config, unit, result, record: result) as verify):
            planned = self.interrupted_adoption()
            editor = self.open_editor()
            _, before = editor()
            overlapping = self.base.replace("reference A", "different choice").replace("old label", "first human edit")
            status, saved = editor({"kind": "save", "run_id": self.run_id, "target": "Example.cs",
                                    "base": self.base, "content": overlapping, "edit_id": before["edit"]["id"]})
            self.assertEqual(status, 200)
            self.assertTrue(saved["queued"])
            self.assertEqual(self.resume_fixture()["status"], "editor_checkpoint")
            _, conflicted = self.open_editor()()
            self.assertEqual(conflicted["content"], planned)
            self.assertEqual(conflicted["edit"]["content"], overlapping)
            self.assertEqual(conflicted["edit"]["status"], "conflict")
            self.assertEqual(self.target.read_text(encoding="utf-8-sig"), planned)
            self.assertNotIn("reference_refresh", self.record())
            continuation.assert_not_called()
            verify.assert_not_called()
            resolved = planned.replace("model choice", "checked resolution")
            self.save_editor("Example.cs", resolved, resolve=True)
            self.assertEqual(self.resume_fixture()["completed_capsules"], 2)
        model.assert_called_once()
        continuation.assert_called_once()
        verify.assert_called_once()
        with workflow.editor_queue(self.run_path) as queue:
            self.assertEqual(queue["edits"], {})
            self.assertEqual(len(queue["operator_edits"]["Example.cs"]), 2)

    def test_recovery_with_only_partial_writes_stops_before_replacing_targets_if_reference_changes(self):
        with (patch.object(workflow, "_propose_reference_refresh", side_effect=self.propose) as model,
              patch.object(workflow.junie_cli, "run_interactive") as continuation):
            planned = self.interrupted_adoption()
            saved = self.save_editor("Example.cs", planned.replace("\n2\n", "\nnew edit\n"))["edit"]
            before = self.target.read_bytes()
            self.reference.write_text("reference C", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "反映を再開する前に参考資料が更新"):
                self.resume_fixture()
            self.assertEqual(self.target.read_bytes(), before)
            self.assertIn("target_apply_pending", self.record())
            with workflow.editor_queue(self.run_path) as queue:
                self.assertEqual(queue["edits"]["Example.cs"], saved)
            model.assert_called_once()
            continuation.assert_not_called()

    def test_recovery_leaves_unrecognized_external_changes_and_new_edits_untouched(self):
        with (patch.object(workflow, "_propose_reference_refresh", side_effect=self.propose) as model,
              patch.object(workflow.junie_cli, "run_interactive") as continuation):
            planned = self.interrupted_adoption()
            saved = self.save_editor("Example.cs", planned.replace("\n2\n", "\nnew edit\n"))["edit"]
            for path, diagnostic in ((self.target, "移行先が変更"), (self.agent / "Example.cs", "下書きが変更")):
                with self.subTest(path=path):
                    before = path.read_bytes()
                    path.write_bytes(b"outside change")
                    with self.assertRaisesRegex(ValueError, diagnostic):
                        self.resume_fixture()
                    self.assertEqual(path.read_bytes(), b"outside change")
                    self.assertIn("target_apply_pending", self.record())
                    with workflow.editor_queue(self.run_path) as queue:
                        self.assertEqual(queue["edits"]["Example.cs"], saved)
                        self.assertEqual(queue["operator_edits"], {})
                    path.write_bytes(before)
            model.assert_called_once()
            continuation.assert_not_called()

    def check_recovery_after_queue_commit(self, *, overlap_latest=False):
        observed = []
        message = "Investigate the updated progress label."

        def propose(*args):
            pinned = source_context.target_context_snapshot(self.capsule) / "files" / "refs/Library.cs"
            observed.append(pinned.read_text(encoding="utf-8"))
            with workflow.editor_queue(self.run_path) as queue:
                if len(observed) > 1:
                    if not overlap_latest:
                        self.assertEqual(queue["prompts"][0]["status"], "queued")
                    self.assertEqual(len(queue["operator_edits"]["Example.cs"]), 1)
            proposal = self.propose(*args)
            if observed[-1] == "reference C":
                (proposal / "Example.cs").write_text(self.base.replace("reference A", "model choice C"),
                                                      encoding="utf-8-sig")
            return proposal

        def migrate(command, completion, token, targets, **kwargs):
            if not overlap_latest:
                operator = f"checker-findings-{token}-operator.txt"
                self.assertIn(operator, command[-1])
                self.assertEqual((self.agent / operator).read_text(encoding="utf-8"), f"- {message}\n")
            self.assertEqual((self.agent / "refs/Library.cs").read_text(encoding="utf-8"), "reference C")
            self.assertEqual(targets[0].read_text(encoding="utf-8-sig"), expected)
            completion.write_text(token, encoding="utf-8")
            return 0

        writer = workflow.write_run_record

        def lose_run_history(path, record, **kwargs):
            if record.get("reference_refresh_history"):
                raise OSError("fixture run journal write failed")
            return writer(path, record, **kwargs)

        with (patch.object(workflow, "_propose_reference_refresh", side_effect=propose),
              patch.object(workflow.junie_cli, "require_interactive_cli"),
              patch.object(workflow.junie_cli, "run_interactive", side_effect=migrate) as continuation,
              patch.object(workflow, "_verify_worktree", side_effect=lambda config, unit, result, record: result)):
            planned = self.interrupted_adoption()
            if overlap_latest:
                next_draft = planned.replace("model choice", "second human edit")
                expected = planned.replace("model choice", "second human edit, reconciled with C")
            else:
                next_draft = planned.replace("\n2\n", "\nsecond human edit\n")
                expected = next_draft.replace("model choice", "model choice C")
            saved = self.save_editor("Example.cs", next_draft)["edit"]
            if not overlap_latest:
                with workflow.editor_queue(self.run_path) as queue:
                    queue["prompts"].append({"id": "late", "text": message, "status": "queued"})
            with (patch.object(workflow, "write_run_record", side_effect=lose_run_history),
                  self.assertRaisesRegex(OSError, "fixture run journal write failed")):
                self.resume_fixture()
            continuation.assert_not_called()
            self.assertNotIn("reference_refresh_history", self.record())
            self.assertIn("adoption", self.record()["reference_refresh"])
            self.assertEqual(self.target.read_text(encoding="utf-8-sig"), planned)
            with workflow.editor_queue(self.run_path) as queue:
                self.assertEqual(queue["edits"]["Example.cs"], saved)
                self.assertEqual(len(queue["operator_edits"]["Example.cs"]), 1)
                if not overlap_latest:
                    self.assertEqual(queue["prompts"][0]["status"], "queued")
            self.reference.write_text("reference C", encoding="utf-8")
            if overlap_latest:
                with self.assertRaisesRegex(ValueError, "手修正と競合"):
                    self.resume_fixture()
                continuation.assert_not_called()
                _, conflicted = self.open_editor()()
                self.assertEqual(conflicted["content"], planned)
                self.assertEqual(conflicted["edit"]["content"], next_draft)
                self.assertEqual(conflicted["edit"]["status"], "conflict")
                self.save_editor("Example.cs", expected, resolve=True)
            self.assertEqual(self.resume_fixture()["completed_capsules"], 2)
        continuation.assert_called_once()
        self.assertEqual(observed, ["reference B", "reference C"])
        self.assertEqual(len(self.record()["reference_refresh_history"]), 2)
        with workflow.editor_queue(self.run_path) as queue:
            self.assertEqual(queue["edits"], {})
            self.assertEqual([item["status"] for item in queue["prompts"]], [] if overlap_latest else ["sent"])
            self.assertEqual(len(queue["operator_edits"]["Example.cs"]), 2)
            self.assertIn("+first human edit", queue["operator_edits"]["Example.cs"][0])
            self.assertIn("+second human edit", queue["operator_edits"]["Example.cs"][1])

    def test_recovery_after_queue_commit_rechecks_new_reference_without_replaying_history_or_prompts(self):
        self.check_recovery_after_queue_commit()

    def test_new_reference_overlapping_a_resave_from_the_recovered_version_is_editable(self):
        self.check_recovery_after_queue_commit(overlap_latest=True)

    def test_save_after_staging_replans_before_any_adoption_or_prompt_consumption(self):
        self.saved_targets()
        self.target.write_bytes(self.base.encode("utf-8-sig"))
        message = "Verify the unrelated manual label."
        use_queue = workflow.editor_queue
        with use_queue(self.run_path) as queue:
            queue["operator_edits"].clear()
            queue["prompts"].append({"id": "unrelated", "text": message, "status": "queued"})
        human = self.base.replace("old label", "late human edit")
        saved = None

        @contextmanager
        def queue_with_late_save(run_path):
            nonlocal saved
            with use_queue(run_path) as queue:
                yield queue
            if saved is None and self.record().get("reference_refresh", {}).get("adoption"):
                saved = self.save_editor("Example.cs", human)["edit"]

        def propose(*args):
            with use_queue(self.run_path) as queue:
                self.assertEqual(queue["prompts"], [{"id": "unrelated", "text": message, "status": "queued"}])
            self.assertFalse(list(self.agent.glob("*-operator.txt")))
            return self.propose(*args)

        def migrate(command, completion, token, targets, **kwargs):
            instructions = f"checker-findings-{token}-operator.txt"
            self.assertIn(instructions, command[-1])
            self.assertEqual((self.agent / instructions).read_text(encoding="utf-8"), f"- {message}\n")
            self.assertEqual((self.agent / "refs/Library.cs").read_text(encoding="utf-8"), "reference B")
            self.assertEqual(targets[0].read_text(encoding="utf-8-sig"), human.replace("reference A", "model choice"))
            completion.write_text(token, encoding="utf-8")
            return 0

        with (patch.object(workflow, "editor_queue", side_effect=queue_with_late_save),
              patch.object(workflow, "_propose_reference_refresh", side_effect=propose) as model,
              patch.object(workflow.junie_cli, "require_interactive_cli"),
              patch.object(workflow.junie_cli, "run_interactive", side_effect=migrate) as continuation,
              patch.object(workflow, "_verify_worktree", side_effect=lambda config, unit, result, record: result)):
            with self.assertRaisesRegex(ValueError, "成果物は上書きしていません。もう一度再開"):
                self.resume_fixture()
            self.assertIsNotNone(saved)
            self.assertEqual(self.target.read_bytes(), self.base.encode("utf-8-sig"))
            self.assertNotIn("adoption", self.record()["reference_refresh"])
            continuation.assert_not_called()
            with use_queue(self.run_path) as queue:
                self.assertEqual(queue["edits"]["Example.cs"], saved)
                self.assertEqual(queue["prompts"][0]["status"], "queued")
            self.assertEqual(self.resume_fixture()["completed_capsules"], 2)
        self.assertEqual(model.call_count, 2)
        continuation.assert_called_once()
        with use_queue(self.run_path) as queue:
            self.assertEqual(len(queue["operator_edits"]["Example.cs"]), 1)
            self.assertEqual(queue["prompts"][0]["status"], "sent")

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

    def check_resolution_with_second_edit(self, *, proposed_change=None, overlaps=False):
        base = "start\nreference A\n1\n2\n3\n4\nold label\nend\n"
        self.saved_targets(complete=True, multi=True, second_base=base)
        second_name = self.targets[1].name
        second = self.worktree / second_name
        second_agent = self.agent / second_name
        old, new = ("reference A", "human reference choice") if overlaps else ("old label", "human label")
        draft = base.replace(old, new)
        candidate = base if proposed_change is None else base.replace("reference A", proposed_change)

        def propose(*args):
            proposal = self.propose(*args)
            (proposal / second_name).write_bytes(candidate.replace("\n", "\r\n").encode("utf-8-sig"))
            return proposal

        def verify_targets(*_args):
            expected = candidate.replace(old, new)
            for root in (self.worktree, self.agent):
                self.assertEqual((root / "Example.cs").read_text(encoding="utf-8-sig"), self.resolved)
                self.assertEqual((root / second_name).read_text(encoding="utf-8-sig"), expected)
            return {"status": "verified"}

        with (patch.object(workflow, "_propose_reference_refresh", side_effect=propose) as model,
              patch.object(workflow, "_verify_worktree", side_effect=verify_targets) as verify):
            with self.assertRaisesRegex(ValueError, "手修正と競合"):
                self.resume_fixture()
            pending = self.record()["reference_refresh"]
            self.assertEqual(pending["conflict"]["targets"], ["Example.cs"])
            saved = {"Example.cs": self.save_resolution()["edit"], second_name: self.save_editor(second_name, draft)["edit"]}
            if overlaps:
                paths = (self.target, second, self.agent / "Example.cs", second_agent)
                before = tuple(path.read_bytes() for path in paths)
                for _ in range(2):
                    with self.assertRaisesRegex(ValueError, "別の追従案と手修正が競合しています: Second\\.cs"):
                        self.resume_fixture()
                self.assertEqual(tuple(path.read_bytes() for path in paths), before)
                self.assertEqual(self.record()["reference_refresh"], pending)
                with workflow.editor_queue(self.run_path) as queue:
                    self.assertEqual(queue["edits"], saved)
                verify.assert_not_called()
            else:
                self.assertEqual(self.resume_fixture()["status"], "verified")
                self.assertNotIn("reference_refresh", self.record())
                with workflow.editor_queue(self.run_path) as queue:
                    self.assertEqual(queue["edits"], {})
                    self.assertEqual(len(queue["operator_edits"][second_name]), 1)
                verify.assert_called_once()
        model.assert_called_once()

    def test_reference_resolution_keeps_second_edit_when_proposal_does_not_change_it(self):
        self.check_resolution_with_second_edit()

    def test_reference_resolution_merges_independent_second_edits(self):
        self.check_resolution_with_second_edit(proposed_change="reference B")

    def test_reference_resolution_leaves_overlapping_second_edit_pending(self):
        self.check_resolution_with_second_edit(proposed_change="reference B", overlaps=True)

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
