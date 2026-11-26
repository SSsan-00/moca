"""設定・ルールの版確認と保存、参照資料プレビュー、ドライラン。"""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import nullcontext, suppress
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from ..config import find_unit, load_config, target_paths
from ..context.source_context import preview_target_context
from ..errors import format_error
from ..execution import ExecutionControl, OperationCancelled, control_scope
from ..files import harness_asset
from ..junie.junie_cli import list_models
from . import DASHBOARD_ERRORS
from .settings_form import build_settings_draft


def read_settings(path: Path) -> tuple[bytes, str]:
    if not path.exists():
        return harness_asset("harness.example.toml").read_bytes(), "missing"
    data = path.read_bytes()
    return data, hashlib.sha256(data).hexdigest()


def save_settings_file(path: Path, content: str, version: str, *, manager=None, verify_origins=None) -> dict[str, Any]:
    # 設定ファイルと同じディレクトリで検証し、相対パスの基準を保つ。
    with NamedTemporaryFile(mode="w", dir=path.parent, encoding="utf-8", newline="", delete=False) as stream:
        candidate = Path(stream.name)
    try:
        candidate.write_text(content, encoding="utf-8", newline="")
        normalized = load_config(candidate)
        normalized["config_path"] = str(path)
        with manager.settings_switch(normalized) if manager else nullcontext():
            if verify_origins:
                verify_origins(Path(normalized["project_root"]))
            if read_settings(path)[1] != version:
                raise ValueError("設定ファイルが更新されています。最新の設定を読み込み、変更を反映し直してください。")
            candidate.replace(path)
        return normalized
    finally:
        candidate.unlink(missing_ok=True)


def send_rule_workspace(request, *, action=None, query=None):
    config = request.server.config
    manager = request.server.manager
    dashboard = request.server.root_view
    rules = request.server.rules

    def apply_config(content, version, verify_origins):
        condition = manager.condition if manager else dashboard.condition
        with condition:
            if manager and manager.busy:
                raise ValueError("実行中の処理が終わってから反映してください。")
            path = Path(config["config_path"])
            updated = save_settings_file(path, content, version, manager=manager, verify_origins=verify_origins)
            config.update(updated)
            if manager:
                manager.start_pending()
        with dashboard.condition:
            dashboard.publish({**dashboard.state, "settings_revision": dashboard.revision + 1})

    try:
        if action is not None:
            value = rules.action(action, apply_config)
        else:
            identifier = query.get("id", [None])[0]
            source = query.get("source", [None])[0]
            if source is not None:
                value = rules.file_text(identifier, source)
            elif query.get("registered") == ["1"]:
                value = {"registered": rules.first_registered_source()}
            elif query.get("archive") == ["1"]:
                with rules.lock:
                    value = {"archives": rules._archives()}
            else:
                value = rules.snapshot(identifier)
    except DASHBOARD_ERRORS as exc:
        status = 500 if isinstance(exc, OSError) and action and action.get("action") == "edit" else 409
        request.send_body(status, "text/plain; charset=utf-8", format_error(exc).encode())
    else:
        request.send_body(200, "application/json", json.dumps(value, ensure_ascii=False).encode())


def send_settings(request) -> None:
    config = request.server.config
    try:
        path = Path(config["config_path"])
        data, version = read_settings(path)
        body = json.dumps({"path": str(path), "version": version, **build_settings_draft(data.decode("utf-8"))}).encode()
    except DASHBOARD_ERRORS as exc:
        message = "設定を読み込めませんでした。ファイルの場所と文字コードを確認してください。\n" + format_error(exc)
        request.send_body(409, "text/plain; charset=utf-8", message.encode())
    else:
        request.send_body(200, "application/json", body)


