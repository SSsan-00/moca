import json
from pathlib import Path
import runpy
import shutil
import subprocess
import tempfile
import unittest

from migration_harness import php_literals


class PhpLiteralTests(unittest.TestCase):
    def test_csharp_primitives_do_not_leak_php_or_python_literal_spelling(self):
        for value, expected in ((True, "true"), (False, "false"), (None, "null"),
                                (-(2**63), "-9223372036854775808"), (2**63-1, "9223372036854775807"),
                                ('quote\'"\\\n\u0085\u2028😀', '"quote\'\\"\\\\\\n\\u0085\\u2028\\ud83d\\ude00"')):
            with self.subTest(value=value):
                self.assertEqual(php_literals.csharp_literal(value), expected)
        for value in (1.5, float("nan"), {}, [], b"bytes", 2**63, -(2**63)-1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                php_literals.csharp_literal(value)

    cases = [
        ('"edge-15001-\\"<&>/\\\\日本"', 'edge-15001-"<&>/\\日本'),
        (r"'don\'t \\ \n \" $value'", "don't \\ \\n \\\" $value"),
        (r'"a\n\r\t\v\e\f\\\$\"z"', "a\n\r\t\v\x1b\f\\$\"z"),
        (r'"\101\x42\7\400\48"', "AB\x07\x00\x048"),
        (r'"\u{1f600}\u{65e5}本"', "😀日本"),
        (r'"\q\/\xG\9"', r"\q\/\xG\9"),
        ('"line1\nline2"', "line1\nline2"),
        ("''", ""),
    ]

    def test_php_escaping_without_double_decoding(self):
        for literal, expected in self.cases:
            with self.subTest(literal=literal):
                self.assertEqual(php_literals.decode_literal(literal), expected)

    @unittest.skipUnless(shutil.which("php"), "PHP is optional; fixed expectations run everywhere")
    def test_values_match_the_php_runtime(self):
        source = "<?php echo json_encode([" + ",".join(literal for literal, _ in self.cases) + "]);"
        result = subprocess.run(["php", "-n", "-d", "display_errors=stderr"],
                                input=source.encode(), capture_output=True, check=True)
        self.assertEqual(json.loads(result.stdout), [value for _, value in self.cases])

    def test_unsupported_or_partial_literals_fail_explicitly(self):
        for literal in ('"value\\"', '"a" . "b"', '"a" tail', r'"\xff"', r'"\200"', '"$name"',
                        '"$' + '{name}"', '"{$name}"', r'"\u{x}"', r'"\u{110000}"', r'"\u{d800}"'):
            with self.subTest(literal=literal), self.assertRaises(ValueError):
                php_literals.decode_literal(literal)

    def test_complete_appends_keep_special_values_order_and_duplicates(self):
        literals = [literal for literal, _ in self.cases if "\n" not in literal] * 2
        statements = ["<?php", "$rows = [];"] + ["$rows[] = " + value + ";" for value in literals]
        statements += ["echo json_encode($rows);"]
        self.assertEqual(php_literals.extract_appends(statements, "rows"),
                         [php_literals.decode_literal(value) for value in literals])
        for tail in ('$rows[] = "bad\\";', '$rows[] = get_value();', '$rows[0] = "x";',
                     '$rows[] = "ok"; $rows[] = "lost";'):
            with self.subTest(tail=tail), self.assertRaises(ValueError):
                php_literals.extract_appends(statements + [tail], "rows")
        with self.assertRaises(ValueError):
            php_literals.extract_appends(["$other[] = 'x';"], "rows")

    def test_workspace_helper_runs_without_import_cache_files(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "php_literals.py"
            shutil.copyfile(php_literals.__file__, file)
            helper = runpy.run_path(str(file))
            self.assertEqual(helper["extract_appends"](['$rows[] = "test";'], "rows"), ["test"])
            self.assertEqual(list(Path(directory).iterdir()), [file])

    def test_capsule_extraction_uses_only_the_declared_php_rows(self):
        context = ('# Capsule\n## migration_rules — rule.md\n```text\n'
                   '     1 | $rows[] = "example";\n```\n'
                   '## php — \x60legacy/file\x60 lines 8-10\n\n```text\n'
                   '     8 | $rows[] = "first";\n'
                   '     9 | $rows[] = "quote-\\"日本";\n'
                   '    10 | echo json_encode($rows);\n```\n')
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "context.md"
            file.write_bytes(context.replace("\n", "\r\n").encode())
            self.assertEqual(php_literals.capsule_appends(str(file), "rows"), ["first", 'quote-"日本'])
            broken = [
                context.replace('     8 | $rows[] = "first";\n', ""),
                context.replace("     9 |", "    11 |"),
                context.replace("     9 |", "     8 |"),
                context.replace("     8 |", "     8 (focus chars 0-10) |"),
                context + '## php — \x60other\x60 lines 1-1\n     1 | $rows[] = "other";\n',
                '# Next source rows (reference only)\n     8 | $rows[] = "outside";\n',
            ]
            for value in broken:
                with self.subTest(value=value):
                    file.write_text(value, encoding="utf-8")
                    with self.assertRaises(ValueError):
                        php_literals.capsule_appends(str(file), "rows")
