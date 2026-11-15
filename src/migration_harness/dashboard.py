from __future__ import annotations

import hashlib
import json
import re
import secrets
import sys
import time
from collections import deque
from collections.abc import Mapping
from concurrent.futures import Future
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Empty, Queue
from subprocess import CalledProcessError
from tempfile import NamedTemporaryFile
from threading import Condition, Thread
from typing import Any

import tomlkit
from tomlkit.exceptions import TOMLKitError

from migration_harness.config import REFERENCE_ROLES, SCRIPT_PHASES, load_config
from migration_harness.diff_viewer import create_snapshot, review_html, snapshot_bytes
from migration_harness.review_anchors import generate_anchors
from migration_harness.workflow import (
    RunInterrupted, RunScriptError, _load_saved_run, can_resume_run, migration_ledger, prepare_run, resume_with_junie,
    review_checker, run_observer, run_with_junie,
)


PROGRESS_FIELDS = (
    "run_id", "unit_id", "target", "worktree_path", "status", "created_at", "current_capsule",
    "context_capsules", "completed_capsules", "context_part_chars", "max_context_chars",
    "build_exit_code", "build_log_path", "checker_exit_code", "checker_log_path", "error",
    "script_phase", "script_logs",
)


def _dashboard_asset(name: str) -> Path:
    path = Path(__file__).resolve().parents[2] / name
    return path if path.is_file() else Path(sys.prefix) / "share" / "php-to-csharp-harness" / name


def _read_settings(path: Path) -> tuple[bytes, str]:
    if not path.exists():
        return _dashboard_asset("harness.example.toml").read_bytes(), "missing"
    data = path.read_bytes()
    return data, hashlib.sha256(data).hexdigest()


_PROJECT_SETTINGS = ("root", "worktree_root", "run_root")
_UNIT_SETTINGS = ("id", "source", "targets")
_REFERENCE_TARGET_ROLES = ("reference_csharp", "reference_razor")


def _form_input(item: Any, index: int, *, common: bool = False) -> dict[str, Any]:
    if isinstance(item, Mapping) and isinstance(item.get("path"), str):
        role = "migration_rules" if common else item.get("role")
        if common or role in REFERENCE_ROLES:
            return {"index": index, "role": role, **{key: item[key] for key in ("path", "start_line", "end_line") if key in item}}
    return {"index": index}


def _edit_inputs(container: Mapping, key: str, edits: Any) -> None:
    """フォーム対応の入力だけを編集し、他の入力と既存テーブルの属性を保持する。"""
    inputs = container.get(key, [])
    common = key == "migration_rules"
    roles = ("migration_rules",) if common else REFERENCE_ROLES
    error = "移行ルール・参考実装の入力内容を確認してください。"
    if not isinstance(inputs, list) or not isinstance(edits, list):
        raise ValueError(error)
    retained = set()
    previous_index = -1
    ordered = []
    for edit in edits:
        if not isinstance(edit, dict):
            raise ValueError(error)
        index = edit.get("index")
        if index is not None:
            if type(index) is not int or not previous_index < index < len(inputs):
                raise ValueError(error)
            previous_index = index
            retained.add(index)
            item = _form_input(inputs[index], index, common=common)
            if "role" not in item:
                if set(edit) != {"index"}:
                    raise ValueError(error)
                ordered.append((index, None))
                continue
            if edit.get("role") != item["role"] and not (
                edit.get("role") in _REFERENCE_TARGET_ROLES and item["role"] in _REFERENCE_TARGET_ROLES
            ):
                raise ValueError(error)
        role, path = edit.get("role"), edit.get("path")
        if role not in roles or not isinstance(path, str):
            raise ValueError(error)
        ordered.append((index, (role, path)))

    for index in reversed(range(len(inputs))):
        if index not in retained:
            if "role" not in _form_input(inputs[index], index, common=common):
                raise ValueError(error)
            del inputs[index]
    if not ordered:
        container.pop(key, None)
        return
    inputs = container.setdefault(key, tomlkit.aot())
    for position, (index, values) in enumerate(ordered):
        if values is None:
            continue
        role, path = values
        if index is None:
            item = tomlkit.inline_table() if isinstance(inputs, tomlkit.items.Array) else tomlkit.table()
            if not common:
                item["role"] = role
            item["path"] = path
            inputs.insert(position, item)
        else:
            item = inputs[position]
            if item["path"] != path:
                item["path"] = path
            if not common and item["role"] != role:
                item["role"] = role


