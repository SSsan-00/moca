from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from migration_harness.verification import checker_output
from migration_harness.junie import junie_cli
from migration_harness.verification import repairs
from tests.support.fixtures import SavedRunFixture


def diagnostic(path="Example.cs", line="12", remedy="送料を加算する", message="合計に送料を含めてください"):
    return (f"ファイル: {path}\n対　処: {remedy}\n内　容: {message}\n行番号: {line}\n"
            "コード: return subtotal;\n備　考: 元PHPの送料加算を確認してください\n")


class PrefixedCheckerParsingTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.worktree = Path(temporary.name).resolve()
        self.log = self.worktree / "checker.log"

    def parse(self, text):
        self.log.write_bytes(text.encode("utf-8"))
        return checker_output.checker_findings(self.log, self.worktree)

    def test_spacing_bom_crlf_and_line_shifts_keep_identity(self):
        original, unknown = self.parse(diagnostic())
        self.assertFalse(unknown)
        variants = [
            diagnostic(line="23").replace("　", ""),
            "\ufeff" + diagnostic(line="23").replace(": ", "　：\t").replace("\n", "\r\n"),
            diagnostic(line="23").replace("ファイル:", "  フ ァ イ ル :").replace("対　処:", "対\t処  :")
            .replace("行番号:", "行　番 号:").replace("コード:", "コ ー ド :"),
        ]
        for text in variants:
            with self.subTest(text=text):
                findings, unknown = self.parse(text)
                self.assertFalse(unknown)
                self.assertEqual(len(findings), 1)
                self.assertEqual(findings[0].finding_id, original[0].finding_id)
                self.assertEqual((findings[0].path, findings[0].line), ("Example.cs", "23"))

    def test_multiple_blocks_and_legacy_format_keep_every_finding(self):
        legacy = "MIG101 Example.cs:1 warning: use SharedUtility"
        old, _ = self.parse(legacy)
        findings, unknown = self.parse(
            legacy + "\n\n" + diagnostic("Pages/Menu.cshtml") + "\n"
            + diagnostic("Pages\\Menu.cshtml.cs", remedy="メニューを読み込む") + legacy,
        )
        self.assertFalse(unknown)
        self.assertEqual([item.path for item in findings],
                         ["Example.cs", "Pages/Menu.cshtml", "Pages/Menu.cshtml.cs", "Example.cs"])
        self.assertEqual(findings[0].finding_id, old[0].finding_id)
        self.assertEqual(findings[-1].finding_id, old[0].finding_id)
        self.assertNotEqual(findings[1].finding_id, findings[2].finding_id)

    def test_multiline_remedy_content_code_and_notes_reach_diagnostic_unchanged(self):
        remedy = "送料を加算する\n税込金額を返す"
        text = diagnostic(remedy=remedy).replace("コード: return subtotal;", "コード:\n    var total = subtotal;\n\n    return total;")
        text = text.replace("行番号:", "送料が欠落しています\n行番号:") + "    税計算の順序も維持してください\n"
        findings, unknown = self.parse(text)
        self.assertFalse(unknown)
        self.assertEqual(findings[0].diagnostic, text.strip())
        self.assertEqual(checker_output._prefixed_checker_fields(text)["対処"], remedy)

    def test_empty_remedy_code_and_notes_are_allowed(self):
        text = "ファイル: Example.cs\n対処:\n内容: 送料が欠落\n行番号: 1\nコード:\n備考:\n"
        findings, unknown = self.parse(text)
        self.assertFalse(unknown)
        self.assertEqual(len(findings), 1)

    def test_different_context_cannot_reuse_approval(self):
        original, _ = self.parse(diagnostic())
        for text in (
            diagnostic(path="Other.cs"), diagnostic(remedy="共通メソッドに委譲する"), diagnostic(message="税が欠落"),
            diagnostic().replace("return subtotal;", "return tax;"),
            diagnostic().replace("元PHPの送料加算を確認してください", "税込金額を使ってください"),
        ):
            with self.subTest(text=text):
                findings, unknown = self.parse(text)
                self.assertFalse(unknown)
                self.assertNotEqual(findings[0].finding_id, original[0].finding_id)

    def test_malformed_blocks_preserve_raw_output_fallback(self):
        for text in (
            diagnostic().replace("行番号: 12\n", ""), diagnostic(line="不明"), diagnostic(line="0"),
            diagnostic(path=""), diagnostic(message=""), diagnostic() + "対処: duplicate\n",
            diagnostic().replace("return subtotal;", "return\0subtotal;"),
            diagnostic().replace("対　処:", "unknown prefix:"), diagnostic(path="../Outside.cs"),
        ):
            with self.subTest(text=text):
                findings, unknown = self.parse(text)
                self.assertTrue(unknown)
                self.assertEqual(findings, [])

    def test_invalid_encoding_does_not_hide_following_valid_block(self):
        self.log.write_bytes(b"\xff\n" + diagnostic().encode("utf-8"))
        findings, unknown = checker_output.checker_findings(self.log, self.worktree)
        self.assertTrue(unknown)
        self.assertEqual(len(findings), 1)


