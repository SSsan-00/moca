"""Run regression tests with optional CI enforcement of the required UI checks."""

import argparse
import os
from collections import Counter
from pathlib import Path
import unittest
from time import perf_counter


WINDOWS_SHARDS = ("conversation-editor", "remaining")
CONVERSATION_EDITOR_MODULES = {
    "test_dashboard_editor",
    "test_junie_conversation",
    "test_parallel_dashboard",
    "test_reference_refresh",
}

REQUIRED_UI = {
    "test_settings_browser.SettingsBrowserTests.test_settings_round_trip_in_chromium",
    "test_editor_run_isolation.EditorRunIsolationTests.test_same_file_in_multiple_runs_with_delayed_responses",
    "test_parallel_demo.ParallelDemoTests.test_five_workers_fifo_resume_and_cancellation",
}


class RegressionResult(unittest.TextTestResult):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.passed = set()
        self.timings = []

    def startTest(self, test):
        """unittestのセットアップ前にテストの計測を始める。"""
        self.started = perf_counter()
        super().startTest(test)

    def stopTest(self, test):
        self.timings.append((test.id(), perf_counter() - self.started))
        super().stopTest(test)

    def addSuccess(self, test):
        """必須テストの成功を、発見や起動と区別して記録する。"""
        super().addSuccess(test)
        self.passed.add(test.id())


def test_cases(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from test_cases(test)
        else:
            yield test


def windows_shards(tests):
    """全テストを発見順に分け、重複、欠落、空の分割を拒否する。"""
    groups = {name: [] for name in WINDOWS_SHARDS}
    for test in tests:
        module = test.id().partition(".")[0]
        name = "conversation-editor" if module in CONVERSATION_EDITOR_MODULES else "remaining"
        groups[name].append(test)
    discovered = Counter(test.id() for test in tests)
    scheduled = Counter(test.id() for group in groups.values() for test in group)
    if scheduled != discovered or any(count != 1 for count in scheduled.values()) or not all(groups.values()):
        raise ValueError("Windows test shards must cover each discovered test exactly once and be nonempty")
    return groups


def main():
    """回帰テストを発見・配分して実行し、CIで必要な成功を確認する。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("shard", choices=("full", *WINDOWS_SHARDS))
    parser.add_argument("--require-ui", action="store_true")
    parser.add_argument("--timings", action="store_true", help="Print test durations and IDs")
    args = parser.parse_args()
    loader = unittest.TestLoader()
    tests = list(test_cases(loader.discover(str(Path(__file__).resolve().parent))))
    if loader.errors:
        parser.exit(1, "\n".join(loader.errors) + "\n")
    try:
        groups = windows_shards(tests)
    except ValueError as error:
        parser.error(str(error))
    selected = unittest.TestSuite(tests if args.shard == "full" else groups[args.shard])
    if args.require_ui:
        os.environ["HARNESS_REQUIRE_UI"] = "1"
    discovered = selected.countTestCases()
    result = unittest.TextTestRunner(verbosity=2, resultclass=RegressionResult).run(selected)
    if args.timings:
        for name, seconds in result.timings:
            print(f"TEST_TIME {seconds:.3f} {name}")
    print(f"Discovered: {discovered}; ran: {result.testsRun}; passed: {len(result.passed)}; "
          f"failed: {len(result.failures)}; errors: {len(result.errors)}; skipped: {len(result.skipped)}")
    missing_ui = REQUIRED_UI - result.passed if args.require_ui else set()
    if missing_ui:
        print("Required UI tests did not pass: " + ", ".join(sorted(missing_ui)))
    return 0 if result.wasSuccessful() and not missing_ui else 1


if __name__ == "__main__":
    raise SystemExit(main())
