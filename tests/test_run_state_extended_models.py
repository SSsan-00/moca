from dataclasses import dataclass, replace
from enum import IntEnum, StrEnum
import unittest

from test_run_state_models import ModelFailure, explore


class JobState(StrEnum):
    WAITING = "waiting"
    RUNNING = "running"
    STOPPING = "stopping"
    FENCED = "fenced"
    DONE = "done"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class Dispatch:
    queue: tuple[int, ...] = (0, 1, 2)
    active: tuple[int, ...] = ()
    jobs: tuple[JobState, ...] = (JobState.WAITING,) * 3

    def change(self, run, phase, **changes):
        jobs = (*self.jobs[:run], phase, *self.jobs[run + 1:])
        return replace(self, jobs=jobs, **changes)


def dispatch_actions(state):
    if state.queue and len(state.active) < 2:
        run, *tail = state.queue
        yield f"start {run}", state.change(run, JobState.RUNNING, queue=tuple(tail), active=(*state.active, run))
    for run in state.queue:
        yield f"cancel {run}", state.change(run, JobState.CANCELLED, queue=tuple(i for i in state.queue if i != run))
    for run in state.active:
        if state.jobs[run] == JobState.RUNNING:
            yield f"stop {run}", state.change(run, JobState.STOPPING)
        else:
            yield f"unconfirmed {run}", state.change(run, JobState.FENCED, active=tuple(i for i in state.active if i != run))
        yield f"finish {run}", state.change(run, JobState.DONE, active=tuple(i for i in state.active if i != run))
    if len(state.active) == 2:
        finished = replace(state, active=())
        for run in state.active:
            finished = finished.change(run, JobState.DONE)
        yield "finish both", finished
    yield "restart", replace(state, active=(), jobs=tuple(
        JobState.FENCED if job in (JobState.RUNNING, JobState.STOPPING) else job for job in state.jobs
    ))


def dispatch_contract(before, action, after):
    if len(set(after.active)) != len(after.active):
        return "DISPATCH-DOUBLE-START"
    if len(after.active) > 2:
        return "DISPATCH-CAPACITY"
    if sorted(after.active) != [i for i, job in enumerate(after.jobs) if job in (JobState.RUNNING, JobState.STOPPING)]:
        return "DISPATCH-OWNERSHIP"
    if len(after.queue) != len(set(after.queue)) or set(after.queue) != {
        i for i, job in enumerate(after.jobs) if job == JobState.WAITING
    }:
        return "DISPATCH-QUEUE"
    if action == "restart":
        for run, old in enumerate(before.jobs):
            if (run in before.active and after.jobs[run] != JobState.FENCED) or (run not in before.active and old != after.jobs[run]):
                return "DISPATCH-REPLAY"
        if after.queue != before.queue:
            return "DISPATCH-FIFO"
    if action and action.startswith(("start ", "cancel ", "stop ")):
        operation, text = action.split()
        run = int(text)
        if operation == "start" and (not before.queue or before.queue[0] != run or after.queue != before.queue[1:]):
            return "DISPATCH-FIFO"
        if operation == "cancel" and (run not in before.queue or after.queue != tuple(i for i in before.queue if i != run)):
            return "DISPATCH-FIFO"
        if operation == "stop" and after.queue != before.queue:
            return "DISPATCH-FIFO"
        if any(old != new for i, (old, new) in enumerate(zip(before.jobs, after.jobs, strict=True)) if i != run):
            return "DISPATCH-OTHER-RUN"
    if action == "finish both" and (len(before.active) != 2 or after.active or before.queue != after.queue
            or any(after.jobs[i] != (JobState.DONE if i in before.active else old) for i, old in enumerate(before.jobs))):
        return "DISPATCH-BATCH-FINISH"
    return None