def save_settings(request, action: dict[str, Any]) -> None:
    config = request.server.config
    manager = request.server.manager
    if action.get("mode") in {"reference-preview", "reference-cancel"}:
        reference_preview(request, action)
        return
    if action.get("mode") == "preview":
        if not isinstance(action.get("content"), str) or not isinstance(action.get("changes"), dict):
            request.send_error(400)
            return
        try:
            draft = build_settings_draft(action["content"], action["changes"])
        except DASHBOARD_ERRORS as exc:
            request.send_conflict(exc)
        else:
            request.send_body(200, "application/json", json.dumps(draft).encode())
        return
    if not all(isinstance(action.get(key), str) for key in ("content", "version")) or type(action.get("revision")) is not int:
        request.send_error(400)
        return
    view = request.dashboard
    try:
        with view.condition:
            if (manager.busy if manager else not view.state.get("ready") or action["revision"] != view.revision):
                raise ValueError("実行中の処理が終わってから設定を保存してください。")
            path = Path(config["config_path"])
            updated = save_settings_file(path, action["content"], action["version"],
                                     manager=manager)
            config.pop("checker_from_cli", None)
            config.pop("toml_checker_command", None)
            config.update(updated)
            if manager:
                manager.start_pending()
            view.publish({**view.state, "ready": True, "settings_revision": view.revision + 1,
                                    "diff_viewer_enabled": bool(config.get("diff_viewer_html"))})
    except DASHBOARD_ERRORS as exc:
        message = f"設定を保存できませんでした。内容を確認してください。\n{format_error(exc)}"
        request.send_body(409, "text/plain; charset=utf-8", message.encode())
    else:
        version = hashlib.sha256(action["content"].encode()).hexdigest()
        request.send_body(200, "application/json", json.dumps({"version": version}).encode())


def reference_preview(request, action):
    """未保存のTOMLから参照候補だけを走査し、同じ画面へ進捗を返す。"""
    config = request.server.config
    preview_lock = request.server.preview_lock
    reference_previews = request.server.reference_previews
    request_id = action.get("request_id")
    if not isinstance(request_id, str) or not re.fullmatch(r"[0-9a-f-]{36}", request_id):
        request.send_error(400)
        return
    if action["mode"] == "reference-cancel":
        with preview_lock:
            if control := reference_previews.get(request_id):
                control.cancelled.set()
        request.send_body(200, "application/json", b"{}")
        return
    if not all(isinstance(action.get(key), str) for key in ("content", "unit")):
        request.send_error(400)
        return

    def send_event(data):
        """参照候補の進捗を改行区切りで即時に送る。"""
        request.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8") + b"\n")
        request.wfile.flush()

    control = ExecutionControl(lambda event: send_event({"progress": event["message"]}))
    with preview_lock:
        if reference_previews:
            request.send_body(409, "text/plain; charset=utf-8", "別の参照資料を確認中です。".encode())
            return
        reference_previews[request_id] = control
    try:
        request.send_response(200)
        request.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        request.send_header("Cache-Control", "no-store")
        request.send_header("Connection", "close")
        request.end_headers()
        request.close_connection = True
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
    except DASHBOARD_ERRORS as exc:
        with suppress(OSError):
            send_event({"error": format_error(exc)})
    finally:
        with preview_lock:
            reference_previews.pop(request_id, None)


def send_dry_run(request, action):
    config = request.server.config
    preview_lock = request.server.preview_lock
    dry_run_controls = request.server.dry_run_controls
    from migration_harness.runs.dry_run import plan_dry_run

    request_id = action.get("request_id")
    if not isinstance(request_id, str) or not re.fullmatch(r"[0-9a-f-]{36}", request_id):
        request.send_error(400)
        return
    if action["action"] == "dry-run-cancel":
        with preview_lock:
            if control := dry_run_controls.get(request_id):
                control.cancelled.set()
        request.send_body(200, "application/json", b"{}")
        return

    unit = action.get("unit_id")
    if not isinstance(unit, str) or not unit:
        request.send_error(400)
        return
    control = ExecutionControl(lambda _: None)
    with preview_lock:
        occupied = bool(dry_run_controls)
        if not occupied:
            dry_run_controls[request_id] = control
    if occupied:
        request.send_body(409, "text/plain; charset=utf-8", "別の設定を検証しています。".encode())
        return

    try:
        with control_scope(control):
            report = plan_dry_run(load_config(Path(config["config_path"])), unit)
        request.send_body(200, "application/json", json.dumps(report, ensure_ascii=False).encode())
    except OperationCancelled:
        request.send_body(200, "application/json", b'{"cancelled": true}')
    except DASHBOARD_ERRORS as exc:
        request.send_conflict(exc)
    finally:
        with preview_lock:
            dry_run_controls.pop(request_id, None)


def send_models(request):
    try:
        body = json.dumps({"models": list_models()}).encode()
    except DASHBOARD_ERRORS as exc:
        request.send_conflict(exc)
    else:
        request.send_body(200, "application/json", body)
