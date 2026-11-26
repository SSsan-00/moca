import argparse
from collections import Counter
from contextlib import ExitStack
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Thread, active_count
from time import monotonic, sleep
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener
from unittest.mock import patch
from uuid import uuid4

if sys.platform != "win32":
    raise SystemExit("This evaluation requires native Windows Job Objects.")

from tests.support.fixtures import HarnessCase, stub_syntax_diagnostics
from migration_harness import execution
from migration_harness.junie import junie_cli
from migration_harness.junie.junie_notifications import notification_settings
from migration_harness.runs import run_records
from migration_harness.runs.run_manager import RunManager
from migration_harness.web.dashboard import Dashboard


class MemoryCounters(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
        (name, ctypes.c_size_t) for name in ("peak_working", "working", "peak_paged", "paged", "peak_non_paged",
                                             "non_paged", "pagefile", "peak_pagefile", "private")
    ]


class WindowsProbe:
    def __init__(self):
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        self.current = kernel.GetCurrentProcess
        self.current.restype = wintypes.HANDLE
        self.handles = kernel.GetProcessHandleCount
        self.handles.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        self.handles.restype = wintypes.BOOL
        self.memory = psapi.GetProcessMemoryInfo
        self.memory.argtypes = [wintypes.HANDLE, ctypes.POINTER(MemoryCounters), wintypes.DWORD]
        self.memory.restype = wintypes.BOOL
        self.open = kernel.OpenProcess
        self.open.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.open.restype = wintypes.HANDLE
        self.wait = kernel.WaitForSingleObject
        self.wait.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.wait.restype = wintypes.DWORD
        self.close = kernel.CloseHandle
        self.close.argtypes = [wintypes.HANDLE]
        self.close.restype = wintypes.BOOL

    def snapshot(self):
        handle = self.current()
        count = wintypes.DWORD()
        memory = MemoryCounters(size=ctypes.sizeof(MemoryCounters))
        if not self.handles(handle, ctypes.byref(count)) or not self.memory(handle, ctypes.byref(memory), memory.size):
            raise ctypes.WinError(ctypes.get_last_error())
        return {"handles": count.value, "python_threads": active_count(), "working_set_bytes": memory.working,
                "private_bytes": memory.private}

    def watch(self, pid, stack):
        handle = self.open(0x00100000, False, pid)  # SYNCHRONIZE; only the PID published by the owned fixture.
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        stack.callback(self.close, handle)
        if self.wait(handle, 0) != 258:  # WAIT_TIMEOUT: it really started and is still running.
            raise RuntimeError(f"fixture process {pid} did not stay alive at its start barrier")
        return handle


# Both child processes are launched by the fixture, never located by their executable names.
OWNED = """
from pathlib import Path
import json, os, subprocess, sys, time
folder = Path(sys.argv[1]); release = Path(sys.argv[2])
with subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(70)']) as child:
    pending = folder / 'pid.tmp'; pending.write_text(json.dumps([os.getpid(), child.pid]), encoding='ascii')
    pending.replace(folder / 'pids.json')
    try:
        until = time.monotonic() + 60
        while not release.is_file() and time.monotonic() < until:
            time.sleep(.025)
        if not release.is_file():
            raise RuntimeError('fixture release timed out')
    finally:
        if child.poll() is None:
            child.terminate()
"""


def wait_for(predicate, label, timeout=45):
    end = monotonic() + timeout
    while monotonic() < end:
        if predicate():
            return
        sleep(.03)
    raise TimeoutError(label)


def disk_use(root):
    totals = Counter()
    for name, directory in (("runs", root / "runs"), ("worktrees", root / "trees"), ("process_fixtures", root / "owned")):
        totals[name + "_bytes"] = sum(p.stat().st_size for p in directory.rglob("*") if p.is_file())
    totals["run_records"] = len(list((root / "runs").glob("*/run.json")))
    totals["log_bytes"] = sum(p.stat().st_size for p in (root / "runs").rglob("*.log"))
    totals["pending_tmp_files"] = len(list((root / "runs").rglob("*.tmp")))
    return totals


