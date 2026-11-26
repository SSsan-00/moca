import json
import os
import re
import shutil
import uuid
from pathlib import Path
from tempfile import NamedTemporaryFile

from ..files import safe_target, sha256_file
from .worktree_targets import target_hash


def save_target_snapshot(run_path, sources, before, *, workspace_unchanged, stopped_snapshot=None, base_snapshot=None):
    name = f"stopped-repair-{uuid.uuid4().hex}"
    root = run_path / name
    root.mkdir(mode=0o700)
    hashes = {}
    for index, (name_in_run, source) in enumerate(sources.items()):
        if source.is_file():
            copy = root / f"{index}.draft"
            shutil.copyfile(source, copy)
            hashes[name_in_run] = sha256_file(copy)
        else:
            hashes[name_in_run] = None
    draft = {
        "snapshot": name, "base": before.copy(), "targets": hashes,
        "workspace_unchanged": workspace_unchanged,
    }
    if stopped_snapshot is not None:
        draft["stopped_snapshot"] = stopped_snapshot
    if base_snapshot is not None:
        draft["base_snapshot"] = base_snapshot
    (root / "draft.json").write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")
    return draft


def save_workspace_snapshot(run_path, workspace, targets, before, *, workspace_unchanged):
    sources = {path.as_posix(): safe_target(workspace, path) for path in targets}
    return save_target_snapshot(run_path, sources, before, workspace_unchanged=workspace_unchanged)


def save_capsule_checkpoint(run_path, worktree, targets, hashes):
    checkpoint = save_workspace_snapshot(run_path, worktree, targets, hashes, workspace_unchanged=True)
    if checkpoint["targets"] != hashes:
        raise ValueError("保存中に移行先が更新されました。")
    return checkpoint


def verified_snapshot_files(run_path, draft):
    snapshot = draft.get("snapshot", "")
    if not re.fullmatch(r"stopped-repair-[0-9a-f]{32}", snapshot):
        raise ValueError("停止時の snapshot の名前が無効です。")
    manifest = safe_target(run_path, Path(snapshot) / "draft.json")
    draft_files = {key: value for key, value in draft.items() if key not in {"junie_memos", "junie_memo_warning"}}
    if not manifest.is_file() or json.loads(manifest.read_bytes()) != draft_files:
        raise ValueError("停止時の snapshot の記録が一致しません。")
    files = {name: safe_target(run_path, Path(snapshot) / f"{index}.draft")
             for index, name in enumerate(draft["targets"])}
    if any(target_hash(path) != draft["targets"][name] for name, path in files.items()):
        raise ValueError("停止時の snapshot の内容が一致しません。")
    return files


def restore_snapshot_files(sources, destinations, expected, selected, *, preserve_mode=False):
    # A retry can see either the original version or the version already restored by this plan.
    hashes = {name: target_hash(file) for name, file in destinations.items()}
    if any(hashes[name] not in (expected[name], selected.get(name, expected[name])) for name in hashes):
        raise ValueError("復元対象が更新されました。進捗画面で復元計画を取り直してください。")
    for name, source in sources.items():
        destination = destinations[name]
        digest = selected[name]
        if hashes[name] == digest:
            continue
        if digest is None:
            if target_hash(destination) != hashes[name]:
                raise ValueError("復元中に対象が更新されました。")
            destination.unlink()
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(dir=destination.parent, delete=False) as stream:
            temporary = Path(stream.name)
        try:
            shutil.copyfile(source, temporary)
            if target_hash(temporary) != digest or target_hash(destination) != hashes[name]:
                raise ValueError("復元中に対象が更新されました。")
            if hashes[name] is None:
                os.link(temporary, destination)
            else:
                if preserve_mode:
                    shutil.copymode(destination, temporary)
                temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
