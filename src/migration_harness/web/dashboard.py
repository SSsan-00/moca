from __future__ import annotations

import copy
import json
import secrets
import time
import webbrowser
from collections import deque
from concurrent.futures import Future
from contextlib import suppress
from http.server import ThreadingHTTPServer
from itertools import count
from pathlib import Path
from queue import Empty, Queue
from threading import Condition, Thread
from typing import Any

from ..errors import format_error
from ..execution import ExecutionControl, control_scope
from ..files import harness_asset
from ..junie.junie_cli import dashboard_progress_style, unconfirmed_junie_stop
from ..junie.junie_conversation import (
    fail_pending_after_unconfirmed_stop,
    finish_instruction,
    run_instruction,
)
from ..junie.junie_notifications import (
    JunieNotifications,
    RunNotice,
    notification_settings,
    pending_requests,
    run_notice,
)
from ..junie.junie_thoughts import FILENAME as JUNIE_THOUGHTS_FILENAME
from ..rules.rule_workspace import RuleWorkspace
from ..runs.run_records import (
    edit_run_record,
    load_dashboard_record,
    load_saved_run,
    run_observer,
    validate_run_id,
)
from ..runs.timings import freeze_timings
from ..workflow import (
    RunScriptError,
    can_resume_build,
    can_resume_run,
    resume_with_junie,
    review_checker,
    run_with_junie,
    script_recovery,
)
from . import DASHBOARD_ERRORS
from .dashboard_http import DashboardServer
from .dashboard_runs import check_run_model_revision
from .review_anchors import generate_anchors


PROGRESS_FIELDS = (
    "run_id", "unit_id", "target", "worktree_path", "status", "created_at", "current_capsule", "review_kind",
    "context_capsules", "completed_capsules", "context_part_chars", "max_context_chars",
    "build_exit_code", "build_log_path", "build_attempt_seconds",
    "checker_exit_code", "checker_log_path", "checker_review_reason", "checker_advisory", "error",
    "script_phase", "script_logs", "script_advisories", "stage_timings", "active_stage", "timing_updated_at", "last_stop", "cost_exchange",
)


def _progress_fields(record):
    values = {key: record[key] for key in PROGRESS_FIELDS if key in record}
    diagnostic = record.get("roslyn_preflight")
    if (record.get("status") == "build_failed" and record.get("build_exit_code") is None
            and isinstance(diagnostic, dict) and diagnostic.get("status") in {"blocked", "cancelled"}):
        values["review_kind"] = "roslyn_preflight"
    return values


_junie_request_sequence = count(1)


