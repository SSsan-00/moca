from collections import deque
from dataclasses import dataclass, replace
from enum import Enum, auto
from time import monotonic
import unittest


@dataclass(frozen=True)
class Exploration:
    states: int
    transitions: int
    seconds: float
    witnessed: frozenset[str]


class ModelFailure(AssertionError):
    def __init__(self, condition, trace, state):
        self.condition = condition
        self.trace = trace
        super().__init__(f"{condition}: {' -> '.join(trace) or 'initial'}\n{state!r}")


def explore(initial, transitions, invariant, *, state_limit=5_000, time_limit=2.0):
    started = monotonic()
    deadline = started + time_limit
    parents = {initial: None}
    pending = deque([initial])
    edges = 0
    witnessed = set()

    def trace(state):
        path = []
        while link := parents[state]:
            state, action = link
            path.append(action)
        return tuple(reversed(path))

    if state_limit < 1:
        raise ModelFailure("incomplete/state-limit", (), initial)
    if condition := invariant(initial, None, initial):
        raise ModelFailure(condition, (), initial)
    while pending:
        before = pending.popleft()
        if monotonic() >= deadline:
            raise ModelFailure("incomplete/time-limit", trace(before), before)
        for action, after in transitions(before):
            edges += 1
            if edges % 256 == 0 and monotonic() >= deadline:
                raise ModelFailure("incomplete/time-limit", (*trace(before), action), after)
            if condition := invariant(before, action, after):
                raise ModelFailure(condition, (*trace(before), action), after)
            witnessed.add(action)
            if after not in parents:
                if len(parents) >= state_limit:
                    raise ModelFailure("incomplete/state-limit", (*trace(before), action), after)
                parents[after] = before, action
                pending.append(after)
    elapsed = monotonic() - started
    if elapsed >= time_limit:
        raise ModelFailure("incomplete/time-limit", (), initial)
    return Exploration(len(parents), edges, elapsed, frozenset(witnessed))


class Run(Enum):
    WAIT = auto()
    WORK = auto()
    STOP = auto()
    OUTPUT = auto()
    BLOCK = auto()
    RECOVER = auto()
    DONE = auto()


class Fence(Enum):
    NO = auto()
    STARTED_JOB = auto()
    SAVED_BLOCK = auto()


@dataclass(frozen=True)
class SlotModel:
    runs: tuple[Run, Run] = (Run.WAIT, Run.WAIT)
    evidence: tuple[Fence, Fence] = (Fence.NO, Fence.NO)
    holders: tuple[int, ...] = ()
    storage_available: bool = True

    def change(self, run, phase, *, evidence=None, holders=None):
        updated_phases = list(self.runs)
        updated_evidence = list(self.evidence)
        updated_phases[run] = phase
        if evidence is not None:
            updated_evidence[run] = evidence
        return replace(self, runs=tuple(updated_phases), evidence=tuple(updated_evidence),
                       holders=self.holders if holders is None else holders)


def slot_actions(state):
    yield ("disk ok" if not state.storage_available else "disk failure",
           replace(state, storage_available=not state.storage_available))
    restarted = replace(state, holders=(), storage_available=True)
    for run, phase in enumerate(state.runs):
        if phase in (Run.WORK, Run.STOP, Run.OUTPUT, Run.BLOCK):
            restarted = restarted.change(run, Run.BLOCK)
    yield "restart", restarted

    for run, phase in enumerate(state.runs):
        if phase == Run.WAIT and not state.holders and state.storage_available:
            yield f"start {run}", state.change(run, Run.WORK, evidence=Fence.STARTED_JOB, holders=(run,))
        if phase == Run.WORK:
            yield f"stop {run}", state.change(run, Run.STOP)
            yield f"confirmed exit {run}", state.change(run, Run.OUTPUT)
        if phase == Run.OUTPUT and state.storage_available:
            yield f"adopt {run}", state.change(run, Run.DONE, evidence=Fence.NO, holders=())
        if phase == Run.STOP:
            yield f"unknown exit {run}", state.change(run, Run.BLOCK, holders=())
            yield f"confirmed exit {run}", state.change(run, Run.RECOVER, holders=())
        if phase == Run.BLOCK and state.storage_available and state.evidence[run] != Fence.SAVED_BLOCK:
            yield f"persist fence {run}", state.change(run, Run.BLOCK, evidence=Fence.SAVED_BLOCK)
        if phase in (Run.BLOCK, Run.RECOVER) and state.storage_available:
            yield f"explicit verified recovery {run}", state.change(run, Run.WAIT, evidence=Fence.NO)


