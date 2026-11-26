import hashlib
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from tests.support.fixtures import SavedRunFixture
from migration_harness import workflow
from migration_harness import errors, execution
from migration_harness.verification import checker_cycle
from migration_harness.verification import checker_advisory, checker_output
from tests.verification.test_checker_prefixes import diagnostic


class CheckerEncodingTests(SavedRunFixture):
    def run_checker(self, raw, exit_code):
        self.config["checker_command"] = [
            sys.executable,
            "-c",
            f"import sys; sys.stdout.buffer.write(bytes.fromhex({raw.hex()!r})); sys.exit({exit_code})",
        ]
        run_process = execution.run_process

        def child(command, **kwargs):
            if command[0] == "dotnet":
                return subprocess.CompletedProcess(command, 0)
            self.assertEqual(command[0], sys.executable)
            return run_process(command, **kwargs)

        with patch.object(execution, "run_process", side_effect=child):
            return workflow.resume_with_junie(self.config, self.run_id)

    def test_shift_jis_and_cp932_prefixes_match_utf8_finding_identity(self):
        for encoding, message in (("shift_jis", "請求金額"), ("cp932", "髙額請求")):
            with self.subTest(encoding=encoding):
                text = diagnostic(message=message, remedy="first line\n\uff82\uff69").replace("\n", "\r\n")
                raw = text.encode(encoding)
                expected = self.worktree / "expected.log"
                expected.write_bytes(text.encode("utf-8"))
                expected_findings, _ = checker_output.checker_findings(expected, self.worktree)
                self.assertEqual(self.run_checker(raw, 0)["status"], "checker_needs_review")
                findings, unknown = checker_output.checker_findings(self.checker_log, self.worktree)
                self.assertEqual(findings, expected_findings)
                self.assertFalse(unknown)
                self.assertEqual(self.checker_log.read_bytes(), raw)

    def test_canonical_success_keeps_legacy_exit_code_and_opt_in_checker_guard(self):
        raw = (checker_output.CHECKER_SUCCESS_MESSAGE + "\r\n").encode("cp932")
        for code in (0, 1):
            with self.subTest(exit_code=code):
                result = self.run_checker(raw, code)
                self.assertEqual(result["status"], "checker_succeeded")
                self.assertEqual(result["checker_exit_code"], code)
        self.config.update(checker_review_free_output=True, junie_apply_output_decisions=True)
        with patch.object(checker_advisory, "run_text_advisory", return_value='{"verdict":"passed","evidence_lines":[1]}'):
            self.assertEqual(self.run_checker(raw, 1)["status"], "checker_needs_review")
        self.assertNotIn("checker_success_message", self.record())

    def test_cp932_success_text_with_failure_or_invalid_bytes_never_passes(self):
        success = checker_output.CHECKER_SUCCESS_MESSAGE.encode("cp932")
        for raw in (
            success + b"\r\n" + "検査に失敗しました".encode("cp932"),
            success + b"\r\n\x81",
            b"\xef\xbb\xbf" + success,
        ):
            with self.subTest(raw=raw):
                self.assertEqual(self.run_checker(raw, 0)["status"], "checker_needs_review")
                self.assertNotIn("checker_success_message", self.record())

    def test_advisory_citations_bind_cp932_text_to_the_original_bytes(self):
        text = "メソッドの合計値が一致しません。"
        raw = (text + "\r\n").encode("cp932")
        self.checker_log.write_bytes(raw)
        with patch.object(
            checker_advisory, "run_text_advisory", return_value='{"verdict":"problem","evidence_lines":[1]}',
        ) as judge:
            advice = checker_advisory.checker_advisory(self.run_path, 1)
        judge.assert_called_once()
        self.assertEqual(json.loads(judge.call_args.args[0].split("\n", 1)[1])["untrusted_lines"], [text])
        self.assertEqual(advice["log_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(advice["byte_length"], len(raw))
        self.assertEqual(checker_advisory.matching_evidence(self.run_path, advice), {text})
        self.checker_log.write_bytes(raw + b" ")
        self.assertEqual(checker_advisory.matching_evidence(self.run_path, advice), set())

    def test_counted_cp932_pass_still_requires_zero_exit(self):
        raw = "\u0032\u4ef6\u5168\u4ef6\u5408\u683c\u3002".encode("cp932")
        self.config.update(checker_review_free_output=True, junie_apply_output_decisions=True)
        with patch.object(checker_advisory, "run_text_advisory", return_value='{"verdict":"passed","evidence_lines":[1]}') as judge:
            self.assertEqual(self.run_checker(raw, 0)["status"], "checker_succeeded")
            judge.assert_not_called()
            self.assertEqual(self.run_checker(raw, 2)["status"], "checker_needs_review")
            judge.assert_called_once()

    def test_raw_repair_decodes_cp932_without_changing_bytes_or_input_limit(self):
        text = "検査結果に差分あり。"
        raw = text.encode("cp932")
        self.checker_log.write_bytes(raw)
        log, _, decoded, _ = checker_cycle.checker_repair_inputs(
            self.run_path, [], (Path("Example.cs"),), use_raw_output=True,
        )
        self.assertEqual(decoded, text)
        self.assertEqual(log.read_bytes(), raw)
        self.checker_log.write_bytes(raw + b"a" * (65_537 - len(raw)))
        with self.assertRaisesRegex(errors.CheckerRepairBlocked, "too large"):
            checker_cycle.checker_repair_inputs(self.run_path, [], (Path("Example.cs"),), use_raw_output=True)