def _promote_legacy_materials(document: Any, units: list) -> bool:
    """旧設定の共通資料を移し、行範囲、文字コード、コメントを保持する。"""
    promoted = False
    for unit in units:
        inputs = unit.get("inputs")
        if not isinstance(inputs, list):
            continue
        legacy = []
        for index, item in enumerate(inputs):
            if not isinstance(item, Mapping):
                continue
            role = item.get("role")
            if role == "migration_rules":
                key = "migration_rules"
                item.pop("role")
            elif role in REFERENCE_ROLES and item.get("shared") is True:
                key = "references"
            else:
                continue
            legacy.append(index)
            item.pop("shared", None)
            project = document.setdefault("project", tomlkit.table())
            materials = project.setdefault(key, tomlkit.aot())
            if key == "migration_rules" and item in materials:
                continue
            if isinstance(materials, tomlkit.items.Array):
                destination = tomlkit.inline_table()
                destination.update(item.unwrap())
                item = destination
            elif isinstance(item, tomlkit.items.InlineTable):
                destination = tomlkit.table()
                destination.update(item)
                item = destination
            materials.append(item)
        for index in reversed(legacy):
            del inputs[index]
        if legacy and not inputs:
            unit.pop("inputs")
        promoted = promoted or bool(legacy)
    return promoted