def slot_invariant(before, action, state):
    if len(set(state.holders)) != len(state.holders):
        return "RUN-DUPLICATE"
    if len(state.holders) > 1:
        return "RUN-CAPACITY"
    for run, phase in enumerate(state.runs):
        if (run in state.holders) != (phase in (Run.WORK, Run.STOP, Run.OUTPUT)):
            return "RUN-OWNER"
        if phase == Run.BLOCK and state.evidence[run] == Fence.NO:
            return "RUN-DURABLE-FENCE"
        if action == f"unknown exit {run}" and phase != Run.BLOCK:
            return "RUN-FENCE-NOT-ENTERED"
        if action == "restart" and before.runs[run] in (Run.WORK, Run.STOP, Run.OUTPUT, Run.BLOCK) and phase != Run.BLOCK:
            return "RUN-STARTED-AFTER-RESTART"
        if before.runs[run] == Run.BLOCK and phase != Run.BLOCK and action != f"explicit verified recovery {run}":
            return "RUN-UNCONFIRMED"
        if action == f"start {run}" and (before.runs[run] != Run.WAIT or not before.storage_available):
            return "RUN-START-NOT-SAVED"
        if action == "restart" and before.runs[run] == Run.WAIT and phase != Run.WAIT:
            return "RUN-WAITING-LOST"
        if action == f"adopt {run}" and (before.runs[run] != Run.OUTPUT or not before.storage_available):
            return "RUN-ADOPT-BEFORE-EXIT"
    return None


@dataclass(frozen=True)
class Receipt:
    run: int
    operation: int
    revision: int


@dataclass(frozen=True)
class VerificationModel:
    run: int
    checker_required: bool
    revision: int = 0
    edit_base: int | None = None
    build: Receipt | None = None
    check_success: Receipt | None = None
    check_failure: Receipt | None = None
    approved: Receipt | None = None
    complete: bool = False
    started_builds: int = 0
    requested_check: Receipt | None = None


def checker_result(state, reply, success):
    if reply != state.requested_check:
        return state
    if reply != state.build or reply.revision != state.revision:
        return replace(state, requested_check=None)
    if success:
        return replace(state, requested_check=None, check_success=reply, check_failure=None, approved=None)
    return replace(state, requested_check=None, check_success=None, check_failure=reply, approved=None, complete=False)


def verification_actions(state):
    if state.edit_base is None and state.revision < 2:
        yield "queue edit", replace(state, edit_base=state.revision, complete=False)
    if state.edit_base is not None:
        yield "withdraw edit", replace(state, edit_base=None)
    if state.revision < 2:
        yield "external edit", replace(state, revision=state.revision + 1, complete=False)
        if state.edit_base == state.revision:
            yield "apply queued edit", replace(state, revision=state.revision + 1, edit_base=None, complete=False)
    if state.started_builds < 3 and state.edit_base is None:
        operation = state.started_builds + 1
        receipt = Receipt(state.run, operation, state.revision)
        yield "build success", replace(state, build=receipt, started_builds=operation, complete=False)
        yield "build failure", replace(state, build=None, started_builds=operation, complete=False)
    if state.build and state.build.revision == state.revision and not state.requested_check:
        yield "request checker", replace(state, requested_check=state.build)
    if state.requested_check:
        expected = state.requested_check
        yield "ignore foreign result", checker_result(state, replace(expected, run=1 - state.run), True)
        yield "ignore wrong operation", checker_result(state, replace(expected, operation=0), True)
        if expected == state.build and expected.revision == state.revision:
            yield "checker success", checker_result(state, expected, True)
            yield "checker failure", checker_result(state, expected, False)
        else:
            yield "discard stale checker result", checker_result(state, expected, True)
    if state.check_failure == state.build and state.build and state.build.revision == state.revision:
        yield "approve finding with reason", replace(state, approved=state.check_failure)
    if state.edit_base is None and state.build and state.build.revision == state.revision:
        if not state.checker_required or state.build in (state.check_success, state.approved):
            yield "publish complete", replace(state, complete=True)


def verification_invariant(before, action, state):
    if action == "apply queued edit" and before.edit_base != before.revision:
        return "VERIFY-EDIT-STALE"
    if action in ("ignore foreign result", "ignore wrong operation") and state != before:
        return "VERIFY-RESULT-IDENTITY"
    if any(receipt and receipt.run != state.run for receipt in
           (state.build, state.check_success, state.check_failure, state.approved, state.requested_check)):
        return "VERIFY-RUN-ISOLATION"
    if state.approved and state.approved != state.check_failure:
        return "VERIFY-APPROVAL"
    if state.complete:
        if not state.build or state.build.revision != state.revision:
            return "VERIFY-STALE-BUILD"
        if state.edit_base is not None:
            return "VERIFY-EDIT-PENDING"
        if state.checker_required and state.build not in (state.check_success, state.approved):
            return "VERIFY-CHECKER"
    return None