class Stage(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    RECEIVED = "received"


@dataclass(frozen=True)
class Evidence:
    run: int
    operation: int
    version: int
    build: int = 0


@dataclass(frozen=True)
class Pending:
    identity: Evidence
    stage: Stage = Stage.QUEUED
    success: bool | None = None


def deliver(job, identity, success):
    if job.stage != Stage.RUNNING or identity != job.identity:
        return job
    return replace(job, stage=Stage.RECEIVED, success=success)


def job_actions(job):
    if job.stage == Stage.QUEUED:
        yield "begin", replace(job, stage=Stage.RUNNING)
    if job.stage == Stage.RUNNING:
        yield "receive success", deliver(job, job.identity, True)
        yield "receive failure", deliver(job, job.identity, False)
    if job.stage != Stage.QUEUED:
        identity = job.identity
        yield "foreign reply", deliver(job, replace(identity, run=1), True)
        yield "old operation reply", deliver(job, replace(identity, operation=identity.operation - 1), True)
        yield "wrong version reply", deliver(job, replace(identity, version=1 - identity.version), True)
        if identity.build:
            yield "wrong build reply", deliver(job, replace(identity, build=3 - identity.build), True)
    if job.stage == Stage.RECEIVED:
        yield "duplicate reply", deliver(job, job.identity, not job.success)


@dataclass(frozen=True)
class BuildFlow:
    version: int = 0
    issued: int = 0
    pending: Pending | None = None
    saved: Evidence | None = None
    storage: bool = True
    complete: bool = False


def build_actions(state):
    if not state.version:
        yield "edit", replace(state, version=1, complete=False)
    yield ("storage fails" if state.storage else "storage returns"), replace(state, storage=not state.storage)
    if state.pending is None and state.issued < 2:
        number = state.issued + 1
        yield "accept build", replace(state, issued=number, pending=Pending(Evidence(0, number, state.version)),
                                      saved=None, complete=False)
    if job := state.pending:
        for action, next_job in job_actions(job):
            yield action, replace(state, pending=next_job)
        if job.stage == Stage.RECEIVED:
            if state.storage and job.success and job.identity.version == state.version:
                yield "save build", replace(state, pending=None, saved=job.identity)
            elif state.storage:
                yield "discard result", replace(state, pending=None)
            else:
                yield "save fails", state
    if state.saved and state.saved.version == state.version:
        yield "complete build-only", replace(state, complete=True)


IGNORED_RESULTS = ("foreign reply", "old operation reply", "wrong version reply", "wrong build reply", "duplicate reply", "save fails")


def operation_contract(before, action, after, phase):
    if action in IGNORED_RESULTS and after != before:
        return f"{phase}-REJECTED-STATE-CHANGED"
    if action == "begin" and (
        not before.pending or before.pending.stage != Stage.QUEUED
        or after.pending != Pending(before.pending.identity, Stage.RUNNING)
    ):
        return f"{phase}-START-UNACCEPTED"
    if action in ("receive success", "receive failure") and (
        not before.pending or before.pending.stage != Stage.RUNNING
        or after.pending != Pending(before.pending.identity, Stage.RECEIVED, action == "receive success")
    ):
        return f"{phase}-REPLY-BEFORE-START"
    return None


def build_contract(before, action, after):
    if violation := operation_contract(before, action, after, "BUILD"):
        return violation
    if action == "accept build" and (before.pending is not None or after.issued != before.issued + 1
            or after.pending != Pending(Evidence(0, after.issued, before.version))):
        return "BUILD-WRONG-IDENTITY"
    if after.saved and after.saved != before.saved:
        if before.pending is None or before.pending.stage != Stage.RECEIVED or not before.pending.success or not before.storage:
            return "BUILD-UNSAVED-RESULT"
        if after.saved != before.pending.identity or after.saved.version != after.version:
            return "BUILD-STALE-RESULT"
    if after.complete and after.saved != Evidence(0, after.issued, after.version):
        return "BUILD-STALE-COMPLETE"
    return None


@dataclass(frozen=True)
class CheckFlow:
    version: int = 0
    build: Evidence = Evidence(0, 1, 0)
    issued: int = 0
    pending: Pending | None = None
    saved: Evidence | None = None
    failed: Evidence | None = None
    approved: Evidence | None = None
    storage: bool = True
    complete: bool = False


def check_actions(state):
    if not state.version:
        yield "edit", replace(state, version=1, complete=False)
    if state.build.operation == 1 and state.pending is None:
        yield "new build evidence", replace(state, build=Evidence(0, 2, state.version), complete=False)
    yield ("storage fails" if state.storage else "storage returns"), replace(state, storage=not state.storage)
    if state.pending is None and state.issued < 2 and state.build.version == state.version:
        number = state.issued + 1
        identity = Evidence(0, number, state.version, state.build.operation)
        yield "accept check", replace(state, issued=number, pending=Pending(identity), saved=None, failed=None,
                                      approved=None, complete=False)
    if job := state.pending:
        for action, next_job in job_actions(job):
            yield action, replace(state, pending=next_job)
        if job.stage == Stage.RECEIVED:
            if not state.storage:
                yield "save fails", state
            elif job.identity.version != state.version or job.identity.build != state.build.operation:
                yield "discard result", replace(state, pending=None)
            elif job.success:
                yield "save check", replace(state, pending=None, saved=job.identity)
            else:
                yield "save finding", replace(state, pending=None, failed=job.identity)
    if (finding := state.failed) and state.storage and (finding.version, finding.build) == (state.version, state.build.operation):
        yield "approve current finding with reason", replace(state, approved=finding)
    if (verified := state.saved or state.approved) and (verified.version, verified.build) == (state.version, state.build.operation):
        yield "complete required check", replace(state, complete=True)


def check_contract(before, action, after):
    if violation := operation_contract(before, action, after, "CHECK"):
        return violation
    if action == "accept check" and (before.pending is not None or after.issued != before.issued + 1
            or after.pending != Pending(Evidence(0, after.issued, before.version, before.build.operation))):
        return "CHECK-WRONG-IDENTITY"
    for success, new, old in ((True, after.saved, before.saved), (False, after.failed, before.failed)):
        if new and new != old:
            if before.pending is None or before.pending.stage != Stage.RECEIVED or not before.storage:
                return "CHECK-UNSAVED-RESULT"
            if before.pending.success is not success:
                return "CHECK-OUTCOME-CHANGED"
            if new != before.pending.identity or new.build != after.build.operation or new.version != after.version:
                return "CHECK-STALE-RESULT"
    if after.approved and (after.approved != after.failed or (action == "approve current finding with reason" and not before.storage)):
        return "CHECK-INVALID-APPROVAL"
    if after.complete:
        current = Evidence(0, after.issued, after.version, after.build.operation)
        if current not in (after.saved, after.approved) or after.version != after.build.version:
            return "CHECK-UNVERIFIED-COMPLETE"
    return None


@dataclass(frozen=True)
class Revisions:
    rule: int = 0
    reference: int = 0
    target: int = 0
    edit: int = 0


@dataclass(frozen=True)
class References:
    current: Revisions = Revisions()
    adopted: Revisions = Revisions()
    proposal: Revisions | None = None
    reviewed: bool = False
    conflicts: bool = False


def reference_actions(state):
    for name in ("rule", "reference", "target", "edit"):
        if not getattr(state.current, name):
            yield f"update {name}", replace(state, current=replace(state.current, **{name: 1}))
    yield "restart", state
    if state.proposal is None or state.proposal != state.current:
        if state.current != state.adopted:
            yield "prepare proposal", replace(state, proposal=state.current, reviewed=False, conflicts=False)
    if state.proposal:
        if state.proposal != state.current:
            yield "preserve conflict", replace(state, conflicts=True)
        elif not state.reviewed:
            yield "review proposal", replace(state, reviewed=True)
        elif state.current == state.proposal:
            yield "adopt proposal", replace(state, adopted=state.proposal, proposal=None, reviewed=False)


def reference_contract(before, action, after):
    if after.adopted != before.adopted and (
        action != "adopt proposal" or not before.reviewed or before.proposal != before.current or after.adopted != before.current
    ):
        return "REFERENCE-STALE-ADOPTION"
    if action == "preserve conflict" and (
        not after.conflicts or after.proposal != before.proposal or after.current != before.current
    ):
        return "REFERENCE-LOST-CONFLICT"
    return None


class Lock(IntEnum):
    EDITOR = 0
    WORKTREE = 1
    RUN = 2


@dataclass(frozen=True)
class Locks:
    owners: tuple[int | None, ...] = (None,) * 3
    waiting: tuple[Lock | None, ...] = (None,) * 2


def request_lock(state, worker, lock):
    if state.owners[lock] is not None:
        return replace(state, waiting=tuple(lock if w == worker else x for w, x in enumerate(state.waiting)))
    return Locks(tuple(worker if l == lock else owner for l, owner in enumerate(state.owners)),
                 tuple(None if w == worker else x for w, x in enumerate(state.waiting)))


def lock_actions(state):
    paths = ((Lock.EDITOR, Lock.WORKTREE, Lock.RUN), (Lock.WORKTREE, Lock.RUN))
    for worker, path in enumerate(paths):
        held = [lock for lock in path if state.owners[lock] == worker]
        if state.waiting[worker] is not None:
            lock = state.waiting[worker]
            if state.owners[lock] is None:
                yield f"acquire {worker}:{lock.name}", request_lock(state, worker, lock)
        elif len(held) == len(path):
            yield f"release {worker}", replace(state, owners=tuple(None if w == worker else w for w in state.owners))
        else:
            lock = path[len(held)]
            yield f"request {worker}:{lock.name}", request_lock(state, worker, lock)


def lock_contract(before, action, after):
    dependencies = tuple(after.owners[lock] if lock is not None else None for lock in after.waiting)
    if dependencies == (1, 0):
        return "LOCK-WAIT-CYCLE"
    for worker, waiting in enumerate(after.waiting):
        if waiting is not None and any(owner == worker and lock >= waiting for lock, owner in enumerate(after.owners)):
            return "LOCK-INVERTED-ORDER"
        newly = [lock for lock, owner in enumerate(after.owners) if owner == worker and before.owners[lock] != worker]
        if newly and any(owner == worker and lock >= min(newly) for lock, owner in enumerate(before.owners)):
            return "LOCK-INVERTED-ORDER"
    return None


class ExtendedRunStateModelsTests(unittest.TestCase):
    def check_model(self, initial, actions, contract, require):
        result = explore(initial, actions, contract)
        self.assertLessEqual(set(require), result.witnessed)
        self.assertGreater(result.states, 1)
        print(f"MODEL {type(initial).__name__} {result.states} states / {result.transitions} transitions / {result.seconds:.3f}s")

    def assert_counterexample(self, initial, actions, contract, mutation, expected_id, expected_trace):
        def mutated(state):
            yield from actions(state)
            if transition := mutation(state):
                yield transition

        with self.assertRaises(ModelFailure) as result:
            explore(initial, mutated, contract)
        self.assertEqual((result.exception.condition, result.exception.trace), (expected_id, expected_trace))

    def test_two_slots_are_fifo_and_other_runs_are_isolated(self):
        self.check_model(Dispatch(), dispatch_actions, dispatch_contract,
                         ("start 0", "start 2", "cancel 1", "stop 0", "unconfirmed 0", "finish both", "restart"))
        self.assert_counterexample(Dispatch(), dispatch_actions, dispatch_contract,
                                   lambda s: ("start 1", s.change(1, JobState.RUNNING, queue=(0, 2), active=(1,)))
                                   if s == Dispatch() else None,
                                   "DISPATCH-FIFO", ("start 1",))
        self.assert_counterexample(Dispatch(), dispatch_actions, dispatch_contract,
                                   lambda s: ("start 2", s.change(2, JobState.RUNNING, queue=(), active=(0, 1, 2)))
                                   if s.active == (0, 1) and s.queue == (2,) else None,
                                   "DISPATCH-CAPACITY", ("start 0", "start 1", "start 2"))
        self.assert_counterexample(Dispatch(), dispatch_actions, dispatch_contract,
                                   lambda s: ("stop 0", s.change(1, JobState.STOPPING)) if s.active == (0, 1) else None,
                                   "DISPATCH-OTHER-RUN", ("start 0", "start 1", "stop 0"))
        self.assert_counterexample(Dispatch(), dispatch_actions, dispatch_contract,
                                   lambda s: ("restart", Dispatch()) if s.active == (0,) and s.queue == (1, 2) else None,
                                   "DISPATCH-REPLAY", ("start 0", "restart"))
        self.assert_counterexample(Dispatch(), dispatch_actions, dispatch_contract,
                                   lambda s: ("start 0", replace(s, active=(0, 0))) if s.active == (0,) else None,
                                   "DISPATCH-DOUBLE-START", ("start 0", "start 0"))
        self.assert_counterexample(Dispatch(), dispatch_actions, dispatch_contract,
                                   lambda s: ("finish both", s) if s.active == (0, 1) else None,
                                   "DISPATCH-BATCH-FINISH", ("start 0", "start 1", "finish both"))

    def test_async_build_cannot_save_an_old_version_or_publish_before_save(self):
        self.check_model(BuildFlow(), build_actions, build_contract,
                         ("accept build", "begin", "receive success", "receive failure", "foreign reply",
                          "wrong version reply", "duplicate reply", "save fails", "save build", "discard result", "complete build-only"))
        self.assert_counterexample(BuildFlow(), build_actions, build_contract,
                                   lambda s: ("save build", replace(s, saved=s.pending.identity, pending=None))
                                   if s.pending and s.pending.success and s.version > s.pending.identity.version and s.storage else None,
                                   "BUILD-STALE-RESULT", ("accept build", "edit", "begin", "receive success", "save build"))
        self.assert_counterexample(BuildFlow(), build_actions, build_contract,
                                   lambda s: ("complete build-only", replace(s, complete=True))
                                   if s.pending and s.pending.success and not s.saved else None,
                                   "BUILD-STALE-COMPLETE", ("accept build", "begin", "receive success", "complete build-only"))
        self.assert_counterexample(BuildFlow(), build_actions, build_contract,
                                   lambda s: ("receive success", replace(s, pending=replace(
                                       s.pending, stage=Stage.RECEIVED, success=True)))
                                   if s.pending and s.pending.stage == Stage.QUEUED else None,
                                   "BUILD-REPLY-BEFORE-START", ("accept build", "receive success"))
        self.assert_counterexample(BuildFlow(), build_actions, build_contract,
                                   lambda s: ("begin", replace(s, pending=Pending(
                                       replace(s.pending.identity, operation=42), Stage.RUNNING)))
                                   if s.pending and s.pending.stage == Stage.QUEUED else None,
                                   "BUILD-START-UNACCEPTED", ("accept build", "begin"))

    def test_second_check_on_same_build_rejects_late_results_and_needs_valid_approval(self):
        self.check_model(CheckFlow(), check_actions, check_contract,
                         ("new build evidence", "accept check", "begin", "receive success", "receive failure", "old operation reply",
                          "wrong build reply", "duplicate reply", "save fails", "save finding", "approve current finding with reason",
                          "complete required check", "discard result"))
        self.assert_counterexample(CheckFlow(), check_actions, check_contract,
                                   lambda s: ("old operation reply", replace(s, pending=replace(s.pending, stage=Stage.RECEIVED, success=True)))
                                   if s.issued == 2 and s.build.operation == 1 and s.pending and s.pending.stage == Stage.RUNNING else None,
                                   "CHECK-REJECTED-STATE-CHANGED",
                                   ("accept check", "begin", "receive success", "save check", "accept check", "begin", "old operation reply"))
        self.assert_counterexample(CheckFlow(), check_actions, check_contract,
                                   lambda s: ("complete required check", replace(s, complete=True)) if s.failed else None,
                                   "CHECK-UNVERIFIED-COMPLETE",
                                   ("accept check", "begin", "receive failure", "save finding", "complete required check"))
        self.assert_counterexample(CheckFlow(), check_actions, check_contract,
                                   lambda s: ("save check", replace(s, pending=None, saved=s.pending.identity))
                                   if s.pending and s.pending.success is False and s.storage else None,
                                   "CHECK-OUTCOME-CHANGED", ("accept check", "begin", "receive failure", "save check"))

    def test_reference_adoption_does_not_replace_a_newer_version(self):
        self.check_model(References(), reference_actions, reference_contract,
                         ("update rule", "update reference", "update target", "update edit", "prepare proposal",
                          "review proposal", "preserve conflict", "restart", "adopt proposal"))
        self.assert_counterexample(References(), reference_actions, reference_contract,
                                   lambda s: ("adopt proposal", replace(s, adopted=s.proposal))
                                   if s.proposal and s.reviewed and s.proposal != s.current else None,
                                   "REFERENCE-STALE-ADOPTION", ("update rule", "prepare proposal", "review proposal", "update reference", "adopt proposal"))

    def test_lock_order_allows_progress_but_rejects_a_reverse_acquire(self):
        self.check_model(Locks(), lock_actions, lock_contract,
                         ("request 0:EDITOR", "request 1:WORKTREE", "acquire 0:WORKTREE", "release 0", "release 1"))
        self.assert_counterexample(Locks(), lock_actions, lock_contract,
                                   lambda s: ("request 1:EDITOR", request_lock(s, 1, Lock.EDITOR))
                                   if s.owners[:2] == (0, 1) else None,
                                   "LOCK-INVERTED-ORDER", ("request 0:EDITOR", "request 1:WORKTREE", "request 1:EDITOR"))

    def test_circular_wait_with_two_workers_is_detected(self):
        cycle = Locks((0, 1, None), (Lock.WORKTREE, Lock.EDITOR))
        self.assertEqual(lock_contract(cycle, "wait", cycle), "LOCK-WAIT-CYCLE")
