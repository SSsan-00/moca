"""Runの進捗・出力、復旧計画と実行操作。"""

from __future__ import annotations

import os
import json
import re
import shutil
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from ..errors import format_error
from ..files import safe_target
from ..junie.junie_thoughts import read_junie_thoughts
from ..runs.editor_queue import editor_text
from ..runs.recovery_status import recovery_status
from ..runs.run_records import load_dashboard_record, load_saved_run, validate_run_id
from ..runs.worktree_targets import EDIT_BYTES
from ..verification.diagnostic_log import review_log
from ..workflow import (
    BUILD_THROUGH_STAGE,
    build_failure_review,
    migration_ledger,
    plan_stopped_repair,
    plan_stage_resume,
    preview_stopped_repair,
    resolve_stopped_repair,
    script_recovery,
)
from . import DASHBOARD_ERRORS
from .dashboard_comparison import send_anchored_comparison


def check_run_model_revision(config, run_id, action):
    if "model_revision" not in action:
        return
    _, record = load_dashboard_record(config, run_id)
    if action["model_revision"] != record.get("junie_model_revision"):
        raise ValueError("モデル設定が更新されました。モデルと推論の強さを確認して、もう一度操作してください。")


def send_junie_thoughts(request) -> None:
    config = request.server.config
    manager = request.server.manager
    state = request.dashboard.state
    args = parse_qs(urlsplit(request.path).query)
    run_id = args.get("run_id", [None])[0]
    if not run_id or run_id != state.get("run_id"):
        request.send_error(404)
        return
    try:
        validate_run_id(run_id)
        cursor = int(args.get("cursor", ["0"])[0])
        if cursor < 0 or cursor > 2**63 - 1:
            raise ValueError("invalid log cursor")
    except ValueError:
        request.send_error(400)
        return
    try:
        settings = manager.config if manager else config
        run_root = Path(settings["run_root"]).resolve()
        batch = read_junie_thoughts(run_root, run_id, cursor)
    except (KeyError, OSError, ValueError):
        request.send_error(409)
        return
    live = bool(state.get("operation_id") and not state.get("ready") and not state.get("queued"))
    request.send_body(200, "application/json", json.dumps({
        **batch, "live": live,
        "log_language": settings.get("junie_log_language", "ja"),
        "log_tone": settings.get("junie_log_tone", "neutral"),
    }, ensure_ascii=False).encode("utf-8"))


def send_checker_output(request) -> None:
    config = request.server.config
    query = parse_qs(urlsplit(request.path).query)
    with request.dashboard.condition:
        state = request.dashboard.state
        current_config = config.copy()
    run_id = state.get("run_id")
    if (not run_id or query.get("run_id") != [run_id] or not state.get("ready")
            or state.get("status") not in {"checker_needs_review", "checker_failed"}):
        request.send_body(409, "text/plain; charset=utf-8", "現在の実行から確認画面を開き直してください。".encode())
        return
    try:
        run_path, _, record, _ = load_saved_run(current_config, run_id)
        if record["status"] not in {"checker_needs_review", "checker_failed"}:
            raise ValueError("チェッカーの結果が変わりました。確認画面を開き直してください。")
        if query.get("download") != ["1"]:
            output, truncated = review_log(run_path, "checker.log")
            body = json.dumps({"output": output, "truncated": truncated}, ensure_ascii=False).encode()
            request.send_body(200, "application/json; charset=utf-8", body)
            return
        path = safe_target(run_path, Path("checker.log"))
        if not path.is_file():
            raise ValueError("チェッカーの出力ファイルが見つかりません。")
        stream = path.open("rb")
    except DASHBOARD_ERRORS as exc:
        request.send_body(409, "text/plain; charset=utf-8", str(exc).encode("utf-8", errors="replace"))
        return
    with stream:
        request.connection.settimeout(5)
        request.send_response(200)
        request.send_header("Content-Type", "application/octet-stream")
        request.send_header("Content-Disposition", 'attachment; filename="checker.log"')
        request.send_header("Content-Length", str(os.fstat(stream.fileno()).st_size))
        request.send_header("Cache-Control", "no-store")
        request.send_header("X-Content-Type-Options", "nosniff")
        request.end_headers()
        try:
            shutil.copyfileobj(stream, request.wfile)
        except OSError:
            return