class Dashboard:

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
        self.junie_request_sequence = 0
        self.trusted_junie_workspaces: set[str] = set()
        self.activity_sequence = 0
        self.notifications = None
        self.operation_started_at = None
        self.migration_milestone = False
        self.saw_migration = False

    def run_notifications(self):
        return self.notifications or getattr(getattr(self, "manager", None), "notifications", None)

    def begin_operation(self, operation_id: str | None = None) -> None:
        """停止要求は実行ごとのIDに結び付け、別のRunに持ち越さない。"""
        with self.condition:
            self.stop_phase = None
            self.operation_started_at = time.monotonic()
            self.migration_milestone = False
            self.saw_migration = False
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
        with self.condition:
            state = self.state.copy()
            state.pop("junie_request", None)
            self.junie_reply = None
            if request is not None and self.control and not self.control.cancelled.is_set():
                state["junie_request"] = {**request, "id": secrets.token_hex(16)}
                self.junie_reply = Future()
                self.junie_request_sequence = next(_junie_request_sequence)
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

    @staticmethod
    def list_record(record: dict[str, Any]) -> dict[str, Any]:
        fields = ("token_usage", "hidden_from_dashboard", "repair_pending",
                  "migration_stop_unconfirmed", "checker_required", "verification", "target_apply_pending", "script_inflight")
        result = _progress_fields(record) | {key: record[key] for key in fields if key in record}
        result["repair_history"] = range(len(record.get("repair_history", [])))
        return result

    def observe(self, record: dict[str, Any]) -> None:
        """Run記録から公開する項目を絞り、進捗に影響する変更だけを配信する。"""
        state = copy.deepcopy(_progress_fields(record))
        state["repair_count"] = len(record.get("repair_history", [])) + (
            record.get("status") == "repairing" and "repair_pending" in record
        )
        with self.condition:
            notices = self.run_notifications()
            if notices and self.operation_started_at is not None and self.control and not self.control.cancelled.is_set():
                if state.get("status") in {"junie_running", "junie_finished"}:
                    self.saw_migration = True
                threshold = notices.settings().get("milestone_after_seconds", 0)
                if (self.saw_migration and not self.migration_milestone and threshold
                        and state.get("status") in {"building", "checking"}):
                    self.migration_milestone = True
                    if time.monotonic() - self.operation_started_at >= threshold:
                        notices.post(RunNotice(state["run_id"], state["status"], "milestone"))
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

    @staticmethod
    def available_actions(config: dict[str, Any], record: dict[str, Any]) -> dict[str, bool]:
        return {
            "resumable": can_resume_run(config, record),
            "build_resumable": can_resume_build(record),
            "diff_viewer_enabled": bool(config.get("diff_viewer_html")),
        }

    def ready(self, config: dict[str, Any], result: dict[str, Any], message: str = "") -> None:
        stopped = result.get("last_stop") or {}
        if not message and stopped.get("saved_status") == result["status"]:
            message = stopped["message"]
        state = {
            **self.state, "ready": True, "status": result["status"], "message": message,
            "error_details": "",
            **self.available_actions(config, result),
            "cancellable": False, "stop_requested": False, "operation_id": None, "generating_anchors": False,
            "stage_timings": freeze_timings(self.state), "active_stage": None,
        }
        state.pop("review_error", None)
        if result["status"] == "checker_needs_review":
            try:
                state["review"] = review_checker(config, result["run_id"], None, None)
            except DASHBOARD_ERRORS as exc:
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
            if action["action"] in ("run", "resume", "resume-run", "anchors", "build-response"):
                self.begin_operation()
            self.actions.put_nowait(action)
            return True

    def server(
        self, config: dict[str, Any], html: bytes, script: bytes,
    ) -> ThreadingHTTPServer:
        """Runの画面、イベント、操作を同じローカルオリジンに公開する。"""
        # ポート番号だけで操作できないようにし、他サイトからのPOSTも拒否する。
        prefix = "/" + secrets.token_urlsafe(24) + "/"
        server = DashboardServer(("127.0.0.1", 0), self, config, html, script, prefix)
        host = f"127.0.0.1:{server.server_port}"
        server.dashboard_url = f"http://{host}{prefix}"
        manager = getattr(self, "manager", None)
        notifications = JunieNotifications(
            lambda: pending_requests(self, manager),
            lambda: config.get("junie_notifications") or notification_settings({}),
            f"http://{host}", prefix,
        )
        server.junie_notifications = self.notifications = notifications
        if manager:
            manager.notifications = notifications
        close = server.server_close

        def close_server():
            try:
                notifications.close()
            finally:
                close()

        server.server_close = close_server
        notifications.start()
        print(f"Progress dashboard: {server.dashboard_url}", flush=True)
        print("Press Ctrl+C in this terminal to close the dashboard.", flush=True)
        return server


