"""Run単位で、エディタからの編集と追加指示の反映待ちを管理する。"""
from __future__ import annotations

import hashlib
import json
import secrets
from contextlib import contextmanager
from dataclasses import dataclass
from difflib import unified_diff
from pathlib import Path
from subprocess import run
from tempfile import TemporaryDirectory
from typing import Any, Iterator

from . import run_records, worktree_targets as target_files
from ..files import safe_target
from .migration_draft import save_stopped_migration
from .run_records import lock_run_record, replace_run_record
from .worktree_targets import (
    EDIT_BYTES, finish_pending_targets, read_target, save_target_edit, target_application_versions,
    target_edit_bytes, target_hash, worktree_source_lock,
)

EDITOR_TARGET_SUFFIXES = frozenset({".cs", ".cshtml"})
QUEUE_PHASES = {"junie_running", "editor_checkpoint", "editor_followup", "interrupted", "junie_failed"}
AGENT_PHASES = QUEUE_PHASES | {"repairing"}
EDITOR_SAVE_PENDING = "editor-save.pending.json"
_HISTORY_RECOVERY = "エディタの保存履歴が未確定です。エディタを開き直し、保存内容を確認してください。"


def _read_queue(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return '{"edits": {}, "prompts": []}'


def _digest(data: bytes | None) -> str | None:
    return hashlib.sha256(data).hexdigest() if data is not None else None


def editor_save_pending(run_path: Path) -> bool:
    try:
        (run_path / EDITOR_SAVE_PENDING).lstat()
    except FileNotFoundError:
        return False
    return True


def editor_save_intent(name, base, draft, edit_id, resolve):
    return {"target": name, "base": _digest(base.encode("utf-8")), "draft": _digest(draft.encode("utf-8")),
            "edit_id": edit_id, "resolve": resolve}


def _record_save_request(queue, save_id, intent, **result):
    latest = queue.setdefault("latest_save_requests", {})
    if save_id is None:
        latest.pop(intent["target"], None)
    else:
        latest[intent["target"]] = save_id
        queue.setdefault("save_requests", {})[save_id] = {**intent, **result}


def stage_editor_save(run_path, queue, original, draft, save_id, intent):
    before = _digest(original)
    after = _digest(target_edit_bytes(original or b"", draft))
    if save_id is None:
        queue.setdefault("immediate_saves", {})[intent["target"]] = {
            "base": intent["base"], "draft": intent["draft"], "after": after,
        }
    _record_save_request(queue, save_id, intent, after=after)
    if before != after:
        _stage_editor_recovery(run_path, {"target": intent["target"], "before": before, "after": after,
                                          "history_after": queue})
    return before


def _stage_editor_recovery(run_path, receipt):
    path = safe_target(run_path, Path(EDITOR_SAVE_PENDING))
    safe_target(run_path, path.with_suffix(".json.tmp").relative_to(run_path))
    if path.exists():
        raise ValueError(_HISTORY_RECOVERY)
    replace_run_record(path, {**receipt, "history_before": _digest(_read_queue(run_path / "editor.json").encode("utf-8"))})


def stage_editor_adoption(run_path, queue, merged, *, migration=None):
    entries = {}
    for name, (data, current, content) in merged.items():
        edit = queue["edits"][name]
        entries[name] = {"before": _digest(data), "after": _digest(target_edit_bytes(data or b"", content)),
                         "edit_id": edit["id"], "diff": _adopted_editor_diff(edit, current, content)}
    adoption = {"id": secrets.token_hex(16), "entries": entries}
    if migration is not None:
        adoption["migration"] = migration
    _stage_editor_recovery(run_path, {"adoption": adoption})
    return adoption


def complete_editor_adoption(queue, worktree, targets, adoption):
    entries = adoption["entries"]
    names = {target.as_posix() for target in targets if target.suffix.lower() in EDITOR_TARGET_SUFFIXES}
    if not isinstance(entries, dict) or entries.keys() - names:
        raise ValueError(_HISTORY_RECOVERY + " 保存先が現在の移行先と一致しません。")
    applied = []
    for name, receipt in entries.items():
        if queue["edits"].get(name, {}).get("id") != receipt["edit_id"]:
            raise ValueError(_HISTORY_RECOVERY + " 反映した編集の版が一致しません。")
        current = target_hash(safe_target(worktree, Path(name)))
        if current == receipt["after"]:
            applied.append(name)
        elif current != receipt["before"]:
            raise ValueError(_HISTORY_RECOVERY + " 対象ファイルが外部で更新されたため、履歴を確定せず停止しました。")
    for name in applied:
        adoption_entry = entries[name]
        _append_operator_diff(queue, name, adoption_entry["diff"])
        finish_applied_editor_edit(queue, name, adoption_entry["after"])
    if applied:
        queue["last_editor_adoption"] = adoption["id"]


def _recover_editor_adoption(path, before, worktree, targets, record):
    adoption = record["adoption"]
    queue = json.loads(before)
    if queue.get("last_editor_adoption") == adoption["id"]:
        return
    if _digest(before.encode("utf-8")) != record["history_before"]:
        raise ValueError(_HISTORY_RECOVERY + " 編集履歴が外部で更新されたため、反映を停止しました。")
    with worktree_source_lock(worktree):
        root = worktree
        if "migration" in adoption:
            root = _recover_migration_editor_adoption(path.parent, worktree, targets, queue, adoption)
        complete_editor_adoption(queue, root, targets, adoption)
        if queue.get("last_editor_adoption") == adoption["id"]:
            replace_run_record(path, queue)


def record_queued_editor_save(queue, edit, save_id, intent):
    if save_id is not None:
        edit["save_id"] = save_id
    _record_save_request(queue, save_id, intent, queued_edit=edit["id"])


def finish_applied_editor_edit(queue, name, applied_version):
    edit = queue["edits"].pop(name)
    receipt = queue.get("save_requests", {}).get(edit.get("save_id"))
    if receipt and receipt["target"] == name and receipt.get("queued_edit") == edit["id"]:
        receipt["after"] = applied_version
    return edit


def applied_editor_save_id(queue, name, original, content):
    save_id = queue.get("latest_save_requests", {}).get(name)
    if save_id is None:
        return None
    receipt = queue["save_requests"][save_id]
    if "after" in receipt:
        matches = receipt["after"] == _digest(original)
    else:
        matches = "queued_edit" in receipt and original is not None and receipt["draft"] == _digest(content.encode("utf-8"))
    return save_id if matches else None


def check_editor_adoption(queue, name, original, base_digest, adoption):
    save_id = adoption["save_id"]
    receipt = queue.get("save_requests", {}).get(save_id, {})
    if (original is None or queue.get("latest_save_requests", {}).get(name) != save_id
            or receipt.get("target") != name or receipt.get("queued_edit") != adoption["edit_id"]
            or receipt.get("after") != adoption["version"] or _digest(original) != adoption["version"]
            or receipt.get("draft") != base_digest):
        raise ValueError("採用後に対象ファイルが更新されたか、保存の基準版が一致しません。下書きを残しました。")


def replayed_editor_save(queue, edit, original, content, save_id, intent):
    if save_id is None:
        receipt = queue.get("immediate_saves", {}).get(intent["target"])
        if receipt is None or any(receipt[key] != intent[key] for key in ("base", "draft")):
            return False
    else:
        receipt = queue.get("save_requests", {}).get(save_id)
        if receipt is None:
            old = queue.get("immediate_save_requests", {}).get(save_id)
            if old is None:
                return False
            receipt = {**old, "edit_id": None, "resolve": False}
        if receipt.get("adoption") != intent.get("adoption") or any(receipt.get(key) != value for key, value in intent.items()):
            raise ValueError("同じ保存IDが別の編集に使われています。エディタを開き直してください。")
    if "queued_edit" in receipt:
        if edit is not None and edit["id"] == receipt["queued_edit"]:
            return True
        if edit is None and applied_editor_save_id(queue, intent["target"], original, content) == save_id:
            return True
        raise ValueError("保存済みの編集が更新されました。未保存の内容を控え、最新版を確認してください。")
    if edit is not None or _digest(original) != receipt["after"]:
        raise ValueError("前回の保存後に対象ファイルが更新されました。最新の内容を読み込んでください。")
    return True


def _recover_editor_save(run_path, path, before, worktree, targets):
    journal = safe_target(run_path, Path(EDITOR_SAVE_PENDING))
    try:
        record = json.loads(journal.read_text(encoding="utf-8"))
        if not isinstance(record, dict):
            raise ValueError("invalid editor save recovery")
    except ValueError as exc:
        raise ValueError(_HISTORY_RECOVERY + " 保存時の復旧記録を読み取れません。") from exc
    if "adoption" in record:
        try:
            _recover_editor_adoption(path, before, worktree, targets, record)
        except (KeyError, TypeError) as exc:
            raise ValueError(_HISTORY_RECOVERY + " 保存時の復旧記録を読み取れません。") from exc
        journal.unlink()
        return
    try:
        name, after = record["target"], record["history_after"]
        if not isinstance(name, str) or not isinstance(after, dict):
            raise ValueError("invalid editor save recovery")
        previous, updated, history_before = record["before"], record["after"], record["history_before"]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(_HISTORY_RECOVERY + " 保存時の復旧記録を読み取れません。") from exc
    if name not in {target.as_posix() for target in targets if target.suffix.lower() in EDITOR_TARGET_SUFFIXES}:
        raise ValueError(_HISTORY_RECOVERY + " 保存先が現在の移行先と一致しません。")
    with worktree_source_lock(worktree):
        original, _ = read_target(worktree, Path(name))
        current = _digest(original)
        unchanged_history = _digest(before.encode("utf-8")) == history_before
        if current == updated and (unchanged_history or json.loads(before) == after):
            if unchanged_history:
                replace_run_record(path, after)
        elif current != previous or not unchanged_history:
            raise ValueError("対象ファイルか履歴が外部で更新されたため、保存を復旧せず停止しました。"
                             "現在のファイルを控え、Run の editor-save.pending.json を確認してください。")
        journal.unlink()


@contextmanager
def editor_queue(
    run_path: Path, *, name: str = "editor.json", recover: tuple[Path, tuple[Path, ...]] | None = None,
) -> Iterator[dict[str, Any]]:
    """Runの編集・指示履歴をロック付きで管理する。"""
    path = run_path / name
    with lock_run_record(path):
        if path.is_symlink() or path.with_suffix(".json.tmp").is_symlink():
            raise ValueError("エディタの保存先がシンボリックリンクです。")
        try:
            before = _read_queue(path)
            if name == "editor.json" and editor_save_pending(run_path):
                if recover is None:
                    raise ValueError(_HISTORY_RECOVERY)
                _recover_editor_save(run_path, path, before, *recover)
                before = _read_queue(path)
            queue = json.loads(before)
            yield queue
            if queue != json.loads(before):
                replace_run_record(path, queue)
                if name == "editor.json" and editor_save_pending(run_path):
                    safe_target(run_path, Path(EDITOR_SAVE_PENDING)).unlink()
        except OSError as exc:
            if name == "editor.json" and editor_save_pending(run_path):
                raise OSError(_HISTORY_RECOVERY) from exc
            raise


def editor_text(content: Any, limit: int = EDIT_BYTES) -> str:
    if not isinstance(content, str) or "\0" in content or len(content.encode("utf-8")) > limit:
        raise ValueError("テキストの形式またはサイズを確認してください。")
    return content.replace("\r\n", "\n").replace("\r", "\n")


def editor_target(root: Path, target: Path) -> tuple[bytes | None, str]:
    data, content = read_target(root, target)
    return data, editor_text(content)


def merge_edit(base: str, draft: str, current: str) -> str | None:
    """利用者の差分を基準版と現行版へ安全に合流する。"""
    if current == base or current == draft:
        return draft
    if base == draft:
        return current
    with TemporaryDirectory() as directory:
        paths = [Path(directory) / name for name in ("current", "base", "draft")]
        for path, text in zip(paths, (current, base, draft), strict=True):
            path.write_bytes(text.encode("utf-8"))
        merged = run(["git", "merge-file", "-p", *(str(path) for path in paths)], capture_output=True)
    if merged.returncode == 0:
        return editor_text(merged.stdout.decode("utf-8"))
    if 0 < merged.returncode <= 127 and merged.stdout:
        return None
    raise ValueError("C# の変更を合流できませんでした。Git と対象ファイルを確認してください。")


def _operator_edit_diff(before, after):
    if before == after:
        return ""
    lines = unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True), n=2)
    return "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in lines)


