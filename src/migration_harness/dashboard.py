from __future__ import annotations

import hashlib
import copy
import json
import re
import secrets
import time
from collections import deque
from concurrent.futures import Future
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Empty, Queue
from subprocess import CalledProcessError
from tempfile import NamedTemporaryFile
from threading import Condition, Thread
from typing import Any

from tomlkit.exceptions import TOMLKitError

from migration_harness.config import load_config
from migration_harness.diff_viewer import create_snapshot, review_html, snapshot_bytes
from migration_harness.errors import format_error
from migration_harness.execution import ExecutionControl, OperationCancelled, control_scope
from migration_harness.files import harness_asset
from migration_harness.timings import freeze_timings
from migration_harness.review_anchors import generate_anchors
from migration_harness.settings_form import build_settings_draft
from migration_harness.workflow import (
    RunInterrupted, RunScriptError, _load_saved_run, can_resume_run, migration_ledger, prepare_run, resume_with_junie,
    review_checker, run_observer, run_with_junie,
)


PROGRESS_FIELDS = (
    "run_id", "unit_id", "target", "worktree_path", "status", "created_at", "current_capsule",
    "context_capsules", "completed_capsules", "context_part_chars", "max_context_chars",
    "build_exit_code", "build_log_path", "checker_exit_code", "checker_log_path", "error",
    "script_phase", "script_logs", "stage_timings", "active_stage", "timing_updated_at",
)
_ERRORS = (OSError, ValueError, RuntimeError, CalledProcessError, KeyError, TypeError, AttributeError, TOMLKitError)


def _read_settings(path: Path) -> tuple[bytes, str]:
    if not path.exists():
        return harness_asset("harness.example.toml").read_bytes(), "missing"
    data = path.read_bytes()
    return data, hashlib.sha256(data).hexdigest()


def _save_settings(path: Path, content: str, version: str) -> dict[str, Any]:
    # 設定ファイルと同じディレクトリで検証し、相対パスの基準を保つ。
    with NamedTemporaryFile(mode="w", dir=path.parent, encoding="utf-8", newline="", delete=False) as stream:
        candidate = Path(stream.name)
    try:
        candidate.write_text(content, encoding="utf-8", newline="")
        normalized = load_config(candidate)
        if _read_settings(path)[1] != version:
            raise ValueError("設定ファイルが更新されています。最新の設定を読み込み、変更を反映し直してください。")
        candidate.replace(path)
        return {**normalized, "config_path": str(path)}
    finally:
        candidate.unlink(missing_ok=True)


