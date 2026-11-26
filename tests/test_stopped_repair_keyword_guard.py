import codecs
import json
import re
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch

from fixtures import SavedRunFixture
from migration_harness import workflow
from migration_harness.files import sha256_file
from migration_harness.runs.recovery_status import recovery_status


_BEFORE = {
    "Example.cs": b"class Original { int @class = 1; }\n",
    "View.cshtml": b"@if (true) { <p>old</p> }\n",
    "View.cshtml.cs": b"class ViewModel {}\n",
}
_AFTER = {
    "Example.cs": b"class Original { int @class = 2; void Use(dynamic api) { api.@event(); } }\n",
    "View.cshtml": b"@using System\n@if (true) { <p>new</p> }\n",
    "View.cshtml.cs": b"class ViewModel { int Value = 1; }\n",
}
_SUCCESS = (("dotnet", 0, b"build passed\n"), ("fixture-checker", 0, b""))


@unittest.skipUnless(shutil.which("dotnet"), "requires .NET 9 SDK")
class StoppedRepairKeywordGuardTests(SavedRunFixture):
    def setUp(self):
        super().setUp()
        self.config["units"][0]["targets"] = list(_BEFORE)
        self.agent = self.run_path / "agent-workspace"
        self.agent.mkdir()
        for name, content in _BEFORE.items():
            (self.worktree / name).write_bytes(content)
        self.hashes = {name: sha256_file(self.worktree / name) for name in _BEFORE}
        self.save_record({**self.record(), "targets": list(_BEFORE), "target_hashes": self.hashes})

    def choose_draft(self, proposed):
        for name, content in proposed.items():
            (self.agent / name).write_bytes(content)
        draft = workflow._snapshot_stopped_repair(
            self.run_path, self.agent, tuple(map(Path, proposed)), self.hashes, workspace_unchanged=True,
        )
        for name in proposed:
            (self.agent / name).write_bytes(b"// independent Junie workspace bytes\n")
        self.save_record({**self.record(), "repair_pending": {"draft": draft, "details": {}}})
        self.choose(dict.fromkeys(proposed, "draft"))
        return draft

    def choose(self, choices):
        plan = workflow.plan_stopped_repair(self.config, self.run_id)
        workflow.resolve_stopped_repair(self.config, self.run_id, plan["version"], choices)

    def files(self):
        return {path: path.read_bytes() for root in (self.worktree, self.agent) for name in _BEFORE
                if (path := root / name).is_file()}

    def assert_rejected(self, name, line=2, spelling="@event"):
        files = self.files()
        pending = self.record()["repair_pending"]
        mock, calls = self.mock_commands(*_SUCCESS)
        with mock:
            result = workflow.resume_with_junie(self.config, self.run_id)
        self.assertNotEqual(result["status"], "checker_succeeded")
        self.assertFalse(calls)
        self.assertRegex(result["error"].splitlines()[0], fr"{re.escape(name)}:{line}:.*{re.escape(spelling)}")
        self.assertIn("作業ツリーを選び直す", result["error"])
        self.assertEqual(self.files(), files)
        self.assertEqual(self.record()["repair_pending"], pending)
        self.assertTrue(recovery_status(self.config, self.run_id)["draft_keyword_blocked"])
        ledger = workflow.migration_ledger(self.config, self.run_id)
        self.assertFalse(ledger["completion"]["complete"])

    def test_rejects_bad_razor_before_any_adoption_and_allows_reselection(self):
        original = self.choose_draft({**_AFTER, "View.cshtml": b"@using System\n@{ int @event = 1; }\n"})
        self.assert_rejected("View.cshtml")
        plan = workflow.plan_stopped_repair(self.config, self.run_id)
        self.assertEqual(plan["snapshot"], original["snapshot"])
        preview = workflow.preview_stopped_repair(self.config, self.run_id, plan["version"], "View.cshtml")
        self.assertIn("@event", preview["draft"]["text"])
        self.choose({**dict.fromkeys(_AFTER, "draft"), "View.cshtml": "worktree"})
        self.assertFalse(recovery_status(self.config, self.run_id)["draft_keyword_blocked"])
        self.assertEqual(self.resume(*_SUCCESS)["status"], "checker_succeeded")
        self.assertEqual(self.target.read_bytes(), codecs.BOM_UTF8 + _AFTER["Example.cs"])
        self.assertEqual((self.worktree / "View.cshtml").read_bytes(), _BEFORE["View.cshtml"])
        self.assertTrue(workflow.migration_ledger(self.config, self.run_id)["completion"]["complete"])

    def test_legacy_partial_restore_does_not_count_new_declaration_as_existing(self):
        invalid = b"class Original {\n    int @event = 1;\n}\n"
        stopped = self.choose_draft({**_AFTER, "Example.cs": invalid})
        saved = self.record()
        selected = saved["repair_pending"]["draft"]
        del selected["base_snapshot"]
        manifest = self.run_path / selected["snapshot"] / "draft.json"
        manifest.write_text(json.dumps(selected), encoding="utf-8")
        self.save_record(saved)
        # Reproduce an older version reaching the first file, then stopping before the others.
        self.target.write_bytes(codecs.BOM_UTF8 + invalid)
        self.assert_rejected("Example.cs")
        self.assertEqual(workflow.plan_stopped_repair(self.config, self.run_id)["snapshot"], stopped["snapshot"])
        for choice in ("draft", "worktree"):
            with self.subTest(choice=choice), self.assertRaisesRegex(ValueError, r"Example.cs:2:.*@event"):
                self.choose({**dict.fromkeys(_AFTER, "draft"), "Example.cs": choice})
        manual = invalid.replace(b"@event", b"EventValue")
        self.target.write_bytes(manual)
        self.choose({**dict.fromkeys(_AFTER, "draft"), "Example.cs": "worktree"})
        self.assertEqual(self.resume(*_SUCCESS)["status"], "checker_succeeded")
        self.assertEqual(self.target.read_bytes(), manual)

    def test_already_restored_legacy_selection_is_checked_before_verification(self):
        invalid = b"class Original {\n    int @event = 1;\n}\n"
        proposed = {**_AFTER, "Example.cs": invalid}
        self.choose_draft(proposed)
        saved = self.record()
        attempt = saved["repair_pending"]
        del attempt["restore_from"]
        for name, text in proposed.items():
            (self.agent / name).write_bytes(text)
            (self.worktree / name).write_bytes(codecs.BOM_UTF8 + text)
        attempt["selection"] = {"base": {name: sha256_file(self.worktree / name) for name in proposed}, "partial": []}
        self.save_record(saved)
        self.assert_rejected("Example.cs")

    def test_permitted_recovery_retries_after_partial_write_and_preserves_manual_code(self):
        manual = b"class ViewModel { int @event = 42; }\n"
        self.choose_draft(_AFTER)
        (self.worktree / "View.cshtml.cs").write_bytes(manual)
        self.choose({**dict.fromkeys(_AFTER, "draft"), "View.cshtml.cs": "worktree"})
        replace = Path.replace

        def fail_on_view(path, destination):
            if destination == self.worktree / "View.cshtml":
                raise OSError("injected restore failure")
            return replace(path, destination)

        with patch.object(Path, "replace", fail_on_view):
            self.assertIn("injected restore failure", self.resume()["error"])
        self.assertEqual(self.target.read_bytes(), codecs.BOM_UTF8 + _AFTER["Example.cs"])
        self.assertEqual((self.worktree / "View.cshtml").read_bytes(), _BEFORE["View.cshtml"])
        self.assertIn("restore_from", self.record()["repair_pending"])
        self.assertEqual(self.resume(*_SUCCESS)["status"], "checker_succeeded")
        self.assertEqual((self.worktree / "View.cshtml").read_bytes(), codecs.BOM_UTF8 + _AFTER["View.cshtml"])
        self.assertEqual((self.worktree / "View.cshtml.cs").read_bytes(), manual)

    def test_conditional_keywords_in_a_stopped_draft_are_not_adopted(self):
        conditional = b"class Original {\n#if NET9_0\n    int @event = 1;\n#endif\n}\n"
        self.choose_draft({**_AFTER, "Example.cs": conditional})
        self.assert_rejected("Example.cs", line=3)

    def test_unicode_tuple_in_stopped_draft_does_not_change_any_target(self):
        spelling = r"@cl\u0061ss"
        draft = (
            "class Original {\n#if NET9_0\n"
            f"    (int {spelling}, int Value) Pair;\n"
            "#endif\n}\n"
        ).encode()
        self.choose_draft({**_AFTER, "Example.cs": draft})
        self.assert_rejected("Example.cs", line=3, spelling=spelling)
        self.choose({**dict.fromkeys(_AFTER, "draft"), "Example.cs": "worktree"})
        self.assertEqual(self.resume(*_SUCCESS)["status"], "checker_succeeded")
        self.assertEqual(self.target.read_bytes(), _BEFORE["Example.cs"])

    def test_external_override_event_forms_can_be_adopted_from_a_stopped_draft(self):
        proposed = {
            **_AFTER,
            "Example.cs": b"class Page : External { public override event System.Action @event; }\n",
            "View.cshtml.cs": (b"class PageModel : External { public override event System.Action @event "
                              b"{ add {} remove {} } }\n"),
        }
        self.choose_draft(proposed)
        self.assertEqual(self.resume(*_SUCCESS)["status"], "checker_succeeded")
        for name, content in proposed.items():
            self.assertEqual((self.worktree / name).read_bytes(), codecs.BOM_UTF8 + content)

    def test_unicode_newline_in_stopped_draft_is_rejected_without_writing(self):
        for separator in ("\x85", "\u2028", "\u2029"):
            with self.subTest(separator=separator):
                invalid = f'class Original {{{separator}    string Text = "😀"; int @event; }}'.encode()
                self.choose_draft({**_AFTER, "Example.cs": invalid})
                self.assert_rejected("Example.cs")

    def test_permitted_recovery_keeps_mixed_newlines(self):
        edited = 'class Original {\r\n    string Text = "😀";\x85    int @class = 2;\u2028}\r\u2029'.encode()
        proposed = {**_AFTER, "Example.cs": edited}
        self.choose_draft(proposed)
        self.assertEqual(self.resume(*_SUCCESS)["status"], "checker_succeeded")
        for name, content in proposed.items():
            self.assertEqual((self.worktree / name).read_bytes(), codecs.BOM_UTF8 + content)

    def test_a_keyword_in_another_type_does_not_adopt_any_stopped_draft_target(self):
        draft = b"class Original { int oldValue = 1; }\nclass Other { int @class = 1; }\n"
        self.choose_draft({**_AFTER, "Example.cs": draft})
        self.assert_rejected("Example.cs", spelling="@class")

    def test_callback_switch_in_a_stopped_draft_keeps_all_targets_unchanged(self):
        before = b"class Page { void Use() { Call(() => { { int @event = 1; } },\n() => { { int valid = 2; } }); } }"
        self.target.write_bytes(before)
        self.hashes["Example.cs"] = sha256_file(self.target)
        self.save_record({**self.record(), "target_hashes": self.hashes})
        after = before.replace(b"@event", b"normal").replace(b"valid = 2", b"@event = 1")
        self.choose_draft({**_AFTER, "Example.cs": after})
        self.assert_rejected("Example.cs")