def _adopted_editor_diff(edit, current, adopted):
    before, after = (edit["base"], edit["content"]) if current == adopted else (current, adopted)
    return _operator_edit_diff(before, after)


def _append_operator_diff(queue, target, diff):
    if diff:
        queue.setdefault("operator_edits", {}).setdefault(target, []).append(diff)


def record_operator_edit(queue: dict[str, Any], target: str, before: str, after: str) -> None:
    _append_operator_diff(queue, target, _operator_edit_diff(before, after))


def editor_prompt_selection(value, targets):
    if value is None:
        return None
    if not isinstance(value, dict) or value.keys() != {"path", "start_line", "end_line", "text"}:
        raise ValueError("添付した選択範囲の形式が無効です。")
    path, start, end = value["path"], value["start_line"], value["end_line"]
    if (not isinstance(path, str) or path not in targets or type(start) is not int
            or type(end) is not int or not 1 <= start <= end <= 2_000_000 or end - start >= 20):
        raise ValueError("現在のRunの移行先で選択し直してください（最大20行）。")
    text = editor_text(value["text"], 2_000)
    if not text.strip() or text.count("\n") + (not text.endswith("\n")) != end - start + 1:
        raise ValueError("添付した選択範囲または選択行数が無効です。")
    return {"path": path, "start_line": start, "end_line": end, "text": text}