def _settings_draft(content: str, changes: dict[str, Any] | None = None) -> dict[str, Any]:
    """フォームで指定したキーだけを更新し、残りのTOMLを保持する。"""
    try:
        document = tomlkit.parse(content)
        project = document.get("project", {})
        units = document.get("units", [])
        scripts = document.get("scripts", {})
        if (
            not isinstance(project, Mapping) or not isinstance(units, list)
            or any(not isinstance(unit, Mapping) for unit in units)
            or not isinstance(scripts, Mapping)
            or not isinstance(project.get("migration_rules", []), list)
            or not isinstance(project.get("references", []), list)
        ):
            raise ValueError("invalid settings form data")
    except (TOMLKitError, ValueError):
        if changes:
            raise ValueError("TOMLを直接編集し、書式を修正してください。") from None
        return {"content": content, "form": None}

    promoted = _promote_legacy_materials(document, units)
    if promoted:
        project = document["project"]
    if changes:
        project_changes = changes.get("project", {})
        unit_changes = changes.get("units", {})
        if not isinstance(project_changes, dict) or not isinstance(unit_changes, dict):
            raise ValueError("フォームの入力内容を確認してください。")
        if project_changes:
            project = document.setdefault("project", tomlkit.table())
            for key, value in project_changes.items():
                if key not in _PROJECT_SETTINGS or not isinstance(value, str):
                    raise ValueError("プロジェクト設定を確認してください。")
                project[key] = value
        for field, key in (("migrationRules", "migration_rules"), ("references", "references")):
            if field in changes:
                project = document.setdefault("project", tomlkit.table())
                _edit_inputs(project, key, changes[field])
        if unit_changes:
            units = document.setdefault("units", tomlkit.aot())
            for identifier, fields in unit_changes.items():
                if not identifier.isascii() or not identifier.isdecimal() or not isinstance(fields, dict):
                    raise ValueError("移行単位を選び直してください。")
                index = int(identifier)
                if not 0 <= index <= len(units):
                    raise ValueError("移行単位を選び直してください。")
                if index == len(units):
                    units.append(tomlkit.table())
                unit = units[index]
                for key, value in fields.items():
                    if key not in _UNIT_SETTINGS:
                        raise ValueError("移行単位の入力内容を確認してください。")
                    elif key == "targets":
                        if not isinstance(value, list) or any(not isinstance(path, str) for path in value):
                            raise ValueError("移行先は1行に1ファイル指定してください。")
                        if len(value) == 1 and "targets" not in unit:
                            unit["target"] = value[0]
                        else:
                            previous = unit.pop("target", None)
                            targets = tomlkit.item(value)
                            if previous is not None:
                                targets.trivia.comment = previous.trivia.comment
                                targets.trivia.comment_ws = previous.trivia.comment_ws
                            unit["targets"] = targets
                    elif not isinstance(value, str):
                        raise ValueError("移行単位の入力内容を確認してください。")
                    elif key == "source" and not value:
                        unit.pop(key, None)
                    else:
                        unit[key] = value
        if "removeEmptyUnits" in changes:
            indices = changes["removeEmptyUnits"]
            if not isinstance(indices, list) or any(
                type(index) is not int or not 0 <= index < len(units) for index in indices
            ):
                raise ValueError("空欄の移行単位を確認してください。")
            for index in sorted(set(indices), reverse=True):
                unit = units[index]
                if (
                    unit.keys() - {"id", "source", "target", "targets"}
                    or any(not isinstance(value, str) or value.strip()
                           for key, value in unit.items() if key != "targets")
                    or unit.get("targets", []) != []
                ):
                    raise ValueError("入力済みの移行単位は削除できません。")
                del units[index]
        if "scripts" in changes:
            edits = changes["scripts"]
            if not isinstance(edits, list):
                raise ValueError("スクリプトの工程とコマンドを確認してください。")
            if edits:
                scripts = document.setdefault("scripts", tomlkit.table())
            commands_by_phase = {phase: [] for phase in SCRIPT_PHASES}
            for entry in edits:
                if not isinstance(entry, dict):
                    raise ValueError("スクリプトの工程とコマンドを確認してください。")
                phase, command = entry.get("phase"), entry.get("command")
                if not isinstance(phase, str) or phase not in commands_by_phase or not isinstance(command, list) or any(
                    not isinstance(arg, str) for arg in command
                ):
                    raise ValueError("スクリプトの工程とコマンドを確認してください。")
                commands_by_phase[phase].append(command)
            for phase, commands in commands_by_phase.items():
                if commands and scripts.get(phase) != commands:
                    scripts[phase] = commands
                elif not commands:
                    scripts.pop(phase, None)
    if changes or promoted:
        content = tomlkit.dumps(document)

    return {"content": content, "form": {
        "project": {key: value for key in _PROJECT_SETTINGS if isinstance(value := project.get(key), str)},
        "migrationRules": [
            _form_input(item, index, common=True) for index, item in enumerate(project.get("migration_rules", []))
        ],
        "references": [_form_input(item, index) for index, item in enumerate(project.get("references", []))],
        "scripts": [
            {"phase": phase, "command": command}
            for phase in SCRIPT_PHASES if isinstance(commands := scripts.get(phase), list)
            for command in commands if isinstance(command, list) and all(isinstance(arg, str) for arg in command)
        ],
        "units": [{
            **{key: value for key in ("id", "source") if isinstance(value := unit.get(key), str)},
            "targets": [path for path in unit.get("targets", [unit.get("target")]) if isinstance(path, str)]
            if isinstance(unit.get("targets", []), list) else [],
        } for unit in units],
    }}


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
        state = {key: record[key] for key in PROGRESS_FIELDS if key in record}
        state["repair_count"] = len(record.get("repair_history", []))
        with self.condition:
            fields = (*PROGRESS_FIELDS, "repair_count")
            if state != {key: self.state[key] for key in fields if key in self.state}:
                self.publish(state)

    def ready(self, config: dict[str, Any], result: dict[str, Any], message: str = "") -> None:
        """処理が一区切りついた時点の操作可否とレビュー状態を公開する。"""
        state = {
            **self.state, "ready": True, "status": result["status"], "message": message,
            "resumable": can_resume_run(config, result),
            "anchor_reference": config.get("diff_viewer_anchor_reference") is not None,
        }
        if result["status"] == "checker_needs_review":
            try:
                state["review"] = review_checker(config, result["run_id"], None, None)
            except (OSError, ValueError, RuntimeError) as exc:
                state["review_error"] = str(exc)
        self.publish(state)

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
            self.actions.put_nowait(action)
            self.publish({
                **self.state, "ready": False, "message": "", "generating_anchors": generating_anchors,
            })
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
                elif self.path == prefix + "settings":
                    self.send_settings()
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

            def send_workspace(self) -> None:
                try:
                    with dashboard.condition:
                        units = [unit["id"] for unit in config.get("units", [])]
                        records = Path(config["run_root"]).glob("*/run.json") if units else ()
                        newest = sorted(records, key=lambda path: path.stat().st_mtime, reverse=True)
                        runs = []
                        for record_path in newest:
                            try:
                                _, _, record, _ = _load_saved_run(config, record_path.parent.name)
                            except (OSError, ValueError, TypeError):
                                continue
                            runs.append({key: record[key] for key in ("run_id", "unit_id")})
                            if len(runs) == 30:
                                break
                        body = json.dumps({"units": units, "runs": runs}).encode()
                except OSError:
                    message = "保存済みの実行を読み込めませんでした。保存先を確認して、実行タブを開き直してください。"
                    self.send_body(409, "text/plain; charset=utf-8", message.encode())
                    return
                self.send_body(200, "application/json", body)

            def send_settings(self) -> None:
                try:
                    path = Path(config["config_path"])
                    data, version = _read_settings(path)
                    body = json.dumps({"path": str(path), "version": version, **_settings_draft(data.decode("utf-8"))}).encode()
                except (KeyError, OSError, UnicodeError):
                    self.send_body(409, "text/plain; charset=utf-8", "設定を読み込めませんでした。ファイルの場所と文字コードを確認してください。".encode())
                else:
                    self.send_body(200, "application/json", body)

            def send_comparison(self) -> None:
                """保存済み比較をダウンロード用JSONか隔離されたViewerとして返す。"""
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
                    if action.get("action") not in ("accept", "resume", "anchors", "prepare", "run", "open-run", "resume-run"):
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
                if action["action"] == "anchors":
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
                        draft = _settings_draft(action["content"], action["changes"])
                    except (TOMLKitError, ValueError) as exc:
                        self.send_body(409, "text/plain; charset=utf-8", str(exc).encode())
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
                        config.update(updated)
                        dashboard.publish({"status": "idle", "ready": True, "settings_revision": dashboard.revision + 1})
                except (KeyError, OSError, ValueError) as exc:
                    message = f"設定を保存できませんでした。内容を確認してください。\n{exc}"
                    self.send_body(409, "text/plain; charset=utf-8", message.encode())
                else:
                    version = hashlib.sha256(action["content"].encode()).hexdigest()
                    self.send_body(200, "application/json", json.dumps({"version": version}).encode())

            def send_anchored_comparison(self, action: dict[str, Any]) -> None:
                """端末側でアンカー生成が終わるまで待ち、保存した比較のIDを返す。"""
                if config.get("diff_viewer_anchor_reference") is None:
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


