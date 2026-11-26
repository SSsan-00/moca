from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from migration_harness.workflow import _run_checker


class CheckerIncompleteOutputTests(unittest.TestCase):
    def _checked(self, message, exit_code, *, advisory=False):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            run = root / "run"
            run.mkdir()
            (root / "A.cs").write_text("class A {}", encoding="utf-8")
            record = {"target": ["A.cs"]}

            def process(_command, *, stdout, **_kwargs):
                stdout.write(message.encode("utf-8"))
                return SimpleNamespace(returncode=exit_code)

            with patch("migration_harness.execution.run_process", side_effect=process), patch(
                "migration_harness.runs.run_records.write_run_record"
            ):
                result, findings, raw = _run_checker(
                    ["fixture-checker"], {"run_path": run, "worktree_path": root}, record, None,
                    require_zero_for_success=advisory,
                )
            return result, findings, raw, record

    def test_explicit_no_inspection_never_passes_or_enters_raw_repair(self):
        for exit_code in (0, 1, 2):
            for advisory in (False, True):
                with self.subTest(exit_code=exit_code, advisory=advisory):
                    result, findings, raw, record = self._checked(
                        "Error: No checks were performed.\n", exit_code, advisory=advisory,
                    )
                    self.assertEqual(result["status"], "checker_needs_review")
                    self.assertEqual(result["checker_exit_code"], exit_code)
                    self.assertEqual(record["checker_review_reason"], "checker_incomplete")
                    self.assertFalse(raw)
                    self.assertFalse(findings)

    def test_quoted_no_inspection_does_not_hide_real_finding(self):
        result, findings, raw, record = self._checked(
            'MIG901 A.cs:3 error: do not display "No checks were performed." here\n', 1,
        )
        self.assertEqual(result["status"], "checker_needs_review")
        self.assertEqual([item.path for item in findings], ["A.cs"])
        self.assertFalse(raw)
        self.assertNotIn("checker_review_reason", record)

    def test_unspecified_nonzero_remains_ordinary_free_output(self):
        result, findings, raw, record = self._checked("unrecognized client-specific failure\n", 2)
        self.assertEqual(result["status"], "checker_needs_review")
        self.assertTrue(raw)
        self.assertFalse(findings)
        self.assertNotIn("checker_review_reason", record)
