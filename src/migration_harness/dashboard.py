from __future__ import annotations

import hashlib
import copy
import os
import json
import re
import secrets
import shutil
import time
import uuid
from collections import deque
from concurrent.futures import Future
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Empty, Queue
from subprocess import CalledProcessError
from tempfile import NamedTemporaryFile
from threading import Condition, Lock, Thread
from typing import Any
from urllib.parse import parse_qs, urlsplit

from tomlkit.exceptions import TOMLKitError

from migration_harness.capsules import (
    input_encoding, load_capsule_manifest, migration_php_encodings, pinned_php_sources, read_pinned_php,
)
from migration_harness.config import find_unit, load_config, target_paths
from migration_harness.diff_viewer import _inside_file, create_snapshot, review_html, snapshot_bytes
from migration_harness.editor_queue import (
    AGENT_PHASES, EDITOR_TARGET_SUFFIXES, QUEUE_PHASES,
    add_prompt, editor_queue, editor_target, editor_text, record_operator_edit,
)
from migration_harness.errors import format_error
from migration_harness.execution import ExecutionControl, OperationCancelled, control_scope
from migration_harness.files import harness_asset, safe_target
from migration_harness.junie_cli import list_models, unconfirmed_junie_stop
from migration_harness.junie_conversation import (
    fail_pending_after_unconfirmed_stop, finish_instruction, invalidate_queued_steering, run_instruction,
)
from migration_harness.junie_thoughts import FILENAME as JUNIE_THOUGHTS_FILENAME, read_junie_thoughts
from migration_harness.review_anchors import generate_anchors
from migration_harness.run_records import edit_run_record, load_saved_run, run_observer, validate_run_id
from migration_harness.settings_form import build_settings_draft
from migration_harness.source_context import preview_target_context
from migration_harness.timings import freeze_timings
from migration_harness.workflow import (
    RunScriptError, _review_log,
    build_failure_review, can_resume_build, can_resume_run, migration_ledger, plan_stage_resume, prepare_run, resume_with_junie,
    review_checker, run_with_junie,
)
from migration_harness.worktree_targets import EDIT_BYTES, save_target_edit, worktree_source_lock


PROGRESS_FIELDS = (
    "run_id", "unit_id", "target", "worktree_path", "status", "created_at", "current_capsule",
    "context_capsules", "completed_capsules", "context_part_chars", "max_context_chars",
    "build_exit_code", "build_log_path", "build_attempt_seconds",
    "checker_exit_code", "checker_log_path", "checker_review_reason", "error",
    "script_phase", "script_logs", "stage_timings", "active_stage", "timing_updated_at", "last_stop",
)
_ERRORS = (OSError, ValueError, RuntimeError, CalledProcessError, KeyError, TypeError, AttributeError, TOMLKitError)

def _editor_stamp(path: Path) -> tuple | None:
    """ポーリング時に本文を読まず、差し替えや外部編集を検知する。"""
    try:
        value = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return None
    return value.st_dev, value.st_ino, value.st_mode, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _editor_version(*fields) -> str:
    """個別の取得版を、Runや対象をまたいで再利用できない短い値にする。"""
    return hashlib.sha256(json.dumps(fields, ensure_ascii=False).encode("utf-8")).hexdigest()


def _editor_history_delta(payload, previous_version, versions):
    """最近の版を基準に、指示と会話の変更だけを返す。古い版には全件を返す。"""
    fields = ("prompts", "conversation")
    current = {key: {entry["id"]: _editor_version(entry) for entry in payload.get(key, [])} for key in fields}
    version = _editor_version(payload["run_id"], current)
    payload["history_version"] = version
    previous = versions.get((payload["run_id"], previous_version))
    if previous is not None:
        delta = {}
        for key in fields:
            if key not in payload:
                continue
            rows = payload.pop(key)
            changed = [row for row in rows if previous[key].get(row["id"]) != current[key][row["id"]]]
            removed = list(previous[key].keys() - current[key].keys())
            if changed or removed:
                delta[key] = {"changed": changed, "removed": removed}
        if delta:
            payload["history_delta"] = delta
    versions.setdefault((payload["run_id"], version), current)
    if len(versions) > 8:
        del versions[next(iter(versions))]


def _prompt_timing(config, state, run_path, record, *, live: bool = False, pending_edits: bool = False) -> str | None:
    """保留中の指示に適用する送信タイミングを選ぶ。"""
    if state.get("stop_requested") or state.get("generating_anchors"):
        return None
    phase = record["status"]
    if pending_edits and not state.get("ready") and phase == "repairing":
        return "repair_restart"
    if live and not pending_edits and not state.get("ready") and phase in {"junie_running", "editor_followup", "repairing"}:
        return "immediate"
    if phase == "prepared" or phase in QUEUE_PHASES or (
        phase in {"script_running", "script_failed"}
        and record.get("script_phase") in {"after_prepare", "before_migration"}
    ):
        return "resume" if state.get("ready") else "checkpoint"
    agent = run_path / "agent-workspace"
    if (record.get("completed_capsules", 0) > 0 and record.get("completed_capsules") == record.get("context_capsules")
            and not agent.is_symlink() and agent.is_dir() and can_resume_run(config, record)):
        return "resume"
    return None


def _read_settings(path: Path) -> tuple[bytes, str]:
    """設定ファイルの内容と版を読み取る。"""
    if not path.exists():
        return harness_asset("harness.example.toml").read_bytes(), "missing"
    data = path.read_bytes()
    return data, hashlib.sha256(data).hexdigest()


def _save_settings(path: Path, content: str, version: str, *, before_replace=None) -> dict[str, Any]:
    # 設定ファイルと同じディレクトリで検証し、相対パスの基準を保つ。
    """検証済み設定を版を照合して保存する。"""
    with NamedTemporaryFile(mode="w", dir=path.parent, encoding="utf-8", newline="", delete=False) as stream:
        candidate = Path(stream.name)
    try:
        candidate.write_text(content, encoding="utf-8", newline="")
        normalized = load_config(candidate)
        if _read_settings(path)[1] != version:
            raise ValueError("設定ファイルが更新されています。最新の設定を読み込み、変更を反映し直してください。")
        normalized["config_path"] = str(path)
        if before_replace:
            before_replace(normalized)
        candidate.replace(path)
        return normalized
    finally:
        candidate.unlink(missing_ok=True)