def send_build_review(request) -> None:
    config = request.server.config
    query = parse_qs(urlsplit(request.path).query)
    with request.dashboard.condition:
        state = request.dashboard.state
        if (not state.get("ready") or state.get("status") != "build_failed"
                or query.get("run_id") != [state.get("run_id")]):
            request.send_body(409, "text/plain; charset=utf-8", "現在の実行から確認画面を開き直してください。".encode())
            return
        current_config = config.copy()
    try:
        review = build_failure_review(current_config, state["run_id"], query.get("target", [None])[0])
    except DASHBOARD_ERRORS as exc:
        request.send_conflict(exc)
    else:
        request.send_body(200, "application/json; charset=utf-8", json.dumps(review, ensure_ascii=False).encode())


def send_workspace(request, *, include_usage: bool = False) -> None:
    config = request.server.config
    manager = request.server.manager
    if manager:
        try:
            body = json.dumps(manager.workspace(include_usage=include_usage), ensure_ascii=False).encode()
        except DASHBOARD_ERRORS as exc:
            message = "実行一覧を読み込めませんでした。設定と保存先を確認してください。\n" + format_error(exc)
            request.send_body(409, "text/plain; charset=utf-8", message.encode())
        else:
            request.send_body(200, "application/json", body)
        return
    try:
        with request.dashboard.condition:
            workspace_config = config.copy()
        units = [unit["id"] for unit in workspace_config.get("units", [])]
        records = Path(workspace_config["run_root"]).glob("*/run.json") if units else ()
        newest = sorted(records, key=lambda path: path.stat().st_mtime, reverse=True)
        runs = []
        for record_path in newest:
            try:
                _, _, record, _ = load_saved_run(workspace_config, record_path.parent.name)
            except DASHBOARD_ERRORS:
                continue
            keys = ("run_id", "unit_id", "created_at", "token_usage", "cost_exchange") if include_usage else ("run_id", "unit_id")
            runs.append({key: record[key] for key in keys if key in record})
            if not include_usage and len(runs) == 30:
                break
        body = json.dumps({"units": units, "runs": runs}).encode()
    except DASHBOARD_ERRORS as exc:
        message = "保存済みの実行を読み込めませんでした。保存先を確認して、実行タブを開き直してください。\n" + format_error(exc)
        request.send_body(409, "text/plain; charset=utf-8", message.encode())
        return
    request.send_body(200, "application/json", body)


def require_stopped_view(view, manager, run_id):
    validate_run_id(run_id)
    if (view.state.get("run_id") != run_id or not view.state.get("ready")
            or manager and (run_id in manager.jobs or run_id in manager.blocked_conversations)):
        raise ValueError("停止中の実行を選び、実行状況を確認してください。")


def send_recovery_plan(request):
    manager = request.server.manager
    config = request.server.config
    try:
        query = parse_qs(urlsplit(request.path).query)
        run_id, kind = query.get("run_id", [None])[0], query.get("kind", [None])[0]
        with request.dashboard.condition:
            require_stopped_view(request.dashboard, manager, run_id)
        if kind == "script":
            plan = script_recovery(config, run_id)
        elif kind == "draft":
            plan = plan_stopped_repair(config, run_id)
        else:
            raise ValueError("復旧方法を選択してください。")
        with request.dashboard.condition:
            require_stopped_view(request.dashboard, manager, run_id)
        request.send_body(200, "application/json", json.dumps(plan, ensure_ascii=False).encode("utf-8"))
    except DASHBOARD_ERRORS as exc:
        request.send_conflict(exc)


