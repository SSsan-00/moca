import re
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
            self.apply_pair("Page.cs", "class Page {}", r'class Page { string @eventName = "日本語\u3042"; }')

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

    @unittest.skipUnless(shutil.which("dotnet"), "requires .NET 9 SDK")
    def test_normal_razor_control_flow_can_be_adopted(self):
        before = "@page\n@using System\n@if (true) { <p>old</p> }\n"
        after = (
            "@page\n@using System\n@using System.Linq\n@if (true) { <p>new</p> }\n"
            "@if (Model.Enabled) { <p>enabled</p> }\n"
            "@foreach (var name in Model.Names) { <p>@name</p> }\n"
            "@using (Html.BeginForm()) { <p>form</p> }\n"
            '@{ const string Example = "@event"; /* int @class; */ }\n'
            "@* int @event; *@\n"
        )
        self.apply_pair("Page.cshtml", before, after)
        self.apply_pair("Page.cshtml", "@page\n", "@page\n@using System\n")
        self.apply_pair("Page.cshtml", after, after.replace("enabled", "active").replace(
            "<p>@name</p>", "<span>@name</span>"))

    @unittest.skipUnless(shutil.which("dotnet"), "requires .NET 9 SDK")
    def test_partial_razor_can_be_repaired_without_losing_original_declarations(self):
        before = "@page\n@if (true) {\n    int @event = 1;\n"
        self.apply_pair("Page.cshtml", before, before + "}\n")
        self.apply_pair("Page.cshtml", before, before.replace("@event", "eventValue") + "}\n")
        self.apply_pair("Page.cshtml", before, before + "    int @class = 2;\n", r"Page.cshtml:4:.*@class")

    @unittest.skipUnless(shutil.which("dotnet"), "requires .NET 9 SDK")
    def test_razor_reports_original_column_with_unicode(self):
        self.apply_pair("Page.cshtml", "@page\n",
                        '@page\n@{ var example = "😀"; int @event = 1; }\n', r"Page.cshtml:2:.*@event")

    @unittest.skipUnless(shutil.which("dotnet"), "requires .NET 9 SDK")
    def test_conditional_declarations_in_csharp_are_checked_before_adoption(self):
        cases = [
            ("#if NET9_0\n    int @event = 1;\n#endif", 3),
            ("#if DEBUG\n    int @event = 1;\n#endif", 3),
            ("#if NET9_0 && (FEATURE == true) && !SILENT\n    int @event = 1;\n#endif", 3),
            ("#if DEBUG\n#elif CUSTOM\n    int @event = 1;\n#else\n#endif", 4),
            ("#if DEBUG\n#elif CUSTOM\n#else\n    int @event = 1;\n#endif", 5),
            ("#if OUTER\n#if !INNER\n    int @event = 1;\n#endif\n#endif", 4),
        ]
        for conditional, line in cases:
            with self.subTest(conditional=conditional):
                self.apply_pair("Page.cs", "class Page {}", f"class Page {{\n{conditional}\n}}",
                                fr"Page.cs:{line}:.*@event")

    @unittest.skipUnless(shutil.which("dotnet"), "requires .NET 9 SDK")
    def test_unresolved_branch_stops_instead_of_assuming_result_is_empty(self):
        expression = " && ".join(f"SYMBOL_{i}" for i in range(20))
        self.apply_pair("Page.cs", "class Page {}",
                        f"class Page {{\n#if {expression}\n    int @event;\n#endif\n}}",
                        r"Page.cs:3:.*条件付きコンパイル")

    @unittest.skipUnless(shutil.which("dotnet"), "requires .NET 9 SDK")
    def test_conditional_comments_existing_declarations_and_unreachable_branches_remain_allowed(self):
        before = "class Page {\n#if LOCAL_FEATURE\n    int @event = 1;\n#endif\n}"
        after = ('class Page {\n#if LOCAL_FEATURE\n    int @event = 2;\n'
                 '    // int @class;\n    string Saved = "@if";\n'
                 '    string Lines = """\n        int @event;\n        """;\n'
                 '#elif CUSTOM\n    /* int @struct; */\n#endif\n'
                 '#if false\n    int @event;\n#endif\n}')
        self.apply_pair("Page.cs", before, after)
        inactive = "class Page {\n#if false\n    int @event;\n#endif\n}"
        self.apply_pair("Page.cs", inactive, inactive.replace("#if false", "#if NET9_0"),
                        r"Page.cs:3:.*@event")
        self.apply_pair("Page.cs", "class Page {}",
                        "#define LOCAL\nclass Page {\n#if LOCAL\n    int @event;\n#endif\n}",
                        r"Page.cs:4:.*@event")

    @unittest.skipUnless(shutil.which("dotnet"), "requires .NET 9 SDK")
    def test_conditional_razor_checks_csharp_and_allows_comments_and_regular_directives(self):
        before = '@using System\n@if (true) { <p>existing</p> }\n'
        allowed = before + '@{\n#if CUSTOM\n    // int @event;\n    var text = "@class";\n#endif\n}\n'
        self.apply_pair("Page.cshtml", before, allowed)
        self.apply_pair("Page.cshtml", before, allowed.replace("// int @event;", "int @event;"),
                        r"Page.cshtml:5:.*@event")

    @unittest.skipUnless(shutil.which("dotnet"), "requires .NET 9 SDK")
    def test_override_event_forms_preserve_external_base_contract_but_plain_events_are_blocked(self):
        before = "class Page : Base {}"
        for member in ("public override event System.Action @event;",
                       "public override event System.Action @event { add {} remove {} }"):
            with self.subTest(member=member):
                self.apply_pair("Page.cs", before, f"class Page : Base {{ {member} }}")
        for member in ("public event System.Action @event;", "public event System.Action @event { add {} remove {} }",
                       "public System.Action @event;"):
            with self.subTest(member=member):
                self.apply_pair("Page.cs", before, f"class Page : Base {{ {member} }}", r"Page.cs:1:.*@event")

    @unittest.skipUnless(shutil.which("dotnet"), "requires .NET 9 SDK")
    def test_new_tuple_alias_namespace_and_label_names_are_rejected(self):
        for content in (
            "class Page { (int @event, int Value) Pair; }",
            "using @event = System.Int32; class Page { @event Value; }",
            "namespace @event { class Page {} }",
            "namespace Outer.@event; class Page {}",
            "class Page { void Use() { @event: return; } }",
        ):
            with self.subTest(content=content):
                self.apply_pair("Page.cs", "class Page {}", content, r"Page.cs:1:.*@event")
        self.apply_pair("Page.cs", "class Page {}", "using Alias = External.@event; class Page { void Use() { Api.Call(@event: 1); } }")
        self.apply_pair("Page.cs", "class Page { int @event; }", "class Page { (int @event, int Value) Pair; }",
                        r"Page.cs:1:.*@event")

    @unittest.skipUnless(shutil.which("dotnet"), "requires .NET 9 SDK")
    def test_unicode_keyword_variants_are_reported_at_the_original_span(self):
        for spelling in (r"@cl\u0061ss", r"cl\u0061ss", r"@\U00000063lass", "@cl\u200cass"):
            with self.subTest(spelling=spelling):
                self.apply_pair("Page.cs", "class Page {}", f'class Page {{ string S = "😀"; int {spelling}; }}',
                                fr"Page.cs:1:.*{re.escape(spelling)}")
        self.apply_pair("Page.cshtml", "@page\n", '@page\n@{ var text = "😀"; int @cl\\u0061ss; }\n',
                        r"Page.cshtml:2:.*@cl\\u0061ss")

    @unittest.skipUnless(shutil.which("dotnet"), "requires .NET 9 SDK")
    def test_razor_namespace_and_using_directives_map_the_original_unicode_name(self):
        original = "@page\n<p>Old</p>\n"
        name = "ev\u200cent"
        for directive in (f"@namespace {name}", f"@namespace Outer.{name}.Inner",
                          f"@using {name} = System.Int32"):
            with self.subTest(directive=directive):
                after = f"@page\n@* {directive} *@\n{directive}\n<p>New</p>\n"
                self.apply_pair("Page.cshtml", original, after, fr"Page.cshtml:3:.*{re.escape(name)}")
                self.apply_pair("Page.cshtml", after, after.replace("<p>New</p>", "<p>Edited</p>"))
                self.apply_pair("Page.cshtml", original, f"@page\n@* {directive} *@\n<p>Unchanged name</p>\n")

    @unittest.skipUnless(shutil.which("dotnet"), "requires .NET 9 SDK")
    def test_edited_lines_preserve_an_old_keyword_name_across_unicode_spellings(self):
        before = r'class Page { string S = "😀"; int @cl\u0061ss = 1; }'
        after = r'class Page { string S = "new"; int @class = 2; }'
        self.apply_pair("Page.cs", before, after)
        self.apply_pair("Page.cs", before, r'class Page { string S = "new"; int @cl\u0061ss = 2; }')
        self.apply_pair("Page.cs", before, after[:-2] + r" void Method() { int @cl\u0061ss; } }",
                        r"Page.cs:1:.*@cl\\u0061ss")
        self.apply_pair("Page.cs", "class Page {}", r'class Page { string S = "@cl\u0061ss"; /* int @cl\u0061ss; */ }')
        self.apply_pair("Page.cs", "class Page {}", r"class Page : External { public override int @cl\u0061ss => 1; }")
        self.apply_pair("Page.cs", "class Page {}", r"class Page { void Use() { api.@cl\u0061ss(); } }")

    @unittest.skipUnless(shutil.which("dotnet"), "requires .NET 9 SDK")
    def test_unicode_declarations_in_inactive_branches_use_the_same_roslyn_token_rules(self):
        before = "class Page {}"
        branches = "class Page {\n#if FEATURE\n" + r"    (int @cl\u0061ss, int Value) Pair;" + "\n#endif\n}"
        self.apply_pair("Page.cs", before, branches, r"Page.cs:3:.*@cl\\u0061ss")
        self.apply_pair("Page.cs", before, branches.replace("FEATURE", "false"))
        self.apply_pair("Page.cs", before, branches.replace("    (int", "    // (int"))
        expression = " && ".join(f"SYMBOL_{i}" for i in range(20))
        self.apply_pair("Page.cs", before, branches.replace("FEATURE", expression), r"Page.cs:3:.*条件付きコンパイル")