class File(Enum):
    BASE = auto()
    DRAFT = auto()
    HAND_EDIT = auto()


@dataclass(frozen=True)
class ApplyModel:
    files: tuple[File, File] = (File.BASE, File.BASE)
    pending: bool = False
    adopted: bool = False
    store_available: bool = True


def apply_actions(state):
    yield ("disk ok" if not state.store_available else "disk failure",
           replace(state, store_available=not state.store_available))
    yield "restart", replace(state, store_available=True)
    for index, current in enumerate(state.files):
        if current != File.HAND_EDIT:
            files = tuple(File.HAND_EDIT if i == index else value for i, value in enumerate(state.files))
            yield f"external edit {index}", replace(state, files=files, adopted=False)
    if not state.store_available or File.HAND_EDIT in state.files:
        return
    if state.pending:
        for index, current in enumerate(state.files):
            if current == File.BASE:
                files = tuple(File.DRAFT if i == index else value for i, value in enumerate(state.files))
                yield f"write {index}", replace(state, files=files)
        if state.files == (File.DRAFT, File.DRAFT):
            yield "record adoption", replace(state, pending=False, adopted=True)
    elif state.files == (File.BASE, File.BASE):
        yield "save pending plan", replace(state, pending=True)


def apply_invariant(before, action, state):
    if any(was == File.HAND_EDIT and now != File.HAND_EDIT for was, now in zip(before.files, state.files, strict=True)):
        return "APPLY-MANUAL-EDIT"
    if action and action.startswith("write ") and (not before.pending or not before.store_available):
        return "APPLY-WRITE-BEFORE-RECEIPT"
    if state.adopted and (state.pending or any(value != File.DRAFT for value in state.files)):
        return "APPLY-PARTIAL-COMPLETE"
    return None


class Hook(Enum):
    READY = auto()
    RESERVED = auto()
    RUNNING = auto()
    UNKNOWN = auto()
    SUCCESS = auto()
    FAILURE = auto()


@dataclass(frozen=True)
class HookModel:
    stage: Hook = Hook.READY
    attempts: int = 0
    store_available: bool = True


def hook_actions(state):
    yield ("disk ok" if not state.store_available else "disk failure",
           replace(state, store_available=not state.store_available))
    unfinished = state.stage in (Hook.RESERVED, Hook.RUNNING)
    yield "restart", replace(state, stage=Hook.UNKNOWN if unfinished else state.stage, store_available=True)
    if state.stage == Hook.READY and state.store_available:
        yield "persist hook attempt", replace(state, stage=Hook.RESERVED, attempts=1)
    if state.stage == Hook.RESERVED:
        yield "spawn hook", replace(state, stage=Hook.RUNNING)
    if unfinished:
        yield "interrupt hook", replace(state, stage=Hook.UNKNOWN)
    if state.stage == Hook.RUNNING and state.store_available:
        yield "save hook success", replace(state, stage=Hook.SUCCESS)
        yield "save hook failure", replace(state, stage=Hook.FAILURE)
    if state.stage == Hook.UNKNOWN and state.store_available and state.attempts == 1:
        yield "explicit retry with verified command", replace(state, stage=Hook.RESERVED, attempts=2)


def hook_invariant(before, action, state):
    if state.stage == Hook.RUNNING and action == "spawn hook" and (before.stage != Hook.RESERVED or not before.attempts):
        return "HOOK-NOT-RECORDED"
    if (action in ("restart", "interrupt hook") and before.stage in (Hook.RESERVED, Hook.RUNNING)
            and (state.stage != Hook.UNKNOWN or state.attempts != before.attempts)):
        return "HOOK-INCOMPLETE-ATTEMPT-LOST"
    if before.stage == Hook.UNKNOWN and state.stage not in (Hook.UNKNOWN, Hook.RESERVED):
        return "HOOK-UNKNOWN-RESULT"
    if before.stage == Hook.UNKNOWN and state.stage == Hook.RESERVED and action != "explicit retry with verified command":
        return "HOOK-UNSAFE-RETRY"
    if before.stage == Hook.SUCCESS and state.stage != Hook.SUCCESS:
        return "HOOK-SUCCESS-REPLAY"
    return None


