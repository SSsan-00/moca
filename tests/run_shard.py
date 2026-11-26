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
    "test_editor_semantic_project",
    "test_junie_conversation",
    "test_junie_steering",
    "test_parallel_dashboard",
    "test_reference_refresh",
}

REQUIRED_UI = {
    "test_settings_browser.SettingsBrowserTests.test_settings_round_trip_in_chromium",
    "test_editor_run_isolation.EditorRunIsolationTests.test_same_file_in_multiple_runs_with_delayed_responses",
    "test_parallel_demo.ParallelDemoTests.test_five_workers_fifo_resume_and_cancellation",
}


REQUIRED_RUN_MODELS = {
    "test_run_state_model_traces.RunStateModelTraceTests.test_apply_plan_survives_interruption_and_rejects_manual_edit",
    "test_run_state_live_traces.RunQueueTraceTests.test_stopped_unconfirmed_run_stays_fenced_when_waiting_run_restarts",
    "test_run_state_live_traces.VerificationTraceTests.test_rebuild_disqualifies_old_revision_and_requires_checker_or_approval",
    "test_run_state_live_traces.HookTraceTests.test_unknown_hook_is_not_replayed_until_explicit_verified_retry",
    *("test_run_state_models.RunStateModelsTests." + name for name in (
        "test_run_slot_safety_and_recovery",
        "test_verification_evidence_for_required_and_optional_checker",
        "test_partial_adoption_and_hook_recovery",
        "test_shortest_counterexample_for_duplicate_start",
        "test_shortest_counterexample_for_fenced_adoption",
        "test_shortest_counterexample_for_missing_exit_fence",
        "test_shortest_counterexample_for_stale_success",
        "test_restart_cannot_discard_an_unfinalized_hook_attempt",
        "test_incomplete_search_is_a_failure",
    )),
    *("test_run_state_extended_models.ExtendedRunStateModelsTests." + name for name in (
        "test_two_slots_are_fifo_and_other_runs_are_isolated",
        "test_async_build_cannot_save_an_old_version_or_publish_before_save",
        "test_second_check_on_same_build_rejects_late_results_and_needs_valid_approval",
        "test_reference_adoption_does_not_replace_a_newer_version",
        "test_lock_order_allows_progress_but_rejects_a_reverse_acquire",
        "test_circular_wait_with_two_workers_is_detected",
    )),
}


REQUIRED_WINDOWS_RECOVERY = {
    "test_workflow_mutations.WorkflowMutationTests.test_stale_mapping_is_detected",
    "test_workflow_mutations.WorkflowMutationTests.test_only_the_expected_assertion_counts_as_detection",
    *("test_windows_recovery_faults.WindowsRecoveryFaultTests." + name for name in (
        "test_atomic_json_survives_termination_before_and_after_replace",
        "test_real_windows_sharing_violation_retries_or_leaves_old_record",
        "test_two_target_adoption_restarts_at_each_durable_cut",
        "test_queue_restart_only_launches_waiting_operations",
    )),
    "test_windows_recovery_faults.WindowsHookCrashTests.test_unknown_once_hook_survives_process_death_without_implicit_replay",
}


REQUIRED_CONTROL_FLOW = {
    "test_migration_control_flow.MigrationControlFlowTests.test_pinned_cli_projects_match_change_and_uncertainty_with_real_roslyn",
    "test_migration_control_flow.MigrationControlFlowTests.test_stale_pin_invalid_path_or_ai_candidate_cannot_pass",
    "test_migration_control_flow.ModeledInputTests.test_distinguishes_each_order_and_equality_inside_open_intervals",
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
    missing_models = REQUIRED_RUN_MODELS - result.passed if args.shard != "conversation-editor" else set()
    if missing_models:
        print("Required Run state checks did not pass: " + ", ".join(sorted(missing_models)))
    missing_recovery = REQUIRED_WINDOWS_RECOVERY - result.passed if args.shard != "conversation-editor" else set()
    if missing_recovery:
        print("Required Windows recovery checks did not pass: " + ", ".join(sorted(missing_recovery)))
    missing_flow = REQUIRED_CONTROL_FLOW - result.passed if args.shard != "conversation-editor" else set()
    if missing_flow:
        print("Required static control-flow checks did not pass: " + ", ".join(sorted(missing_flow)))
    return 0 if result.wasSuccessful() and not (missing_ui or missing_models or missing_recovery or missing_flow) else 1


if __name__ == "__main__":
    raise SystemExit(main())
