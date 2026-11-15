from __future__ import annotations

import json
import re
import secrets
import sys
import time
from collections import deque
from concurrent.futures import Future
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Empty, Queue
from threading import Condition, Thread
from typing import Any

from migration_harness.diff_viewer import create_snapshot, review_html, snapshot_bytes
from migration_harness.review_anchors import generate_anchors
from migration_harness.workflow import (
    _load_saved_run, can_resume_run, migration_ledger, resume_with_junie, review_checker, run_observer, run_with_junie,
)


PROGRESS_FIELDS = (
    "run_id", "unit_id", "target", "worktree_path", "status", "created_at", "current_capsule",
    "context_capsules", "completed_capsules", "context_part_chars", "max_context_chars",
    "build_exit_code", "build_log_path", "checker_exit_code", "checker_log_path", "error",
)


class Dashboard:
    def __init__(self) -> None:
        self.condition = Condition()
        self.events: deque[tuple[int, bytes]] = deque(maxlen=100)
        self.revision = 0
        self.state: dict[str, Any] = {}
        self.actions: Queue[dict[str, Any]] = Queue(maxsize=1)
        self.closed = False

    def publish(self, state: dict[str, Any]) -> None:
        with self.condition:
            if state == self.state:
                return
            self.state = state
            self.revision += 1
            payload = json.dumps({**state, "observed_at": time.time()}, ensure_ascii=False).encode("utf-8")
            self.events.append((self.revision, payload))
            self.condition.notify_all()

    def observe(self, record: dict[str, Any]) -> None:
        state = {key: record[key] for key in PROGRESS_FIELDS if key in record}
        state["repair_count"] = len(record.get("repair_history", []))
        with self.condition:
            fields = (*PROGRESS_FIELDS, "repair_count")
            if state != {key: self.state[key] for key in fields if key in self.state}:
                self.publish(state)

    def ready(self, config: dict[str, Any], result: dict[str, Any], message: str = "") -> None:
        state = {
            **self.state, "ready": True, "status": result["status"], "message": message,
            "resumable": can_resume_run(config, result),
        }
        if result["status"] == "checker_needs_review":
            try:
                state["review"] = review_checker(config, result["run_id"], None, None)
            except (OSError, ValueError, RuntimeError) as exc:
                state["review_error"] = str(exc)
        self.publish(state)

    def submit(self, action: dict[str, Any]) -> bool:
        with self.condition:
            generating_anchors = action["action"] == "anchors"
            if (
                not self.state.get("ready")
                or action.get("revision") != self.revision
                or (generating_anchors and not self.state.get("worktree_path"))
                or (action["action"] == "resume" and not self.state.get("resumable"))
                or (action["action"] == "accept" and self.state.get("status") != "checker_needs_review")
            ):
                return False
            self.actions.put_nowait(action)
            self.publish({
                **self.state, "ready": False, "message": "", "generating_anchors": generating_anchors,
            })
            return True

    def server(
        self, config: dict[str, Any], html: bytes, script: bytes, anchor_reference: Path | None,
    ) -> ThreadingHTTPServer:
        dashboard = self
        # ポート番号だけで操作できないようにし、他サイトからのPOSTも拒否する。
        prefix = "/" + secrets.token_urlsafe(24) + "/"

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                pass

            def send_body(
                self, status: int, content_type: str, body: bytes, headers: dict[str, str] | None = None,
            ) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                for name, value in {"X-Frame-Options": "DENY", **(headers or {})}.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                if self.headers.get("Host") != host:
                    self.send_error(403)
                elif self.path == prefix:
                    self.send_body(200, "text/html; charset=utf-8", html)
                elif self.path == prefix + "dashboard.js":
                    self.send_body(200, "text/javascript; charset=utf-8", script)
                elif self.path == prefix + "events":
                    self.send_events()
                elif self.path == prefix + "migration-ledger":
                    run_id = dashboard.state.get("run_id")
                    if run_id and dashboard.state.get("worktree_path"):
                        try:
                            ledger = migration_ledger(config, run_id)
                        except (OSError, ValueError, KeyError, TypeError) as exc:
                            self.send_body(409, "text/plain; charset=utf-8", str(exc).encode("utf-8"))
                        else:
                            self.send_body(200, "application/json", json.dumps(ledger, ensure_ascii=False).encode("utf-8"))
                    else:
                        self.send_error(404)
                elif self.path.startswith(prefix + "review/"):
                    self.send_comparison()
                else:
                    self.send_error(404)

            def send_comparison(self) -> None:
                match = re.fullmatch(r"([a-f0-9]{32})\.(html|json)", self.path.removeprefix(prefix + "review/"))
                if not match or not (run_id := dashboard.state.get("run_id")):
                    self.send_error(404)
                    return
                try:
                    workspace = snapshot_bytes(config, run_id, match[1])
                    if match[2] == "json":
                        self.send_body(200, "application/json", workspace, {
                            "Content-Disposition": f'attachment; filename="review-{match[1]}.json"',
                        })
                    else:
                        # Viewerにはスナップショットだけを渡し、進捗画面や制御APIから隔離する。
                        self.send_body(200, "text/html; charset=utf-8", review_html(config, workspace), {
                            "X-Frame-Options": "SAMEORIGIN",
                            "Content-Security-Policy": (
                                "sandbox allow-scripts allow-downloads; default-src 'none'; "
                                "script-src 'unsafe-inline' blob:; style-src 'unsafe-inline' blob:; "
                                "img-src data: blob:; font-src data: blob:; worker-src blob:; "
                                "base-uri 'none'; frame-ancestors 'self'"
                            ),
                        })
                except (OSError, RuntimeError, ValueError) as exc:
                    headers = {"X-Frame-Options": "SAMEORIGIN"} if match[2] == "html" else None
                    self.send_body(409, "text/plain; charset=utf-8", f"比較ツールを開けませんでした。\n{exc}".encode("utf-8"), headers)

            def send_events(self) -> None:
                try:
                    cursor = max(0, int(self.headers.get("Last-Event-ID", "0")))
                except ValueError:
                    self.send_error(400)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.connection.settimeout(5)
                try:
                    while True:
                        with dashboard.condition:
                            dashboard.condition.wait_for(
                                lambda: dashboard.closed or dashboard.revision > cursor, timeout=10,
                            )
                            events = [(seq, data) for seq, data in dashboard.events if seq > cursor]
                            if dashboard.closed and not events:
                                return
                        if not events:
                            self.wfile.write(b": keep-alive\n\n")
                        for cursor, data in events:
                            self.wfile.write(f"id: {cursor}\ndata: ".encode() + data + b"\n\n")
                        self.wfile.flush()
                except OSError:
                    return

            def do_POST(self) -> None:
                if self.path not in (prefix + "control", prefix + "comparison"):
                    self.send_error(404)
                    return
                if self.headers.get("Host") != host or self.headers.get("Origin") != "http://" + host:
                    self.send_error(403)
                    return
                comparison = self.path == prefix + "comparison"
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if comparison and length == 0:
                        action = {}
                    else:
                        if not 0 < length <= 16_384 or self.headers.get_content_type() != "application/json":
                            raise ValueError
                        self.connection.settimeout(5)
                        action = json.loads(self.rfile.read(length))
                        if not isinstance(action, dict):
                            raise ValueError
                except (OSError, ValueError):
                    self.send_error(400)
                    return
                if comparison:
                    try:
                        run_id = dashboard.state.get("run_id")
                        if not run_id or not dashboard.state.get("worktree_path"):
                            raise ValueError("worktreeの準備が終わってから、もう一度お試しください。")
                        snapshot_id = create_snapshot(config, run_id, focus=action.get("focus"))
                        response = json.dumps({"id": snapshot_id}).encode("utf-8")
                        self.send_body(201, "application/json", response)
                    except (OSError, RuntimeError, ValueError) as exc:
                        self.send_body(409, "text/plain; charset=utf-8", str(exc).encode("utf-8"))
                    return
                try:
                    if type(action.get("revision")) is not int:
                        raise ValueError
                    if action.get("action") not in ("accept", "resume", "anchors"):
                        raise ValueError
                    if action["action"] == "accept" and (
                        not isinstance(action.get("finding_id"), str) or len(action["finding_id"]) != 12
                        or not isinstance(action.get("reason"), str) or not 0 < len(action["reason"].strip()) <= 2_000
                    ):
                        raise ValueError
                except (OSError, ValueError):
                    self.send_error(400)
                    return
                if action["action"] == "anchors":
                    self.send_anchored_comparison(action)
                elif dashboard.submit(action):
                    self.send_body(202, "application/json", b"{}")
                else:
                    self.send_error(409)

            def send_anchored_comparison(self, action: dict[str, Any]) -> None:
                if anchor_reference is None:
                    self.send_body(409, "text/plain; charset=utf-8", "設定ファイルで参照ワークスペースJSONを指定してください。".encode())
                    return
                # ブラウザーを閉じても端末側は結果を保存できる。応答は要求元だけに返す。
                reply: Future[tuple[str, int]] = Future()
                if not dashboard.submit({**action, "reply": reply}):
                    self.send_body(409, "text/plain; charset=utf-8", "実行中の処理が終わってから、もう一度お試しください。".encode())
                    return
                try:
                    snapshot_id, count = reply.result()
                except (OSError, ValueError, RuntimeError) as exc:
                    self.send_body(409, "text/plain; charset=utf-8", str(exc).encode())
                    return
                self.send_body(201, "application/json", json.dumps({"id": snapshot_id, "count": count}).encode())

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        host = f"127.0.0.1:{server.server_port}"
        print(f"Progress dashboard: http://{host}{prefix}", flush=True)
        print("Press Ctrl+C in this terminal to close the dashboard.", flush=True)
        return server


