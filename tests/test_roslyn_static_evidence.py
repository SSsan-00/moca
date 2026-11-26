import json
import shutil
import subprocess
from unittest import TestCase, skipUnless
from unittest.mock import patch

from migration_harness.verification import roslyn_syntax


def fact(kind, value, start, end=None):
    return {"kind": kind, "value": value, "start_line": start, "end_line": start if end is None else end}


class StaticEvidenceTransportTests(TestCase):
    def test_empty_or_invalid_inputs_do_not_start_compiler(self):
        with patch.object(roslyn_syntax, "_prepare_tool") as prepare:
            self.assertEqual(roslyn_syntax.collect_static_evidence([]), [])
            for source in ({"Path": "on-disk.cs"}, {"Path": "bad\0.cs", "Text": ""}):
                with self.subTest(source=source), self.assertRaises(ValueError):
                    roslyn_syntax.collect_static_evidence([source])
            prepare.assert_not_called()

    def test_batch_order_shape_ranges_and_no_truncation_are_required(self):
        source = [{"Path": "First.cs", "Text": "class First {}"}, {"Path": "Second.cs", "Text": "class Second {}"}]
        valid = [{"index": index, "path": row["Path"], "complete": True, "reason": "ok",
                  "facts": [fact("type", row["Path"].split(".")[0], 1)]}
                 for index, row in enumerate(source)]
        for bad in (
            valid[:1], list(reversed(valid)), [{**valid[0], "index": True}, valid[1]],
            [valid[0], {**valid[1], "complete": False}],
            [valid[0], {**valid[1], "facts": [fact("method", "fake", 2)]}],
            [valid[0], {**valid[1], "facts": [fact("other", "fake", 1)]}],
        ):
            with self.subTest(bad=bad), patch.object(roslyn_syntax, "_prepare_tool", return_value=["fake"]), patch.object(
                roslyn_syntax, "_run_editor",
                return_value=subprocess.CompletedProcess([], 0, stdout=json.dumps(bad).encode()),
            ):
                self.assertEqual(roslyn_syntax.collect_static_evidence(source),
                                 [{"complete": False, "reason": "analyzer_output", "facts": []}] * 2)

    def test_nonzero_exit_and_unavailable_compiler_are_not_absence(self):
        source = [{"Path": "Example.cs", "Text": "class Example {}"}]
        for prepare_side_effect, result, reason in (
            (OSError("no SDK"), None, "analyzer_unavailable"),
            (None, subprocess.CompletedProcess([], 1, stdout=b"[]"), "analyzer_output"),
        ):
            with self.subTest(reason=reason), patch.object(
                roslyn_syntax, "_prepare_tool", return_value=["fake"], side_effect=prepare_side_effect,
            ), patch.object(roslyn_syntax, "_run_editor", return_value=result):
                self.assertEqual(roslyn_syntax.collect_static_evidence(source),
                                 [{"complete": False, "reason": reason, "facts": []}])


@skipUnless(shutil.which("dotnet"), "requires .NET 9 SDK")
class RoslynStaticEvidenceTests(TestCase):
    def test_csharp_explicit_facts_use_source_locations_not_comments_or_calls(self):
        text = '\n'.join((
            '// void Ghost() => "comment";',
            'namespace Demo;',
            'public record Example {',
            '    public const string Code = "A\\u0042", Empty = "";',
            '    public int Actual { get; init; }',
            '    public string @event() {',
            '        const string LocalCode = """',
            '            first',
            '            second',
            '            """;',
            '        string Local() => Other(LocalCode);',
            '        return $"dynamic {Actual}";',
            '    }',
            '}',
        ))
        report, = roslyn_syntax.collect_static_evidence([{"Path": "Example.cs", "Text": text}])
        self.assertTrue(report["complete"], report)
        self.assertEqual(report["facts"], [
            fact("type", "Example", 3), fact("const", "Code", 4), fact("literal", "AB", 4),
            fact("const", "Empty", 4), fact("literal", "", 4), fact("property", "Actual", 5),
            fact("method", "event", 6), fact("const", "LocalCode", 7), fact("literal", "first\nsecond", 7, 10),
            fact("method", "Local", 11),
        ])

    def test_razor_uses_original_lines_and_does_not_use_scaffolding_to_prove_absence(self):
        report, = roslyn_syntax.collect_static_evidence([{"Path": "View.cshtml", "Text":
            '<header>markup</header>\n@* void Fake() {} *@\n@functions {\n  string Real() => "日本語";\n}\n<p>markup</p>\n'}])
        self.assertFalse(report["complete"])
        self.assertEqual(report["reason"], "razor_context_unverified")
        self.assertEqual(report["facts"], [fact("method", "Real", 4), fact("literal", "日本語", 4)])

    def test_invalid_conditional_unsupported_and_empty_inputs_are_incomplete(self):
        samples = [
            ("Conditional.cs", "#if UNKNOWN\nclass Hidden {}\n#endif", "project_preprocessor_required"),
            ("Broken.cs", "class Broken {", "syntax_errors"),
            ("Broken.cshtml", "@{ if (true) {", "razor_parse_errors"),
            ("Ignored.vb", "class Ignored {}", "unsupported_file_type"),
            ("Empty.cs", "  \r\n", "empty_target"),
        ]
        reports = roslyn_syntax.collect_static_evidence([{"Path": path, "Text": text} for path, text, _ in samples])
        self.assertEqual([(item["complete"], item["reason"]) for item in reports],
                         [(False, reason) for _, _, reason in samples])

    def test_cutoffs_cannot_certify_absence(self):
        source = [{"Path": "Wide.cs", "Text": 'class Wide { string Example = "' + "x" * 4_097 + '"; }'},
                  {"Path": "After.cs", "Text": "class After {}"}]
        wide, after = roslyn_syntax.collect_static_evidence(source)
        self.assertEqual(wide, {"complete": False, "reason": "fact_limit", "facts": [fact("type", "Wide", 1)]})
        self.assertEqual(after, {"complete": True, "reason": "ok", "facts": [fact("type", "After", 1)]})
