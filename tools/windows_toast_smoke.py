"""Desktop smoke test for the production Windows toast backend. No model or project needed."""

import argparse
import os
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Lock, Thread
from time import monotonic
from urllib.parse import parse_qs, urlsplit

from migration_harness.junie.junie_notifications import JunieNotifications, RequestKey, WindowsToast, notification_settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=int, help="Override with a uniform interval for fast checks")
    parser.add_argument("--timeout", type=int, default=330)
    parser.add_argument("--verify-stop", action="store_true", help="Keep the request pending through the final reminder")
    args = parser.parse_args()
    if os.name != "nt" or args.timeout <= 0:
        parser.error("Run on Windows with a positive timeout.")
    settings = notification_settings({"repeat_after_seconds": [args.interval, 2 * args.interval]}
                                     if args.interval is not None else {})
    key = RequestKey("native-fixture-b", "fixture-operation-b", "fixture-request-b")
    expected = {"run": [key.run_id], "junie_operation": [key.operation_id], "junie_request": [key.request_id]}
    waiting, clicked = Event(), Event()
    waiting.set()
    backend = WindowsToast()
    started = monotonic()
    show_count = 0
    show_attempts = 0
    attempts_at_open = None
    lock = Lock()

    def report(action, tag, url):
        nonlocal show_count, show_attempts
        if action == "show":
            with lock:
                show_attempts += 1
        try:
            backend(action, tag, url)
        except (OSError, subprocess.SubprocessError) as error:
            print(action, type(error).__name__, getattr(error, "returncode", ""), flush=True)
            raise
        if action == "show":
            with lock:
                show_count += 1
        print(action, "Windows API accepted", f"{monotonic() - started:.1f}s", flush=True)

    class Endpoint(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass  # Do not print the opaque notification link.

        def do_GET(self):
            nonlocal attempts_at_open
            parsed = urlsplit(self.path)
            if parsed.path.startswith("/junie-open/"):
                with lock:
                    target = notices.location(parsed.path.rsplit("/", 1)[-1], activate=True)
                    if target and attempts_at_open is None:
                        attempts_at_open = show_attempts
                if target:
                    self.send_response(303)
                    self.send_header("Location", target)
                    self.end_headers()
                    return
            if parsed.path == "/acceptance" and parse_qs(parsed.query) == expected:
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"Windows notification test: correct Run B opened. No migration was run.")
                clicked.set()
                return
            self.send_error(404)

    with ThreadingHTTPServer(("127.0.0.1", 0), Endpoint) as server:
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        notices = JunieNotifications(lambda: {key: {}} if waiting.is_set() else {},
                                     lambda: settings, f"http://127.0.0.1:{server.server_port}",
                                     "/acceptance", backend=report)
        try:
            print("Minimize the browser. Click the Windows toast to verify the Run B link.", flush=True)
            if args.verify_stop:
                print("Click before the last scheduled reminder; the fixture stays pending after opening.", flush=True)
            notices.start()
            success = clicked.wait(args.timeout)
            print("Matching Run/operation/request:", success, "successful API calls:", show_count, flush=True)
            if not success or not args.verify_stop:
                return 0 if success else 1
            offsets = settings["repeat_after_seconds"]
            if attempts_at_open is None or attempts_at_open > len(offsets):
                print("INCONCLUSIVE: open the toast before the last scheduled reminder.", flush=True)
                return 2
            Event().wait(max(0, started + offsets[-1] + 8 - monotonic()))
            with lock:
                stopped = waiting.is_set() and show_attempts == attempts_at_open
            print("No further show attempts while the request remained pending:", stopped, flush=True)
            return 0 if stopped else 1
        finally:
            waiting.clear()
            notices.close()
            server.shutdown()
            worker.join()


if __name__ == "__main__":
    raise SystemExit(main())