class RunStateModelsTests(unittest.TestCase):
    def check_model(self, initial, actions, invariant, required):
        result = explore(initial, actions, invariant)
        self.assertLessEqual(set(required), result.witnessed)
        self.assertGreater(result.states, 1)
        print(f"MODEL {type(initial).__name__} {result.states} states / {result.transitions} transitions / {result.seconds:.3f}s")

    def test_run_slot_safety_and_recovery(self):
        self.check_model(SlotModel(), slot_actions, slot_invariant,
                         ("start 0", "start 1", "adopt 1", "unknown exit 0", "restart", "explicit verified recovery 0"))
        blocked = dict(slot_actions(SlotModel((Run.BLOCK, Run.WAIT), (Fence.STARTED_JOB, Fence.NO))))
        self.assertIn("start 1", blocked)
        self.assertNotIn("start 0", blocked)
        self.assertEqual(blocked["restart"].runs[0], Run.BLOCK)

    def test_verification_evidence_for_required_and_optional_checker(self):
        for run, required in ((0, True), (1, False)):
            with self.subTest(run=run, checker_required=required):
                witnesses = ["publish complete", "apply queued edit", "discard stale checker result",
                             "approve finding with reason", "ignore foreign result", "ignore wrong operation"]
                self.check_model(VerificationModel(run, required), verification_actions, verification_invariant, witnesses)

    def test_partial_adoption_and_hook_recovery(self):
        self.check_model(ApplyModel(), apply_actions, apply_invariant,
                         ("save pending plan", "write 0", "write 1", "record adoption", "external edit 1", "restart"))
        partial = ApplyModel((File.DRAFT, File.BASE), pending=True)
        conflict = dict(apply_actions(dict(apply_actions(partial))["external edit 1"]))
        self.assertNotIn("write 1", conflict)
        self.assertNotIn("record adoption", conflict)
        self.check_model(HookModel(), hook_actions, hook_invariant,
                         ("persist hook attempt", "spawn hook", "interrupt hook", "restart",
                          "save hook success", "explicit retry with verified command"))

    def test_shortest_counterexample_for_duplicate_start(self):
        def mutated(state):
            yield from slot_actions(state)
            if state.runs[0] == Run.WORK:
                yield "start 0 again", replace(state, holders=(0, 0))

        with self.assertRaises(ModelFailure) as problem:
            explore(SlotModel(), mutated, slot_invariant)
        self.assertEqual(problem.exception.condition, "RUN-DUPLICATE")
        self.assertEqual(problem.exception.trace, ("start 0", "start 0 again"))

    def test_shortest_counterexample_for_fenced_adoption(self):
        def mutated(state):
            yield from slot_actions(state)
            if state.runs[0] == Run.BLOCK:
                yield "adopt 0", state.change(0, Run.DONE)

        with self.assertRaises(ModelFailure) as problem:
            explore(SlotModel(), mutated, slot_invariant)
        self.assertEqual(problem.exception.condition, "RUN-UNCONFIRMED")
        self.assertEqual(problem.exception.trace, ("start 0", "restart", "adopt 0"))

    def test_shortest_counterexample_for_missing_exit_fence(self):
        def mutated(state):
            for action, after in slot_actions(state):
                if action == "unknown exit 0":
                    after = after.change(0, Run.WAIT, evidence=Fence.NO)
                yield action, after

        with self.assertRaises(ModelFailure) as problem:
            explore(SlotModel(), mutated, slot_invariant)
        self.assertEqual(problem.exception.condition, "RUN-FENCE-NOT-ENTERED")
        self.assertEqual(problem.exception.trace, ("start 0", "stop 0", "unknown exit 0"))

    def test_shortest_counterexample_for_stale_success(self):
        def mutated(state):
            yield from verification_actions(state)
            if state.build and state.build.revision != state.revision:
                yield "publish stale complete", replace(state, complete=True)

        with self.assertRaises(ModelFailure) as problem:
            explore(VerificationModel(0, True), mutated, verification_invariant)
        self.assertEqual(problem.exception.condition, "VERIFY-STALE-BUILD")
        self.assertEqual(problem.exception.trace, ("build success", "external edit", "publish stale complete"))

    def test_restart_cannot_discard_an_unfinalized_hook_attempt(self):
        def mutated(state):
            for action, after in hook_actions(state):
                if action == "restart" and state.stage == Hook.RUNNING:
                    after = HookModel()
                yield action, after

        with self.assertRaises(ModelFailure) as problem:
            explore(HookModel(), mutated, hook_invariant)
        self.assertEqual(problem.exception.condition, "HOOK-INCOMPLETE-ATTEMPT-LOST")
        self.assertEqual(problem.exception.trace, ("persist hook attempt", "spawn hook", "restart"))

    def test_incomplete_search_is_a_failure(self):
        with self.assertRaisesRegex(ModelFailure, "incomplete/state-limit"):
            explore(SlotModel(), slot_actions, slot_invariant, state_limit=1)
        with self.assertRaisesRegex(ModelFailure, "incomplete/time-limit"):
            explore(SlotModel(), slot_actions, slot_invariant, time_limit=0)
