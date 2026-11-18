"""Run regression tests with optional CI enforcement of the required UI checks."""

import argparse
import os
from pathlib import Path
import unittest


REQUIRED_UI = {
    "test_settings_browser.SettingsBrowserTests.test_settings_round_trip_in_chromium",
    "test_editor_run_isolation.EditorRunIsolationTests.test_same_file_in_multiple_runs_with_delayed_responses",
    "test_parallel_demo.ParallelDemoTests.test_five_workers_fifo_resume_and_cancellation",
}


class RegressionResult(unittest.TextTestResult):
    """成功したテストを記録し、必須検証が実行されたことを確認する。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.passed = set()

    def addSuccess(self, test):
        super().addSuccess(test)
        self.passed.add(test.id())


def test_cases(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from test_cases(test)
        else:
            yield test


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("shard", choices=("full", "conversation", "remaining"))
    parser.add_argument("--require-ui", action="store_true")
    args = parser.parse_args()
    suite = unittest.defaultTestLoader.discover(str(Path(__file__).resolve().parent))
    conversation = args.shard == "conversation"
    selected = unittest.TestSuite(
        test for test in test_cases(suite)
        # Module import failures also end in the module name.
        if args.shard == "full" or ("test_junie_conversation" in test.id().split(".")) == conversation
    )
    if not selected.countTestCases():
        parser.error(f"{args.shard} shard is empty")
    if args.require_ui:
        os.environ["HARNESS_REQUIRE_UI"] = "1"
    discovered = selected.countTestCases()
    result = unittest.TextTestRunner(verbosity=2, resultclass=RegressionResult).run(selected)
    print(f"Discovered: {discovered}; ran: {result.testsRun}; passed: {len(result.passed)}; "
          f"failed: {len(result.failures)}; errors: {len(result.errors)}; skipped: {len(result.skipped)}")
    missing_ui = REQUIRED_UI - result.passed if args.require_ui else set()
    if missing_ui:
        print("Required UI tests did not pass: " + ", ".join(sorted(missing_ui)))
    return 0 if result.wasSuccessful() and not missing_ui else 1


if __name__ == "__main__":
    raise SystemExit(main())
