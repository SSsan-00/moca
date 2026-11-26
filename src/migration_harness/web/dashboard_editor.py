"""実行に固定した編集対象、指示と診断のHTTP操作。"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from ..config import target_paths, validate_junie_model
from ..context.capsules import (
    assigned_php_sources,
    load_capsule_manifest,
    read_pinned_php,
)
from ..errors import format_error
from ..junie.junie_conversation import invalidate_queued_steering
from ..runs.editor_queue import (
    AGENT_PHASES,
    EDITOR_TARGET_SUFFIXES,
    QUEUE_PHASES,
    add_prompt,
    applied_editor_save_id,
    check_editor_adoption,
    editor_queue,
    editor_save_intent,
    editor_save_pending,
    editor_target,
    editor_text,
    record_operator_edit,
    record_queued_editor_save,
    replayed_editor_save,
    stage_editor_save,
    valid_save_id,
)
from ..runs.editor_view import editor_version, editor_view_versions, prompt_timing, update_history_delta
from ..runs.run_records import edit_run_record, load_saved_run, validate_run_id
from ..runs.worktree_targets import save_target_edit, worktree_source_lock
from . import DASHBOARD_ERRORS


def send_prompts(request, action: dict[str, Any] | None = None) -> None:
    config = request.server.config
    manager = request.server.manager
    state = request.dashboard.state
    run_id = state.get("run_id")
    payload = {"run_id": run_id, "timing": None, "prompts": []}
    try:
        if action is not None and action.get("run_id") != run_id:
            raise ValueError("表示中の実行が変わりました。")
        if run_id and state.get("worktree_path"):
            run_path, _, _, _ = load_saved_run(config, run_id)
            with editor_queue(run_path) as queue:
                with request.dashboard.condition:
                    state = request.dashboard.state
                    if state.get("run_id") != run_id:
                        raise ValueError("表示中の実行が変わりました。")
                    _, _, record, _ = load_saved_run(config, run_id)
                    timing = prompt_timing(config, state, run_path, record, pending_edits=bool(queue["edits"]),
                                            live=bool(request.dashboard.control and request.dashboard.control.junie_steering
                                                      and not queue["edits"]))
                    if action is not None:
                        if timing is None:
                            raise ValueError("現在の工程では指示を登録できません。")
                        add_prompt(queue, action)
                payload.update(timing=timing, prompts=queue["prompts"])
                if manager:
                    history = manager.conversations.preparation_replies(run_id, state) + queue.get("conversation", [])
                    payload["conversation"] = sorted(history, key=lambda entry: entry.get("received_at", ""))
        elif action is not None:
            raise ValueError("移行の実行を開いてから指示を登録してください。")
        request.send_body(200, "application/json", json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    except DASHBOARD_ERRORS as exc:
        request.send_body(409, "text/plain; charset=utf-8", str(exc).encode("utf-8", errors="replace"))


def send_editor_php(request) -> None:
    """Runに固定されたPHP入力を読み取り専用で返す。"""
    config = request.server.config
    query = parse_qs(urlsplit(request.path).query)
    run_id = request.dashboard.state.get("run_id")
    try:
        if not run_id or query.get("run_id", [None])[0] != run_id:
            raise ValueError("表示中の実行が変わりました。")
        run_path, _, _, _ = load_saved_run(config, run_id)
        capsule, manifest = load_capsule_manifest(run_path)
        sources = {Path(name).as_posix(): entry for name, entry in assigned_php_sources(manifest).items()}
        names = list(sources)
        name = query.get("source", names[:1] or [None])[0]
        if name is not None and name not in names:
            raise ValueError("この実行の移行元 PHP を選んでください。")
        content = ""
        error = None
        if name is not None:
            entry = sources[name]
            content = read_pinned_php(capsule, entry).decode(entry["encoding"])
        payload = {"run_id": run_id, "sources": names, "source": name, "content": content, "error": None}
        request.send_body(200, "application/json", json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    except DASHBOARD_ERRORS as exc:
        request.send_conflict(exc)


def send_editor(request, action: dict[str, Any] | None = None) -> None:
    """Runと編集版を照合し、キューロック内で取得または保存する。"""
    manager = request.server.manager
    query = parse_qs(urlsplit(request.path).query)
    state = request.dashboard.state
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
            payload = editor_payload(request.dashboard, request.server.config, manager, query, state, run_id, action)
            if payload is None:
                request.send_body(200, "application/json", b'{"unchanged":true}')
                return
        request.send_body(200, "application/json", json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    except DASHBOARD_ERRORS as exc:
        request.send_conflict(exc)


@dataclass(frozen=True)
class EditorSave:
    base: str
    draft: str
    save_id: str
    edit_id: str | None
    resolve: bool
    intent: dict

    @classmethod
    def from_action(cls, name, action):
        base = editor_text(action.get("base"))
        draft = editor_text(action.get("content"))
        save_id = action.get("save_id")
        if not valid_save_id(save_id):
            raise ValueError("保存IDが無効です。エディタを開き直してください。")
        edit_id, resolve = action.get("edit_id"), action.get("resolve", False)
        if type(resolve) is not bool:
            raise ValueError("編集の競合を解消する指定が無効です。")
        intent = editor_save_intent(name, base, draft, edit_id, resolve)
        adoption = action.get("adoption")
        base_version = None
        if "base_version" in action:
            base_version = action["base_version"]
            if (not isinstance(base_version, str) or not re.fullmatch(r"[0-9a-f]{64}", base_version)
                    or edit_id is not None or resolve):
                raise ValueError("下書きの元の版を確認できません。")
            intent["base_version"] = base_version
        if "adoption" in action:
            if (not isinstance(adoption, dict) or adoption.keys() != {"save_id", "edit_id", "version"}
                    or any(not isinstance(adoption[key], str) or not re.fullmatch(pattern, adoption[key])
                           for key, pattern in (("save_id", r"[0-9a-f]{32}"), ("edit_id", r"[0-9a-f]{24}"),
                                                ("version", r"[0-9a-f]{64}")))
                    or base_version is not None or edit_id is not None or resolve):
                raise ValueError("編集中の下書きの採用版を確認できません。")
            intent["adoption"] = adoption
        return cls(base, draft, save_id, edit_id, resolve, intent)


def editor_payload(view, config, manager, query, state, run_id, action):
    """取得または版を照合して保存し、キューの更新を完了してから内容を返す。"""
    if action is not None and action.get("run_id") != run_id:
        raise ValueError("表示中の実行が変わりました。")
    save = (EditorSave.from_action(action.get("target"), action)
            if action is not None and action.get("kind") == "save" else None)
    run_path, worktree, record, unit = load_saved_run(config, run_id)
    versions = None
    if action is None:
        versions = editor_view_versions(view, query, state, run_id, run_path, worktree, record, unit)
        view_version, content_version, _, _ = versions
        if content_version and query.get("view_version") == [view_version] and not editor_save_pending(run_path):
            return None
    with editor_queue(run_path, recover=(worktree, target_paths(unit))) as queue:
        state = view.state
        if state.get("run_id") != run_id:
            raise ValueError("表示中の実行が変わりました。")
        _, worktree, record, unit = load_saved_run(config, run_id)
        targets = tuple(path for path in target_paths(unit) if path.suffix.lower() in EDITOR_TARGET_SUFFIXES)
        names = [path.as_posix() for path in targets]
        name = query.get("target", names[:1] or [None])[0]
        if save is not None:
            name = action.get("target")
            if name not in names:
                raise ValueError("設定された移行先ファイルを選んでください。")
        elif name is not None and name not in names:
            raise ValueError("設定された移行先ファイルを選んでください。")
        phase = record["status"]
        agent_root = run_path / "agent-workspace"
        if agent_root.is_symlink():
            raise ValueError("Junie の作業場所がシンボリックリンクです。")
        references = record.get("reference_refresh", {})
        resolving_references = bool(references.get("conflict"))
        from_agent = not resolving_references and phase in AGENT_PHASES and agent_root.is_dir()
        root = agent_root if from_agent else worktree
        busy_other = state.get("generating_anchors") or state.get("stop_requested")
        queued = bool(resolving_references or references.get("adoption") or queue["edits"] or not state.get("ready")
                      or busy_other or phase in AGENT_PHASES)
        timing = prompt_timing(config, state, run_path, record, pending_edits=bool(queue["edits"]),
                               live=bool(view.control and view.control.junie_steering and not queue["edits"]))
        read_error = None
        try:
            original, content = editor_target(root, Path(name)) if name else (None, "")
        except (OSError, ValueError) as exc:
            if save is not None:
                raise
            original, content = None, ""
            read_error = format_error(exc)
        edit = queue["edits"].get(name)
        if action is not None:
            if action.get("kind") == "model":
                if not isinstance(action.get("model"), str) or not isinstance(action.get("effort"), str):
                    raise ValueError("モデルと推論の強さを選択してください。")
                model, effort = validate_junie_model(action.get("model"), action.get("effort"))
                with edit_run_record(run_path / "run.json") as (saved, changes):
                    if action.get("model_revision") != saved.get("junie_model_revision"):
                        raise ValueError("モデル設定が別の画面で変更されました。画面を更新してください。")
                    changes.update(junie_model=model, junie_effort=effort, junie_model_revision=secrets.token_hex(12))
                    record.update(changes)
            elif action.get("kind") == "prompt" and timing:
                add_prompt(queue, action)
            elif save is not None and name:
                base_version = save.intent.get("base_version")
                adoption = save.intent.get("adoption")
                # キュー、対象ファイル、Runの順序でロックし、保存直前に再照合する。
                with worktree_source_lock(worktree), view.condition:
                    state = view.state
                    if state.get("run_id") != run_id:
                        raise ValueError("表示中の実行が変わりました。")
                    if base_version is not None and not from_agent:
                        original, content = editor_target(worktree, Path(name))
                    if adoption is not None:
                        original, content = editor_target(root, Path(name))
                    replay = replayed_editor_save(
                        queue, edit, original, content, save.save_id, save.intent,
                    )
                    if not replay:
                        if save.edit_id != (edit["id"] if edit else None):
                            raise ValueError("保存済みの編集が別の画面で変更されました。")
                        if adoption is not None:
                            check_editor_adoption(queue, name, original, save.intent["base"], adoption)
                        # 保存直前に実行が始まった場合も、編集をRunへ退避する。
                        busy_other = state.get("generating_anchors") or state.get("stop_requested")
                        queued = bool(queued or not state.get("ready") or busy_other or content != save.base)
                        if base_version is not None and (
                            queued or from_agent or original is None or hashlib.sha256(original).hexdigest() != base_version
                        ):
                            raise ValueError("対象ファイルが更新されたか、編集中のため保存できません。下書きを残しました。")
                        queue["edit_revision"] = secrets.token_hex(12)
                        invalidate_queued_steering(queue)
                        if queued:
                            if edit and edit["status"] == "conflict" and (
                                not save.resolve or save.base != content
                            ):
                                raise ValueError("最新版との差分を確認し、競合を解消して保存してください。")
                            edit = {"id": secrets.token_hex(12), "base": save.base, "content": save.draft, "status": "queued"}
                            record_queued_editor_save(queue, edit, save.save_id, save.intent)
                            queue["edits"][name] = edit
                            timing = prompt_timing(config, state, run_path, record, pending_edits=True)
                        else:
                            record_operator_edit(queue, name, content, save.draft)
                            version = stage_editor_save(run_path, queue, original, save.draft, save.save_id, save.intent)
                            save_target_edit(worktree, targets, {"target": name, "version": version, "content": save.draft})
                            original, content = editor_target(worktree, Path(name))
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
            "can_prompt": timing is not None, "prompt_timing": timing,
            "prompts": queue["prompts"], "capsule": record.get("current_capsule"),
            "junie_model": record.get("junie_model"),
            "junie_effort": record.get("junie_effort"), "junie_model_revision": record.get("junie_model_revision"),
            "status": phase, "operator_question": record.get("operator_question"),
        }
        if name and edit is None and read_error is None:
            if accepted := applied_editor_save_id(queue, name, original, content):
                payload["accepted_save_id"] = accepted
                receipt = queue["save_requests"][accepted]
                if after := receipt.get("after"):
                    payload["accepted_version"] = after
                    if edit_id := receipt.get("queued_edit"):
                        payload["accepted_edit_id"] = edit_id
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
            edit_version = editor_version(run_id, name, edit)
            payload["edit_version"] = edit_version
            if content_version and query.get("content_version") == [content_version]:
                del payload["content"]
            if query.get("edit_version") == [edit_version]:
                del payload["edit"]
            with view.condition:
                update_history_delta(payload, query.get("history_version", [None])[0],
                                      view.editor_history_versions)
    return payload


def send_roslyn_diagnostics(request, action: dict[str, Any]) -> None:
    config = request.server.config
    from migration_harness.verification.roslyn_syntax import (
        editor_context_revision, editor_diagnostics, editor_semantic_diagnostics,
    )

    try:
        run_id = action.get("run_id")
        validate_run_id(run_id)
        state = request.dashboard.state
        if (state.get("run_id") != run_id or not state.get("worktree_path")
                or request.headers.get("X-Run-ID", run_id) != run_id):
            raise ValueError("表示中の実行が変わりました。")
        _, worktree, _, unit = load_saved_run(config, run_id)
        target, version = action.get("target"), action.get("version")
        if target not in [p.as_posix() for p in target_paths(unit) if p.suffix.lower() in EDITOR_TARGET_SUFFIXES]:
            raise ValueError("設定された移行先ファイルを選んでください。")
        if not isinstance(version, str) or not 0 < len(version) <= 128:
            raise ValueError("診断する下書きの版が無効です。")
        mode = action.get("mode", "syntax")
        if mode == "syntax":
            result = editor_diagnostics(target, editor_text(action.get("content")), version)
            result["supports_semantic"] = True
        elif mode == "semantic":
            result = editor_semantic_diagnostics(
                worktree, config["build_project"], target, editor_text(action.get("content")), version,
                worker=request.server.editor_semantic_worker,
            )
        elif mode == "context":
            verify = action.get("verify_revision")
            if verify is not None and (not isinstance(verify, str) or not re.fullmatch("[0-9a-f]{64}", verify)):
                raise ValueError("診断した依存版の形式が無効です。")
            result = editor_context_revision(worktree, config["build_project"], target, version,
                                             verify_revision=verify)
        else:
            raise ValueError("診断の種類が不正です。")
        request.send_body(200, "application/json", json.dumps(result, ensure_ascii=False).encode("utf-8"))
    except DASHBOARD_ERRORS as exc:
        request.send_conflict(exc)


def post_editor(request, action: dict[str, Any]) -> None:
    """編集を保存するか、会話を永続化して受付後にディスパッチへ通知する。"""
    manager = request.server.manager
    if action.get("kind") == "conversation" and manager:
        run_id = request.dashboard.state.get("run_id")
        try:
            if not run_id or run_id != action.get("run_id"):
                raise ValueError("表示中の実行が変わりました。")
            entry = manager.conversations.submit(run_id, action)
        except DASHBOARD_ERRORS as exc:
            request.send_conflict(exc)
        else:
            try:
                request.send_body(202, "application/json", json.dumps(entry, ensure_ascii=False).encode())
            finally:
                if not entry.get("status_only"):
                    manager.conversations.notify(run_id)
        return
    send_editor(request, action)
