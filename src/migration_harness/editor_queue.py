"""Run単位で、エディタからの編集と追加指示をJunieの区切りまで保持する。"""
from __future__ import annotations

import json
import secrets
from contextlib import contextmanager
from pathlib import Path
from subprocess import run
from tempfile import TemporaryDirectory
from typing import Any, Iterator

EDIT_BYTES = 512 * 1024
QUEUE_PHASES = {"junie_running", "editor_checkpoint", "editor_followup", "interrupted", "junie_failed"}
AGENT_PHASES = QUEUE_PHASES | {"repairing"}


@contextmanager
def editor_queue(run_path: Path) -> Iterator[dict[str, Any]]:
    from migration_harness.workflow import _lock_run_record, _replace_run_record

    path = run_path / "editor.json"
    with _lock_run_record(path):
        if path.is_symlink() or path.with_suffix(".json.tmp").is_symlink():
            raise ValueError("エディタの保存先がシンボリックリンクです。")
        before = path.read_text(encoding="utf-8") if path.exists() else '{"edits": {}, "prompts": []}'
        queue = json.loads(before)
        yield queue
        if queue != json.loads(before):
            _replace_run_record(path, queue)


def editor_text(content: Any, limit: int = EDIT_BYTES) -> str:
    if not isinstance(content, str) or "\0" in content or len(content.encode("utf-8")) > limit:
        raise ValueError("テキストの形式またはサイズを確認してください。")
    return content.replace("\r\n", "\n").replace("\r", "\n")


def editor_target(root: Path, target: Path) -> tuple[bytes | None, str]:
    from migration_harness.workflow import _safe_target

    path = _safe_target(root, target)
    try:
        with path.open("rb") as stream:
            data = stream.read(EDIT_BYTES + 1)
    except FileNotFoundError:
        return None, ""
    if len(data) > EDIT_BYTES:
        raise ValueError("512 KiB を超える C# ファイルは外部エディタで開いてください。")
    try:
        return data, editor_text(data.decode("utf-8-sig"))
    except UnicodeDecodeError:
        raise ValueError("UTF-8 の C# ファイルを選んでください。") from None


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
        if prompt["status"] == "sending":
            prompt["status"] = "sent" if success else "queued"