def run_dashboard(config: dict[str, Any], *, unit_id: str | None = None, run_id: str | None = None) -> dict[str, Any] | None:
    """サーバーを開き、対話が必要な作業を起動元の端末で処理する。"""
    if unit_id is not None and run_id is not None:
        raise ValueError("choose a migration unit or a saved Run ID")
    package_dir = Path(__file__).resolve().parent
    demo = _dashboard_asset("progress-demo.html")
    html, marker, _ = demo.read_text(encoding="utf-8").partition("<!-- dashboard-script -->")
    if not marker:
        raise ValueError("progress dashboard template is missing its script marker")
    view = Dashboard()
    anchor_reference = config.get("diff_viewer_anchor_reference")
    options = " data-live"
    saved = None
    if run_id is None and unit_id is not None:
        view.publish({"status": "preparing", "unit_id": unit_id})
    elif run_id is not None:
        _, _, saved, _ = _load_saved_run(config, run_id)
        view.observe(saved)
        view.ready(config, saved)
        options += " data-saved"
    else:
        view.publish({"status": "idle", "ready": True})
        options += " data-workspace"
    if anchor_reference is not None:
        options += " data-anchor-reference"
    html = html.replace('<html lang="ja">', f'<html lang="ja"{options}>')
    html += '<script src="dashboard.js" defer></script></body></html>'
    with view.server(config, html.encode("utf-8"), (package_dir / "dashboard.js").read_bytes()) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        token = run_observer.set(view.observe)

        def show_script_failure(error: RunScriptError) -> dict[str, Any] | None:
            if error.run_id is None:
                view.publish({"status": "script_failed", "ready": True, "message": str(error)})
                return None
            _, _, failed, _ = _load_saved_run(config, error.run_id)
            view.observe(failed)
            view.ready(config, failed, str(error))
            return failed

        try:
            # Junieは起動元の端末を必要とするため、HTTPスレッドは操作をキューに渡す。
            if unit_id is not None:
                try:
                    result = run_with_junie(config, unit_id)
                except RunScriptError as exc:
                    result = show_script_failure(exc)
                else:
                    view.ready(config, result)
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
                        path, count = generate_anchors(
                            config, result["run_id"], config.get("diff_viewer_anchor_reference"), focus=action.get("focus"),
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
                    if action["action"] in ("open-run", "resume-run"):
                        _, _, result, _ = _load_saved_run(config, action["run_id"])
                        view.observe(result)
                        if action["action"] == "resume-run":
                            result = resume_with_junie(config, action["run_id"])
                        view.ready(config, result)
                    elif action["action"] in ("prepare", "run"):
                        view.publish({"status": "preparing", "unit_id": action["unit_id"]})
                        if action["action"] == "prepare":
                            prepared = prepare_run(config, action["unit_id"])
                            _, _, result, _ = _load_saved_run(config, prepared["run_id"])
                        else:
                            result = run_with_junie(config, action["unit_id"])
                        view.ready(config, result)
                    elif action["action"] == "accept":
                        review_checker(config, result["run_id"], action["finding_id"], action["reason"])
                        view.ready(config, result, "判断理由を記録しました。")
                    else:
                        result = resume_with_junie(config, result["run_id"])
                        view.ready(config, result)
                except RunInterrupted as exc:
                    _, _, result, _ = _load_saved_run(config, exc.run_id)
                    view.observe(result)
                    view.ready(config, result)
                except RunScriptError as exc:
                    result = show_script_failure(exc)
                except (OSError, ValueError, RuntimeError, CalledProcessError) as exc:
                    message = f"操作を完了できませんでした。設定や実行ログを確認してください。{exc}"
                    if result is not None and view.state.get("run_id") == result.get("run_id"):
                        view.ready(config, result, message)
                    else:
                        view.publish({"status": "idle", "ready": True, "message": message})
        finally:
            run_observer.reset(token)
            with view.condition:
                view.closed = True
                view.condition.notify_all()
            server.shutdown()
            thread.join()
