"""ローカルHTTPの受付、共通検証、Runの特定、機能別の振り分け。"""

from __future__ import annotations

import json
import re
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import parse_qs, urlsplit

from ..errors import format_error
from ..execution import ExecutionControl
from ..junie.junie_notifications import answer_pending, pending_requests
from ..rules.rule_workspace import RuleWorkspace
from ..runs.worktree_targets import EDIT_BYTES
from . import DASHBOARD_ERRORS, dashboard_comparison, dashboard_editor, dashboard_runs, dashboard_settings


class DashboardServer(ThreadingHTTPServer):
    # Chromium can queue the UI bundles and the event stream together on a Run reopen.
    request_queue_size = 32

    def __init__(self, address, dashboard, config, html, script, prefix):
        from migration_harness.verification.roslyn_editor_worker import EditorSemanticWorker

        self.root_view = dashboard
        self.manager = getattr(dashboard, "manager", None)
        self.config = config
        self.html, self.script, self.prefix = html, script, prefix
        self.preview_lock = Lock()
        self.reference_previews: dict[str, ExecutionControl] = {}
        self.dry_run_controls: dict[str, ExecutionControl] = {}
        self.rules = getattr(dashboard, "rule_workspace", None) or RuleWorkspace(config)
        self.editor_semantic_worker = EditorSemanticWorker()
        super().__init__(address, DashboardHandler)

    def server_close(self):
        try:
            self.editor_semantic_worker.close()
        finally:
            super().server_close()


POST_ROUTES = frozenset({
    "control", "comparison", "settings", "build-response", "editor", "roslyn-diagnostics",
    "prompts", "rule-workspace", "junie-requests", "recovery-plan",
})