def dashboard_connection(view, config, stack):
    server = view.server(config, b"", b"")
    stack.callback(server.server_close)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
    thread.start()
    stack.callback(thread.join, 5)
    stack.callback(server.shutdown)
    return server.dashboard_url


class LocalDashboard:
    def __init__(self, url):
        self.url = url
        self.origin = f"http://{urlsplit(url).netloc}"
        self.opener = build_opener(ProxyHandler({}))

    def request(self, endpoint, run_id=None, *, body=None, cursor=None):
        headers = {"X-Run-ID": run_id} if run_id else {}
        if cursor is not None:
            headers["Last-Event-ID"] = str(cursor)
        if body is not None:
            headers.update(Origin=self.origin, **{"Content-Type": "application/json"})
        return self.opener.open(Request(self.url + endpoint, json.dumps(body).encode() if body is not None else None,
                                        headers), timeout=8)

    def workspace(self):
        with self.request("workspace") as response:
            return json.load(response)

    def event(self, run_id, after=0):
        with self.request("events", run_id, cursor=after) as response:
            for _ in range(32):
                line = response.readline(128)
                if line.startswith(b"id: "):
                    event_id = int(line[4:])
                    data = response.readline(512 * 1024)
                    if event_id <= after or not data.startswith(b"data: ") or json.loads(data[6:])["run_id"] != run_id:
                        raise RuntimeError("a reconnect returned an old or unrelated event")
                    return event_id
                if not line:
                    break
        raise RuntimeError("no dashboard event received")


def cycle(manager, dashboard, probe, config, owned, logs, index):
    release = owned / "release"
    started = [owned / str(unit) for unit in range(5)]
    for directory in started:
        directory.mkdir(parents=True)
    launches = Counter()
    native_process = execution.run_process

    def agent(_command, completion, token, targets, **_kwargs):
        unit = int(targets[0].stem.removeprefix("Page"))
        launches[unit] += 1
        folder = started[unit]
        if launches[unit] > 1:
            folder = started[unit] / "resume"
            folder.mkdir()
        targets[0].write_text(f"class Page{unit} {{}}\n", encoding="utf-8")
        result = native_process([sys.executable, "-c", OWNED, str(folder), str(release)], label="owned fixture", synchronous=True,
                                timeout=80, cwd=owned, stdout=logs, stderr=subprocess.STDOUT)
        if result.returncode:
            raise RuntimeError(f"synthetic child failed with {result.returncode}")
        if unit == 1 and launches[unit] == 1:
            raise junie_cli.JunieConnectionError("synthetic connection failure at migration boundary")
        completion.write_text(token, encoding="ascii")
        return 0

    units = [f"unit-{unit}" for unit in range(5)]
    with patch.object(junie_cli, "run_interactive", agent), ExitStack() as tracked:
        run_ids = manager.submit({"action": "run", "unit_ids": units})["run_ids"]
        try:
            wait_for(lambda: all((folder / "pids.json").is_file() for folder in started), "five workers did not start")
            pids = [pid for folder in started for pid in json.loads((folder / "pids.json").read_text(encoding="ascii"))]
            if len(set(pids)) != 10 or len(manager.workers) != 5:
                raise RuntimeError("expected five simultaneous launcher/child pairs")
            handles = [probe.watch(pid, tracked) for pid in pids]
            active = probe.snapshot()
            cursor = dashboard.event(run_ids[0])
            if not dashboard.workspace()["busy"]:
                raise RuntimeError("dashboard omitted the running jobs")
            with manager.condition:
                view = manager.get(run_ids[0])
                last_seen = view.revision
                if cursor > last_seen or not view.request_stop({"operation_id": view.state["operation_id"]}):
                    raise RuntimeError("the intended run did not receive the stop request")
            dashboard.event(run_ids[0], after=last_seen)
            wait_for(lambda: all(probe.wait(handle, 0) == 0 for handle in handles[:2]), "stopped fixture pair remained")
        finally:
            release.touch()
        wait_for(lambda: not manager.busy, "initial fixtures did not settle")
        if any(probe.wait(handle, 0) != 0 for handle in handles):
            raise RuntimeError("a tracked fixture process remained after completion")
        for unit, key in enumerate(("last_stop", "error")):
            saved = run_records.read_run_record(Path(config["run_root"]) / run_ids[unit] / "run.json")
            if not saved.get(key) or saved["completed_capsules"] or not manager.get(run_ids[unit]).state.get("resumable"):
                raise RuntimeError(f"missing temporary failure or resume checkpoint for unit-{unit}")
        for run_id in run_ids[:2]:
            manager.submit({"action": "resume-run", "run_id": run_id})
        wait_for(lambda: not manager.busy, "resumed runs did not finish")
    rows = {row["run_id"]: row for row in dashboard.workspace()["runs"]}
    if any(rows[run_id]["status"] != "build_succeeded" for run_id in run_ids) or len(rows) != index * 5:
        raise RuntimeError("runs mixed, were lost, or did not finish")
    return {"cycle": index, "completed_runs": len(rows), "active": active, "idle": probe.snapshot(),
            "observed_stopped_or_completed_processes": len(pids), "disk": disk_use(Path(config["run_root"]).parent)}