def run_dashboard(config: dict[str, Any], *, unit_id: str | None = None, run_id: str | None = None) -> dict[str, Any]:
    if (unit_id is None) == (run_id is None):
        raise ValueError("dashboard requires a migration unit or a saved Run ID")
    package_dir = Path(__file__).resolve().parent
    demo = package_dir.parents[1] / "progress-demo.html"
    if not demo.is_file():
        demo = Path(sys.prefix) / "share" / "php-to-csharp-harness" / "progress-demo.html"
    html, marker, _ = demo.read_text(encoding="utf-8").partition("<!-- dashboard-script -->")
    if not marker:
        raise ValueError("progress dashboard template is missing its script marker")
    view = Dashboard()
    anchor_reference = config.get("diff_viewer_anchor_reference")
    options = " data-live"
    saved = None
    if run_id is None:
        view.publish({"status": "preparing", "unit_id": unit_id})
    else:
        _, _, saved, _ = _load_saved_run(config, run_id)
        view.observe(saved)
        view.ready(config, saved)
        options += " data-saved"
        if not view.state["resumable"]:
            anchor_reference = None
    if anchor_reference is not None:
        options += " data-anchor-reference"
    html = html.replace('<html lang="ja">', f'<html lang="ja"{options}>')
    html += '<script src="dashboard.js" defer></script></body></html>'
    with view.server(config, html.encode("utf-8"), (package_dir / "dashboard.js").read_bytes(), anchor_reference) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        token = run_observer.set(view.observe)
        try:
            # Junieは起動元の端末を必要とするため、HTTPスレッドは操作をキューに渡す。
            if saved is None:
                assert unit_id is not None
                result = run_with_junie(config, unit_id)
                view.ready(config, result)
            else:
                result = saved
            while True:
                try:
                    action = view.actions.get(timeout=0.25)
                except KeyboardInterrupt:
                    return result
                except Empty:
                    continue
                if action["action"] == "anchors":
                    reply = action["reply"]
                    try:
                        path, count = generate_anchors(
                            config, result["run_id"], anchor_reference, focus=action.get("focus"),
                        )
                    except KeyboardInterrupt:
                        reply.set_exception(ValueError("Junieのアンカー生成を中断しました。"))
                    except (OSError, ValueError, RuntimeError) as exc:
                        reply.set_exception(exc)
                    else:
                        reply.set_result((path.stem, count))
                    finally:
                        view.publish({**view.state, "ready": True, "generating_anchors": False})
                    continue
                try:
                    if action["action"] == "accept":
                        review_checker(config, result["run_id"], action["finding_id"], action["reason"])
                        view.ready(config, result, "判断理由を記録しました。")
                    else:
                        result = resume_with_junie(config, result["run_id"])
                        view.ready(config, result)
                except (OSError, ValueError, RuntimeError) as exc:
                    view.ready(config, result, f"操作を完了できませんでした。{exc}")
        finally:
            run_observer.reset(token)
            with view.condition:
                view.closed = True
                view.condition.notify_all()
            server.shutdown()
            thread.join()
