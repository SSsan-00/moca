from __future__ import annotations

from pathlib import Path

from . import editor_queue as editor_queue_store
from .. import config as harness_config
from ..errors import format_error
from ..execution import check_cancelled
from .editor_queue import (
    EDITOR_TARGET_SUFFIXES, complete_editor_adoption, editor_target, merge_edit, stage_editor_adoption,
)
from .editor_queue import saved_edits
from .worktree_targets import target_versions
from .resume_plan import BUILD_THROUGH_STAGE
from .worktree_targets import save_target_edit, worktree_source_lock


def apply_verification_edits(run_path, worktree, unit, request):
    """検証依頼に含まれる編集を対象版と照合して反映する。"""

    targets = harness_config.target_paths(unit)
    apply_error = None
    with editor_queue_store.editor_queue(run_path, recover=(worktree, targets)) as queue, worktree_source_lock(worktree):
        check_cancelled()
        if (saved_edits(queue) != request["edits"] or queue.get("edit_revision") != request["edit_revision"]
                or target_versions(worktree, unit) != request["targets"]
                or (request.get("start_stage") == BUILD_THROUGH_STAGE
                    and any(prompt["status"] != "sent" for prompt in queue["prompts"]))):
            raise ValueError("指示を受け付けた後に移行先または保存版が更新されました。エディタで確認して指示を送信し直してください。")
        merged = {}
        conflicts = []
        for name, edit in queue["edits"].items():
            if Path(name) not in targets or Path(name).suffix.lower() not in EDITOR_TARGET_SUFFIXES:
                raise ValueError("エディタの保存先が設定された移行先と一致しません。")
            data, current = editor_target(worktree, Path(name))
            content = None if edit["status"] == "conflict" else merge_edit(edit["base"], edit["content"], current)
            if content is None:
                edit["status"] = "conflict"
                conflicts.append(name)
            else:
                merged[name] = data, current, content
        if not conflicts:
            if merged:
                adoption = stage_editor_adoption(run_path, queue, merged)
                for name, (_, _, content) in merged.items():
                    try:
                        save_target_edit(worktree, targets, {
                            "target": name, "version": adoption["entries"][name]["before"], "content": content,
                        })
                    except (OSError, ValueError) as exc:
                        apply_error = ValueError(f"{name} を反映できませんでした。残りの保存版を確認してください。{format_error(exc)}")
                        break
                complete_editor_adoption(queue, worktree, targets, adoption)
            if apply_error is None:
                return target_versions(worktree, unit)
    if apply_error is not None:
        raise apply_error
    raise ValueError(f"編集が競合しました。エディタで「競合を解消して保存」を選んでください。対象: {', '.join(conflicts)}")