def exercise(seconds, max_cycles):
    probe = WindowsProbe()
    fixture = HarnessCase()
    began = monotonic()
    native_process = execution.run_process
    baseline = probe.snapshot()
    with ExitStack() as stack:
        stack.callback(fixture.doCleanups)
        fixture.setUp()
        (fixture.project / "source.php").write_text("<?php echo 42;\n", encoding="utf-8")
        fixture.commit_project()
        config = fixture.config()
        config["junie_notifications"] = notification_settings({"enabled": False})
        template = config["units"][0]
        config["units"] = [{**template, "id": f"unit-{index}", "target": f"Page{index}.cs"} for index in range(5)]
        view = Dashboard()
        view.publish({"status": "idle", "ready": True})
        manager = RunManager(config, view)
        view.manager = manager
        url = dashboard_connection(view, config, stack)
        stack.callback(manager.close)
        dashboard = LocalDashboard(url)
        owned = fixture.root / "owned"
        owned.mkdir()
        logs = stack.enter_context((owned / "owned.log").open("ab"))

        def process(args, **kwargs):
            if args[0] != "dotnet":
                return native_process(args, **kwargs)
            kwargs["stdout"].write(b"Synthetic build log: fixture reached the build phase.\n")
            return subprocess.CompletedProcess(args, 0)

        count = 0
        with (patch.object(junie_cli, "require_interactive_cli"), patch.object(execution, "run_process", process),
              stub_syntax_diagnostics()):
            while count < max_cycles and (count == 0 or monotonic() - began < seconds):
                count += 1
                item = cycle(manager, dashboard, probe, config, owned / f"cycle-{count}", logs, count)
                print("SOAK_SAMPLE " + json.dumps(item), flush=True)
        if item["disk"]["pending_tmp_files"]:
            raise RuntimeError("run record temporary files remained at quiescence")
    wait_for(lambda: active_count() <= baseline["python_threads"], "dashboard threads did not quiesce", timeout=16)
    after = probe.snapshot()
    if fixture.root.exists():
        raise RuntimeError("synthetic workspace was not removed")
    print("SOAK_RESULT " + json.dumps({"system": platform.platform(), "python": sys.version.split()[0], "elapsed_seconds":
          round(monotonic() - began, 3), "cycles": count, "before": baseline, "after": after, "scratch_removed": True}), flush=True)


def main():
    parser = argparse.ArgumentParser(description="Run a bounded native Windows endurance evaluation with owned fixture processes.")
    parser.add_argument("--seconds", type=int, default=600)
    parser.add_argument("--max-cycles", type=int, default=100)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 1 <= args.seconds <= 3600 or not 1 <= args.max_cycles <= 100:
        parser.error("choose 1–3600 seconds and 1–100 cycles")
    if args.worker:
        exercise(args.seconds, args.max_cycles)
    else:
        with TemporaryDirectory(prefix="moca-endurance-supervisor-") as directory:
            env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
            env.update(TMP=directory, TEMP=directory, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
            execution.run_process([sys.executable, __file__, *sys.argv[1:], "--worker"], label="Windows endurance evaluation",
                                  synchronous=True, check=True, timeout=args.seconds + 180, env=env)


if __name__ == "__main__":
    main()
