from __future__ import annotations

import hashlib
import copy
import os
import json
import re
import secrets
import shutil
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
from urllib.parse import parse_qs, urlsplit

from tomlkit.exceptions import TOMLKitError

from migration_harness.capsules import (
    input_encoding, load_capsule_manifest, migration_php_encodings, pinned_php_sources, read_pinned_php,
)
from migration_harness.config import load_config, target_paths
from migration_harness.diff_viewer import _inside_file, create_snapshot, review_html, snapshot_bytes
from migration_harness.editor_queue import (
    AGENT_PHASES, EDIT_BYTES, EDITOR_TARGET_SUFFIXES, QUEUE_PHASES,
    add_prompt, editor_queue, editor_target, editor_text, record_operator_edit,
)
from migration_harness.errors import format_error
from migration_harness.execution import ExecutionControl, OperationCancelled, control_scope
from migration_harness.files import harness_asset, safe_target
from migration_harness.review_anchors import generate_anchors
from migration_harness.run_records import load_saved_run, run_observer
from migration_harness.settings_form import build_settings_draft
from migration_harness.timings import freeze_timings
from migration_harness.workflow import (
    BUILD_REVIEW_EDIT_BYTES, RunInterrupted, RunScriptError, _review_log, _save_build_review_edit,
    build_failure_review, can_resume_run, migration_ledger, prepare_run, resume_with_junie,
    review_checker, run_with_junie,
)
from migration_harness.worktree_targets import worktree_source_lock


PROGRESS_FIELDS = (
    "run_id", "unit_id", "target", "worktree_path", "status", "created_at", "current_capsule",
    "context_capsules", "completed_capsules", "context_part_chars", "max_context_chars",
    "build_exit_code", "build_log_path",
    "checker_exit_code", "checker_log_path", "checker_review_reason", "error",
    "script_phase", "script_logs", "stage_timings", "active_stage", "timing_updated_at",
)
_ERRORS = (OSError, ValueError, RuntimeError, CalledProcessError, KeyError, TypeError, AttributeError, TOMLKitError)