class Dashboard:
    """端末側の実行状態、ブラウザー向けのイベント履歴、操作待ち行列を管理する。"""

    def __init__(self) -> None:
        """接続し直すブラウザー向けに直近のイベントを保持できる状態を作る。"""
        self.condition = Condition()
        self.events: deque[tuple[int, bytes]] = deque(maxlen=100)
        self.revision = 0
        self.state: dict[str, Any] = {}
        self.actions: Queue[dict[str, Any]] = Queue(maxsize=1)
        self.closed = False
        self.control: ExecutionControl | None = None
        self.activity_sequence = 0

    def begin_operation(self) -> None:
        """停止要求は実行ごとのIDに結び付け、別のRunに持ち越さない。"""
        with self.condition:
            self.control = ExecutionControl(self.activity)
            self.publish({**self.state, "ready": False, "cancellable": True,
                          "operation_id": secrets.token_hex(16), "stop_requested": False})

    def activity(self, entry: dict[str, Any]) -> None:
        with self.condition:
            if not self.control or self.state.get("ready"):
                return
            self.activity_sequence += 1
            self.publish({**self.state, "activity": {**entry, "sequence": self.activity_sequence}})

    def request_stop(self, action: dict[str, Any]) -> bool:
        with self.condition:
            if (self.control is None or self.state.get("ready")
                    or action.get("operation_id") != self.state.get("operation_id")):
                return False
            self.control.cancelled.set()
            self.publish({**self.state, "stop_requested": True, "cancellable": False,
                          "message": "停止しています。保存済みの編集を残して処理を終了します。"})
            return True

    def publish(self, state: dict[str, Any]) -> None:
        """状態が変わったときだけ再送用の連番を付けて配信を通知する。"""
        with self.condition:
            if state == self.state:
                return
            self.state = state
            self.revision += 1
            payload = json.dumps({**state, "observed_at": time.time()}, ensure_ascii=False).encode("utf-8")
            self.events.append((self.revision, payload))
            self.condition.notify_all()

    def observe(self, record: dict[str, Any]) -> None:
        """Run記録から公開する項目を絞り、進捗に影響する変更だけを配信する。"""
        state = {key: copy.deepcopy(record[key]) for key in PROGRESS_FIELDS if key in record}
        state["repair_count"] = len(record.get("repair_history", [])) + (
            record.get("status") == "repairing" and "repair_pending" in record
        )
        with self.condition:
            fields = (*PROGRESS_FIELDS, "repair_count")
            usage = [row.copy() for row in record.get("token_usage", [])]
            if state != {key: self.state[key] for key in fields if key in self.state}:
                operation = {key: self.state[key] for key in (
                    "operation_id", "cancellable", "stop_requested", "activity", "generating_anchors",
                    "diff_viewer_enabled",
                ) if key in self.state}
                self.publish({**state, **operation, "token_usage": usage})
            elif usage != self.state.get("token_usage", []):
                self.publish({**self.state, "token_usage": usage})

    def ready(self, config: dict[str, Any], result: dict[str, Any], message: str = "") -> None:
        """処理が一区切りついた時点の操作可否とレビュー状態を公開する。"""
        state = {
            **self.state, "ready": True, "status": result["status"], "message": message,
            "error_details": "",
            "resumable": can_resume_run(config, result),
            "diff_viewer_enabled": bool(config.get("diff_viewer_html")),
            "cancellable": False, "stop_requested": False, "operation_id": None,
            "stage_timings": freeze_timings(self.state), "active_stage": None,
        }
        state.pop("review_error", None)
        if result["status"] == "checker_needs_review":
            try:
                state["review"] = review_checker(config, result["run_id"], None, None)
            except _ERRORS as exc:
                state["review_error"] = format_error(exc)
        self.control = None
        self.publish(state)

    def failed(self, error: BaseException) -> None:
        """Keep the workspace usable after an operation or saved-state failure."""
        details = format_error(error)
        self.control = None
        self.publish({
            **self.state, "status": self.state.get("status", "idle"), "ready": True,
            "resumable": self.state.get("resumable", False), "generating_anchors": False, "error_details": details,
            "message": "操作を完了できませんでした。設定や実行ログを確認してください。\n" + details,
            "cancellable": False, "stop_requested": False, "operation_id": None,
            "stage_timings": freeze_timings(self.state), "active_stage": None,
        })

    def submit(self, action: dict[str, Any]) -> bool:
        """古い画面からの操作や、処理中の重複実行を拒否する。"""
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
            self.publish({
                **self.state, "ready": False, "message": "", "generating_anchors": generating_anchors,
                "error_details": "",
            })
            if action["action"] in ("prepare", "run", "resume", "resume-run", "anchors"):
                self.begin_operation()
            self.actions.put_nowait(action)
            return True

    def server(
        self, config: dict[str, Any], html: bytes, script: bytes,
    ) -> ThreadingHTTPServer:
        """Runの画面、イベント、操作を同じローカルオリジンに公開する。"""
        dashboard = self
        # ポート番号だけで操作できないようにし、他サイトからのPOSTも拒否する。
        prefix = "/" + secrets.token_urlsafe(24) + "/"

        class Handler(BaseHTTPRequestHandler):
            """ダッシュボード専用のパスとオリジンでHTTPの受付範囲を絞る。"""

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
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                """閲覧用のアセット、進捗配信、保存された結果を振り分ける。"""
                if self.headers.get("Host") != host:
                    self.send_error(403)
                elif self.path == prefix:
                    self.send_body(200, "text/html; charset=utf-8", html)
                elif self.path == prefix + "dashboard.js":
                    self.send_body(200, "text/javascript; charset=utf-8", script)
                elif self.path == prefix + "events":
                    self.send_events()
                elif self.path == prefix + "workspace":
                    self.send_workspace()
                elif self.path == prefix + "token-runs":
                    self.send_workspace(include_usage=True)
                elif self.path == prefix + "settings":
                    self.send_settings()
                elif self.path == prefix + "migration-ledger":
                    run_id = dashboard.state.get("run_id")
                    if run_id and dashboard.state.get("worktree_path"):
                        try:
                            ledger = migration_ledger(config, run_id)
                        except _ERRORS as exc:
                            self.send_body(409, "text/plain; charset=utf-8", format_error(exc).encode("utf-8"))
                        else:
                            self.send_body(200, "application/json", json.dumps(ledger, ensure_ascii=False).encode("utf-8"))
                    else:
                        self.send_error(404)
                elif self.path.startswith(prefix + "review/"):
                    self.send_comparison()
                else:
                    self.send_error(404)

            def send_workspace(self, *, include_usage: bool = False) -> None:
                try:
                    with dashboard.condition:
                        workspace_config = config.copy()
                    units = [unit["id"] for unit in workspace_config.get("units", [])]
                    records = Path(workspace_config["run_root"]).glob("*/run.json") if units else ()
                    newest = sorted(records, key=lambda path: path.stat().st_mtime, reverse=True)
                    runs = []
                    for record_path in newest:
                        try:
                            _, _, record, _ = _load_saved_run(workspace_config, record_path.parent.name)
                        except _ERRORS:
                            continue
                        keys = ("run_id", "unit_id", "created_at", "token_usage") if include_usage else ("run_id", "unit_id")
                        runs.append({key: record[key] for key in keys if key in record})
                        if not include_usage and len(runs) == 30:
                            break
                    body = json.dumps({"units": units, "runs": runs}).encode()
                except _ERRORS as exc:
                    message = "保存済みの実行を読み込めませんでした。保存先を確認して、実行タブを開き直してください。\n" + format_error(exc)
                    self.send_body(409, "text/plain; charset=utf-8", message.encode())
                    return
                self.send_body(200, "application/json", body)

            def send_settings(self) -> None:
                try:
                    path = Path(config["config_path"])
                    data, version = _read_settings(path)
                    body = json.dumps({"path": str(path), "version": version, **build_settings_draft(data.decode("utf-8"))}).encode()
                except _ERRORS as exc:
                    message = "設定を読み込めませんでした。ファイルの場所と文字コードを確認してください。\n" + format_error(exc)
                    self.send_body(409, "text/plain; charset=utf-8", message.encode())
                else:
                    self.send_body(200, "application/json", body)

            def send_comparison(self) -> None:
                """保存済み比較をダウンロード用JSONか隔離されたViewerとして返す。"""
                match = re.fullmatch(r"([^/]+)/([a-f0-9]{32})\.(html|json)", self.path.removeprefix(prefix + "review/"))
                with dashboard.condition:
                    comparison_config = config.copy()
                if not match or not comparison_config.get("run_root"):
                    self.send_error(404)
                    return
                run_id, snapshot_id, extension = match.groups()
                try:
                    workspace = snapshot_bytes(comparison_config, run_id, snapshot_id)
                    if extension == "json":
                        self.send_body(200, "application/json", workspace, {
                            "Content-Disposition": f'attachment; filename="review-{snapshot_id}.json"',
                        })
                    else:
                        # Viewerにはスナップショットだけを渡し、進捗画面や制御APIから隔離する。
                        self.send_body(200, "text/html; charset=utf-8", review_html(comparison_config, workspace), {
                            "X-Frame-Options": "SAMEORIGIN",
                            "Content-Security-Policy": (
                                "sandbox allow-scripts allow-downloads; default-src 'none'; "
                                "script-src 'unsafe-inline' blob:; style-src 'unsafe-inline' blob:; "
                                "img-src data: blob:; font-src data: blob:; worker-src blob:; "
                                "base-uri 'none'; frame-ancestors 'self'"
                            ),
                        })
                except _ERRORS as exc:
                    headers = {"X-Frame-Options": "SAMEORIGIN"} if extension == "html" else None
                    self.send_body(409, "text/plain; charset=utf-8", f"比較ツールを開けませんでした。\n{format_error(exc)}".encode("utf-8"), headers)

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
                """オリジンと操作内容を検証してから比較または端末側の操作へ渡す。"""
                if self.path not in (prefix + "control", prefix + "comparison", prefix + "settings"):
                    self.send_error(404)
                    return
                if self.headers.get("Host") != host or self.headers.get("Origin") != "http://" + host:
                    self.send_error(403)
                    return
                comparison = self.path == prefix + "comparison"
                settings = self.path == prefix + "settings"
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if comparison and length == 0:
                        action = {}
                    else:
                        limit = 8 * 1024 * 1024 if settings else 16_384
                        if not 0 < length <= limit or self.headers.get_content_type() != "application/json":
                            raise ValueError
                        self.connection.settimeout(5)
                        action = json.loads(self.rfile.read(length))
                        if not isinstance(action, dict):
                            raise ValueError
                except (OSError, ValueError):
                    self.send_error(400)
                    return
                if settings:
                    self.save_settings(action)
                    return
                if comparison:
                    with dashboard.condition:
                        comparison_config = config.copy()
                        state = dashboard.state
                    try:
                        if not comparison_config.get("diff_viewer_html"):
                            raise ValueError("設定の差分ビューアーのHTMLを指定してください。")
                        run_id = state.get("run_id")
                        if action.get("run_id", run_id) != run_id:
                            raise ValueError("表示中のRunが変わりました。現在のRunで比較を開き直してください。")
                        if not run_id or not state.get("worktree_path"):
                            raise ValueError("worktreeの準備が終わってから、もう一度お試しください。")
                        snapshot_id = create_snapshot(comparison_config, run_id, focus=action.get("focus"))
                        response = json.dumps({"run_id": run_id, "id": snapshot_id}).encode("utf-8")
                        self.send_body(201, "application/json", response)
                    except _ERRORS as exc:
                        self.send_body(409, "text/plain; charset=utf-8", format_error(exc).encode("utf-8"))
                    return
                try:
                    if type(action.get("revision")) is not int:
                        raise ValueError
                    if action.get("action") not in ("stop", "accept", "resume", "anchors", "prepare", "run", "open-run", "resume-run"):
                        raise ValueError
                    if action["action"] in ("prepare", "run", "open-run", "resume-run"):
                        name = action.get("run_id" if action["action"] in ("open-run", "resume-run") else "unit_id")
                        if not config.get("units") or not isinstance(name, str) or not name.strip():
                            raise ValueError
                    if action["action"] == "accept" and (
                        not isinstance(action.get("finding_id"), str) or len(action["finding_id"]) != 12
                        or not isinstance(action.get("reason"), str) or not 0 < len(action["reason"].strip()) <= 2_000
                    ):
                        raise ValueError
                except (OSError, ValueError):
                    self.send_error(400)
                    return
                if action["action"] == "stop":
                    if dashboard.request_stop(action):
                        self.send_body(202, "application/json", b"{}")
                    else:
                        self.send_error(409)
                elif action["action"] == "anchors":
                    self.send_anchored_comparison(action)
                elif dashboard.submit(action):
                    self.send_body(202, "application/json", b"{}")
                else:
                    self.send_error(409)

            def save_settings(self, action: dict[str, Any]) -> None:
                if action.get("mode") == "preview":
                    if not isinstance(action.get("content"), str) or not isinstance(action.get("changes"), dict):
                        self.send_error(400)
                        return
                    try:
                        draft = build_settings_draft(action["content"], action["changes"])
                    except _ERRORS as exc:
                        self.send_body(409, "text/plain; charset=utf-8", format_error(exc).encode())
                    else:
                        self.send_body(200, "application/json", json.dumps(draft).encode())
                    return
                if not all(isinstance(action.get(key), str) for key in ("content", "version")) or type(action.get("revision")) is not int:
                    self.send_error(400)
                    return
                try:
                    with dashboard.condition:
                        if not dashboard.state.get("ready") or action["revision"] != dashboard.revision:
                            raise ValueError("実行中の処理が終わってから設定を保存してください。")
                        path = Path(config["config_path"])
                        updated = _save_settings(path, action["content"], action["version"])
                        config.pop("checker_from_cli", None)
                        config.pop("toml_checker_command", None)
                        config.update(updated)
                        dashboard.publish({"status": "idle", "ready": True, "settings_revision": dashboard.revision + 1,
                                           "diff_viewer_enabled": bool(config.get("diff_viewer_html"))})
                except _ERRORS as exc:
                    message = f"設定を保存できませんでした。内容を確認してください。\n{format_error(exc)}"
                    self.send_body(409, "text/plain; charset=utf-8", message.encode())
                else:
                    version = hashlib.sha256(action["content"].encode()).hexdigest()
                    self.send_body(200, "application/json", json.dumps({"version": version}).encode())

            def send_anchored_comparison(self, action: dict[str, Any]) -> None:
                """端末側でアンカー生成が終わるまで待ち、作成元Runと比較のIDを返す。"""
                if not config.get("diff_viewer_html"):
                    self.send_body(409, "text/plain; charset=utf-8", "設定の差分ビューアーのHTMLを指定してください。".encode())
                    return
                # ブラウザーを閉じても端末側は結果を保存できる。応答は要求元だけに返す。
                reply: Future[tuple[str, str, int]] = Future()
                if not dashboard.submit({**action, "reply": reply}):
                    self.send_body(409, "text/plain; charset=utf-8", "実行中の処理が終わってから、もう一度お試しください。".encode())
                    return
                try:
                    run_id, snapshot_id, count = reply.result()
                except _ERRORS as exc:
                    self.send_body(409, "text/plain; charset=utf-8", format_error(exc).encode())
                    return
                self.send_body(201, "application/json", json.dumps({
                    "run_id": run_id, "id": snapshot_id, "count": count,
                }).encode())

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        host = f"127.0.0.1:{server.server_port}"
        print(f"Progress dashboard: http://{host}{prefix}", flush=True)
        print("Press Ctrl+C in this terminal to close the dashboard.", flush=True)
        return server