class DashboardHandler(BaseHTTPRequestHandler):
    server: DashboardServer

    @property
    def expected_host(self) -> str:
        return f"127.0.0.1:{self.server.server_port}"

    def bind_view(self) -> bool:
        manager = self.server.manager
        dashboard = self.server.root_view
        try:
            requested = self.headers.get("X-Run-ID") or parse_qs(urlsplit(self.path).query).get("run_id", [None])[0]
            self.dashboard = manager.get(requested) if manager else dashboard
            return True
        except DASHBOARD_ERRORS as exc:
            self.send_conflict(exc)
            return False

    def log_message(self, format: str, *args: Any) -> None:
        """継続接続のHTTPログがJunieの対話端末に混ざるのを防ぐ。"""
        pass

    def send_body(
        self, status: int, content_type: str, body: bytes, headers: dict[str, str] | None = None,
    ) -> None:
        """保存済みの画面やソースをキャッシュしない応答を返す。"""
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        for name, value in {"X-Frame-Options": "DENY", **(headers or {})}.items():
            self.send_header(name, value)
        with suppress(BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.end_headers()
            self.wfile.write(body)

    def send_conflict(self, error: BaseException) -> None:
        self.send_body(409, "text/plain; charset=utf-8", format_error(error).encode("utf-8"))

    def do_GET(self) -> None:
        manager = self.server.manager
        dashboard = self.server.root_view
        prefix = self.server.prefix
        if self.headers.get("Host") != self.expected_host:
            self.send_error(403)
            return
        if self.path.startswith("/junie-open/"):
            token = self.path.removeprefix("/junie-open/")
            if re.fullmatch(r"[A-Za-z0-9_-]{32}", token):
                location = self.server.junie_notifications.location(token, activate=True)
                if location:
                    self.send_body(303, "text/plain; charset=utf-8", b"", {"Location": location})
                    return
            self.send_body(410, "text/plain; charset=utf-8",
                           "この通知は期限切れです。ダッシュボードを開き直してください。".encode())
            return
        if not self.path.startswith(prefix):
            self.send_error(403)
            return
        if self.path == prefix + "junie-requests":
            if self.headers.get("X-Dashboard-Visible") == "1":
                self.server.junie_notifications.mark_visible()
            waiting = list(pending_requests(dashboard, manager).values())
            self.send_body(200, "application/json", json.dumps(waiting, ensure_ascii=False).encode())
            return
        if not self.bind_view():
            return
        self.send_get_route()

    def send_get_route(self) -> None:
        prefix = self.server.prefix
        route = self.path.removeprefix(prefix).partition("?")[0]
        if not route:
            self.send_body(200, "text/html; charset=utf-8", self.server.html)
        elif self.path == prefix + "dashboard.js":
            self.send_body(200, "text/javascript; charset=utf-8", self.server.script)
        elif self.path.removeprefix(prefix) in (
            "dashboard_runs.js", "dashboard_settings.js", "dashboard_editor.js",
            "dashboard_rule_workspace.js", "dashboard_log_presentation.js", "dashboard_chat.js",
        ):
            asset = Path(__file__).with_name(self.path.removeprefix(prefix))
            self.send_body(200, "text/javascript; charset=utf-8", asset.read_bytes())
        elif self.path.startswith(prefix + "monaco/"):
            name = self.path.removeprefix(prefix + "monaco/")
            content_type = {
                "editor.js": "text/javascript",
                "worker.js": "text/javascript",
                "editor.css": "text/css",
            }.get(name)
            if content_type is None:
                self.send_error(404)
                return
            try:
                asset = (Path(__file__).with_name("monaco") / name).read_bytes()
            except OSError:
                self.send_error(404)
            else:
                self.send_body(200, content_type, asset)
        elif route == "editor":
            dashboard_editor.send_editor(self)
        elif route == "recovery":
            dashboard_runs.send_recovery_status(self)
        elif route == "recovery-plan":
            dashboard_runs.send_recovery_plan(self)
        elif route == "recovery-file":
            dashboard_runs.send_recovery_file(self)
        elif route == "resume-plan":
            dashboard_runs.send_resume_plan(self)
        elif route == "editor-php":
            dashboard_editor.send_editor_php(self)
        elif route == "events":
            self.send_events()
        elif route == "junie-thoughts":
            dashboard_runs.send_junie_thoughts(self)
        elif route == "workspace":
            dashboard_runs.send_workspace(self)
        elif route == "token-runs":
            dashboard_runs.send_workspace(self, include_usage=True)
        elif route == "rule-workspace":
            query = parse_qs(urlsplit(self.path).query)
            dashboard_settings.send_rule_workspace(self, query=query)
        elif route == "settings":
            dashboard_settings.send_settings(self)
        elif route == "junie-models":
            dashboard_settings.send_models(self)
        elif route == "run-model":
            dashboard_runs.send_run_model(self)
        elif route == "prompts":
            dashboard_editor.send_prompts(self)
        elif route == "checker-output":
            dashboard_runs.send_checker_output(self)
        elif route == "build-review":
            dashboard_runs.send_build_review(self)
        elif route == "migration-ledger":
            dashboard_runs.send_ledger(self)
        elif route == "migration-evidence":
            dashboard_runs.send_migration_evidence(self)
        elif self.path.startswith(prefix + "review/"):
            dashboard_comparison.send_comparison(self)
        else:
            self.send_error(404)

    def send_events(self) -> None:
        """SSEの再接続では、受信済みの連番より新しいイベントを再送する。"""
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
                with self.dashboard.condition:
                    self.dashboard.condition.wait_for(
                        lambda: self.dashboard.closed or self.dashboard.revision > cursor, timeout=10,
                    )
                    events = [(seq, data) for seq, data in self.dashboard.events if seq > cursor]
                    if self.dashboard.closed and not events:
                        return
                if not events:
                    self.wfile.write(b": keep-alive\n\n")
                for cursor, data in events:
                    self.wfile.write(f"id: {cursor}\ndata: ".encode() + data + b"\n\n")
                self.wfile.flush()
        except OSError:
            return

    def do_POST(self) -> None:
        manager = self.server.manager
        dashboard = self.server.root_view
        prefix = self.server.prefix
        route = self.path.removeprefix(prefix)
        if not self.path.startswith(prefix) or route not in POST_ROUTES:
            if (self.path.partition("?")[0] == prefix + "editor-php"
                    and self.headers.get("Host") == self.expected_host
                    and self.headers.get("Origin") == "http://" + self.expected_host):
                # Closing with unread request bytes can reset the connection on Windows.
                with suppress(OSError, ValueError):
                    length = int(self.headers.get("Content-Length", "0"))
                    if 0 < length <= 16_384:
                        self.connection.settimeout(1)
                        self.rfile.read(length)
            self.send_error(404)
            return
        if self.headers.get("Host") != self.expected_host or self.headers.get("Origin") != "http://" + self.expected_host:
            self.send_error(403)
            return
        try:
            action = self.read_post_action(route)
        except (OSError, ValueError):
            self.send_error(400)
            return
        if route == "control" and action.get("action") in ("dry-run", "dry-run-cancel"):
            dashboard_settings.send_dry_run(self, action)
            return
        if route == "rule-workspace":
            dashboard_settings.send_rule_workspace(self, action=action)
            return
        if route == "junie-requests":
            if answer_pending(dashboard, manager, action):
                self.send_body(202, "application/json", b"{}")
            else:
                self.send_error(409)
            return
        if not self.bind_post_view(route, action):
            return
        if route == "settings":
            dashboard_settings.save_settings(self, action)
        elif route == "editor":
            dashboard_editor.post_editor(self, action)
        elif route == "roslyn-diagnostics":
            dashboard_editor.send_roslyn_diagnostics(self, action)
        elif route == "prompts":
            dashboard_editor.send_prompts(self, action)
        elif route == "comparison":
            dashboard_comparison.create_comparison(self, action)
        elif route == "recovery-plan":
            dashboard_runs.save_recovery_plan(self, action)
        else:
            dashboard_runs.post_control(self, action)

    def read_post_action(self, route: str) -> dict[str, Any]:
        """ルートごとの上限とJSONの共通条件を確認して本文を読む。"""
        manager = self.server.manager
        limit = 16_384
        if route in {"settings", "rule-workspace"}:
            limit = 13 * 1024 * 1024
        elif route == "roslyn-diagnostics":
            limit = 6 * EDIT_BYTES + 16_384
        elif route == "build-response":
            # JSON エスケープはソースの 1 バイトを最大 6 バイトに展開する。
            limit = 6 * EDIT_BYTES + 256 * 1024
        elif route == "editor":
            limit = 12 * EDIT_BYTES + 16_384
        elif route == "prompts":
            limit = 32_768
        length = int(self.headers.get("Content-Length", "0"))
        if route == "comparison" and length == 0:
            action = {}
        else:
            if not 0 < length <= limit or self.headers.get_content_type() != "application/json":
                raise ValueError
            self.connection.settimeout(5)
            action = json.loads(self.rfile.read(length))
            if not isinstance(action, dict):
                raise ValueError
            if (route == "build-response") != (action.get("action") == "build-response"):
                raise ValueError
            if action.get("action") == "hide-run" and (
                not manager or not isinstance(action.get("run_id"), str) or not action["run_id"]
            ):
                raise ValueError
        return action

    def bind_post_view(self, route: str, action: dict[str, Any]) -> bool:
        manager = self.server.manager
        creating = manager and route == "control" and action.get("action") == "run"
        if creating:
            self.dashboard = manager.get()
        elif not self.bind_view():
            return False
        if not creating and manager and action.get("run_id"):
            try:
                self.dashboard = manager.get(action["run_id"])
            except DASHBOARD_ERRORS as exc:
                self.send_conflict(exc)
                return False
        return True