def save_recovery_plan(request, action):
    config = request.server.config
    manager = request.server.manager
    try:
        run_id, kind, version = action.get("run_id"), action.get("kind"), action.get("version")
        if not isinstance(version, str) or not re.fullmatch(r"[0-9a-f]{64}", version):
            raise ValueError("復旧計画を確認し直してください。")
        with request.dashboard.condition:
            require_stopped_view(request.dashboard, manager, run_id)
            if request.headers.get("X-Run-ID", run_id) != run_id:
                raise ValueError("表示中の実行が変わりました。")
        if kind == "draft" and set(action) == {"run_id", "kind", "version", "choices"}:
            choices = action["choices"]
            if not isinstance(choices, dict) or any(not isinstance(k, str) or v not in ("draft", "worktree")
                                                    for k, v in choices.items()):
                raise ValueError("復元する版を選択してください。")
            resolve_stopped_repair(config, run_id, version, choices)
            with request.dashboard.condition:
                request.dashboard.publish({**request.dashboard.state,
                                        "recovery_revision": request.dashboard.state.get("recovery_revision", 0) + 1})
            status = 200
        elif (kind == "script" and set(action) == {"run_id", "kind", "version", "confirmed"}
              and action["confirmed"] is True and manager):
            plan = script_recovery(config, run_id)
            if not plan["can_retry"] or plan["version"] != version:
                raise ValueError("復旧計画が更新されました。実行状況を確認し直してください。")
            with request.dashboard.condition:
                require_stopped_view(request.dashboard, manager, run_id)
                manager.submit({"action": "recover-script", "run_id": run_id, "retry_version": version,
                                "revision": request.dashboard.revision}, request.dashboard)
            status = 202
        else:
            raise ValueError("復旧方法と確認内容を選択してください。")
        request.send_body(status, "application/json", b"{}")
    except DASHBOARD_ERRORS as exc:
        request.send_conflict(exc)


def send_recovery_file(request):
    manager = request.server.manager
    config = request.server.config
    try:
        query = parse_qs(urlsplit(request.path).query)
        run_id, version, target = (query.get(key, [None])[0] for key in ("run_id", "version", "target"))
        with request.dashboard.condition:
            require_stopped_view(request.dashboard, manager, run_id)
        preview = preview_stopped_repair(config, run_id, version, target)
        with request.dashboard.condition:
            require_stopped_view(request.dashboard, manager, run_id)
        request.send_body(200, "application/json", json.dumps(preview, ensure_ascii=False).encode("utf-8"))
    except DASHBOARD_ERRORS as exc:
        request.send_conflict(exc)


def validate_control_action(config, manager, action: dict[str, Any]) -> None:
    """再開計画、判断理由、ビルド失敗時の入力などを検証する。"""
    if type(action.get("revision")) is not int:
        raise ValueError
    if "verification_request" in action:
        raise ValueError
    if "model_revision" in action and (
        action.get("action") not in {"resume", "resume-run", "build-response", "anchors"}
        or not isinstance(action.get("run_id"), str)
        or (action["model_revision"] is not None and (
            not isinstance(action["model_revision"], str)
            or not re.fullmatch(r"[0-9a-f]{24}", action["model_revision"])
        ))
    ):
        raise ValueError
    brave = action.get("junie_brave", False)
    if type(brave) is not bool or (brave and (
        not manager or action.get("action") not in {"run", "resume-run"}
        or (action["action"] == "resume-run" and action.get("start_stage") != "migration")
    )):
        raise ValueError
    if "start_stage" in action and (not manager or action.get("action") != "resume-run"
                                    or action["start_stage"] not in {"build", BUILD_THROUGH_STAGE, "migration", "checker"}):
        raise ValueError
    if any(key in action for key in ("through_capsule", "rewind_to")) and (
        action.get("start_stage") != "migration"
        or any(type(action[key]) is not int for key in ("through_capsule", "rewind_to") if key in action)
    ):
        raise ValueError
    if action.get("action") not in (
        "stop", "junie-answer", "accept", "resume", "anchors", "run", "open-run", "resume-run",
        "build-response", "cancel-queued", "hide-run",
    ):
        raise ValueError
    if action["action"] in ("run", "open-run", "resume-run"):
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


