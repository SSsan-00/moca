"""Opt-in Windows browser measurement; only generated local Run data is used."""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from socket import SHUT_RDWR
from threading import Lock, Thread
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from uuid import UUID
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "tests"))

from browser_run import run_browser
from migration_harness.config import load_config
from migration_harness.verification import roslyn_syntax
from migration_harness.web import dashboard, dashboard_settings


class Workload:
    def __init__(self, config):
        self.config = config
        self.views = {}
        self.conversations = SimpleNamespace(preparation_replies=lambda *_: [])
        self.sockets = {}
        self.reconnects = []
        self.requests = Counter()
        self.lock = Lock()
        self.ids = [str(UUID(int=index)) for index in range(1, 6)]
        for index, run_id in enumerate(self.ids, 1):
            run = Path(config["run_root"]) / run_id
            tree = Path(config["worktree_root"]) / run_id
            run.mkdir(parents=True)
            tree.mkdir(parents=True)
            (tree / "Page.cs").write_text(f"class Bench{index} {{}}\n", encoding="utf-8")
            view = self.views[run_id] = dashboard.Dashboard()
            record = {
                "run_id": run_id, "unit_id": f"unit-{index}", "targets": ["Page.cs"], "target": "Page.cs",
                "project_root": config["project_root"], "worktree_path": str(tree),
                "status": "junie_running", "context_capsules": 10, "completed_capsules": 2, "current_capsule": 3,
            }
            (run / "run.json").write_text(json.dumps(record), encoding="utf-8")
            view.publish({**record, "operation_id": f"operation-{index}", "ready": False, "cancellable": True,
                          "active_stage": 3, "stage_timings": {"3": {"elapsed_seconds": 6}}})

    def get(self, run_id=None):
        return self.views[run_id or self.ids[0]]

    def workspace(self, *, include_usage):
        return {"units": [view.state["unit_id"] for view in self.views.values()],
                "runs": list(view.state for view in self.views.values()), "parallel_limit": 5, "busy": True}

    def burst(self, start, count):
        def push(run_id, view):
            # The state changes are not coalesced. All important events belong to each Run.
            base = view.state
            for number in range(start, start + count):
                activity = {"sequence": number, "kind": "command", "at": 1_700_000_000 + number,
                            "message": f"log {base['unit_id']} {number:03d}", "detail": "詳細ログ " * 32}
                state = {**base, "activity": activity, "observed_at": activity["at"]}
                if number in {17, 39, 71}:
                    state.update(current_capsule={17: 4, 39: 5, 71: 6}[number], completed_capsules={17: 3, 39: 4, 71: 5}[number])
                    base = state
                if number == 59:
                    state.update(stop_requested=True)
                    base = state
                if number == 60:
                    state.update(stop_requested=False)
                    base = state
                view.publish(state)
                if number % 5 == 0:
                    time.sleep(.012)
            return run_id, view.revision
        with ThreadPoolExecutor(max_workers=5) as pool:
            return dict(pool.map(lambda item: push(*item), self.views.items()))

    def handler(self, base):
        workload = self

        class FixtureHandler(base):
            def do_GET(self):
                route = urlsplit(self.path).path.removeprefix(self.server.prefix)
                with workload.lock:
                    workload.requests[route] += 1
                super().do_GET()

            def send_get_route(self):
                url = urlsplit(self.path)
                route = url.path.removeprefix(self.server.prefix)
                args = parse_qs(url.query)
                if route == "bench-burst":
                    data = workload.burst(int(args["start"][0]), int(args["count"][0]))
                elif route == "bench-drop":
                    run_id = args["run"][0]
                    with workload.lock:
                        connection = workload.sockets[run_id]
                    connection.shutdown(SHUT_RDWR)
                    data = {"dropped": run_id}
                elif route == "bench-counts":
                    with workload.lock:
                        data = {"requests": dict(workload.requests), "reconnects": workload.reconnects.copy()}
                else:
                    super().send_get_route()
                    return
                self.send_body(200, "application/json", json.dumps(data).encode())

            def send_events(self):
                run_id = self.dashboard.state["run_id"]
                with workload.lock:
                    workload.sockets[run_id] = self.connection
                    if cursor := self.headers.get("Last-Event-ID"):
                        workload.reconnects.append([run_id, int(cursor)])
                super().send_events()

        return FixtureHandler


def main(driver="browser.mjs", workload_type=Workload):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chrome", type=Path, default=Path(os.environ["ProgramFiles"]) / "Google/Chrome/Application/chrome.exe")
    args = parser.parse_args()
    node = shutil.which("node")
    if not node or not args.chrome.is_file():
        parser.error("Node.js 22+ and Chrome (or --chrome) must already be installed")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
        root = Path(directory)
        project = root / "app"
        project.mkdir()
        (project / ".git").mkdir()
        (project / "source.php").write_text("<?php echo 1;\n", encoding="utf-8")
        config_path = root / "harness.toml"
        config_path.write_text('[project]\nroot = "app"\nworktree_root = "trees"\nrun_root = "runs"\n' + "".join(
            f'\n[[units]]\nid = "unit-{n}"\nsource = "source.php"\ntarget = "Page.cs"\n' for n in range(1, 6)
        ), encoding="utf-8")
        config = load_config(config_path)
        workload = workload_type(config)
        view = workload.get()
        view.manager = workload
        assets = Path(dashboard.__file__).parent
        with view.server(config, dashboard.render_dashboard_html(" data-live data-workspace"),
                         (assets / "dashboard.js").read_bytes()) as server:
            server.RequestHandlerClass = workload.handler(server.RequestHandlerClass)
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with (
                    patch.object(dashboard_settings, "list_models", return_value=[]),
                    patch.object(roslyn_syntax, "editor_diagnostics", side_effect=lambda target, _content, version: {
                        "status": "unavailable", "target": target, "version": version, "findings": [],
                    }),
                ):
                    result = run_browser([
                        node, str(HERE / driver), str(args.chrome), str(root / "chrome"),
                        server.dashboard_url, *workload.ids[:2],
                    ], timeout=70)
            finally:
                for run_view in workload.views.values():
                    with run_view.condition:
                        run_view.closed = True
                        run_view.condition.notify_all()
                server.shutdown()
                thread.join()
            print(result.stdout, end="")
            if result.stderr:
                print(result.stderr, end="", file=sys.stderr)
            return result.returncode


if __name__ == "__main__":
    sys.exit(main())