def add_prompt(queue: dict[str, Any], action: dict[str, Any]) -> None:
    text = editor_text(action.get("text"), 4_000).strip()
    if not text:
        raise ValueError("Junie に渡す指示を入力してください。")
    identifier = action.get("id")
    prompts = queue["prompts"]
    if identifier is not None:
        for prompt in prompts:
            if prompt["id"] == identifier and prompt["status"] == "queued":
                prompt["text"] = text
                return
        raise ValueError("指示の送信状態が変わりました。表示を更新してください。")
    if sum(prompt["status"] != "sent" for prompt in prompts) >= 16:
        raise ValueError("送信待ちの指示は最大16件です。")
    prompts.append({"id": secrets.token_hex(12), "text": text, "status": "queued"})


def dispatch_prompts(queue: dict[str, Any]) -> str:
    prompts = [prompt for prompt in queue["prompts"] if prompt["status"] == "queued"]
    for prompt in prompts:
        prompt["status"] = "sending"
    return "\n".join(f"- {prompt['text']}" for prompt in prompts)


def finish_prompts(queue: dict[str, Any], success: bool) -> None:
    for prompt in queue["prompts"]:
        if prompt["status"] in {"sending", "applying"}:
            prompt["status"] = "sent" if success else "queued"


@dataclass
class LivePrompts:
    """実行途中に送った指示だけを追跡し、未完了なら通常の送信待ちに戻す。"""

    run_path: Path
    interrupt_for_edits: bool = False

    def needs_handoff(self) -> bool:
        if not self.interrupt_for_edits:
            return False
        with editor_queue(self.run_path) as queue:
            return bool(queue["edits"] and any(prompt["status"] == "queued" for prompt in queue["prompts"]))

    def recover(self) -> None:
        """中断した配信状態から未完了指示を復旧する。"""
        with editor_queue(self.run_path) as queue:
            for prompt in queue["prompts"]:
                if prompt["status"] in {"steering", "applying"}:
                    prompt["status"] = "queued"

    def take(self) -> tuple[list[str], str]:
        with editor_queue(self.run_path) as queue:
            # 保留中の手修正に対する指示は、ファイルを合流できる区切りまで待つ。
            if queue["edits"]:
                return [], ""
            prompts = [prompt for prompt in queue["prompts"] if prompt["status"] == "queued"]
            for prompt in prompts:
                prompt["status"] = "steering"
            return [prompt["id"] for prompt in prompts], "\n".join(prompt["text"] for prompt in prompts)

    def finish(self, identifiers: list[str], success: bool) -> None:
        with editor_queue(self.run_path) as queue:
            for prompt in queue["prompts"]:
                if prompt["id"] in identifiers and prompt["status"] == "steering":
                    # Junieの完了と、ハーネスによる検証・反映の完了を区別する。
                    prompt["status"] = "applying" if success else "queued"


