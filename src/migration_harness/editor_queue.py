"""Run単位で、エディタからの編集と追加指示の反映待ちを管理する。"""
from __future__ import annotations

import json
import secrets
from contextlib import contextmanager
from dataclasses import dataclass
from difflib import unified_diff
from pathlib import Path
from subprocess import run
from tempfile import TemporaryDirectory
from typing import Any, Iterator

from .run_records import lock_run_record, replace_run_record
from .worktree_targets import EDIT_BYTES, read_target

EDITOR_TARGET_SUFFIXES = frozenset({".cs", ".cshtml"})
QUEUE_PHASES = {"junie_running", "editor_checkpoint", "editor_followup", "interrupted", "junie_failed"}
AGENT_PHASES = QUEUE_PHASES | {"repairing"}


@contextmanager
def editor_queue(run_path: Path, *, name: str = "editor.json") -> Iterator[dict[str, Any]]:
    path = run_path / name
    with lock_run_record(path):
        if path.is_symlink() or path.with_suffix(".json.tmp").is_symlink():
            raise ValueError("エディタの保存先がシンボリックリンクです。")
        before = path.read_text(encoding="utf-8") if path.exists() else '{"edits": {}, "prompts": []}'
        queue = json.loads(before)
        yield queue
        if queue != json.loads(before):
            replace_run_record(path, queue)


def editor_text(content: Any, limit: int = EDIT_BYTES) -> str:
    if not isinstance(content, str) or "\0" in content or len(content.encode("utf-8")) > limit:
        raise ValueError("テキストの形式またはサイズを確認してください。")
    return content.replace("\r\n", "\n").replace("\r", "\n")


def editor_target(root: Path, target: Path) -> tuple[bytes | None, str]:
    data, content = read_target(root, target)
    return data, editor_text(content)


def merge_edit(base: str, draft: str, current: str) -> str | None:
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


def record_operator_edit(queue: dict[str, Any], target: str, before: str, after: str) -> None:
    if before == after:
        return
    diff = "".join(unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True), n=2))
    queue.setdefault("operator_edits", {}).setdefault(target, []).append(diff)


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
