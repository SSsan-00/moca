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

from ..files import safe_target
from .run_records import lock_run_record, replace_run_record
from .worktree_targets import EDIT_BYTES, read_target, target_edit_bytes, target_hash, worktree_source_lock

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
        _append_operator_diff(queue, name, entries[name]["diff"])
        del queue["edits"][name]
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
            from ..workflow import _recover_migration_editor_adoption
            root = _recover_migration_editor_adoption(path.parent, worktree, targets, queue, adoption)
        complete_editor_adoption(queue, root, targets, adoption)
        if queue.get("last_editor_adoption") == adoption["id"]:
            replace_run_record(path, queue)


def record_queued_editor_save(queue, edit, save_id, intent):
    if save_id is not None:
        edit["save_id"] = save_id
    _record_save_request(queue, save_id, intent, queued_edit=edit["id"])


def applied_editor_save_id(queue, name, original, content):
    save_id = queue.get("latest_save_requests", {}).get(name)
    if save_id is None:
        return None
    receipt = queue["save_requests"][save_id]
    if "queued_edit" in receipt:
        matches = original is not None and receipt["draft"] == _digest(content.encode("utf-8"))
    else:
        matches = receipt["after"] == _digest(original)
    return save_id if matches else None


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
        if any(receipt[key] != value for key, value in intent.items()):
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