def apply_editor_edits(
    queue: dict[str, Any], agent_root: Path, worktree: Path, targets: tuple[Path, ...],
    record: dict[str, Any], record_path: Path,
) -> bool:

    edits = queue["edits"]
    if not edits:
        return True
    merged = {}
    names = {target.as_posix() for target in targets if target.suffix.lower() in EDITOR_TARGET_SUFFIXES}
    for name, edit in edits.items():
        if name not in names:
            raise ValueError("エディタの保存先が設定された移行先と一致しません。")
        original, current = editor_target(agent_root, Path(name))
        text = None if edit["status"] == "conflict" else merge_edit(edit["base"], edit["content"], current)
        if text is None:
            edit["status"] = "conflict"
        else:
            merged[name] = original, current, text
    # ひとつでも競合していれば、他のファイルも書き換えずに利用者の解消を待つ。
    if any(edit["status"] == "conflict" for edit in edits.values()):
        record.update(status="editor_checkpoint", error="移行先ファイルの編集が競合しました。エディタで解消してから再開してください。")
        run_records.write_run_record(record_path, record, timing_stage=None)
        return False
    with worktree_source_lock(worktree):
        adopted = record["target_hashes"]
        if any(target_hash(safe_target(worktree, target)) != adopted[target.as_posix()] for target in targets):
            raise ValueError("移行先ファイルが外部で更新されました。編集を採用できません。")
        other = {target.as_posix(): target_application_versions(safe_target(agent_root, target))
                 for target in targets if target.as_posix() not in merged}
        stopped = record.get("stopped_migration_draft")
        adoption = stage_editor_adoption(record_path.parent, queue, merged, migration={
            "worktree": adopted.copy(), "other": other, "stopped": stopped["snapshot"] if stopped else None,
        })
        _finish_migration_editor_adoption(agent_root, worktree, targets, queue, adoption, record, record_path, merged=merged)
        complete_editor_adoption(queue, agent_root, targets, adoption)
    return True


