import codecs
from contextlib import ExitStack, contextmanager
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
from queue import Empty, Queue
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest.mock import patch

from fixtures import SavedRunFixture
from migration_harness.runs import run_records, stage_scripts, worktree_targets
from migration_harness.runs.run_manager import RunManager
from migration_harness.web import dashboard


TARGETS = (Path("First.cs"), Path("Second.cs"))
OLD = (b"class OldFirst {}", b"class OldSecond {}")
NEW = (codecs.BOM_UTF8 + b"class NewFirst {}", codecs.BOM_UTF8 + b"class NewSecond {}")
STOP_REASON = "fixture: original stop reason"
HOOK = "before_migration"


def read_json(path):
    return json.loads(path.read_bytes())


def emit(kind, data=None):
    print("MOCA-FAULT " + json.dumps([kind, data], ensure_ascii=True), flush=True)


def checkpoint(name):
    emit("boundary", name)
    if sys.stdin.readline().strip() != "continue":
        raise RuntimeError("The test parent stopped before releasing the fault barrier")


@contextmanager
def child(root, command, cut=""):
    stderr = root / f"{command}-stderr.txt"
    with stderr.open("w+b") as errors:
        process = subprocess.Popen(
            [sys.executable, "-u", str(Path(__file__).resolve()), "--fault-worker", str(root), command, cut],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors, encoding="utf-8",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        messages = Queue()

        def read_events():
            for line in process.stdout:
                if line.startswith("MOCA-FAULT "):
                    messages.put(json.loads(line.removeprefix("MOCA-FAULT ")))
            messages.put(("exit", None))

        reader = Thread(target=read_events, daemon=True)
        reader.start()

        class Handshake:
            def __init__(self):
                self.pid = process.pid

            def expect(self, kind):
                try:
                    event, data = messages.get(timeout=20)
                except Empty:
                    raise AssertionError(f"{command} child did not reach {kind}") from None
                if event != kind:
                    if event == "exit":
                        process.wait(timeout=5)
                        errors.seek(0)
                        raise AssertionError(f"{command} child exited before {kind}: " +
                                             errors.read().decode("utf-8", "replace"))
                    raise AssertionError(f"Expected {kind}, got {event}")
                return data

            def terminate_at(self, name):
                if self.expect("boundary") != name or process.poll() is not None:
                    raise AssertionError(f"{command} missed boundary {name}")
                process.kill()
                if not process.wait(timeout=10):
                    raise AssertionError("Fault worker exited successfully instead of being interrupted")

            def proceed(self):
                process.stdin.write("continue\n")
                process.stdin.flush()

            def result(self):
                payload = self.expect("result")
                if process.wait(timeout=12):
                    errors.seek(0)
                    raise AssertionError(errors.read().decode("utf-8", "replace"))
                return payload

        try:
            yield Handshake()
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
            reader.join(timeout=5)
            process.stdin.close()
            process.stdout.close()


def json_worker(root, cut):
    run_path = root / "run.json"
    if cut in ("before", "after"):
        original = Path.replace

        def replace_record(source, destination):
            if destination == run_path and cut == "before":
                checkpoint(cut)
            result = original(source, destination)
            if destination == run_path and cut == "after":
                checkpoint(cut)
            return result

        with patch.object(Path, "replace", replace_record):
            run_records.write_run_record(run_path, {"status": "interrupted", "error": STOP_REASON, "generation": 1})
    else:
        attempts = 0

        def delay(_seconds):
            nonlocal attempts
            attempts += 1
            if attempts == 1 and cut == "transient":
                checkpoint("sharing violation")

        with patch.object(run_records, "sleep", delay):
            try:
                with run_records.edit_run_record(run_path) as (_, changes):
                    changes["generation"] = 2
            except PermissionError as error:
                if cut != "persistent":
                    raise
                emit("result", {"winerror": error.winerror, "retries": attempts})
                return
        emit("result", {"retries": attempts, "record": read_json(run_path)})


def adoption_worker(root, cut):
    record_path = root / "run.json"
    agent, worktree = root / "agent", root / "tree"
    record = read_json(record_path)
    original = Path.replace
    replaced = []

    def watched_replace(source, destination):
        if destination == record_path:
            label = "plan" if "target_apply_pending" in read_json(source) else "finish"
        elif destination == worktree / TARGETS[0]:
            label = "first"
        elif destination == worktree / TARGETS[1]:
            label = "second"
        else:
            return original(source, destination)
        if cut == "before " + label:
            checkpoint(cut)
        result = original(source, destination)
        if destination.parent == worktree:
            replaced.append(destination.name)
        if cut == "after " + label:
            checkpoint(cut)
        return result

    with patch.object(Path, "replace", watched_replace):
        if cut in ("restart", "conflict"):
            try:
                worktree_targets.finish_pending_targets(agent, worktree, TARGETS, record, record_path)
            except ValueError as error:
                if cut != "conflict" or "external change" not in str(error):
                    raise
                emit("result", {"status": "blocked", "replaced": replaced})
                return
        else:
            worktree_targets.apply_agent_targets(agent, worktree, TARGETS, record["target_hashes"].copy(), record, record_path)
    emit("result", {"status": "ok", "replaced": replaced})


def queue_worker(root, cut):
    project = root / "project"
    project.mkdir(exist_ok=True)
    config = {"project_root": str(project), "run_root": str(root / "runs"), "worktree_root": str(root / "trees"),
              "units": [{"id": unit, "target": unit + ".cs"} for unit in ("a", "b")]}
    view = dashboard.Dashboard()
    view.publish({"status": "idle", "ready": True})
    name = root / "launched.json"
    manager = RunManager(config, view)
    launched = []

    def synthetic_operation(view, supplied, job):
        if supplied["project_root"] != str(project):
            raise AssertionError("Launched an operation outside the fixture")
        launched.append([job["run_id"], job["operation_id"]])
        name.write_text(json.dumps(launched), encoding="utf-8")
        return {}

    original = Path.replace

    def replace_queue(source, destination):
        if destination != manager.queue_path or not any(job.get("running") for job in read_json(source)["jobs"]):
            return original(source, destination)
        if cut == "before start":
            checkpoint(cut)
        result = original(source, destination)
        if cut == "after start":
            checkpoint(cut)
        return result

    try:
        manager.limit = 0 if cut != "restart" else 1
        with patch.object(dashboard, "execute_dashboard_action", synthetic_operation):
            if cut == "restart":
                blocked = dict(manager.blocked_conversations)
                manager.start_pending()
                with manager.condition:
                    if not manager.condition.wait_for(lambda: not manager.jobs, timeout=12):
                        raise AssertionError("restored operations were not dispatched")
                emit("result", {"blocked": blocked, "launched": read_json(name) if name.exists() else []})
            else:
                manager.submit({"action": "run", "unit_ids": ["a", "b"]})
                manager.limit = 1
                with patch.object(Path, "replace", replace_queue):
                    manager.start_pending()
                    with manager.condition:
                        if not manager.condition.wait_for(lambda: not manager.jobs, timeout=12):
                            raise AssertionError("normal operations were not dispatched")
                emit("result", {"blocked": {}, "launched": read_json(name)})
    finally:
        manager.close()


def hook_worker(root, cut):
    config, run_id = read_json(root / "child-hook.json")
    path, worktree, record, _ = run_records.load_saved_run(config, run_id)
    record_path = path / "run.json"
    original = run_records.replace_run_record

    def replace_result(destination, value):
        if destination == record_path and value.get("script_command_progress", {}).get(HOOK):
            checkpoint("hook command exited")
        return original(destination, value)

    if cut == "cut":
        with patch.object(run_records, "replace_run_record", replace_result):
            stage_scripts.run_script_phase(config, HOOK, worktree, path, record, once=True)
    elif cut == "resume":
        try:
            stage_scripts.run_script_phase(config, HOOK, worktree, path, record, once=True)
        except ValueError as error:
            if "実行結果不明" not in str(error):
                raise
            emit("result", {"blocked": True})
        else:
            emit("result", {"blocked": False})
    elif cut == "approve":
        plan = stage_scripts.script_recovery(config, run_id)
        emit("result", stage_scripts.script_recovery(config, run_id, retry_version=plan["version"]))
    else:
        stage_scripts.run_script_phase(config, HOOK, worktree, path, record, once=True)
        emit("result", {"completed": HOOK in read_json(record_path)["completed_script_phases"]})


@contextmanager
def open_without_delete_sharing(path):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    # Read/write sharing lets the writer inspect the old JSON, but without FILE_SHARE_DELETE its replace must fail.
    handle = create(str(path), 0x80000000, 3, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield
    finally:
        if not close(handle):
            raise ctypes.WinError(ctypes.get_last_error())


class WindowsRecoveryFaultTests(unittest.TestCase):
    def test_atomic_json_survives_termination_before_and_after_replace(self):
        for cut, published in (("before", 0), ("after", 1)):
            with self.subTest(cut=cut), TemporaryDirectory() as folder:
                root = Path(folder).resolve()
                record = root / "run.json"
                run_records.write_run_record(record, {"status": "interrupted", "error": STOP_REASON, "generation": 0})
                with child(root, "json", cut) as process:
                    process.terminate_at(cut)
                self.assertEqual(read_json(record)["generation"], published)
                self.assertEqual(read_json(record)["error"], STOP_REASON)
                self.assertEqual(record.with_suffix(".json.tmp").exists(), cut == "before")
                with child(root, "json", "restart") as process:
                    result = process.result()
                self.assertEqual((result["record"]["generation"], result["record"]["error"]), (2, STOP_REASON))
                self.assertFalse(record.with_suffix(".json.tmp").exists())

    def test_real_windows_sharing_violation_retries_or_leaves_old_record(self):
        for cut in ("transient", "persistent"):
            with self.subTest(cut=cut), TemporaryDirectory() as folder:
                root = Path(folder).resolve()
                record = root / "run.json"
                run_records.write_run_record(record, {"status": "interrupted", "error": STOP_REASON, "generation": 0})
                persisted = record.read_bytes()
                with ExitStack() as lock:
                    lock.enter_context(open_without_delete_sharing(record))
                    with child(root, "json", cut) as writer:
                        if cut == "transient":
                            self.assertEqual(writer.expect("boundary"), "sharing violation")
                            self.assertEqual(record.read_bytes(), persisted)
                            lock.close()
                            writer.proceed()
                            result = writer.result()
                            self.assertEqual(result["record"]["generation"], 2)
                        else:
                            result = writer.result()
                            self.assertIn(result["winerror"], (5, 32))
                            self.assertEqual(record.read_bytes(), persisted)
                        self.assertGreater(result["retries"], 0)
                with child(root, "json", "restart") as writer:
                    self.assertEqual(writer.result()["record"]["error"], STOP_REASON)
                self.assertFalse(record.with_suffix(".json.tmp").exists())

    def test_two_target_adoption_restarts_at_each_durable_cut(self):
        cases = (
            ("before plan", 0, False), ("after plan", 0, True),
            ("before first", 0, True), ("after first", 1, True),
            ("before second", 1, True), ("after second", 2, True),
            ("before finish", 2, True), ("after finish", 2, False),
        )
        for cut, count, pending, conflict in [(*case, False) for case in cases] + [("after first", 1, True, True)]:
            with self.subTest(cut=cut, conflict=conflict), TemporaryDirectory() as folder:
                root = Path(folder).resolve()
                tree, agent = root / "tree", root / "agent"
                tree.mkdir()
                agent.mkdir()
                for name, old, new in zip(TARGETS, OLD, NEW, strict=True):
                    (tree / name).write_bytes(old)
                    (agent / name).write_bytes(new)
                record_path = root / "run.json"
                run_records.write_run_record(record_path, {"status": "interrupted", "error": STOP_REASON,
                    "target": TARGETS[0].as_posix(),
                    "target_hashes": {name.as_posix(): worktree_targets.target_hash(tree / name) for name in TARGETS}})
                with child(root, "adoption", cut) as process:
                    process.terminate_at(cut)
                self.assertEqual(tuple((tree / name).read_bytes() for name in TARGETS), NEW[:count] + OLD[count:])
                saved = read_json(record_path)
                self.assertEqual("target_apply_pending" in saved, pending)
                self.assertEqual(bool(saved.get("target_applied")), cut == "after finish")
                if conflict:
                    (tree / TARGETS[1]).write_bytes(b"class ManualEdit {}")
                persisted = record_path.read_bytes()
                with child(root, "adoption", "conflict" if conflict else "restart") as process:
                    result = process.result()
                self.assertEqual(result["status"], "blocked" if conflict else "ok")
                self.assertEqual(result["replaced"], [] if conflict or not pending else [name.name for name in TARGETS[count:]])
                if conflict:
                    self.assertEqual((tree / TARGETS[0]).read_bytes(), NEW[0])
                    self.assertEqual((tree / TARGETS[1]).read_bytes(), b"class ManualEdit {}")
                    self.assertEqual(record_path.read_bytes(), persisted)
                elif cut == "before plan":
                    self.assertEqual(tuple((tree / name).read_bytes() for name in TARGETS), OLD)
                    self.assertEqual(record_path.read_bytes(), persisted)
                    with child(root, "adoption", "normal") as process:
                        self.assertEqual(process.result(), {"status": "ok", "replaced": [name.name for name in TARGETS]})
                if not conflict:
                    self.assertEqual(tuple((tree / name).read_bytes() for name in TARGETS), NEW)
                    final = read_json(record_path)
                    self.assertTrue(final["target_applied"])
                    self.assertNotIn("target_apply_pending", final)
                    self.assertEqual(final["target_hashes"], {name.as_posix(): worktree_targets.target_hash(tree / name)
                                                             for name in TARGETS})
                    self.assertEqual(final["error"], STOP_REASON)

    def test_queue_restart_only_launches_waiting_operations(self):
        for cut in ("before start", "after start", "normal"):
            with self.subTest(cut=cut), TemporaryDirectory() as folder:
                root = Path(folder).resolve()
                if cut == "normal":
                    with child(root, "queue", cut) as first:
                        self.assertEqual(len(first.result()["launched"]), 2)
                    continue
                with child(root, "queue", cut) as first:
                    first.terminate_at(cut)
                self.assertFalse((root / "launched.json").exists())
                queue = root / "runs" / "dashboard-queue.json"
                saved = read_json(queue)["jobs"]
                self.assertEqual([job["unit_id"] for job in saved], ["a", "b"])
                self.assertEqual([bool(job.get("running")) for job in saved], [cut == "after start", False])
                with child(root, "queue", "restart") as restarted:
                    result = restarted.result()
                expected = saved[1:] if cut == "after start" else saved
                self.assertEqual(result["launched"], [[job["run_id"], job["operation_id"]] for job in expected])
                self.assertEqual(set(result["blocked"]), {saved[0]["run_id"]} if cut == "after start" else set())
                self.assertEqual(read_json(queue)["jobs"], [])


class WindowsHookCrashTests(SavedRunFixture):
    def test_unknown_once_hook_survives_process_death_without_implicit_replay(self):
        root = self.run_path.parent.parent
        config = {**self.config}
        config.pop("diff_viewer_anchor_reference")
        config["scripts"] = {HOOK: [[sys.executable, "-c",
            "from pathlib import Path; p=Path('fixture-count'); p.write_text(str(int(p.read_text())+1) if p.exists() else '1')"]]}
        (root / "child-hook.json").write_text(json.dumps([config, self.run_id]), encoding="utf-8")
        self.save_record({**self.record(), "status": "prepared", "completed_capsules": 0})
        counter = self.worktree / "fixture-count"
        with child(root, "hook", "cut") as first:
            first.terminate_at("hook command exited")
        self.assertEqual(counter.read_text(), "1")
        original = self.record()["script_inflight"]
        self.assertEqual((original["phase"], original["number"]), (HOOK, 1))
        receipt = self.record_path.read_bytes()
        with child(root, "hook", "resume") as restarted:
            self.assertEqual(restarted.result(), {"blocked": True})
        self.assertEqual(counter.read_text(), "1")
        self.assertEqual(self.record_path.read_bytes(), receipt)
        with child(root, "hook", "approve") as approved:
            self.assertTrue(approved.result()["retried"])
        self.assertEqual(counter.read_text(), "2")
        self.assertNotIn("script_inflight", self.record())
        with child(root, "hook", "normal") as again:
            self.assertTrue(again.result()["completed"])
        self.assertEqual(counter.read_text(), "2")


if __name__ == "__main__":
    if len(sys.argv) != 5 or sys.argv[1] != "--fault-worker":
        raise SystemExit("Use unittest to start the isolated workers")
    root, command, cut = sys.argv[2:]
    {"json": json_worker, "adoption": adoption_worker, "queue": queue_worker, "hook": hook_worker}[command](
        Path(root).resolve(strict=True), cut
    )
