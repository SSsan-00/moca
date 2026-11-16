"""Junieの編集結果をworktreeへ反映し、中断後に残りの反映を再開する。"""

from __future__ import annotations

import shutil
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import RLock
from typing import Any

from .files import safe_target, sha256_file
from .run_records import write_run_record

# 同じプロセスの比較・手修正・Junie出力の反映を直列化する。
worktree_source_lock = RLock()


def target_hash(path: Path) -> str | None:
    """移行先の未作成状態をハッシュの不一致と区別して扱う。"""
    return sha256_file(path) if path.is_file() else None


def finish_pending_targets(
    agent_root: Path, worktree: Path, targets: tuple[Path, ...], record: dict[str, Any], record_path: Path,
) -> None:
    """中断前後の版を照合し、未反映の移行先だけを適用する。"""
    pending = record.get("target_apply_pending")
    if pending is None:
        return
    if agent_root.is_symlink() or not agent_root.is_dir():
        raise ValueError("saved Junie workspace is missing or unsafe")
    relative = {path.as_posix(): path for path in targets}
    if not isinstance(pending, dict) or pending.keys() - relative.keys():
        raise ValueError("saved target application contains a path outside the configured targets")
    with worktree_source_lock:
        remaining = []
        for name, version in pending.items():
            source = safe_target(agent_root, relative[name])
            destination = safe_target(worktree, relative[name])
            current = target_hash(destination)
            if current == version["after"]:
                continue
            if current != version["before"] or target_hash(source) != version["after"]:
                raise ValueError(f"cannot recover target application after an external change: {name}")
            remaining.append((source, destination, version))
        # ファイル単位で置き換え、途中で停止しても記録したハッシュを使って残りを反映する。
        for source, destination, version in remaining:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(dir=destination.parent, delete=False) as stream:
                temporary = Path(stream.name)
            try:
                shutil.copyfile(source, temporary)
                if target_hash(temporary) != version["after"] or target_hash(destination) != version["before"]:
                    raise ValueError(f"file changed while applying Junie output: {destination.name}")
                shutil.copymode(destination if destination.exists() else source, temporary)
                temporary.replace(destination)
            finally:
                temporary.unlink(missing_ok=True)
        hashes = record.setdefault("target_hashes", {})
        hashes.update((name, version["after"]) for name, version in pending.items())
        primary = Path(record["target"]).as_posix()
        if primary in hashes:
            record["target_sha256"] = hashes[primary]
        record["target_applied"] = True
        del record["target_apply_pending"]
        write_run_record(record_path, record)


def apply_agent_targets(
    agent_root: Path, worktree: Path, targets: tuple[Path, ...],
    before: dict[str, str | None], record: dict[str, Any], record_path: Path,
) -> None:
    """外部編集を検出し、複数ファイルを反映する前に復旧用の記録を残す。"""
    with worktree_source_lock:
        for name, digest in before.items():
            if target_hash(safe_target(worktree, Path(name))) != digest:
                raise ValueError(f"worktree target changed during Junie execution: {name}")
        pending = {}
        for relative in targets:
            name = relative.as_posix()
            after = target_hash(safe_target(agent_root, relative))
            if after is not None and after != before[name]:
                pending[name] = {"before": before[name], "after": after}
        if pending:
            record["target_apply_pending"] = pending
            write_run_record(record_path, record)
            finish_pending_targets(agent_root, worktree, targets, record, record_path)