def run_dashboard(
    config: dict[str, Any], *, unit_id: str | None = None, run_id: str | None = None,
    config_error: str | None = None,
) -> dict[str, Any] | None:
    """サーバーを開き、対話が必要な作業を起動元の端末で処理する。"""
    if unit_id is not None and run_id is not None:
        raise ValueError("choose a migration unit or a saved Run ID")
    package_dir = Path(__file__).resolve().parent
    demo = harness_asset("progress-demo.html")
    html, marker, _ = demo.read_text(encoding="utf-8").partition("<!-- dashboard-script -->")
    if not marker:
        raise ValueError("progress dashboard template is missing its script marker")
    view = Dashboard()
    options = " data-live"
    saved = None
    if config_error is not None:
        view.publish({
            "status": "idle", "ready": True, "error_details": config_error,
            "message": "設定を読み込めませんでした。設定タブで確認して保存してください。\n" + config_error,
        })
        options += " data-workspace"
    elif run_id is None and unit_id is not None:
        view.publish({"status": "preparing", "unit_id": unit_id})
    elif run_id is not None:
        try:
            _, _, saved, _ = _load_saved_run(config, run_id)
            view.observe(saved)
            view.ready(config, saved)
        except _ERRORS as exc:
            saved = None
            view.failed(exc)
            options += " data-workspace"
        else:
            options += " data-saved"
        if config.get("diff_viewer_html"):
            options += " data-viewer-enabled"
    else:
        view.publish({"status": "idle", "ready": True})
        options += " data-workspace"
    view.publish({**view.state, "diff_viewer_enabled": bool(config.get("diff_viewer_html"))})
    html = html.replace('<html lang="ja">', f'<html lang="ja"{options}>')
    html += '<script src="dashboard.js" defer></script></body></html>'
    with view.server(config, html.encode("utf-8"), (package_dir / "dashboard.js").read_bytes()) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        token = run_observer.set(view.observe)

        def show_failure(error: BaseException) -> dict[str, Any] | None:
            # 最終状態の公開まで操作受付を閉じ、新しい実行の状態を上書きしない。
            with view.condition:
                return finish_failure(error)

        def finish_failure(error: BaseException) -> dict[str, Any] | None:
            failed = None
            stopped = isinstance(error, RunInterrupted) or (
                isinstance(error, RunScriptError) and bool(view.control and view.control.cancelled.is_set())
            )
            failed_run_id = getattr(error, "run_id", None) or view.state.get("run_id")
            preparation_only = (view.state.get("status") in {"preparation_failed", "preparation_interrupted"}
                                and not view.state.get("worktree_path"))
            if failed_run_id and not preparation_only:
                try:
                    _, _, failed, _ = _load_saved_run(config, failed_run_id)
                    view.observe(failed)
                    view.ready(config, failed)
                except _ERRORS as load_error:
                    view.failed(load_error)
                    return None
            elif isinstance(error, RunScriptError):
                view.publish({"status": "script_failed"})
            if stopped:
                resumable = bool(failed and can_resume_run(config, failed))
                view.control = None
                view.publish({**view.state, "ready": True, "resumable": resumable,
                              "cancellable": False, "stop_requested": False, "operation_id": None,
                              "stage_timings": freeze_timings(view.state), "active_stage": None,
                              "error_details": "", "message": (
                                  "停止しました。同じRun IDで再開できます。" if resumable else
                                  "準備を停止しました。実行タブから再度準備してください。"
                              )})
            else:
                view.failed(error)
            return failed

        def execute(function, *args, **kwargs):
            with control_scope(view.control):
                try:
                    return function(*args, **kwargs)
                except OperationCancelled:
                    raise RunInterrupted(view.state.get("run_id", "")) from None

        try:
            # Junieは起動元の端末を必要とするため、HTTPスレッドは操作をキューに渡す。
            if unit_id is not None and config_error is None:
                view.begin_operation()
                try:
                    result = execute(run_with_junie, config, unit_id)
                    view.ready(config, result)
                except RunInterrupted as exc:
                    result = show_failure(exc)
                except _ERRORS as exc:
                    result = show_failure(exc)
            else:
                result = saved
            while True:
                try:
                    action = view.actions.get(timeout=0.25)
                except KeyboardInterrupt:
                    return result if result is not None and view.state.get("run_id") == result.get("run_id") else None
                except Empty:
                    continue
                if action["action"] == "anchors":
                    reply = action["reply"]
                    try:
                        source_run_id = view.state["run_id"]
                        path, count = execute(
                            generate_anchors, config, source_run_id, focus=action.get("focus"),
                        )
                    except KeyboardInterrupt:
                        reply.set_exception(ValueError("Junieのアンカー生成を中断しました。"))
                        view.publish({**view.state, "message": "アンカー生成を停止しました。移行結果は保存されています。"})
                    except _ERRORS as exc:
                        view.publish({**view.state, "error_details": format_error(exc),
                                      "message": "アンカー生成に失敗しました。\n" + format_error(exc)})
                        reply.set_exception(exc)
                    else:
                        reply.set_result((source_run_id, path.stem, count))
                    finally:
                        view.control = None
                        view.publish({**view.state, "ready": True, "generating_anchors": False,
                                      "cancellable": False, "stop_requested": False, "operation_id": None})
                    continue
                try:
                    if action["action"] in ("open-run", "resume-run"):
                        _, _, result, _ = _load_saved_run(config, action["run_id"])
                        view.observe(result)
                        if action["action"] == "resume-run":
                            result = execute(resume_with_junie, config, action["run_id"])
                        view.ready(config, result)
                    elif action["action"] in ("prepare", "run"):
                        if action["action"] == "prepare":
                            prepared = execute(prepare_run, config, action["unit_id"])
                            _, _, result, _ = _load_saved_run(config, prepared["run_id"])
                        else:
                            result = execute(run_with_junie, config, action["unit_id"])
                        view.ready(config, result)
                    elif action["action"] == "accept":
                        review_checker(config, view.state["run_id"], action["finding_id"], action["reason"])
                        view.ready(config, result, "判断理由を記録しました。")
                    else:
                        result = execute(resume_with_junie, config, view.state["run_id"])
                        view.ready(config, result)
                except RunInterrupted as exc:
                    result = show_failure(exc)
                except _ERRORS as exc:
                    result = show_failure(exc)
        finally:
            run_observer.reset(token)
            with view.condition:
                view.closed = True
                view.condition.notify_all()
            server.shutdown()
            thread.join()