def execute_dashboard_action(view, config, action):
    """1つのRunだけの状態・入力待ち・停止要求をワーカースレッドに結び付ける。"""
    token = run_observer.set(view.observe)
    result = None
    notice = None
    notices = view.run_notifications()
    run_id = action["run_id"]
    conversation_id = action.get("conversation_id")
    try:
        with control_scope(view.control):
            check_run_model_revision(config, run_id, action)
            if view.control is not None:
                view.control.junie_progress_style = dashboard_progress_style(config)
                view.control.junie_brave = action.get("junie_brave") is True and action["action"] in {"run", "resume-run"}
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
            elif kind == "run":
                result = run_with_junie(config, action["unit_id"], run_id=run_id)
            elif kind == "conversation":
                result = run_instruction(config, run_id, conversation_id)
                if result.pop("selection_review", False):
                    notices = None
                else:
                    finish_instruction(config, run_id, conversation_id, result)
            elif kind == "accept":
                review_checker(config, run_id, action["finding_id"], action["reason"])
                _, _, result, _ = load_saved_run(config, run_id)
            elif kind == "recover-script":
                script_recovery(config, run_id, retry_version=action["retry_version"])
                _, _, result, _ = load_saved_run(config, run_id)
            elif kind in {"resume", "resume-run", "build-response"}:
                kwargs = {}
                if kind == "build-response":
                    kwargs["build_response"] = action
                if "checker_instruction" in action:
                    kwargs["checker_instruction"] = action["checker_instruction"]
                if "verification_request" in action:
                    kwargs["verification_request"] = action["verification_request"]
                result = resume_with_junie(config, run_id, **kwargs)
            else:
                raise ValueError("この操作は利用できません。")
            view.observe(result)
            view.ready(config, result, "判断理由を記録しました。" if kind == "accept" else "")
            if notices and kind != "anchors":
                saved_record = {}
                if result["status"] in {"build_succeeded", "checker_succeeded", "checker_accepted", "editor_checkpoint"}:
                    with suppress(*DASHBOARD_ERRORS):
                        _, saved_record = load_dashboard_record(config, run_id)
                notice = run_notice(run_id, kind, {**view.state, **saved_record, **result})
    except (*DASHBOARD_ERRORS, KeyboardInterrupt) as exc:
        if conversation_id:
            try:
                finish_instruction(config, run_id, conversation_id, None, exc)
            except DASHBOARD_ERRORS as save_error:
                exc.add_note(f"Junie の履歴を保存できませんでした。{format_error(save_error)}")
        unconfirmed_stop = unconfirmed_junie_stop(exc)
        pending_save_error = None
        if unconfirmed_stop is not None:
            if manager := getattr(view, "manager", None):
                manager.block_conversations(run_id, str(unconfirmed_stop))
            try:
                fail_pending_after_unconfirmed_stop(config, run_id, str(unconfirmed_stop))
            except DASHBOARD_ERRORS as save_error:
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
                except DASHBOARD_ERRORS as save_error:
                    pending_save_error = format_error(save_error)
            view.observe(result)
            view.ready(config, result)
        except DASHBOARD_ERRORS as load_error:
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
            notice = run_notice(run_id, action["action"], view.state, failed=True)
            if pending_save_error:
                view.publish({**view.state, "message": view.state["message"] +
                              "\n保留中の指示の停止を保存できません。残存プロセスと保存先を確認してください。\n" +
                              pending_save_error})
        if action.get("reply") and not action["reply"].done():
            action["reply"].set_exception(ValueError("処理を停止しました。" if stopped else format_error(exc)))
    finally:
        run_observer.reset(token)
        if notices:
            notices.post(notice)
    return result


def render_dashboard_html(options: str) -> bytes:
    demo = harness_asset("progress-demo.html")
    html, marker, _ = demo.read_text(encoding="utf-8").partition("<!-- dashboard-script -->")
    if not marker:
        raise ValueError("progress dashboard template is missing its script marker")
    for asset in ("dashboard_runs.js", "dashboard_settings.js", "dashboard_log_presentation.js", "dashboard_chat.js"):
        html = html.replace(f"./src/migration_harness/web/{asset}", asset)
    html = html.replace('<html lang="ja">', f'<html lang="ja"{options}>')
    html += '<script src="dashboard.js" defer></script><script src="dashboard_editor.js" defer></script><script src="dashboard_rule_workspace.js" defer></script></body></html>'
    return html.encode("utf-8")


def run_dashboard(
    config: dict[str, Any], *, run_id: str | None = None,
    config_error: str | None = None, open_browser: bool = False,
) -> dict[str, Any] | None:
    package_dir = Path(__file__).resolve().parent
    view = Dashboard()
    view.rule_workspace = RuleWorkspace(config)
    options = " data-live"
    saved = None
    if config_error is not None:
        view.publish({
            "status": "idle", "ready": True, "error_details": config_error,
            "message": "設定を読み込めませんでした。設定タブで確認して保存してください。\n" + config_error,
        })
        options += " data-workspace"
    elif run_id is not None:
        try:
            _, _, saved, _ = load_saved_run(config, run_id)
            view.observe(saved)
            view.ready(config, saved)
        except DASHBOARD_ERRORS as exc:
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
    from ..runs.run_manager import RunManager

    manager = RunManager(config, view)
    view.manager = manager
    try:
        with view.server(config, html, (package_dir / "dashboard.js").read_bytes()) as server:
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                if open_browser:
                    try:
                        opened = webbrowser.open(server.dashboard_url, new=2)
                    except (OSError, webbrowser.Error):
                        opened = False
                    if not opened:
                        print("ブラウザーを開けませんでした。上の URL を開いてください。", flush=True)
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
                try:
                    view.rule_workspace.close()
                finally:
                    manager.close()
                    server.shutdown()
                    thread.join()
    finally:
        manager.close()
    return manager.results.get(run_id, saved)
