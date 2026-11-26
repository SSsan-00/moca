import hashlib
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests.support.fixtures import HarnessCase
from migration_harness import workflow
from migration_harness.junie import junie_cli

from migration_harness.context.capsules import prepare_context
from migration_harness.context.context_handoff import context_handoff, handoff_prompt, record_handoff


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

    def test_revisions_do_not_consume_the_operator_prompt_budget(self):
        from migration_harness.context import context_evidence

        first = self.add("Unrelated operator question")
        second = self.add("Initial amount check")
        changed = self.add("Intermediate amount check", supersedes=second["id"])
        latest = self.add("Latest amount check", supersedes=changed["id"])
        before = self.record.read_bytes()
        with patch.object(context_evidence, "read_pinned_php", wraps=context_evidence.read_pinned_php) as read:
            prompt = handoff_prompt(self.run, self.root, self.targets, {}, through=1)
        self.assertEqual(1, read.call_count)
        for item in (first, latest):
            self.assertIn(item["note"], prompt)
        for item in (second, changed):
            self.assertNotIn(item["note"], prompt)
        self.assertNotIn("omitted", prompt)
        self.assertEqual(["pending", "superseded", "superseded", "pending"], [
            row["state"] for row in context_handoff(self.run, self.root, self.targets, {}, through=1)
        ])
        self.assertEqual(before, self.record.read_bytes())

    def test_overflow_counts_current_notes_and_gives_bounded_locations(self):
        from migration_harness.context.context_handoff import unresolved_operator_notes

        notes = [self.add(f"operator marker {i}") for i in range(10)]
        prompt = handoff_prompt(self.run, self.root, self.targets, {}, through=1)
        self.assertIn("7 additional pending operator note(s) omitted", prompt)
        self.assertIn("review this run's saved notes", prompt)
        for item in notes[-3:]:
            self.assertIn(item["note"], prompt)
        for item in notes[:-3]:
            self.assertNotIn(item["note"], prompt)
        for item in notes[-6:-3]:
            self.assertIn(item["id"], prompt)
        self.assertNotIn(notes[0]["id"], prompt)
        self.assertEqual([item["id"] for item in reversed(notes)], [
            row["id"] for row in unresolved_operator_notes(self.run, self.root, self.targets, {})
        ])

    def test_transitive_operator_supersession_across_target_restore(self):
        initial = self.target.read_bytes()
        early, a = self.add("Independent early"), self.add("Original first explanation")
        self.target.write_text("class Changed {}", encoding="utf-8")
        b = self.add("Interim replacement", supersedes=a["id"])
        self.target.write_bytes(initial)
        c = self.add("Final replacement", supersedes=b["id"])
        late = self.add("Independent late")
        old_run = self.record.read_bytes()
        rows = context_handoff(self.run, self.root, self.targets, {}, through=1)
        self.assertEqual(
            [(row["id"], state) for row, state in zip((late, c, b, a, early),
               ("pending", "pending", "superseded", "superseded", "pending"), strict=True)],
            [(row["id"], row["state"]) for row in rows],
        )
        prompt = handoff_prompt(self.run, self.root, self.targets, {}, through=1)
        for item in (early, c, late):
            self.assertIn(item["note"], prompt)
        for item in (a, b):
            self.assertNotIn(item["note"], prompt)
        self.assertNotIn("omitted", prompt)
        self.assertEqual(old_run, self.record.read_bytes())

        path = self.run / "context-handoff.json"
        saved = json.loads(path.read_text(encoding="utf-8"))
        for changes in ({"start_line": 777, "end_line": 777}, {"source_sha256": "0" * 64},
                        {"target_hashes": {"not-a-configured-path.cs": None}}):
            with self.subTest(changes=changes):
                def identify(row, **changes):
                    fields = {**{key: value for key, value in row.items() if key != "id"}, **changes}
                    return {**fields, "id": hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()}
                bad = identify(b, **changes)
                replacement = identify(c, supersedes=bad["id"])
                path.write_text(json.dumps({**saved, "notes": [early, a, bad, replacement, late]}), encoding="utf-8")
                rows = context_handoff(self.run, self.root, self.targets, {}, through=1)
                states = {row["id"]: row["state"] for row in rows}
                self.assertEqual("pending", states[a["id"]])
                self.assertEqual("needs_review", states[bad["id"]])

    def test_changed_corrupt_or_out_of_scope_replacement_does_not_hide_pending_predecessor(self):
        from migration_harness.context.context_handoff import unresolved_operator_notes

        prior = self.add("Keep original question")
        current = self.add("Replacement question", supersedes=prior["id"])
        path = self.run / "context-handoff.json"

        def reidentified(**changes):
            fields = {key: value for key, value in current.items() if key != "id"}
            fields.update(changes)
            return {**fields, "id": hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()}

        for damaged in (None, {**current, "note": "tampered"}, {**current, "note": ""},
                        reidentified(source_sha256="0" * 64), reidentified(end_line=999),
                        reidentified(reference_version="old"), reidentified(note=" " * 501 + "nonblank")):
            with self.subTest(damaged=damaged):
                path.write_text(json.dumps({"version": 1, "notes": [prior, damaged]}), encoding="utf-8")
                prompt = handoff_prompt(self.run, self.root, self.targets, {}, through=1)
                self.assertIn(prior["note"], prompt)
                self.assertNotIn(current["note"], prompt)
                self.assertNotIn("tampered", prompt)
                rows = unresolved_operator_notes(self.run, self.root, self.targets, {})
                self.assertEqual([prior["id"]], [row["id"] for row in rows])

    def test_future_capsule_replacement_only_supersedes_when_its_evidence_is_assigned(self):
        run = self.root / "multi"
        (self.root / "demo.php").write_text("<?php\n" + ("echo 2; " + " " * 75 + "\n") * 30, encoding="utf-8")
        prepare_context({"project_root": str(self.root), "max_context_chars": 650}, {
            "id": "unit", "target": "Demo.cs", "inputs": [{
                "role": "php", "path": "demo.php", "start_line": 1, "end_line": None,
                "encoding": "utf-8", "shared": False,
            }],
        }, run)
        args = run, self.root, self.targets, {}
        prior = record_handoff(*args, "php-0001", 1, 1, "Currently visible lead")
        newer = record_handoff(*args, "php-0001", 30, 30, "Future lead", supersedes=prior["id"])
        prompt = handoff_prompt(*args, through=1)
        self.assertIn(prior["note"], prompt)
        self.assertNotIn(newer["note"], prompt)
        prompt = handoff_prompt(*args)
        self.assertIn(newer["note"], prompt)
        self.assertNotIn(prior["note"], prompt)

    def test_missing_and_corrupt_legacy_handoff_does_not_prevent_work(self):
        self.assertEqual("", handoff_prompt(self.run, self.root, self.targets, {}))
        (self.run / "context-handoff.json").write_text("invalid", encoding="utf-8")
        self.assertIn("unreadable", handoff_prompt(self.run, self.root, self.targets, {}))

    def test_unknown_operator_memo_versions_do_not_discard_saved_history(self):
        note = self.add()
        path = self.run / "context-handoff.json"
        run_before = self.record.read_bytes()
        for version in (True, 1.0, "1", 2):
            with self.subTest(version=version):
                raw = json.dumps({"version": version, "notes": [note]}).encode()
                path.write_bytes(raw)
                with self.assertRaisesRegex(ValueError, "invalid context handoff"):
                    self.add("Another note")
                self.assertEqual(path.read_bytes(), raw)
                self.assertEqual(self.record.read_bytes(), run_before)
                self.assertIn("unreadable", handoff_prompt(self.run, self.root, self.targets, {}))

    def test_replay_reinstates_the_operators_note_when_only_its_saved_id_survived(self):
        note = self.add()
        path = self.run / "context-handoff.json"
        damaged = {**note, "note": "accidentally edited history"}
        path.write_text(json.dumps({"version": 1, "notes": [damaged]}), encoding="utf-8")
        run_before = self.record.read_bytes()
        with self.assertRaisesRegex(ValueError, "superseded context handoff ID does not exist"):
            self.add("Do not replace an unverified predecessor", supersedes=note["id"])
        self.assertEqual(self.add(), note)
        self.assertEqual(json.loads(path.read_bytes())["notes"], [damaged, note])
        self.assertEqual([row["state"] for row in context_handoff(self.run, self.root, self.targets, {})],
                         ["pending", "needs_review"])
        self.assertIn(note["note"], handoff_prompt(self.run, self.root, self.targets, {}))
        self.assertNotIn(damaged["note"], handoff_prompt(self.run, self.root, self.targets, {}))
        self.assertEqual(self.record.read_bytes(), run_before)

    def test_junie_memos_are_versioned_bounded_and_do_not_supersede_operator(self):
        from migration_harness.context.context_handoff import junie_memo_request, save_junie_memo
        from migration_harness.runs.worktree_targets import target_hash

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

    def test_corrupt_persisted_memo_does_not_hide_valid_sibling_or_advance_progress(self):
        from migration_harness.context import context_handoff as memo
        from migration_harness.runs.worktree_targets import target_hash

        record = {"completed_capsules": 0, "verification_passed": False,
                  "target_hashes": {"Demo.cs": target_hash(self.target)}}
        draft, _ = memo.junie_memo_request(self.root, "fixture")
        entry = {"kind": "pending", "source_id": "php-0001", "start_line": 2, "end_line": 2,
                 "note": "verified pending source"}
        draft.write_text(json.dumps([entry]), encoding="utf-8")
        memo.save_junie_memo(draft, self.run, record, phase="migration", through=1)
        original, = record["junie_memos"]
        bad_successor = {**original, "id": "1" * 64, "supersedes": original["id"], "kind": "decision"}
        malformed = {k: v for k, v in bad_successor.items() if k != "note"}
        altered = {**original, "note": "do not trust this altered text"}

        for invalid in (42, {"id": "incomplete"}, malformed, bad_successor, altered):
            with self.subTest(invalid=invalid):
                damaged = {**record, "junie_memos": [original, invalid]}
                self.record.write_text(json.dumps(damaged), encoding="utf-8")
                reopened = json.loads(self.record.read_bytes())
                prompt = handoff_prompt(self.run, self.root, self.targets, reopened, through=1)
                self.assertIn(entry["note"], prompt)
                self.assertNotIn("do not trust this altered text", prompt)
                self.assertIn("1 damaged", prompt)
                [pending] = memo.unresolved_junie_memos(self.run, self.root, self.targets, reopened, through=1)
                self.assertEqual(original["id"], pending["id"])
                self.assertEqual("current", pending["state"])
                self.assertEqual(damaged, json.loads(self.record.read_bytes()))

                draft.write_text(json.dumps([{**entry, "note": "a new valid note"}]), encoding="utf-8")
                memo.save_junie_memo(draft, self.run, reopened, phase="migration", through=1)
                self.assertEqual([original, invalid], reopened["junie_memos"][:2])
                self.assertEqual("a new valid note", reopened["junie_memos"][-1]["note"])
                self.assertEqual(0, reopened["completed_capsules"])
                self.assertFalse(reopened["verification_passed"])

    def test_current_decision_resolves_a_chain_with_verified_earlier_links(self):
        from migration_harness.context import context_handoff as memo
        from migration_harness.runs.worktree_targets import target_hash

        record = {"target_hashes": {"Demo.cs": target_hash(self.target)}}
        draft, _ = memo.junie_memo_request(self.root, "fixture")
        predecessor = None
        for index, kind in enumerate(("pending", "pending", "decision"), 1):
            self.target.write_text(f"class Demo{index} {{}}", encoding="utf-8")
            record["target_hashes"]["Demo.cs"] = target_hash(self.target)
            draft.write_text(json.dumps([{
                "kind": kind, "source_id": "php-0001", "start_line": 2, "end_line": 2,
                "note": f"chain item {index}", "supersedes": predecessor,
            }]), encoding="utf-8")
            memo.save_junie_memo(draft, self.run, record, phase="migration", through=1)
            predecessor = record["junie_memos"][-1]["id"]
        self.assertEqual(3, len(record["junie_memos"]))
        self.assertEqual([], memo.unresolved_junie_memos(self.run, self.root, self.targets, record, through=1))
        self.assertNotIn(record["junie_memos"][0]["id"], handoff_prompt(self.run, self.root, self.targets, record, through=1))

        record["junie_memos"][1]["source_sha256"] = "0" * 64
        notes = memo.unresolved_junie_memos(self.run, self.root, self.targets, record, through=1)
        self.assertIn(record["junie_memos"][0]["id"], {row["id"] for row in notes})

    def test_fourth_memo_and_older_stale_note_do_not_silently_disappear(self):
        from migration_harness.context import context_handoff as memo
        from migration_harness.runs.worktree_targets import target_hash

        record = {"target_hashes": {"Demo.cs": target_hash(self.target)}}
        draft, _ = memo.junie_memo_request(self.root, "fixture")
        for text in ("old stale item", "earliest current item", "new item one", "new item two", "new item three"):
            if text == "earliest current item":
                self.target.write_text("class Revised {}", encoding="utf-8")
                record["target_hashes"]["Demo.cs"] = target_hash(self.target)
            draft.write_text(json.dumps([{
                "kind": "pending", "source_path": "demo.php", "start_line": 2, "end_line": 2, "note": text,
            }]), encoding="utf-8")
            memo.save_junie_memo(draft, self.run, record, phase="migration", through=1)
        stale = record["junie_memos"][0]
        prompt = handoff_prompt(self.run, self.root, self.targets, record, through=1)
        self.assertIn("1 additional current", prompt)
        self.assertIn(stale["id"], prompt)
        self.assertNotIn("old stale item", prompt)
        self.assertNotIn("earliest current item", prompt)
        notes = memo.unresolved_junie_memos(self.run, self.root, self.targets, record, through=1)
        self.assertEqual(len(notes), 5)
        self.assertEqual(notes[-2]["note"], "earliest current item")
        self.assertEqual(notes[-1]["state"], "needs_review")
        self.assertNotIn("note", notes[-1])

    def test_new_reference_revision_invalidates_notes_without_changing_php_or_target(self):
        from migration_harness.context.context_handoff import junie_memo_request, save_junie_memo
        from migration_harness.context.source_context import activate_target_context
        from migration_harness.runs.worktree_targets import target_hash

        capsule = self.run / "capsule"
        previous = capsule / "target-context"
        previous.mkdir()
        (previous / "manifest.json").write_text('{"files":[]}', encoding="utf-8")
        operator = self.add("check the reference helper")
        record = {"target_hashes": {"Demo.cs": target_hash(self.target)}}
        draft, _ = junie_memo_request(self.root, "fixture")
        draft.write_text(json.dumps([{
            "kind": "pending", "source_path": "demo.php", "start_line": 2,
            "end_line": 2, "note": "check the helper for cancellation",
        }]), encoding="utf-8")
        save_junie_memo(draft, self.run, record, phase="migration", through=1)
        prompt = handoff_prompt(self.run, self.root, self.targets, record, through=1)
        self.assertIn("check the reference helper", prompt)
        self.assertIn("check the helper for cancellation", prompt)
        changed = capsule / ("target-context-" + "1" * 32)
        changed.mkdir()
        (changed / "manifest.json").write_text('{"files":[{"path":"Shared.cs"}]}', encoding="utf-8")
        activate_target_context(capsule, changed)
        prompt = handoff_prompt(self.run, self.root, self.targets, record, through=1)
        self.assertNotIn("check the reference helper", prompt)
        self.assertNotIn("check the helper for cancellation", prompt)
        self.assertIn(record["junie_memos"][0]["id"], prompt)
        saved, = context_handoff(self.run, self.root, self.targets, {})
        self.assertEqual(operator["id"], saved["id"])
        self.assertEqual("needs_review", saved["state"])

    def test_invalid_successor_does_not_hide_verified_unresolved_note(self):
        from migration_harness.context.context_handoff import junie_memo_request, save_junie_memo
        from migration_harness.runs.worktree_targets import target_hash

        record = {"target_hashes": {"Demo.cs": target_hash(self.target)}}
        draft, _ = junie_memo_request(self.root, "fixture")
        original = {"kind": "pending", "source_path": "demo.php", "start_line": 2,
                    "end_line": 2, "note": "verify the failure path"}
        draft.write_text(json.dumps([original]), encoding="utf-8")
        save_junie_memo(draft, self.run, record, phase="migration", through=1)
        first = record["junie_memos"][0]
        replacement = {**original, "kind": "decision", "note": "no further review",
                       "supersedes": first["id"]}
        draft.write_text(json.dumps([replacement]), encoding="utf-8")
        save_junie_memo(draft, self.run, record, phase="migration", through=1)
        prompt = handoff_prompt(self.run, self.root, self.targets, record, through=1)
        self.assertIn("no further review", prompt)
        self.assertNotIn("verify the failure path", prompt)
        record["junie_memos"][1]["source_sha256"] = "0" * 64
        prompt = handoff_prompt(self.run, self.root, self.targets, record, through=1)
        self.assertNotIn("no further review", prompt)
        self.assertIn("verify the failure path", prompt)


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

        with (patch.object(junie_cli, "require_interactive_cli"),
              patch.object(junie_cli, "run_interactive", side_effect=junie), redirect_stdout(io.StringIO())):
            workflow.run_with_junie(config, "sample")
        self.assertEqual(len(prompts), 2)
        self.assertIn("pending review 1", prompts[1])
        completed, = Path(config["run_root"]).glob("*/run.json")
        saved = json.loads(completed.read_bytes())
        self.assertEqual(saved["completed_capsules"], 1)
        self.assertEqual([row["note"] for row in saved["junie_memos"]], ["pending review 1"])

    def test_stopped_draft_memo_is_used_after_reloading_run(self):
        (self.project / "source.php").write_text("<?php echo 42;\n", encoding="utf-8")
        self.commit_project()
        config = self.config()
        note = "Check the pending return branch."
        prompts = []

        def junie(command, completion, token, targets, **kwargs):
            prompts.append(command[-1])
            if len(prompts) == 1:
                targets[0].write_text("class Provisional {}", encoding="utf-8")
                (completion.parent / f"handoff-{token}.json").write_text(json.dumps([{
                    "kind": "pending", "note": note, "source_id": "php-0001", "start_line": 1, "end_line": 1,
                }]), encoding="utf-8")
                junie_cli.session_closed_observer.get()()
                raise KeyboardInterrupt()
            return 1

        with (patch.object(junie_cli, "require_interactive_cli"),
              patch.object(junie_cli, "run_interactive", side_effect=junie), redirect_stdout(io.StringIO())):
            with self.assertRaises(workflow.RunInterrupted):
                workflow.run_with_junie(config, "sample")
            saved, = Path(config["run_root"]).glob("*/run.json")
            record = json.loads(saved.read_bytes())
            draft = record["stopped_migration_draft"]
            self.assertNotIn("junie_memos", record)
            self.assertEqual(record["completed_capsules"], 0)
            self.assertEqual(draft["junie_memos"][0]["draft_snapshot"], draft["snapshot"])
            agent = saved.parent / "agent-workspace"
            initial_memo, = agent.glob("handoff-*.json")
            initial_memo.write_bytes(b"changed after stop")
            workflow.resume_with_junie(config, record["run_id"])

        self.assertIn("unfinished Junie draft", prompts[1])
        self.assertIn(note, prompts[1])
        changed = {**record, "detail_overrides": {"different": True}}
        self.assertNotIn(note, handoff_prompt(saved.parent, agent, (Path("Page.cs"),), changed, through=1, draft=draft))