def post_control(request, action: dict[str, Any]) -> None:
    config = request.server.config
    manager = request.server.manager
    try:
        validate_control_action(config, manager, action)
    except (OSError, ValueError):
        request.send_error(400)
        return
    try:
        if "model_revision" in action:
            if action["run_id"] != request.dashboard.state.get("run_id"):
                raise ValueError("表示中の実行が変わりました。")
            check_run_model_revision(config, action["run_id"], action)
    except DASHBOARD_ERRORS as exc:
        request.send_conflict(exc)
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
            result = manager.submit(action, request.dashboard)
        except DASHBOARD_ERRORS as exc:
            request.send_conflict(exc)
        else:
            request.send_body(202, "application/json", json.dumps(result).encode())
        return
    if action["action"] == "stop":
        if request.dashboard.request_stop(action):
            request.send_body(202, "application/json", b"{}")
        else:
            request.send_error(409)
    elif action["action"] == "junie-answer":
        if request.dashboard.answer_junie(action):
            request.send_body(202, "application/json", b"{}")
        else:
            request.send_error(409)
    elif action["action"] == "anchors":
        send_anchored_comparison(request, action)
    elif request.dashboard.submit(action):
        request.send_body(202, "application/json", b"{}")
    else:
        request.send_error(409)


def send_recovery_status(request):
    config = request.server.config
    manager = request.server.manager
    try:
        run_id = parse_qs(urlsplit(request.path).query).get("run_id", [""])[0]
        validate_run_id(run_id)
        view = manager.get(run_id) if manager else request.dashboard
        if view.state.get("run_id") != run_id or not view.state.get("ready"):
            raise ValueError("停止・中断した実行を選び直してください。")
        blocked = bool(manager and run_id in manager.blocked_conversations)
        data = recovery_status(config, run_id, blocked=blocked)
        if not view.state.get("ready"):
            raise ValueError("実行状態が変わりました。")
        request.send_body(200, "application/json", json.dumps(data, ensure_ascii=False).encode("utf-8"))
    except DASHBOARD_ERRORS as exc:
        request.send_conflict(exc)


def send_resume_plan(request):
    config = request.server.config
    manager = request.server.manager
    try:
        query = parse_qs(urlsplit(request.path).query)
        run_id = query.get("run_id", [None])[0]
        if run_id:
            validate_run_id(run_id)
        view = manager.get(run_id) if manager and run_id else request.dashboard
        if not run_id or run_id != view.state.get("run_id") or not view.state.get("ready"):
            raise ValueError("処理が終わってから再開方法を選んでください。")
        options = {key: int(query[key][0]) for key in ("through_capsule", "rewind_to") if key in query}
        plan = plan_stage_resume(config, run_id, query.get("start_stage", ["build"])[0], **options)
        request.send_body(200, "application/json", json.dumps(plan, ensure_ascii=False).encode())
    except DASHBOARD_ERRORS as exc:
        request.send_conflict(exc)


def send_run_model(request):
    config = request.server.config
    try:
        run_id = request.dashboard.state.get("run_id")
        if not run_id:
            raise ValueError("実行を選択してください。")
        _, _, record, _ = load_saved_run(config, run_id)
        payload = {"run_id": run_id, "can_select": bool(request.dashboard.state.get("worktree_path")),
                   "model": record.get("junie_model"),
                   "effort": record.get("junie_effort"), "revision": record.get("junie_model_revision")}
        request.send_body(200, "application/json", json.dumps(payload).encode())
    except DASHBOARD_ERRORS as exc:
        request.send_conflict(exc)


def send_ledger(request):
    config = request.server.config
    run_id = request.dashboard.state.get("run_id")
    if run_id and request.dashboard.state.get("worktree_path"):
        try:
            ledger = migration_ledger(config, run_id)
        except DASHBOARD_ERRORS as exc:
            request.send_conflict(exc)
        else:
            request.send_body(200, "application/json", json.dumps(ledger, ensure_ascii=False).encode("utf-8"))
    else:
        request.send_error(404)