class Dashboard:
    """端末側の実行状態、ブラウザー向けのイベント履歴、操作待ち行列を管理する。"""

    def __init__(self) -> None:
        """接続し直すブラウザー向けに直近のイベントを保持できる状態を作る。"""
        self.condition = Condition()
        self.editor_history_versions = {}
        self.events: deque[tuple[int, bytes]] = deque(maxlen=100)
        self.revision = 0
        self.state: dict[str, Any] = {}
        self.actions: Queue[dict[str, Any]] = Queue(maxsize=1)
        self.closed = False
        self.control: ExecutionControl | None = None
        self.junie_reply: Future[str] | None = None
        self.trusted_junie_workspaces: set[str] = set()
        self.activity_sequence = 0

    def begin_operation(self, operation_id: str | None = None) -> None:
        """停止要求は実行ごとのIDに結び付け、別のRunに持ち越さない。"""
        with self.condition:
            self.stop_phase = None
            self.control = ExecutionControl(
                self.activity, on_junie_request=self.show_junie_request,
                operation_id=operation_id or secrets.token_hex(16),
                trusted_junie_workspaces=self.trusted_junie_workspaces,
            )
            state = {key: value for key, value in self.state.items()
                     if key not in {"activity", "phase_activity", "latest_activity", "heartbeat"}}
            self.publish({**state, "ready": False, "cancellable": True,
                          "operation_id": self.control.operation_id, "stop_requested": False})

    def activity(self, entry: dict[str, Any]) -> None:
        """Runの状態と操作可能範囲を画面表示へ反映する。"""
        with self.condition:
            if not self.control or self.state.get("ready"):
                return
            self.activity_sequence += 1
            state = {**self.state, "activity": {**entry, "sequence": self.activity_sequence}}
            if entry.get("kind") == "phase":
                state["phase_activity"] = entry.copy()
                state.pop("latest_activity", None)
                state.pop("heartbeat", None)
            elif entry.get("kind") == "heartbeat":
                state["heartbeat"] = entry.copy()
            else:
                state["latest_activity"] = entry.copy()
            self.publish(state)

    def show_junie_request(self, request: dict | None) -> Future[str] | None:
        """Junieの確認依頼をRunの画面状態へ表示する。"""
        with self.condition:
            state = self.state.copy()
            state.pop("junie_request", None)
            self.junie_reply = None
            if request is not None and self.control and not self.control.cancelled.is_set():
                state["junie_request"] = {**request, "id": secrets.token_hex(16)}
                self.junie_reply = Future()
            self.publish(state)
            return self.junie_reply

    def answer_junie(self, action: dict) -> bool:
        """表示中のJunie確認へ利用者の選択を返す。"""
        with self.condition:
            request = self.state.get("junie_request")
            option = action.get("option_id")
            if (self.junie_reply is None or request is None
                    or action.get("operation_id") != self.state.get("operation_id")
                    or action.get("request_id") != request["id"]
                    or not any(option == choice["id"] for choice in request["options"])):
                return False
            self.junie_reply.set_result(option)
            self.show_junie_request(None)
            return True

    def request_stop(self, action: dict[str, Any]) -> bool:
        """表示中の操作IDに結び付けて停止を要求する。"""
        with self.condition:
            if (self.control is None or self.state.get("ready")
                    or action.get("operation_id") != self.state.get("operation_id")):
                return False
            self.control.cancelled.set()
            self.stop_phase = self.state.get("status")
            self.show_junie_request(None)
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
                    "diff_viewer_enabled", "junie_request",
                ) if key in self.state and (key != "activity" or state.get("run_id") == self.state.get("run_id"))}
                if (state.get("run_id"), state.get("status")) == (self.state.get("run_id"), self.state.get("status")):
                    operation.update((key, self.state[key]) for key in
                                     ("phase_activity", "latest_activity", "heartbeat") if key in self.state)
                self.publish({**state, **operation, "token_usage": usage})
            elif usage != self.state.get("token_usage", []):
                self.publish({**self.state, "token_usage": usage})

    def ready(self, config: dict[str, Any], result: dict[str, Any], message: str = "") -> None:
        """処理が一区切りついた時点の操作可否とレビュー状態を公開する。"""
        stopped = result.get("last_stop") or {}
        if not message and stopped.get("saved_status") == result["status"]:
            message = stopped["message"]
        state = {
            **self.state, "ready": True, "status": result["status"], "message": message,
            "error_details": "",
            "resumable": can_resume_run(config, result),
            "build_resumable": can_resume_build(result),
            "diff_viewer_enabled": bool(config.get("diff_viewer_html")),
            "cancellable": False, "stop_requested": False, "operation_id": None, "generating_anchors": False,
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
                or (action["action"] == "build-response"
                    and (self.state.get("status") != "build_failed" or not self.state.get("resumable")))
                or ("checker_instruction" in action and self.state.get("status") not in {
                    "checker_needs_review", "checker_failed",
                })
                or (action["action"] == "accept" and self.state.get("status") != "checker_needs_review")
            ):
                return False
            self.publish({
                **self.state, "ready": False, "message": "", "generating_anchors": generating_anchors,
                "error_details": "",
            })
            if action["action"] in ("prepare", "run", "resume", "resume-run", "anchors", "build-response"):
                self.begin_operation()
            self.actions.put_nowait(action)
            return True

    def server(
        self, config: dict[str, Any], html: bytes, script: bytes,
    ) -> ThreadingHTTPServer:
        """Runの画面、イベント、操作を同じローカルオリジンに公開する。"""
        # ポート番号だけで操作できないようにし、他サイトからのPOSTも拒否する。
        prefix = "/" + secrets.token_urlsafe(24) + "/"
        handler = _dashboard_handler(self, config, html, script, prefix)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        host = f"127.0.0.1:{server.server_port}"
        print(f"Progress dashboard: http://{host}{prefix}", flush=True)
        print("Press Ctrl+C in this terminal to close the dashboard.", flush=True)
        return server