def _prompt_timing(config, state, run_path, record) -> str | None:
    if state.get("stop_requested") or state.get("generating_anchors"):
        return None
    phase = record["status"]
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
        self.junie_reply: Future[str] | None = None
        self.trusted_junie_workspaces: set[str] = set()
        self.activity_sequence = 0

    def begin_operation(self) -> None:
        """停止要求は実行ごとのIDに結び付け、別のRunに持ち越さない。"""
        with self.condition:
            self.control = ExecutionControl(
                self.activity, on_junie_request=self.show_junie_request,
                trusted_junie_workspaces=self.trusted_junie_workspaces,
            )
            state = {key: value for key, value in self.state.items() if key != "activity"}
            self.publish({**state, "ready": False, "cancellable": True,
                          "operation_id": secrets.token_hex(16), "stop_requested": False})

    def activity(self, entry: dict[str, Any]) -> None:
        with self.condition:
            if not self.control or self.state.get("ready"):
                return
            self.activity_sequence += 1
            self.publish({**self.state, "activity": {**entry, "sequence": self.activity_sequence}})

    def show_junie_request(self, request: dict | None) -> Future[str] | None:
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
        with self.condition:
            if (self.control is None or self.state.get("ready")
                    or action.get("operation_id") != self.state.get("operation_id")):
                return False
            self.control.cancelled.set()
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
                elif self.path.partition("?")[0] == prefix + "editor-php":
                    self.send_editor_php()
                elif self.path == prefix + "events":
                    self.send_events()
                elif self.path == prefix + "workspace":
                    self.send_workspace()
                elif self.path == prefix + "token-runs":
                    self.send_workspace(include_usage=True)
                elif self.path == prefix + "settings":
                    self.send_settings()
                elif self.path == prefix + "prompts":
                    self.send_prompts()
                elif self.path.partition("?")[0] == prefix + "checker-output":
                    self.send_checker_output()
                elif self.path.partition("?")[0] == prefix + "build-review":
                    self.send_build_review()
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

            def send_prompts(self, action: dict[str, Any] | None = None) -> None:
                state = dashboard.state
                run_id = state.get("run_id")
                payload = {"run_id": run_id, "timing": None, "prompts": []}
                try:
                    if action is not None and action.get("run_id") != run_id:
                        raise ValueError("表示中の実行が変わりました。")
                    if run_id and state.get("worktree_path"):
                        run_path, _, _, _ = load_saved_run(config, run_id)
                        with editor_queue(run_path) as queue:
                            with dashboard.condition:
                                state = dashboard.state
                                if state.get("run_id") != run_id:
                                    raise ValueError("表示中の実行が変わりました。")
                                _, _, record, _ = load_saved_run(config, run_id)
                                timing = _prompt_timing(config, state, run_path, record)
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
                query = parse_qs(urlsplit(self.path).query)
                run_id = dashboard.state.get("run_id")
                try:
                    if not run_id or query.get("run_id", [None])[0] != run_id:
                        raise ValueError("表示中の実行が変わりました。")
                    run_path, _, _, _ = load_saved_run(config, run_id)
                    capsule, manifest = load_capsule_manifest(run_path)
                    pinned = pinned_php_sources(manifest)
                    legacy: dict[str, str] = {}
                    if pinned is None:
                        for path, encoding in migration_php_encodings(manifest).items():
                            legacy.setdefault(Path(path).as_posix(), encoding)
                    sources = {Path(entry["path"]).as_posix(): entry for entry in (pinned or [])}
                    names = list(sources if pinned is not None else legacy)
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
                            content = path.read_text(encoding=input_encoding(path, legacy[name]))
                    except (OSError, ValueError, LookupError) as exc:
                        error = format_error(exc)
                    payload = {"sources": names, "source": name, "content": content, "error": error}
                    self.send_body(200, "application/json", json.dumps(payload, ensure_ascii=False).encode("utf-8"))
                except _ERRORS as exc:
                    self.send_body(409, "text/plain; charset=utf-8", format_error(exc).encode("utf-8"))

            def send_editor(self, action: dict[str, Any] | None = None) -> None:
                query = parse_qs(urlsplit(self.path).query)
                state = dashboard.state
                run_id = state.get("run_id")
                try:
                    if not run_id or not state.get("worktree_path"):
                        if action is not None:
                            raise ValueError("移行の実行を開いてから保存してください。")
                        payload = {"run_id": None, "targets": [], "prompts": [], "can_prompt": False}
                    else:
                        if action is not None and action.get("run_id") != run_id:
                            raise ValueError("表示中の実行が変わりました。")
                        run_path, _, _, _ = load_saved_run(config, run_id)
                        with editor_queue(run_path) as queue:
                            state = dashboard.state
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
                            queued = bool(from_agent and phase in QUEUE_PHASES and not busy_other)
                            direct = bool(state.get("ready") and not busy_other and phase not in AGENT_PHASES)
                            prompt_timing = _prompt_timing(config, state, run_path, record)
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
                                elif action.get("kind") == "save" and name and original is not None and (queued or direct):
                                    base = editor_text(action.get("base"))
                                    draft = editor_text(action.get("content"))
                                    if action.get("edit_id") != (edit["id"] if edit else None):
                                        raise ValueError("保存済みの編集が別の画面で変更されました。")
                                    if queued:
                                        if edit and edit["status"] == "conflict" and (
                                            not action.get("resolve") or base != content
                                        ):
                                            raise ValueError("Junie の最新版を確認してから競合を解消してください。")
                                        edit = {"id": secrets.token_hex(12), "base": base, "content": draft, "status": "queued"}
                                        queue["edits"][name] = edit
                                    else:
                                        if content != base:
                                            raise ValueError("ファイルが更新されました。差分を確認してください。")
                                        with worktree_source_lock, dashboard.condition:
                                            if dashboard.state.get("run_id") != run_id or not dashboard.state.get("ready"):
                                                raise ValueError("処理が始まりました。終了後に保存してください。")
                                            _save_build_review_edit(worktree, targets, {
                                                "target": name, "version": hashlib.sha256(original).hexdigest(), "content": draft,
                                            })
                                        record_operator_edit(queue, name, content, draft)
                                        content = draft
                                else:
                                    raise ValueError("現在の工程ではこの操作を受け付けられません。")
                            payload = {
                                "run_id": run_id, "targets": names, "target": name, "content": content, "edit": edit,
                                "source": "agent-workspace" if from_agent else "worktree",
                                "can_save": bool(original is not None and (queued or direct)), "queued": queued,
                                "exists": original is not None, "read_error": read_error,
                                "can_prompt": prompt_timing is not None, "prompt_timing": prompt_timing,
                                "prompts": queue["prompts"], "capsule": record.get("current_capsule"),
                                "status": phase, "operator_question": record.get("operator_question"),
                            }
                    self.send_body(200, "application/json", json.dumps(payload, ensure_ascii=False).encode("utf-8"))
                except _ERRORS as exc:
                    self.send_body(409, "text/plain; charset=utf-8", format_error(exc).encode("utf-8"))

            def send_checker_output(self) -> None:
                query = parse_qs(urlsplit(self.path).query)
                with dashboard.condition:
                    state = dashboard.state
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
                query = parse_qs(urlsplit(self.path).query)
                with dashboard.condition:
                    state = dashboard.state
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
                try:
                    with dashboard.condition:
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
                if self.path not in (prefix + "control", prefix + "comparison", prefix + "settings",
                                      prefix + "build-response", prefix + "editor", prefix + "prompts"):
                    self.send_error(404)
                    return
                if self.headers.get("Host") != host or self.headers.get("Origin") != "http://" + host:
                    self.send_error(403)
                    return
                comparison = self.path == prefix + "comparison"
                settings = self.path == prefix + "settings"
                build_response = self.path == prefix + "build-response"
                editor = self.path == prefix + "editor"
                prompts = self.path == prefix + "prompts"
                limit = 16_384
                if settings:
                    limit = 8 * 1024 * 1024
                elif build_response:
                    # JSON エスケープはソースの 1 バイトを最大 6 バイトに展開する。
                    limit = 6 * BUILD_REVIEW_EDIT_BYTES + 256 * 1024
                elif editor:
                    limit = 12 * EDIT_BYTES + 16_384
                elif prompts:
                    limit = 32_768
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if comparison and length == 0:
                        action = {}
                    else:
                        if not 0 < length <= limit or self.headers.get_content_type() != "application/json":
                            raise ValueError
                        self.connection.settimeout(5)
                        action = json.loads(self.rfile.read(length))
                        if not isinstance(action, dict):
                            raise ValueError
                        if build_response != (action.get("action") == "build-response"):
                            raise ValueError
                except (OSError, ValueError):
                    self.send_error(400)
                    return
                if settings:
                    self.save_settings(action)
                    return
                if editor:
                    self.send_editor(action)
                    return
                if prompts:
                    self.send_prompts(action)
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
                    if action.get("action") not in (
                        "stop", "junie-answer", "accept", "resume", "anchors", "prepare", "run", "open-run", "resume-run",
                        "build-response",
                    ):
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
                            or "\0" in edit["content"] or len(edit["content"].encode("utf-8")) > BUILD_REVIEW_EDIT_BYTES
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
                elif action["action"] == "junie-answer":
                    if dashboard.answer_junie(action):
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
    """サーバーを開き、ブラウザーで移行の進捗と確認操作を公開する。"""
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
    html = html.replace('<html lang="ja">', f'<html lang="ja"{options}>')
    html += '<script src="dashboard.js" defer></script><script src="dashboard_editor.js" defer></script></body></html>'
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
                    _, _, failed, _ = load_saved_run(config, failed_run_id)
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
            # 実行とRunの保存はHTTPスレッドの外で行う。
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
                        _, _, result, _ = load_saved_run(config, action["run_id"])
                        view.observe(result)
                        if action["action"] == "resume-run":
                            result = execute(resume_with_junie, config, action["run_id"])
                        view.ready(config, result)
                    elif action["action"] in ("prepare", "run"):
                        if action["action"] == "prepare":
                            prepared = execute(prepare_run, config, action["unit_id"])
                            _, _, result, _ = load_saved_run(config, prepared["run_id"])
                        else:
                            result = execute(run_with_junie, config, action["unit_id"])
                        view.ready(config, result)
                    elif action["action"] == "accept":
                        review_checker(config, view.state["run_id"], action["finding_id"], action["reason"])
                        view.ready(config, result, "判断理由を記録しました。")
                    elif action["action"] == "build-response":
                        result = execute(resume_with_junie, config, view.state["run_id"], build_response=action)
                        view.ready(config, result)
                    else:
                        if "checker_instruction" in action:
                            result = execute(resume_with_junie, config, view.state["run_id"],
                                             checker_instruction=action["checker_instruction"])
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
