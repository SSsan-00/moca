import json
import os
import re
import shutil
import uuid
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile

from ..files import safe_target, sha256_file
from ..saved_state import content_hash, saved_object, versionless_record
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


_SNAPSHOT_ID = re.compile(r"stopped-repair-[0-9a-f]{32}")
_MANIFEST_KEYS = frozenset({"snapshot", "base", "targets", "workspace_unchanged"})
_LINEAGE_KEYS = frozenset({"stopped_snapshot", "base_snapshot"})
_MEMO_KEYS = frozenset({"junie_memos", "junie_memo_warning"})
_INVALID_METADATA = "停止時の snapshot の記録が無効です。保存済みの下書きと Run の記録を確認してください。"


def validate_snapshot_versions(base, targets):
    if (not isinstance(base, dict) or not isinstance(targets, dict) or not targets
            or not targets.keys() <= base.keys()):
        raise ValueError(_INVALID_METADATA)
    for versions in (base, targets):
        for name, digest in versions.items():
            if not isinstance(name, str) or not name or "\0" in name or "\\" in name:
                raise ValueError(_INVALID_METADATA)
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts or not path.parts or path.as_posix() != name:
                raise ValueError(_INVALID_METADATA)
            if not content_hash(digest, missing=True):
                raise ValueError(_INVALID_METADATA)


def validate_snapshot_metadata(draft):
    if not isinstance(draft, dict):
        raise ValueError(_INVALID_METADATA)
    versionless_record(draft, "snapshot")
    if not _MANIFEST_KEYS <= draft.keys() <= _MANIFEST_KEYS | _LINEAGE_KEYS | _MEMO_KEYS:
        raise ValueError(_INVALID_METADATA)
    snapshot = draft["snapshot"]
    if not isinstance(snapshot, str) or not _SNAPSHOT_ID.fullmatch(snapshot):
        raise ValueError("停止時の snapshot の名前が無効です。")
    if type(draft["workspace_unchanged"]) is not bool:
        raise ValueError(_INVALID_METADATA)
    for key in _LINEAGE_KEYS.intersection(draft):
        related = draft[key]
        if not isinstance(related, str) or related == snapshot or not _SNAPSHOT_ID.fullmatch(related):
            raise ValueError(_INVALID_METADATA)
    validate_snapshot_versions(draft["base"], draft["targets"])
    return {key: value for key, value in draft.items() if key not in _MEMO_KEYS}


def verified_snapshot_files(run_path, draft):
    expected = validate_snapshot_metadata(draft)
    snapshot = expected["snapshot"]
    manifest = safe_target(run_path, Path(snapshot) / "draft.json")
    if not manifest.is_file():
        raise ValueError("停止時の snapshot の記録がありません。")
    try:
        text = manifest.read_text(encoding="utf-8-sig")
    except UnicodeError as error:
        raise ValueError(_INVALID_METADATA) from error
    saved = saved_object(text, "snapshot")
    versionless_record(saved, "snapshot")
    if type(saved.get("workspace_unchanged")) is not bool or saved != expected:
        raise ValueError("停止時の snapshot の記録が一致しません。")
    # The numbered drafts are tied to the manifest order, not the order after reserializing run.json.
    files = {name: safe_target(run_path, Path(snapshot) / f"{index}.draft")
             for index, name in enumerate(saved["targets"])}
    if any(target_hash(path) != expected["targets"][name] for name, path in files.items()):
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
