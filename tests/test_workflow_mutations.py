"""Fixed mutation probes for bookkeeping and evidence display, never the live checkout."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass, replace
from hashlib import sha256
from importlib import import_module
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import perf_counter
import traceback
import unittest


ROOT = Path(__file__).resolve().parents[1]
TIMEOUT = 30


@dataclass(frozen=True)
class Mutation:
    module: str
    before: str
    after: str
    test: str
    assertion: str
    message: str
    stub_roslyn: bool = False

    @property
    def relative_source(self):
        return Path(*self.module.split(".")).with_suffix(".py")


MUTATIONS = {
    "stale-mapping": Mutation(
        "migration_harness.runs.migration_ledger",
        'valid = current_hash(path) == mapping["sha256"]',
        "valid = current_hash(path) is not None",
        "test_migration_ledger.LedgerReviewTest.test_candidate_mapping_becomes_stale_after_edit",
        'self.assertFalse(self.source()["mapping"]["valid"])',
        "True is not false",
    ),
    "stale-verification": Mutation(
        "migration_harness.runs.migration_ledger",
        'current_targets == verification.get("target_hashes")',
        'bool(verification.get("target_hashes"))',
        "test_migration_ledger.LedgerReviewTest.test_checker_success_sentence_counts_as_current_verification",
        'self.assertFalse(self.completion()["complete"])',
        "True is not false",
    ),
    "repair-charged-twice": Mutation(
        "migration_harness.verification.repairs",
        'record["repair_history"].append(attempt["details"])',
        'record["repair_history"].extend([attempt["details"]] * 2)',
        "test_checker_resume_review_dashboard.CheckerResumeAndReviewTests.test_supported_diagnostics_still_allow_automatic_repair",
        'self.assertEqual(len(self.record()["repair_history"]), 1)',
        "2 != 1",
        stub_roslyn=True,
    ),
    "legacy-rule-newline": Mutation(
        "migration_harness.rules.rule_materials",
        'required.append(shared.decode("utf-8"))',
        'required.append(shared.decode("utf-8").replace("\\r\\n", "\\n"))',
        "test_rule_materials.RuleMaterialsTests.test_old_shared_run_and_no_rules",
        'self.assertIn(original, (agent / ".migration-materials/guidelines.md").read_bytes())',
        "b'old mandatory rules",
    ),
}


class ProbeResult(unittest.TestResult):
    def __init__(self, mutation):
        super().__init__()
        self.mutation = mutation
        self.started = []
        self.intended = False

    def startTest(self, test):
        super().startTest(test)
        self.started.append(test.id())

    def addFailure(self, test, err):
        super().addFailure(test, err)
        kind, error, stack = err
        test_file = Path(__file__).with_name(self.mutation.test.partition(".")[0] + ".py").resolve()
        self.intended = (
            kind is AssertionError
            and str(error).startswith(self.mutation.message)
            and any(Path(frame.filename).resolve() == test_file and frame.line == self.mutation.assertion
                    for frame in traceback.extract_tb(stack))
        )

    def outcome(self):
        if (self.started != [self.mutation.test] or self.errors or self.skipped
                or self.expectedFailures or self.unexpectedSuccesses):
            return "unexpected"
        if not self.failures:
            return "passed"
        if len(self.failures) == 1 and self.intended:
            return "intended-assertion"
        return "unexpected"


def probe(name, directory, line):
    case = MUTATIONS[name]
    sys.path[:0] = [str(directory / "src"), str(ROOT / "tests")]
    expected = (directory / "src" / case.relative_source).resolve()
    module = import_module(case.module)
    actual = Path(module.__file__).resolve()
    if actual != expected:
        raise RuntimeError(f"probe loaded {actual}, not {expected}")
    result = ProbeResult(case)
    suite = unittest.defaultTestLoader.loadTestsFromName(case.test)
    executed = False

    def trace_test(frame, event, arg):
        nonlocal executed
        if event == "call" and frame.f_code.co_filename != str(actual):
            return None
        if event == "line" and frame.f_lineno == line:
            executed = True
        return trace_test

    boundary = nullcontext()
    if case.stub_roslyn:
        from fixtures import stub_syntax_diagnostics
        boundary = stub_syntax_diagnostics()
    with boundary:
        sys.settrace(trace_test)
        try:
            suite.run(result)
        finally:
            sys.settrace(None)

    detail = next((entry[1] for entry in result.errors + result.failures), "")
    (directory / "probe.json").write_text(json.dumps({
        "sha256": sha256(actual.read_bytes()).hexdigest(),
        "executed_line": executed,
        "outcome": result.outcome(),
        "detail": detail[-1600:],
    }), encoding="utf-8")


def classify(baseline, mutant):
    if baseline != ("passed", True):
        return "invalid-baseline"
    if mutant == ("intended-assertion", True):
        return "detected"
    if mutant == ("passed", True):
        return "undetected"
    if mutant == ("passed", False):
        return "not-exercised"
    return "inconclusive"


def run_probe(name, directory, source_bytes, line):
    report = directory / "probe.json"
    report.unlink(missing_ok=True)
    try:
        subprocess.run(
            [sys.executable, "-B", str(Path(__file__).resolve()), "--probe", name, str(directory), str(line)],
            cwd=directory, stdin=subprocess.DEVNULL, capture_output=True, check=True, timeout=TIMEOUT,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        evidence = json.loads(report.read_bytes())
    except subprocess.TimeoutExpired:
        return ("timeout", False), f"probe exceeded {TIMEOUT}s"
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b"").decode("utf-8", errors="replace")[-1600:]
        return ("execution-error", False), f"probe exited with {exc.returncode}: {detail}"
    except (OSError, ValueError) as exc:
        return ("invalid-report", False), str(exc)
    expected = sha256(source_bytes).hexdigest()
    if not isinstance(evidence, dict) or evidence.get("sha256") != expected:
        return ("wrong-source", False), f"expected the isolated copy with SHA-256 {expected}"
    if (type(evidence.get("executed_line")) is not bool or evidence.get("outcome") not in
            {"passed", "intended-assertion", "unexpected"} or not isinstance(evidence.get("detail"), str)):
        return ("invalid-report", False), "unexpected probe report"
    return (evidence["outcome"], evidence["executed_line"]), evidence["detail"]


def run_mutation(name):
    case = MUTATIONS[name]
    started = perf_counter()
    with TemporaryDirectory(prefix="moca-mutation-") as folder:
        directory = Path(folder).resolve()
        shutil.copytree(ROOT / "src", directory / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        source = directory / "src" / case.relative_source
        before = source.read_bytes()
        anchor, replacement = case.before.encode("utf-8"), case.after.encode("utf-8")
        if before.count(anchor) != 1:
            return "invalid-mutation", perf_counter() - started, "expected exactly one source anchor"
        after = before.replace(anchor, replacement, 1)
        line = before[:before.index(anchor)].count(b"\n") + 1
        if after == before:
            return "invalid-mutation", perf_counter() - started, "replacement did not change source"
        try:
            compile(after, str(source), "exec")
        except SyntaxError as exc:
            return "invalid-mutation", perf_counter() - started, str(exc)
        baseline, detail = run_probe(name, directory, before, line)
        if baseline != ("passed", True):
            return "invalid-baseline", perf_counter() - started, f"{baseline}: {detail}"
        source.write_bytes(after)
        mutant, detail = run_probe(name, directory, after, line)
        proof = f"{case.module}:{line} {sha256(before).hexdigest()[:12]} -> {sha256(after).hexdigest()[:12]}"
        outcome = classify(baseline, mutant)
        return outcome, perf_counter() - started, f"{proof}\n{mutant}: {detail}"


class WorkflowMutationTests(unittest.TestCase):
    def test_stale_mapping_is_detected(self):
        outcome, seconds, detail = run_mutation("stale-mapping")
        print(f"MUTATION stale-mapping {outcome} {seconds:.2f}s ({detail.splitlines()[0]})")
        self.assertEqual(outcome, "detected", detail)

    def test_only_the_expected_assertion_counts_as_detection(self):
        baseline = ("passed", True)
        for result, expected in (
            (("intended-assertion", True), "detected"),
            (("passed", True), "undetected"),
            (("passed", False), "not-exercised"),
            (("intended-assertion", False), "inconclusive"),
            (("unexpected", True), "inconclusive"),
            (("execution-error", False), "inconclusive"),
            (("timeout", False), "inconclusive"),
            (("wrong-source", False), "inconclusive"),
        ):
            with self.subTest(result=result):
                self.assertEqual(classify(baseline, result), expected)
        self.assertEqual(classify(("unexpected", True), ("intended-assertion", True)), "invalid-baseline")

        class UnrelatedFailure(unittest.TestCase):
            def test_false(self):
                self.assertFalse(True)

            def test_error(self):
                raise ValueError("True is not false")

        for test in (UnrelatedFailure("test_false"), UnrelatedFailure("test_error")):
            with self.subTest(test=test):
                result = ProbeResult(replace(MUTATIONS["stale-mapping"], test=test.id()))
                test.run(result)
                self.assertEqual(result.outcome(), "unexpected")


def run_full():
    failed = False
    for name in MUTATIONS:
        outcome, seconds, detail = run_mutation(name)
        print(f"MUTATION {name} {outcome} {seconds:.2f}s ({MUTATIONS[name].test})")
        print(detail.splitlines()[0])
        if outcome != "detected":
            failed = True
            print(detail)
    return int(failed)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--probe"]:
        _, _, name, directory, line = sys.argv
        probe(name, Path(directory), int(line))
    elif sys.argv[1:] == ["--full"]:
        raise SystemExit(run_full())
    else:
        unittest.main()