def _dashboard_handler(dashboard, config, html, script, prefix):
    """HTTPハンドラーを組み立て、設定とプレビューの状態をサーバー単位で共有する。"""
    manager = getattr(dashboard, "manager", None)
    preview_lock = Lock()
    reference_previews: dict[str, ExecutionControl] = {}

    class Handler(BaseHTTPRequestHandler):
        """ダッシュボード専用のパスとオリジンでHTTPの受付範囲を絞る。"""

        @property
        def expected_host(self) -> str:
            """実際に割り当てたループバックのポートを含むHostを返す。"""
            return f"127.0.0.1:{self.server.server_port}"

        def bind_view(self) -> bool:
            """HTTP要求から対象Runの画面状態を取得する。"""
            try:
                requested = self.headers.get("X-Run-ID") or parse_qs(urlsplit(self.path).query).get("run_id", [None])[0]
                self.dashboard = manager.get(requested) if manager else dashboard
                return True
            except _ERRORS as exc:
                self.send_body(409, "text/plain; charset=utf-8", format_error(exc).encode())
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
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            """閲覧用のアセット、進捗配信、保存された結果を振り分ける。"""
            if self.headers.get("Host") != self.expected_host or not self.path.startswith(prefix):
                self.send_error(403)
                return
            if not self.bind_view():
                return
            self.send_get_route()

        def send_get_route(self) -> None:
            """検証済みのGETを、アセットまたはRunの読み取り操作へ振り分ける。"""
            if self.path.partition("?")[0] == prefix:
                self.send_body(200, "text/html; charset=utf-8", html)
            elif self.path == prefix + "dashboard.js":
                self.send_body(200, "text/javascript; charset=utf-8", script)
            elif self.path == prefix + "dashboard_runs.js":
                self.send_body(200, "text/javascript; charset=utf-8", Path(__file__).with_name("dashboard_runs.js").read_bytes())
            elif self.path == prefix + "dashboard_editor.js":
                self.send_body(200, "text/javascript; charset=utf-8", Path(__file__).with_name("dashboard_editor.js").read_bytes())
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
            elif self.path.partition("?")[0] == prefix + "editor":
                self.send_editor()
            elif self.path.partition("?")[0] == prefix + "resume-plan":
                try:
                    query = parse_qs(urlsplit(self.path).query)
                    run_id = query.get("run_id", [None])[0]
                    if run_id:
                        validate_run_id(run_id)
                    view = manager.get(run_id) if manager and run_id else self.dashboard
                    if not run_id or run_id != view.state.get("run_id") or not view.state.get("ready"):
                        raise ValueError("処理が終わってから再開方法を選んでください。")
                    options = {key: int(query[key][0]) for key in ("through_capsule", "rewind_to") if key in query}
                    plan = plan_stage_resume(config, run_id, query.get("start_stage", ["build"])[0], **options)
                    self.send_body(200, "application/json", json.dumps(plan, ensure_ascii=False).encode())
                except _ERRORS as exc:
                    self.send_body(409, "text/plain; charset=utf-8", format_error(exc).encode())
            elif self.path.partition("?")[0] == prefix + "editor-php":
                self.send_editor_php()
            elif self.path.partition("?")[0] == prefix + "events":
                self.send_events()
            elif self.path.partition("?")[0] == prefix + "junie-thoughts":
                self.send_junie_thoughts()
            elif self.path.partition("?")[0] == prefix + "workspace":
                self.send_workspace()
            elif self.path.partition("?")[0] == prefix + "token-runs":
                self.send_workspace(include_usage=True)
            elif self.path.partition("?")[0] == prefix + "settings":
                self.send_settings()
            elif self.path.partition("?")[0] == prefix + "junie-models":
                try:
                    body = json.dumps({"models": list_models()}).encode()
                except _ERRORS as exc:
                    self.send_body(409, "text/plain; charset=utf-8", format_error(exc).encode())
                else:
                    self.send_body(200, "application/json", body)
            elif self.path.partition("?")[0] == prefix + "prompts":
                self.send_prompts()
            elif self.path.partition("?")[0] == prefix + "checker-output":
                self.send_checker_output()
            elif self.path.partition("?")[0] == prefix + "build-review":
                self.send_build_review()
            elif self.path.partition("?")[0] == prefix + "migration-ledger":
                run_id = self.dashboard.state.get("run_id")
                if run_id and self.dashboard.state.get("worktree_path"):
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

        def send_junie_thoughts(self) -> None:
            """Runに保存されたJunieの思考表示を返す。"""
            state = self.dashboard.state
            args = parse_qs(urlsplit(self.path).query)
            run_id = args.get("run_id", [None])[0]
            if not run_id or run_id != state.get("run_id"):
                self.send_error(404)
                return
            try:
                validate_run_id(run_id)
                cursor = int(args.get("cursor", ["0"])[0])
                if cursor < 0 or cursor > 2**63 - 1:
                    raise ValueError("invalid log cursor")
            except ValueError:
                self.send_error(400)
                return
            try:
                run_root = Path((manager.config if manager else config)["run_root"]).resolve()
                batch = read_junie_thoughts(run_root, run_id, cursor)
            except (KeyError, OSError, ValueError):
                self.send_error(409)
                return
            live = bool(state.get("operation_id") and not state.get("ready") and not state.get("queued"))
            self.send_body(200, "application/json", json.dumps({**batch, "live": live}, ensure_ascii=False).encode("utf-8"))

        def send_prompts(self, action: dict[str, Any] | None = None) -> None:
            """追加指示の受付状態を画面へ返す。"""
            state = self.dashboard.state
            run_id = state.get("run_id")
            payload = {"run_id": run_id, "timing": None, "prompts": []}
            try:
                if action is not None and action.get("run_id") != run_id:
                    raise ValueError("表示中の実行が変わりました。")
                if run_id and state.get("worktree_path"):
                    run_path, _, _, _ = load_saved_run(config, run_id)
                    with editor_queue(run_path) as queue:
                        with self.dashboard.condition:
                            state = self.dashboard.state
                            if state.get("run_id") != run_id:
                                raise ValueError("表示中の実行が変わりました。")
                            _, _, record, _ = load_saved_run(config, run_id)
                            timing = _prompt_timing(config, state, run_path, record, pending_edits=bool(queue["edits"]),
                                                    live=bool(self.dashboard.control and self.dashboard.control.junie_steering
                                                              and not queue["edits"]))
                            if action is not None:
                                if timing is None:
                                    raise ValueError("現在の工程では指示を登録できません。")
                                add_prompt(queue, action)
                        payload.update(timing=timing, prompts=queue["prompts"])
                elif action is not None:
                    raise ValueError("移行の実行を開いてから指示を登録してください。")
                self.send_body(200, "application/json", json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            except _ERRORS as exc:
                self.send_body(409, "text/plain; charset=utf-8", str(exc).encode("utf-8", errors="replace"))

        def send_editor_php(self) -> None:
            """Runに固定されたPHP入力を読み取り専用で返す。"""
            query = parse_qs(urlsplit(self.path).query)
            run_id = self.dashboard.state.get("run_id")
            try:
                if not run_id or query.get("run_id", [None])[0] != run_id:
                    raise ValueError("表示中の実行が変わりました。")
                run_path, _, _, _ = load_saved_run(config, run_id)
                capsule, manifest = load_capsule_manifest(run_path)
                pinned = pinned_php_sources(manifest)
                encodings: dict[str, str] = {}
                for path, encoding in migration_php_encodings(manifest).items():
                    encodings.setdefault(Path(path).as_posix(), encoding)
                sources = {Path(entry["path"]).as_posix(): entry for entry in (pinned or [])}
                if pinned is not None:
                    for name in encodings:
                        if name not in sources:
                            raise ValueError(f"移行対象のPHPがRunに保存されていません: {name}")
                names = list(encodings)
                name = query.get("source", names[:1] or [None])[0]
                if name is not None and name not in names:
                    raise ValueError("この実行の移行元 PHP を選んでください。")
                content = ""
                error = None
                try:
                    if name in sources:
                        entry = sources[name]
                        content = read_pinned_php(capsule, entry).decode(entry["encoding"])
                    elif name is not None:
                        path = _inside_file(Path(config["project_root"]).resolve(), name)
                        content = path.read_text(encoding=input_encoding(path, encodings[name]))
                except (OSError, ValueError, LookupError) as exc:
                    error = format_error(exc)
                payload = {"run_id": run_id, "sources": names, "source": name, "content": content, "error": error}
                self.send_body(200, "application/json", json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            except _ERRORS as exc:
                self.send_body(409, "text/plain; charset=utf-8", format_error(exc).encode("utf-8"))

        def editor_view_versions(self, query, state, run_id, run_path, worktree, record, unit):
            """Run内の現物と履歴の版を算出し、変更なしの取得で本文の再読込を省く。"""
            names = [path.as_posix() for path in target_paths(unit) if path.suffix.lower() in EDITOR_TARGET_SUFFIXES]
            name = query.get("target", names[:1] or [None])[0]
            if name is not None and name not in names:
                raise ValueError("設定された移行先ファイルを選んでください。")
            agent = run_path / "agent-workspace"
            phase = record["status"]
            root = agent if phase in AGENT_PHASES and agent.is_dir() and not agent.is_symlink() else worktree
            try:
                target_stamp = _editor_stamp(safe_target(root, Path(name))) if name else None
                content_version = _editor_version(run_id, str(root), name, target_stamp)
            except (OSError, ValueError):
                content_version = None
            live = bool(self.dashboard.control and self.dashboard.control.junie_steering)
            adopted_stamp = None
            if name and record.get("stopped_migration_draft") and root == agent:
                adopted_stamp = _editor_stamp(safe_target(worktree, Path(name)))
            view_version = _editor_version(
                content_version, adopted_stamp, record.get("stopped_migration_draft"),
                names, phase, record.get("current_capsule"), record.get("operator_question"),
                record.get("completed_capsules"), record.get("context_capsules"), record.get("script_phase"),
                _editor_stamp(run_path / "editor.json"),
                _editor_stamp(run_path.parent / ".preparation-conversations" / (run_id + ".json")), live,
                *(state.get(key) for key in ("ready", "generating_anchors", "stop_requested")),
            )
            return view_version, content_version, root, name

        def send_editor(self, action: dict[str, Any] | None = None) -> None:
            """Runと編集版を照合し、キューロック内で取得または保存する。"""
            query = parse_qs(urlsplit(self.path).query)
            state = self.dashboard.state
            run_id = state.get("run_id")
            try:
                if "run_id" in query and query["run_id"] != [run_id]:
                    raise ValueError("表示中の実行が変わりました。")
                if not run_id or not state.get("worktree_path"):
                    if action is not None:
                        raise ValueError("移行の実行を開いてから保存してください。")
                    payload = {"run_id": None, "targets": [], "prompts": [], "can_prompt": False}
                    if manager and run_id:
                        payload.update(run_id=run_id, can_prompt=True, status_only=True,
                                       conversation=manager.conversations.preparation_replies(run_id, state))
                else:
                    if action is not None and action.get("run_id") != run_id:
                        raise ValueError("表示中の実行が変わりました。")
                    run_path, worktree, record, unit = load_saved_run(config, run_id)
                    versions = None
                    if action is None:
                        versions = self.editor_view_versions(query, state, run_id, run_path, worktree, record, unit)
                        view_version, content_version, _, _ = versions
                        if content_version and query.get("view_version") == [view_version]:
                            self.send_body(200, "application/json", b'{"unchanged":true}')
                            return
                    with editor_queue(run_path) as queue:
                        state = self.dashboard.state
                        if state.get("run_id") != run_id:
                            raise ValueError("表示中の実行が変わりました。")
                        _, worktree, record, unit = load_saved_run(config, run_id)
                        targets = tuple(path for path in target_paths(unit) if path.suffix.lower() in EDITOR_TARGET_SUFFIXES)
                        names = [path.as_posix() for path in targets]
                        name = query.get("target", names[:1] or [None])[0]
                        if action is not None and action.get("kind") == "save":
                            name = action.get("target")
                            if name not in names:
                                raise ValueError("設定された移行先ファイルを選んでください。")
                        elif name is not None and name not in names:
                            raise ValueError("設定された移行先ファイルを選んでください。")
                        phase = record["status"]
                        agent_root = run_path / "agent-workspace"
                        if agent_root.is_symlink():
                            raise ValueError("Junie の作業場所がシンボリックリンクです。")
                        from_agent = phase in AGENT_PHASES and agent_root.is_dir()
                        root = agent_root if from_agent else worktree
                        busy_other = state.get("generating_anchors") or state.get("stop_requested")
                        queued = bool(queue["edits"] or not state.get("ready") or busy_other or phase in AGENT_PHASES)
                        prompt_timing = _prompt_timing(config, state, run_path, record, pending_edits=bool(queue["edits"]),
                                                      live=bool(self.dashboard.control and self.dashboard.control.junie_steering
                                                                and not queue["edits"]))
                        read_error = None
                        try:
                            original, content = editor_target(root, Path(name)) if name else (None, "")
                        except (OSError, ValueError) as exc:
                            if action is not None and action.get("kind") == "save":
                                raise
                            original, content = None, ""
                            read_error = format_error(exc)
                        edit = queue["edits"].get(name)
                        if action is not None:
                            if action.get("kind") == "prompt" and prompt_timing:
                                add_prompt(queue, action)
                            elif action.get("kind") == "save" and name:
                                base = editor_text(action.get("base"))
                                draft = editor_text(action.get("content"))
                                if action.get("edit_id") != (edit["id"] if edit else None):
                                    raise ValueError("保存済みの編集が別の画面で変更されました。")
                                # キュー、対象ファイル、Runの順序でロックし、保存直前に再照合する。
                                with worktree_source_lock, self.dashboard.condition:
                                    state = self.dashboard.state
                                    if state.get("run_id") != run_id:
                                        raise ValueError("表示中の実行が変わりました。")
                                    # 保存直前に実行が始まった場合も、編集をRunへ退避する。
                                    busy_other = state.get("generating_anchors") or state.get("stop_requested")
                                    queued = bool(queued or not state.get("ready") or busy_other or content != base)
                                    if queued:
                                        if edit and edit["status"] == "conflict" and (
                                            not action.get("resolve") or base != content
                                        ):
                                            raise ValueError("最新版との差分を確認し、競合を解消して保存してください。")
                                        edit = {"id": secrets.token_hex(12), "base": base, "content": draft, "status": "queued"}
                                        queue["edits"][name] = edit
                                        prompt_timing = _prompt_timing(config, state, run_path, record, pending_edits=True)
                                    else:
                                        save_target_edit(worktree, targets, {
                                            "target": name, "version": hashlib.sha256(original).hexdigest() if original is not None else None,
                                            "content": draft,
                                        })
                                        record_operator_edit(queue, name, content, draft)
                                        original, content = editor_target(worktree, Path(name))
                                    queue["edit_revision"] = secrets.token_hex(12)
                                    invalidate_queued_steering(queue)
                            else:
                                raise ValueError("現在の工程ではこの操作を受け付けられません。")
                        payload = {
                            "run_id": run_id, "targets": names, "target": name, "content": content, "edit": edit,
                            "source": "agent-workspace" if from_agent else "worktree",
                            "can_save": bool(name and read_error is None), "queued": queued,
                            "save_timing": ("repair_restart" if phase == "repairing" and not state.get("ready") and not busy_other
                                            else "checkpoint" if queued and not state.get("ready") and not busy_other
                                            and phase in QUEUE_PHASES else "resume" if queued else "immediate"),
                            "exists": original is not None, "read_error": read_error,
                            "can_prompt": prompt_timing is not None, "prompt_timing": prompt_timing,
                            "prompts": queue["prompts"], "capsule": record.get("current_capsule"),
                            "status": phase, "operator_question": record.get("operator_question"),
                        }
                        if name and from_agent and (draft := record.get("stopped_migration_draft")):
                            payload["stopped_draft"] = True
                            with suppress(OSError, ValueError):
                                adopted, text = editor_target(worktree, Path(name))
                                digest = hashlib.sha256(adopted).hexdigest() if adopted is not None else None
                                payload["comparison_content"] = text
                                payload["comparison_label"] = ("採用済みとの差分" if digest == draft["base"].get(name)
                                                               else "作業ツリーとの差分")
                        if manager:
                            conversation = manager.conversations.preparation_replies(run_id, state) + queue.get("conversation", [])
                            payload.update(conversation=sorted(conversation, key=lambda entry: entry.get("received_at", "")),
                                           can_prompt=True)
                        if versions and versions[2:] == (root, name):
                            view_version, content_version = versions[:2]
                            if read_error is not None:
                                content_version = None
                            payload.update(view_version=view_version if content_version else None, content_version=content_version)
                            edit_version = _editor_version(run_id, name, edit)
                            payload["edit_version"] = edit_version
                            if content_version and query.get("content_version") == [content_version]:
                                del payload["content"]
                            if query.get("edit_version") == [edit_version]:
                                del payload["edit"]
                            with self.dashboard.condition:
                                _editor_history_delta(payload, query.get("history_version", [None])[0],
                                                      self.dashboard.editor_history_versions)
                self.send_body(200, "application/json", json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            except _ERRORS as exc:
                self.send_body(409, "text/plain; charset=utf-8", format_error(exc).encode("utf-8"))

        def send_checker_output(self) -> None:
            """最新チェッカー結果と対象版を返す。"""
            query = parse_qs(urlsplit(self.path).query)
            with self.dashboard.condition:
                state = self.dashboard.state
                current_config = config.copy()
            run_id = state.get("run_id")
            if (not run_id or query.get("run_id") != [run_id] or not state.get("ready")
                    or state.get("status") not in {"checker_needs_review", "checker_failed"}):
                self.send_body(409, "text/plain; charset=utf-8", "現在の実行から確認画面を開き直してください。".encode())
                return
            try:
                run_path, _, record, _ = load_saved_run(current_config, run_id)
                if record["status"] not in {"checker_needs_review", "checker_failed"}:
                    raise ValueError("チェッカーの結果が変わりました。確認画面を開き直してください。")
                if query.get("download") != ["1"]:
                    output, truncated = _review_log(run_path, "checker.log")
                    body = json.dumps({"output": output, "truncated": truncated}, ensure_ascii=False).encode()
                    self.send_body(200, "application/json; charset=utf-8", body)
                    return
                path = safe_target(run_path, Path("checker.log"))
                if not path.is_file():
                    raise ValueError("チェッカーの出力ファイルが見つかりません。")
                stream = path.open("rb")
            except _ERRORS as exc:
                self.send_body(409, "text/plain; charset=utf-8", str(exc).encode("utf-8", errors="replace"))
                return
            with stream:
                self.connection.settimeout(5)
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition", 'attachment; filename="checker.log"')
                self.send_header("Content-Length", str(os.fstat(stream.fileno()).st_size))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                try:
                    shutil.copyfileobj(stream, self.wfile)
                except OSError:
                    return

        def send_build_review(self) -> None:
            """ビルド失敗と修正確認に必要な情報を返す。"""
            query = parse_qs(urlsplit(self.path).query)
            with self.dashboard.condition:
                state = self.dashboard.state
                if (not state.get("ready") or state.get("status") != "build_failed"
                        or query.get("run_id") != [state.get("run_id")]):
                    self.send_body(409, "text/plain; charset=utf-8", "現在の実行から確認画面を開き直してください。".encode())
                    return
                current_config = config.copy()
            try:
                review = build_failure_review(current_config, state["run_id"], query.get("target", [None])[0])
            except _ERRORS as exc:
                self.send_body(409, "text/plain; charset=utf-8", format_error(exc).encode())
            else:
                self.send_body(200, "application/json; charset=utf-8", json.dumps(review, ensure_ascii=False).encode())

        def send_workspace(self, *, include_usage: bool = False) -> None:
            """Runの状態と画面操作情報を返す。"""
            if manager:
                try:
                    body = json.dumps(manager.workspace(include_usage=include_usage), ensure_ascii=False).encode()
                except _ERRORS as exc:
                    self.send_body(409, "text/plain; charset=utf-8", ("実行一覧を読み込めませんでした。設定と保存先を確認してください。\n" + format_error(exc)).encode())
                else:
                    self.send_body(200, "application/json", body)
                return
            try:
                with self.dashboard.condition:
                    workspace_config = config.copy()
                units = [unit["id"] for unit in workspace_config.get("units", [])]
                records = Path(workspace_config["run_root"]).glob("*/run.json") if units else ()
                newest = sorted(records, key=lambda path: path.stat().st_mtime, reverse=True)
                runs = []
                for record_path in newest:
                    try:
                        _, _, record, _ = load_saved_run(workspace_config, record_path.parent.name)
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
            """設定フォーム用の現在値と選択肢を返す。"""
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
            with self.dashboard.condition:
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
            """URL、オリジン、本文を検証し、対象Runの操作に振り分ける。"""
            if self.path not in (prefix + "control", prefix + "comparison", prefix + "settings",
                                  prefix + "build-response", prefix + "editor", prefix + "prompts"):
                self.send_error(404)
                return
            if self.headers.get("Host") != self.expected_host or self.headers.get("Origin") != "http://" + self.expected_host:
                self.send_error(403)
                return
            route = self.path.removeprefix(prefix)
            try:
                action = self.read_post_action(route)
            except (OSError, ValueError):
                self.send_error(400)
                return
            if not self.bind_post_view(route, action):
                return
            if route == "settings":
                self.save_settings(action)
            elif route == "editor":
                self.post_editor(action)
            elif route == "prompts":
                self.send_prompts(action)
            elif route == "comparison":
                self.create_comparison(action)
            else:
                self.post_control(action)

        def read_post_action(self, route: str) -> dict[str, Any]:
            """ルートごとの上限とJSONの共通条件を確認して本文を読む。"""
            limit = 16_384
            if route == "settings":
                limit = 8 * 1024 * 1024
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
            """新規Runでは既定の画面を使い、既存Runでは明示IDを優先する。"""
            creating = manager and route == "control" and action.get("action") in ("run", "prepare")
            if creating:
                self.dashboard = manager.get()
            elif not self.bind_view():
                return False
            if not creating and manager and action.get("run_id"):
                try:
                    self.dashboard = manager.get(action["run_id"])
                except _ERRORS as exc:
                    self.send_body(409, "text/plain; charset=utf-8", format_error(exc).encode())
                    return False
            return True

        def post_editor(self, action: dict[str, Any]) -> None:
            """編集を保存するか、会話を永続化して受付後にディスパッチへ通知する。"""
            if action.get("kind") == "conversation" and manager:
                run_id = self.dashboard.state.get("run_id")
                try:
                    if not run_id or run_id != action.get("run_id"):
                        raise ValueError("表示中の実行が変わりました。")
                    entry = manager.conversations.submit(run_id, action)
                except _ERRORS as exc:
                    self.send_body(409, "text/plain; charset=utf-8", format_error(exc).encode())
                else:
                    try:
                        self.send_body(202, "application/json", json.dumps(entry, ensure_ascii=False).encode())
                    finally:
                        if not entry.get("status_only"):
                            manager.conversations.notify(run_id)
                return
            self.send_editor(action)

        def create_comparison(self, action: dict[str, Any]) -> None:
            """表示中のRunからアンカー生成なしで比較を作る。"""
            with self.dashboard.condition:
                comparison_config = config.copy()
                state = self.dashboard.state
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

        def validate_control_action(self, action: dict[str, Any]) -> None:
            """再開計画、判断理由、ビルド失敗時の入力などを検証する。"""
            if type(action.get("revision")) is not int:
                raise ValueError
            if "verification_request" in action:
                raise ValueError
            if "start_stage" in action and (not manager or action.get("action") != "resume-run"
                                            or action["start_stage"] not in {"build", "migration", "checker"}):
                raise ValueError
            if any(key in action for key in ("through_capsule", "rewind_to")) and (
                action.get("start_stage") != "migration"
                or any(type(action[key]) is not int for key in ("through_capsule", "rewind_to") if key in action)
            ):
                raise ValueError
            if action.get("action") not in (
                "stop", "junie-answer", "accept", "resume", "anchors", "prepare", "run", "open-run", "resume-run",
                "build-response", "cancel-queued", "hide-run",
            ):
                raise ValueError
            if action["action"] in ("prepare", "run", "open-run", "resume-run"):
                name = action.get("run_id" if action["action"] in ("open-run", "resume-run") else "unit_id")
                if not config.get("units") or (not action.get("unit_ids") and (not isinstance(name, str) or not name.strip())):
                    raise ValueError
            if action["action"] == "accept" and (
                not isinstance(action.get("finding_id"), str) or len(action["finding_id"]) != 12
                or not isinstance(action.get("reason"), str) or not 0 < len(action["reason"].strip()) <= 2_000
            ):
                raise ValueError
            if "checker_instruction" in action:
                if action["action"] != "resume" or not editor_text(action["checker_instruction"], 4_000).strip():
                    raise ValueError
            if action["action"] == "build-response":
                instruction, edit = action.get("instruction", ""), action.get("edit")
                if not isinstance(instruction, str) or len(instruction) > 4_000 or "\0" in instruction:
                    raise ValueError
                instruction.encode("utf-8")
                if edit is not None and (
                    not isinstance(edit, dict) or not all(isinstance(edit.get(key), str) for key in ("target", "version", "content"))
                    or not re.fullmatch(r"[0-9a-f]{64}", edit["version"])
                    or "\0" in edit["content"] or len(edit["content"].encode("utf-8")) > EDIT_BYTES
                ):
                    raise ValueError

        def post_control(self, action: dict[str, Any]) -> None:
            """検証済みの制御操作をRunManagerまたは表示中のRunへ渡す。"""
            try:
                self.validate_control_action(action)
            except (OSError, ValueError):
                self.send_error(400)
                return
            if manager and action["action"] not in {"stop", "junie-answer", "anchors"}:
                try:
                    if stage := action.get("start_stage"):
                        plan = plan_stage_resume(config, action["run_id"], stage,
                                                 through_capsule=action.get("through_capsule"),
                                                 rewind_to=action.get("rewind_to"))
                        if action.get("plan_version") != plan["version"]:
                            raise ValueError("保存版が更新されました。再開計画を確認し直してください。")
                        action["verification_request"] = {**plan, "id": uuid.uuid4().hex}
                    result = manager.submit(action, self.dashboard)
                except _ERRORS as exc:
                    self.send_body(409, "text/plain; charset=utf-8", format_error(exc).encode())
                else:
                    self.send_body(202, "application/json", json.dumps(result).encode())
                return
            if action["action"] == "stop":
                if self.dashboard.request_stop(action):
                    self.send_body(202, "application/json", b"{}")
                else:
                    self.send_error(409)
            elif action["action"] == "junie-answer":
                if self.dashboard.answer_junie(action):
                    self.send_body(202, "application/json", b"{}")
                else:
                    self.send_error(409)
            elif action["action"] == "anchors":
                self.send_anchored_comparison(action)
            elif self.dashboard.submit(action):
                self.send_body(202, "application/json", b"{}")
            else:
                self.send_error(409)

        def save_settings(self, action: dict[str, Any]) -> None:
            """利用者の設定変更を検証して保存する。"""
            if action.get("mode") in {"reference-preview", "reference-cancel"}:
                self.reference_preview(action)
                return
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
                with self.dashboard.condition:
                    if (manager.busy if manager else not self.dashboard.state.get("ready") or action["revision"] != self.dashboard.revision):
                        raise ValueError("実行中の処理が終わってから設定を保存してください。")
                    path = Path(config["config_path"])
                    updated = _save_settings(path, action["content"], action["version"],
                                             before_replace=manager.configure if manager else None)
                    config.pop("checker_from_cli", None)
                    config.pop("toml_checker_command", None)
                    config.update(updated)
                    if manager:
                        manager.start_pending()
                    self.dashboard.publish({**self.dashboard.state, "ready": True, "settings_revision": self.dashboard.revision + 1,
                                            "diff_viewer_enabled": bool(config.get("diff_viewer_html"))})
            except _ERRORS as exc:
                message = f"設定を保存できませんでした。内容を確認してください。\n{format_error(exc)}"
                self.send_body(409, "text/plain; charset=utf-8", message.encode())
            else:
                version = hashlib.sha256(action["content"].encode()).hexdigest()
                self.send_body(200, "application/json", json.dumps({"version": version}).encode())

        def reference_preview(self, action):
            """未保存のTOMLから参照候補だけを走査し、同じ画面へ進捗を返す。"""
            request_id = action.get("request_id")
            if not isinstance(request_id, str) or not re.fullmatch(r"[0-9a-f-]{36}", request_id):
                self.send_error(400)
                return
            if action["mode"] == "reference-cancel":
                with preview_lock:
                    if control := reference_previews.get(request_id):
                        control.cancelled.set()
                self.send_body(200, "application/json", b"{}")
                return
            if not all(isinstance(action.get(key), str) for key in ("content", "unit")):
                self.send_error(400)
                return

            def send_event(data):
                self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8") + b"\n")
                self.wfile.flush()

            control = ExecutionControl(lambda event: send_event({"progress": event["message"]}))
            with preview_lock:
                if reference_previews:
                    self.send_body(409, "text/plain; charset=utf-8", "別の参照資料を確認中です。".encode())
                    return
                reference_previews[request_id] = control
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                send_event({"progress": "参照資料を確認しています。"})
                path = Path(config["config_path"])
                with NamedTemporaryFile(dir=path.parent, delete=False) as stream:
                    candidate = Path(stream.name)
                try:
                    candidate.write_bytes(action["content"].encode("utf-8"))
                    draft = load_config(candidate)
                finally:
                    candidate.unlink(missing_ok=True)
                unit = find_unit(draft, action["unit"])
                with control_scope(control):
                    result = preview_target_context(Path(draft["project_root"]), target_paths(unit), unit.get("target_context"))
                for excess in result["exceeded"].values():
                    excess["limit"] = str(excess["limit"])
                send_event({"result": result})
            except OperationCancelled:
                with suppress(OSError):
                    send_event({"cancelled": True})
            except _ERRORS as exc:
                with suppress(OSError):
                    send_event({"error": format_error(exc)})
            finally:
                with preview_lock:
                    reference_previews.pop(request_id, None)

        def send_anchored_comparison(self, action: dict[str, Any]) -> None:
            """端末側でアンカー生成が終わるまで待ち、作成元Runと比較のIDを返す。"""
            if not config.get("diff_viewer_html"):
                self.send_body(409, "text/plain; charset=utf-8", "設定の差分ビューアーのHTMLを指定してください。".encode())
                return
            # ブラウザーを閉じても端末側は結果を保存できる。応答は要求元だけに返す。
            reply: Future[tuple[str, str, int]] = Future()
            try:
                accepted = manager.submit({**action, "reply": reply, "run_id": self.dashboard.state.get("run_id")}, self.dashboard) if manager else self.dashboard.submit({**action, "reply": reply})
            except _ERRORS as exc:
                self.send_body(409, "text/plain; charset=utf-8", format_error(exc).encode())
                return
            if not accepted:
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

    return Handler


def execute_dashboard_action(view, config, action):
    """1つのRunだけの状態・入力待ち・停止要求をワーカースレッドに結び付ける。"""
    token = run_observer.set(view.observe)
    result = None
    run_id = action["run_id"]
    conversation_id = action.get("conversation_id")
    try:
        with control_scope(view.control):
            if view.control is not None:
                # ログの保存先を決められない場合も、実行側の通常の検証とエラー表示を使う。
                with suppress(KeyError, OSError, ValueError):
                    validate_run_id(run_id)
                    view.control.junie_thoughts_path = Path(config["run_root"]).resolve() / run_id / JUNIE_THOUGHTS_FILENAME
            kind = action["action"]
            if kind == "anchors":
                path, count = generate_anchors(config, run_id, focus=action.get("focus"))
                if action.get("reply"):
                    action["reply"].set_result((run_id, path.stem, count))
                _, _, result, _ = load_saved_run(config, run_id)
            elif kind == "prepare":
                prepare_run(config, action["unit_id"], run_id=run_id)
                _, _, result, _ = load_saved_run(config, run_id)
            elif kind == "run":
                result = run_with_junie(config, action["unit_id"], run_id=run_id)
            elif kind == "conversation":
                result = run_instruction(config, run_id, conversation_id)
                finish_instruction(config, run_id, conversation_id, result)
            elif kind == "accept":
                review_checker(config, run_id, action["finding_id"], action["reason"])
                _, _, result, _ = load_saved_run(config, run_id)
            else:
                kwargs = {}
                if kind == "build-response":
                    kwargs["build_response"] = action
                if "checker_instruction" in action:
                    kwargs["checker_instruction"] = action["checker_instruction"]
                if "verification_request" in action:
                    kwargs["verification_request"] = action["verification_request"]
                result = resume_with_junie(config, run_id, **kwargs)
            view.observe(result)
            view.ready(config, result, "判断理由を記録しました。" if kind == "accept" else "")
    except (*_ERRORS, KeyboardInterrupt) as exc:
        if conversation_id:
            try:
                finish_instruction(config, run_id, conversation_id, None, exc)
            except _ERRORS as save_error:
                exc.add_note(f"Junie の履歴を保存できませんでした。{format_error(save_error)}")
        unconfirmed_stop = unconfirmed_junie_stop(exc)
        pending_save_error = None
        if unconfirmed_stop is not None:
            if manager := getattr(view, "manager", None):
                manager.block_conversations(run_id, str(unconfirmed_stop))
            try:
                fail_pending_after_unconfirmed_stop(config, run_id, str(unconfirmed_stop))
            except _ERRORS as save_error:
                pending_save_error = format_error(save_error)
        stopped = unconfirmed_stop is None and (
            isinstance(exc, KeyboardInterrupt) or bool(view.control and view.control.cancelled.is_set())
        )
        try:
            run_path, _, result, _ = load_saved_run(config, run_id)
            if stopped:
                phase = getattr(view, "stop_phase", None) or result["status"]
                phase_label = {
                    "junie_running": "移行", "editor_followup": "追加指示", "repairing": "自動修正",
                    "building": "ビルド", "checking": "チェッカー",
                }.get(phase, "処理")
                record_message = (f"{phase_label}を停止しました。保存版と保留中の編集を確認してから再開できます。")
                stop = {"phase": phase, "saved_status": result["status"], "message": record_message}
                try:
                    with edit_run_record(run_path / "run.json") as (_, changes):
                        changes["last_stop"] = stop
                    result["last_stop"] = stop
                except _ERRORS as save_error:
                    pending_save_error = format_error(save_error)
            view.observe(result)
            view.ready(config, result)
        except _ERRORS as load_error:
            if isinstance(exc, RunScriptError):
                exc.__cause__ = load_error
            view.publish({**view.state, "status": "preparation_interrupted" if stopped else "preparation_failed"})
        if stopped:
            view.control = None
            view.publish({**view.state, "ready": True, "cancellable": False, "stop_requested": False,
                          "operation_id": None, "generating_anchors": False,
                          "stage_timings": freeze_timings(view.state), "active_stage": None,
                          "message": ((result.get("last_stop", {}).get("message") or
                                       "停止しました。同じRunから再開できます。") if result else
                                      "準備を停止しました。移行単位を選んで新しく実行してください。")
                                     + (f" 停止記録を保存できませんでした。{pending_save_error}" if pending_save_error else "")})
        else:
            view.failed(exc)
            if pending_save_error:
                view.publish({**view.state, "message": view.state["message"] +
                              "\n保留中の指示の停止を保存できません。残存プロセスと保存先を確認してください。\n" +
                              pending_save_error})
        if action.get("reply") and not action["reply"].done():
            action["reply"].set_exception(ValueError("処理を停止しました。" if stopped else format_error(exc)))
    finally:
        run_observer.reset(token)
    return result


def render_dashboard_html(options: str) -> bytes:
    """本番ダッシュボード用に共有テンプレートと相対アセットURLを組み立てる。"""
    demo = harness_asset("progress-demo.html")
    html, marker, _ = demo.read_text(encoding="utf-8").partition("<!-- dashboard-script -->")
    if not marker:
        raise ValueError("progress dashboard template is missing its script marker")
    html = html.replace("./src/migration_harness/dashboard_runs.js", "dashboard_runs.js")
    html = html.replace('<html lang="ja">', f'<html lang="ja"{options}>')
    html += '<script src="dashboard.js" defer></script><script src="dashboard_editor.js" defer></script></body></html>'
    return html.encode("utf-8")


def run_dashboard(
    config: dict[str, Any], *, unit_id: str | None = None, run_id: str | None = None,
    config_error: str | None = None,
) -> dict[str, Any] | None:
    """サーバーを開き、ブラウザーで移行の進捗と確認操作を公開する。"""
    if unit_id is not None and run_id is not None:
        raise ValueError("choose a migration unit or a saved Run ID")
    package_dir = Path(__file__).resolve().parent
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
            _, _, saved, _ = load_saved_run(config, run_id)
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
    html = render_dashboard_html(options)
    from .run_manager import RunManager

    manager = RunManager(config, view)
    view.manager = manager
    try:
        if unit_id is not None and config_error is None:
            submitted = manager.submit({"action": "run", "unit_id": unit_id})
            run_id = submitted["run_id"]
            manager.default = manager.get(run_id)
        with view.server(config, html, (package_dir / "dashboard.js").read_bytes()) as server:
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                manager.start_pending()
                with manager.condition:
                    manager.conversations.start()
                while True:
                    try:
                        action = view.actions.get(timeout=0.25)
                    except Empty:
                        continue
                    manager.submit(action, view)
            except KeyboardInterrupt:
                pass
            finally:
                manager.close()
                server.shutdown()
                thread.join()
    finally:
        manager.close()
    return manager.results.get(run_id, saved)