class PrefixedCheckerRepairTests(SavedRunFixture):
    build = ("dotnet", 0, b"build passed\n")
    success = ("fixture-checker", 0, b"")

    def test_success_sentence_with_bom_and_whitespace_skips_repair(self):
        self.config["max_repair_attempts"] = 2
        text = "\r\n  特に気になる点はありません。\t\r\n"
        for exit_code in (0, 1):
            with self.subTest(exit_code=exit_code), patch.object(repairs, "_repair_target") as repair:
                result = self.resume(self.build, ("fixture-checker", exit_code, text.encode("utf-8-sig")))
            self.assertEqual(result["status"], "checker_succeeded")
            self.assertEqual(result["checker_exit_code"], exit_code)
            self.assertEqual(self.record()["checker_success_message"], "特に気になる点はありません。")
            self.assertIsNone(result["error"])
            repair.assert_not_called()

    def test_quoted_success_or_success_mixed_with_other_output_needs_review(self):
        for text in (
            json.dumps({"error": "出力が不一致", "expected": checker_output.CHECKER_SUCCESS_MESSAGE,
                        "actual": "問題あり"}, ensure_ascii=False),
            '期待値: "特に気になる点はありません。"\n実際: 問題あり',
            "```text\n特に気になる点はありません。\n```",
            "特に気になる点はありません。\n追加チェックで不一致を検出しました。",
        ):
            for exit_code in (0, 1):
                with self.subTest(text=text, exit_code=exit_code), patch.object(repairs, "_repair_target") as repair:
                    result = self.resume(self.build, ("fixture-checker", exit_code, text.encode()))
                self.assertEqual(result["status"], "checker_needs_review")
                self.assertNotIn("checker_success_message", self.record())
                repair.assert_not_called()

    def test_success_sentence_cannot_override_findings_or_incomplete_process(self):
        success = "特に気になる点はありません。\n".encode()
        for exit_code, text in (
            (0, success + diagnostic().encode()), (1, success + diagnostic().encode()),
            (1, success + b"MIG101 Example.cs:1 warning: use SharedUtility\n"),
            (0, success + b"MIG101 ../Outside.cs:1 warning: use SharedUtility\n"),
            (0, success + "ファイル: Example.cs\n内容: 送料が欠落\n".encode()),
            (1, success + "ファイル: Example.cs\n内容: 送料が欠落\n".encode()),
            (1, b"\xff\n" + success), (1, b"\0\n" + success), (-1, success),
        ):
            with self.subTest(exit_code=exit_code, text=text):
                result = self.resume(self.build, ("fixture-checker", exit_code, text))
                self.assertEqual(result["status"], "checker_needs_review")
                self.assertNotIn("checker_success_message", self.record())

    def test_success_message_is_cleared_on_next_verification(self):
        self.resume(self.build, ("fixture-checker", 1, "特に気になる点はありません。".encode()))
        result = self.resume(self.build, ("fixture-checker", 1, diagnostic().encode()))
        self.assertEqual(result["status"], "checker_needs_review")
        self.assertNotIn("checker_success_message", self.record())

    def test_zero_exit_raw_output_reaches_junie_and_is_rechecked(self):
        self.config["max_repair_attempts"] = 1
        (self.run_path / "agent-workspace").mkdir()
        original = self.record()
        for text in ('{"expected":42,"actual":0}\r\n', "合計が一致しません。期待値42、実際0。\n"):
            with self.subTest(text=text):
                self.save_record(original)
                self.target.write_text("class Original {}\n", encoding="utf-8")

                def repair(command, completion, token, targets, **kwargs):
                    guidelines = Path(command[command.index("--ide-guidelines") + 1])
                    bundle = json.loads(guidelines.read_bytes().split(b"\n\n")[-1])
                    self.assertIn(text + "\n", bundle.values())
                    self.assertIn(".migration-materials/source-0001.md", bundle)
                    targets[0].write_text("class Repaired {}\n", encoding="utf-8")
                    completion.write_text(token)
                    return 0

                with patch.object(junie_cli, "require_interactive_cli"), patch.object(
                    junie_cli, "run_interactive", side_effect=repair,
                ) as junie:
                    result = self.resume(self.build, ("fixture-checker", 0, text.encode()),
                                         self.build, ("fixture-checker", 0, b""))
                self.assertEqual(result["status"], "checker_succeeded")
                self.assertEqual(self.target.read_text(encoding="utf-8-sig"), "class Repaired {}\n")
                self.assertEqual((self.run_path / "checker-before-repair-1.log").read_bytes(), text.encode())
                junie.assert_called_once()

    def test_zero_exit_unreadable_or_oversized_output_is_not_accepted(self):
        self.config["max_repair_attempts"] = 1
        for output, reason in ((b"\xff", "output_encoding"), (b"\0", "output_unreadable"),
                               (b"x" * 65_537, "output_too_large")):
            with self.subTest(reason=reason), patch.object(repairs, "_repair_target") as repair:
                result = self.resume(self.build, ("fixture-checker", 0, output))
            self.assertEqual(result["status"], "checker_needs_review")
            self.assertEqual(self.record()["checker_review_reason"], reason)
            repair.assert_not_called()

    def test_zero_exit_code_with_findings_can_be_reviewed_and_repaired(self):
        self.resume(self.build, ("fixture-checker", 0, diagnostic().encode()))
        self.assertEqual(len(self.review()["findings"]), 1)
        self.config["max_repair_attempts"] = 1
        with patch.object(repairs, "_repair_target") as repair, patch.object(junie_cli, "require_interactive_cli"):
            result = self.resume(self.build, ("fixture-checker", 0, diagnostic().encode()), self.build, self.success)
        self.assertEqual(result["status"], "checker_succeeded")
        repair.assert_called_once()

    def test_remaining_blocks_continue_repair_after_line_and_spacing_changes(self):
        self.config["max_repair_attempts"] = 2
        first = diagnostic(remedy="税を加算する", message="税を加算してください")
        second = diagnostic()
        shifted = diagnostic(line="15").replace("　", " ").replace(":", "：")
        with patch.object(repairs, "_repair_target") as repair, patch.object(junie_cli, "require_interactive_cli"):
            result = self.resume(
                self.build, ("fixture-checker", 1, (first + second).encode()),
                self.build, ("fixture-checker", 1, shifted.encode()), self.build, self.success,
            )
        self.assertEqual(result["status"], "checker_succeeded")
        self.assertEqual(repair.call_count, 2)
        self.assertEqual(repair.call_args.args[2], (Path("Example.cs"),))
        self.assertEqual(repair.call_args.args[5], shifted.strip())
        history = self.record()["repair_history"]
        self.assertEqual([len(entry["findings"]) for entry in history], [2, 1])
        self.assertFalse(any(entry.get("raw_checker_output") for entry in history))

    def test_repeated_block_stops_even_when_label_spacing_and_line_change(self):
        self.config["max_repair_attempts"] = 2
        with patch.object(repairs, "_repair_target") as repair, patch.object(junie_cli, "require_interactive_cli"):
            result = self.resume(
                self.build, ("fixture-checker", 1, diagnostic().encode()),
                self.build, ("fixture-checker", 1, diagnostic(line="23").replace("　", "").encode()),
            )
        self.assertEqual(result["status"], "checker_needs_review")
        self.assertEqual(self.record()["checker_review_reason"], "finding_repeated")
        repair.assert_called_once()

    def test_reported_file_outside_configured_targets_blocks_repair(self):
        self.config["max_repair_attempts"] = 1
        with patch.object(repairs, "_repair_target") as repair:
            self.resume(self.build, ("fixture-checker", 1, diagnostic(path="Other.cs").encode()))
        self.assertEqual(self.record()["checker_review_reason"], "outside_targets")
        repair.assert_not_called()

    def test_review_has_location_and_approval_survives_line_shift(self):
        self.resume(self.build, ("fixture-checker", 1, diagnostic().encode()))
        report = self.review()
        self.assertFalse(report["unrecognized"])
        self.assertEqual(report["locations"], [{"path": "Example.cs", "line": "12"}])
        finding_id, text, accepted = report["findings"][0]
        self.assertEqual(text, diagnostic().strip())
        self.assertFalse(accepted)
        self.review(finding_id, "元PHPと一致することを確認")
        result = self.resume(self.build, ("fixture-checker", 1, diagnostic(line="15").encode()))
        self.assertEqual(result["status"], "checker_accepted")

    def test_junie_receives_all_fields_repairs_and_rechecks(self):
        self.config["max_repair_attempts"] = 1
        (self.run_path / "agent-workspace").mkdir()
        text = diagnostic()

        def repair(command, completion, token, targets, **kwargs):
            guidelines = Path(command[command.index("--ide-guidelines") + 1])
            bundle = json.loads(guidelines.read_bytes().split(b"\n\n")[-1])
            self.assertIn(text, bundle.values())
            self.assertIn(".migration-materials/source-0001.md", bundle)
            prompt = command[command.index("--prompt") + 1]
            self.assertIn("対処 the suggested fix", prompt)
            self.assertIn("備考 additional context", prompt)
            target, = targets
            target.write_text("class Repaired {}\n", encoding="utf-8")
            completion.write_text(token)
            return 0

        with patch.object(junie_cli, "require_interactive_cli"), patch.object(junie_cli, "run_interactive", side_effect=repair):
            result = self.resume(self.build, ("fixture-checker", 1, text.encode()), self.build, self.success)
        self.assertEqual(result["status"], "checker_succeeded")
        self.assertEqual(self.target.read_text(encoding="utf-8-sig"), "class Repaired {}\n")
        self.assertEqual((self.run_path / "checker-before-repair-1.log").read_text(encoding="utf-8"), text)
