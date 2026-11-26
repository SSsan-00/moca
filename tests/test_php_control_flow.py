from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys
import unittest


spec = spec_from_file_location("php_control_flow", Path(__file__).resolve().parents[1]
                              / "evaluations" / "business-php" / "php_control_flow.py")
php = module_from_spec(spec)
sys.modules[spec.name] = php
spec.loader.exec_module(php)


def normalized(body, *, parameters="bool $flag, int $count", return_type="bool", newline="\n"):
    lines = ["<?php", "declare(strict_types=1);", f"function checkPolicy({parameters}): {return_type} {{",
             *body.splitlines(), "}"]
    return php.normalize_php(newline.join(lines), name="checkPolicy", start_line=3, end_line=len(lines))


class PhpControlFlowTests(unittest.TestCase):
    def test_typed_comparisons_preserve_precedence_short_circuit_and_line_numbers(self):
        body = "if (!$flag || $count >= 10 && $count !== 12) {\n    return true;\n}\nreturn false;"
        for newline in ("\n", "\r\n"):
            with self.subTest(newline=newline):
                function = normalized(body, newline=newline)
                self.assertEqual(function["params"], ["bool", "int"])
                self.assertEqual(function["return_type"], "bool")
                self.assertEqual((function["start_line"], function["end_line"]), (3, 8))
                self.assertEqual(function["body"], [
                    {"op": "if", "line": 4,
                     "test": ["||", ["not", ["arg", 0]],
                              ["&&", [">=", ["arg", 1], ["int", 10]], ["!=", ["arg", 1], ["int", 12]]]],
                     "then": [{"op": "return", "line": 5, "value": ["bool", True]}], "else": []},
                    {"op": "return", "line": 7, "value": ["bool", False]},
                ])

    def test_elseif_and_braced_else_if_have_the_same_conditional_shape(self):
        for alternative in ("elseif", "else if"):
            with self.subTest(alternative=alternative):
                body = ("if ($count < 0) return -2147483648;\n"
                        f"{alternative} ($count === 0) {{ return 0; }}\n"
                        "else { return 2147483647; }")
                tree = normalized(body, return_type="int")["body"]
                branch, = tree
                self.assertEqual(branch["then"][0]["value"], ["int", -2**31])
                second, = branch["else"]
                self.assertEqual(second["line"], 5)
                self.assertEqual(second["test"], ["==", ["arg", 1], ["int", 0]])
                self.assertEqual(second["else"][0]["value"], ["int", 2**31 - 1])

    def test_exact_matching_range_requires_one_unconditional_declaration(self):
        target = "function checkPolicy(bool $flag): bool {\n    return $flag;\n}"
        comment = "/*\n" + target + "\n*/"
        source = "<?php\n" + comment + "\n" + target
        confirmed = php.normalize_php(source, name="CHECKPOLICY", start_line=7, end_line=9)
        self.assertEqual(confirmed["body"], [{"op": "return", "line": 8, "value": ["arg", 0]}])
        for code, start, end in (
            (source, 3, 5),
            (source, 6, 9),
            (source, 7, 10),
            (source + "\n" + target, 7, 9),
            ("<?php\nif (true) {\n" + target + "\n}", 3, 5),
            ("<?php\nnamespace Unknown;\n" + target, 3, 5),
            ("<?php\ndeclare(ticks=1);\n" + target, 3, 5),
            ("<?php\n#[Uninspected]\n" + target, 3, 5),
        ):
            with self.subTest(code=code, range=(start, end)), self.assertRaises(php.UnsupportedPhp):
                php.normalize_php(code, name="checkPolicy", start_line=start, end_line=end)

    def test_unsupported_or_ill_typed_expressions_never_produce_a_comparable_body(self):
        for condition in (
            "$count", "!$count", "$count && $flag", "$unset", "$Flag", "$count == 1",
            "$count === $flag", "$count === 2147483648", "$count >= -2147483649",
            "$count >= 010", "$count >= 0xA", "$count >= 1.0", "$count >= 1_000",
            "$count >= '10'", "$count === $count === $flag", "$flag || external($count)",
            "false && external($count)", "$flag || $dynamic()", "$flag && ($count++ > 0)",
            "$flag || $$name", "$flag OR true", "$flag ?? true", "(bool) $count",
        ):
            with self.subTest(condition=condition), self.assertRaises(php.UnsupportedPhp) as error:
                normalized(f"if ({condition}) {{ return true; }}\nreturn false;")
            self.assertEqual(error.exception.line, 4)

    def test_fallthrough_side_effects_and_unsupported_statement_families_are_unverified(self):
        for body in (
            "if ($flag) { return true; }", "if (true) { return true; }", "return $count;",
            "$count = 0;\nreturn false;", "while ($flag) { return true; }\nreturn false;",
            "switch ($count) { case 1: return true; }\nreturn false;",
            "try { return true; } finally { logResult(); }",
            "if ($flag): return true; endif;\nreturn false;",
            "return <<<TEXT\ntext\nTEXT;",
        ):
            with self.subTest(body=body), self.assertRaises(php.UnsupportedPhp):
                normalized(body)
        for params in ("$flag", "?bool $flag", "bool &$flag", "bool ...$flag", "bool $flag = false",
                       "bool $flag, bool $flag", "bool $_GET"):
            with self.subTest(params=params), self.assertRaises(php.UnsupportedPhp):
                normalized("return true;", parameters=params)

    def test_markup_missing_opening_tag_and_malformed_body_are_unverified(self):
        function = "function checkPolicy(bool $flag): bool { return $flag; }"
        with self.assertRaisesRegex(php.UnsupportedPhp, "standalone PHP document"):
            php.normalize_php(function, name="checkPolicy", start_line=1, end_line=1)
        with self.assertRaisesRegex(php.UnsupportedPhp, "mixed PHP and markup"):
            php.normalize_php("<?php\n" + function + "\n?>outside", name="checkPolicy", start_line=2, end_line=2)
        with self.assertRaises(php.UnsupportedPhp) as malformed:
            normalized("if ($flag) { return true; }\nreturn false")
        self.assertEqual(malformed.exception.line, 6)

    def test_budget_limits_are_explicit_unverified_results(self):
        huge_expression = " || ".join(["$flag"] * 80)
        with self.assertRaisesRegex(php.UnsupportedPhp, "expression operator budget"):
            normalized(f"return {huge_expression};")
        with self.assertRaisesRegex(php.UnsupportedPhp, "nesting budget"):
            normalized("return " + "(" * 40 + "$flag" + ")" * 40 + ";")
        with self.assertRaisesRegex(php.UnsupportedPhp, "function byte budget"):
            normalized("/*" + "x" * 16400 + "*/\nreturn $flag;")
        with self.assertRaisesRegex(php.UnsupportedPhp, "source byte budget"):
            php.normalize_php("🙂" * 300_000, name="checkPolicy", start_line=1, end_line=1)


if __name__ == "__main__":
    unittest.main()