def _recover_migration_editor_adoption(run_path, worktree, targets, queue, adoption):
    agent_root = run_path / "agent-workspace"
    if agent_root.is_symlink() or not agent_root.is_dir() or not agent_root.resolve().is_relative_to(run_path.resolve()):
        raise ValueError("Junie 作業領域がありません。保存済みの手修正を復旧できません。")
    record_path = safe_target(run_path, Path("run.json"))
    record = json.loads(record_path.read_text(encoding="utf-8"))
    _finish_migration_editor_adoption(agent_root, worktree, targets, queue, adoption, record, record_path)
    return agent_root


def _finish_migration_editor_adoption(agent_root, worktree, targets, queue, adoption, record, record_path, *, merged=None):
    migration = adoption["migration"]
    entries, others, baseline = adoption["entries"], migration["other"], migration["worktree"]
    configured = {target.as_posix(): target for target in targets}
    stopped = record.get("stopped_migration_draft")
    if (baseline.keys() != configured.keys() or set(entries) & set(others)
            or set(entries) | set(others) != configured.keys()
            or bool(migration["stopped"]) != bool(stopped)):
        raise ValueError("保留編集の復旧記録が現在の移行単位と一致しません。")
    after, unchanged, pending_edits = {}, {}, {}
    for name, target in configured.items():
        current = target_hash(safe_target(agent_root, target))
        if name in entries:
            receipt = entries[name]
            edit = queue["edits"].get(name, {})
            if edit.get("id") != receipt["edit_id"]:
                raise ValueError("保留編集の版が変わりました。復旧を中止しました。")
            after[name] = unchanged[name] = receipt["after"]
            if current == receipt["after"]:
                continue
            if current != receipt["before"]:
                raise ValueError(f"保留編集の対象が外部で更新されました: {name}")
            if merged is None:
                data, text = editor_target(agent_root, target)
                content = merge_edit(edit["base"], edit["content"], text)
            else:
                data, _, content = merged[name]
            if content is None or hashlib.sha256(target_edit_bytes(data or b"", content)).hexdigest() != receipt["after"]:
                raise ValueError(f"保留編集の採用結果が変わりました: {name}")
            pending_edits[name] = {"target": name, "version": current, "content": content}
        else:
            raw, normalized = others[name]
            if current not in ((raw,) if stopped else (raw, normalized)):
                raise ValueError(f"Junie 作業領域の対象が外部で更新されました: {name}")
            unchanged[name], after[name] = raw, normalized if normalized is not None else baseline[name]

    saved = record["target_hashes"]
    pending = record.get("target_apply_pending")
    if pending:
        expected = {name: {"before": before, "after": after[name]}
                    for name, before in baseline.items() if before != after[name]}
        if stopped or pending != expected or saved != baseline or pending_edits:
            raise ValueError("移行先の反映記録が保留編集の復旧記録と一致しません。")
        finish_pending_targets(agent_root, worktree, targets, record, record_path)
        return
    if saved != baseline and (stopped or saved != after or pending_edits):
        raise ValueError("移行先の版が変わりました。保留編集を復旧できません。")
    if any(target_hash(safe_target(worktree, target)) != saved[name] for name, target in configured.items()):
        raise ValueError("移行先ファイルが外部で更新されました。保留編集を復旧できません。")
    if stopped and stopped["snapshot"] != migration["stopped"]:
        if pending_edits or stopped["targets"] != unchanged:
            raise ValueError("停止時の下書きが更新されました。保留編集を復旧できません。")
        return
    for edit in pending_edits.values():
        save_target_edit(agent_root, targets, edit)
    if stopped:
        record.pop("stopped_migration_save_error", None)
        save_stopped_migration(record_path, record, agent_root, targets, workspace_unchanged=True)
    elif saved == baseline:
        target_files.apply_agent_targets(agent_root, worktree, targets, baseline, record, record_path)


def saved_edits(queue):
    return {name: edit["id"] for name, edit in queue["edits"].items()}
