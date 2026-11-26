import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from migration_harness.files import sha256_file
from migration_harness.runs.worktree_targets import apply_agent_targets
from migration_harness.verification import csharp_keyword_guard


class CSharpKeywordGuardTests(unittest.TestCase):
    def apply_pair(self, name, before, after, expected=None):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            agent, worktree = root / "agent", root / "tree"
            agent.mkdir()
            worktree.mkdir()
            old, new = worktree / name, agent / name
            old.write_text(before, encoding="utf-8")
            new.write_text(after, encoding="utf-8")
            hashes = {name: sha256_file(old)}
            record = {"target": name, "target_hashes": hashes.copy()}
            args = (agent, worktree, (Path(name),), hashes, record, root / "run.json")
            if expected:
                with self.assertRaisesRegex(ValueError, expected):
                    apply_agent_targets(*args)
                self.assertEqual(old.read_text(encoding="utf-8"), before)
                self.assertNotIn("target_apply_pending", record)
                self.assertEqual(new.read_text(encoding="utf-8-sig"), after)
            else:
                apply_agent_targets(*args)
                self.assertEqual(old.read_text(encoding="utf-8-sig"), after)

    def test_keyword_free_targets_do_not_start_roslyn(self):
        with patch.object(csharp_keyword_guard, "_prepare_tool", side_effect=AssertionError("no analyzer")):
            self.apply_pair("Page.cs", "class Page {}", "class Page { string EventName; }")

    def test_unavailable_analyzer_does_not_mark_generated_output_as_verified(self):
        with patch.object(csharp_keyword_guard, "_prepare_tool", side_effect=OSError("no SDK")):
            self.apply_pair("Page.cs", "class Page {}", "class Page { int @event; }", "予約語の確認を完了できません")

    @unittest.skipUnless(shutil.which("dotnet"), "requires .NET 9 SDK")
    def test_csharp_declarations_are_blocked_but_contract_references_and_old_names_are_not(self):
        before = 'class Page { int @event = 1; }'
        self.apply_pair("Page.cs", before, 'class Page { int @event = 2; const string Example = "@class"; }')
        self.apply_pair("Page.cs", before, "class Page\n{\n    int @event = 2;\n    int Valid;\n}")
        self.apply_pair("Page.cs", before, 'class Page { int @event = 2; void Use() { api.@class(); } }')
        self.apply_pair("Page.cs", before, 'class Page { int @event = 2; int @class = 3; }', "Page.cs:1:.*@class")
        self.apply_pair("Page.cs", before, 'class Page { int @event = 2; string S = "😀"; int @class; }',
                        "Page.cs:1:.*@class")
        self.apply_pair("Page.cs", "class Page { /*\n int @event;\n*/ }", "class Page {\n int @event;\n}", r"Page.cs:2:.*@event")
        self.apply_pair("Page.cs", "class Page {}", "class Page { string record = \"@class\"; /* @event */ }")
        self.apply_pair("Page.cs", "class Page {}", 'class Page : Base { public override int @event => 1; }')
        self.apply_pair(
            "Page.cs", "class Page {}", 'class Page : IContract { int IContract.@event => 1; }',
        )

    @unittest.skipUnless(shutil.which("dotnet"), "requires .NET 9 SDK")
    def test_razor_transitions_and_external_member_references_are_not_keyword_declarations(self):
        before = "@page\n@if (true) { <p>@Model.EventName</p> }\n"
        self.apply_pair("Page.cshtml", before, '@page\n@if (true) { <p>@(Model.@event)</p> }\n@* int @class; *@\n')
        self.apply_pair("Page.cshtml", before, "@page\n@{ int @event = 1; }\n", r"Page.cshtml:2:.*@event")
        self.apply_pair("Page.cshtml", before, "@page\n@functions { public int @event { get; set; } }\n",
                        r"Page.cshtml:2:.*@event")
