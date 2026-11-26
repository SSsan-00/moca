"""Runに固定された比較スナップショットの作成と配信。"""

from __future__ import annotations

import json
import re
from concurrent.futures import Future
from itertools import count
from typing import Any

from ..errors import format_error
from . import DASHBOARD_ERRORS
from .diff_viewer import create_snapshot, review_html, snapshot_bytes


def send_comparison(request) -> None:
    """保存済み比較をダウンロード用JSONか隔離されたViewerとして返す。"""
    config = request.server.config
    prefix = request.server.prefix
    match = re.fullmatch(r"([^/]+)/([a-f0-9]{32})\.(html|json)", request.path.removeprefix(prefix + "review/"))
    with request.dashboard.condition:
        comparison_config = config.copy()
    if not match or not comparison_config.get("run_root"):
        request.send_error(404)
        return
    run_id, snapshot_id, extension = match.groups()
    try:
        workspace = snapshot_bytes(comparison_config, run_id, snapshot_id)
        if extension == "json":
            request.send_body(200, "application/json", workspace, {
                "Content-Disposition": f'attachment; filename="review-{snapshot_id}.json"',
            })
        else:
            # Viewerにはスナップショットだけを渡し、進捗画面や制御APIから隔離する。
            request.send_body(200, "text/html; charset=utf-8", review_html(comparison_config, workspace), {
                "X-Frame-Options": "SAMEORIGIN",
                "Content-Security-Policy": (
                    "sandbox allow-scripts allow-downloads; default-src 'none'; "
                    "script-src 'unsafe-inline' blob:; style-src 'unsafe-inline' blob:; "
                    "img-src data: blob:; font-src data: blob:; worker-src blob:; "
                    "base-uri 'none'; frame-ancestors 'self'"
                ),
            })
    except DASHBOARD_ERRORS as exc:
        headers = {"X-Frame-Options": "SAMEORIGIN"} if extension == "html" else None
        request.send_body(409, "text/plain; charset=utf-8", f"比較ツールを開けませんでした。\n{format_error(exc)}".encode("utf-8"), headers)


def create_comparison(request, action: dict[str, Any]) -> None:
    config = request.server.config
    with request.dashboard.condition:
        comparison_config = config.copy()
        state = request.dashboard.state
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
        request.send_body(201, "application/json", response)
    except DASHBOARD_ERRORS as exc:
        request.send_conflict(exc)


def send_anchored_comparison(request, action: dict[str, Any]) -> None:
    config = request.server.config
    manager = request.server.manager
    if not config.get("diff_viewer_html"):
        request.send_body(409, "text/plain; charset=utf-8", "設定の差分ビューアーのHTMLを指定してください。".encode())
        return
    # ブラウザーを閉じても端末側は結果を保存できる。応答は要求元だけに返す。
    reply: Future[tuple[str, str, int]] = Future()
    try:
        job = {**action, "reply": reply}
        if manager:
            job["run_id"] = request.dashboard.state.get("run_id")
            accepted = manager.submit(job, request.dashboard)
        else:
            accepted = request.dashboard.submit(job)
    except DASHBOARD_ERRORS as exc:
        request.send_conflict(exc)
        return
    if not accepted:
        request.send_body(409, "text/plain; charset=utf-8", "実行中の処理が終わってから、もう一度お試しください。".encode())
        return
    try:
        run_id, snapshot_id, count = reply.result()
    except DASHBOARD_ERRORS as exc:
        request.send_conflict(exc)
        return
    request.send_body(201, "application/json", json.dumps({
        "run_id": run_id, "id": snapshot_id, "count": count,
    }).encode())
