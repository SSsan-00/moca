"""移行先の周辺資料を初回実行時に固定し、同じ相対パスで復元する。"""

from __future__ import annotations

import json
import os
import shutil
import stat
import time
from itertools import chain
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory

from .execution import check_cancelled, report_activity
from .files import has_symlink_component, sha256_file


def _reserved(path: Path) -> bool:
    return any(part.lower() in {".git", ".hg", ".svn"} for part in path.parts) or bool(
        path.parts and (path.parts[0].lower() == "migration-read-rules.py" or path.parts[0].lower().startswith(
            (".migration-", "migration-complete-", "checker-findings-", "migration-rule-reads-")
        ))
    )


def _safe(root: Path, relative: Path) -> Path:
    if relative.anchor or ".." in relative.parts:
        raise ValueError(f"invalid target context path: {relative}")
    if root.is_symlink() or has_symlink_component(root, relative):
        raise ValueError(f"target context symlink: {root / relative}; replace it with a regular file/directory")
    return root / relative


def _stamp(path: Path) -> tuple:
    value = path.lstat()
    return (value.st_dev, value.st_ino, value.st_mode, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _copy(source: Path, destination: Path, expected: str | None = None) -> str:
    before = _stamp(source)
    if not stat.S_ISREG(before[2]):
        raise ValueError(f"target context requires a regular file: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=destination.parent, delete=False) as stream:
        temporary = Path(stream.name)
    try:
        shutil.copyfile(source, temporary, follow_symlinks=False)
        _safe(Path(source.anchor), source.relative_to(source.anchor))
        if temporary.is_symlink() or before != _stamp(source):
            raise ValueError(f"target context changed while copying: {source}; retry when files are stable")
        digest = sha256_file(temporary)
        if expected is not None and digest != expected:
            raise ValueError(f"saved target context hash mismatch: {source}; prepare a new run")
        check_cancelled()
        temporary.chmod(0o444)
        if os.name == "nt" and destination.exists():
            destination.chmod(0o600)
        temporary.replace(destination)
        return digest
    finally:
        if temporary.exists() and not temporary.is_symlink():
            temporary.chmod(0o600)
        temporary.unlink(missing_ok=True)


def stage_target_context(
    worktree: Path, targets: tuple[Path, ...], capsule: Path, agent_root: Path,
) -> str:
    """Call after preparation scripts, before Junie; restore only pinned non-target files.

    The separate target-context/manifest.json is published with its snapshots atomically.
    A legacy run without this snapshot initializes it on its first call.
    """
    check_cancelled()
    worktree, capsule, agent_root = (Path(path).absolute() for path in (worktree, capsule, agent_root))
    for root in (worktree, capsule, agent_root):
        _safe(root, Path())
    worktree, capsule, agent_root = (path.resolve() for path in (worktree, capsule, agent_root))
    if any(output.is_relative_to(worktree) or worktree.is_relative_to(output) for output in (capsule, agent_root)):
        raise ValueError("target context outputs must be outside the worktree; move capsule/agent workspace")
    if capsule.is_relative_to(agent_root) or agent_root.is_relative_to(capsule):
        raise ValueError("target context capsule and agent workspace must be separate directories")
    targets = set(map(Path, targets))
    for target in targets:
        if not target.parts or target.anchor or ".." in target.parts or _reserved(target):
            raise ValueError(f"target conflicts with a reserved or invalid context path: {target}")
    roots = {path.parent for path in targets} | {path.parent.parent / "inc" for path in targets}
    roots.update(path.parent.parent.parent / name for path in targets for name in ("Libs", "Extensions"))
    target_names = sorted(path.as_posix() for path in targets)
    snapshot = _safe(capsule, Path("target-context"))
    manifest_path = _safe(snapshot, Path("manifest.json"))
    processed, last_report = 0, time.monotonic()
    report_activity("移行先の参照資料を検証・復元しています。")

    def progress(relative: Path) -> None:
        nonlocal processed, last_report
        check_cancelled()
        processed += 1
        now = time.monotonic()
        if now - last_report >= 1:
            report_activity(f"移行先の参照資料を処理しています（{processed}項目）: {relative}")
            last_report = now
            check_cancelled()

    check_cancelled()
    if not snapshot.exists():
        capsule.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix=".target-context-", dir=capsule) as temporary:
            staging = Path(temporary)
            manifest = {"targets": target_names, "directories": [], "files": []}
            visited = set()

            def visit(relative: Path) -> None:
                progress(relative)
                if relative in visited or relative in targets or _reserved(relative):
                    return
                source = _safe(worktree, relative)
                before = _stamp(source)
                if stat.S_ISDIR(before[2]):
                    visited.add(relative)
                    manifest["directories"].append(relative.as_posix())
                    with os.scandir(source) as children:
                        for child in children:
                            visit(relative / child.name)
                else:
                    digest = _copy(source, staging / "files" / relative)
                    manifest["files"].append({"path": relative.as_posix(), "source": str(source), "sha256": digest})
                _safe(worktree, relative)
                if before != _stamp(source):
                    raise ValueError(f"target context changed while copying: {source}; retry when files are stable")

            for relative in sorted(roots):
                source = _safe(worktree, relative)
                if source.exists():
                    if not source.is_dir():
                        raise ValueError(f"target context directory is not a directory: {source}")
                    visit(relative)
            with (staging / "manifest.json").open("w", encoding="utf-8") as stream:
                json.dump(manifest, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            check_cancelled()
            staging.rename(snapshot)
    if not manifest_path.is_file():
        raise ValueError(f"saved target context manifest is missing: {manifest_path}; prepare a new run")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("targets") != target_names or any(
        not isinstance(manifest.get(key), list) for key in ("directories", "files")
    ):
        raise ValueError("invalid saved target context manifest or changed targets; prepare a new run")
    for directory, entry in chain(((True, value) for value in manifest["directories"]),
                                  ((False, value) for value in manifest["files"])):
        check_cancelled()
        name = entry if directory else entry.get("path") if isinstance(entry, dict) else None
        if not isinstance(name, str):
            raise ValueError("invalid saved target context entry")
        relative = Path(name)
        if any(relative.is_relative_to(target) for target in targets) or _reserved(relative) or not any(
            relative.is_relative_to(root) for root in roots
        ):
            raise ValueError(f"saved target context path is outside reference directories or is a target: {name}")
        destination = _safe(agent_root, relative)
        source = _safe(snapshot, Path("files") / relative)
        progress(relative)
        if directory:
            destination.mkdir(parents=True, exist_ok=True)
        else:
            digest = entry.get("sha256")
            if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ValueError(f"invalid saved target context hash: {name}")
            if not source.is_file():
                raise ValueError(f"saved target context file is missing: {name}; prepare a new run")
            _copy(source, destination, digest)
    report_activity(f"固定した移行先の参照資料を復元しました（{len(manifest['files'])}ファイル）。")
    check_cancelled()
    return (
        "Pinned, read-only reference files are available at their original relative paths in the target parent "
        "directories, their sibling inc directories, and Libs and Extensions one level above inc. "
        "Read relevant files to understand imports and surrounding behavior. "
        "Do not edit these reference files or expand the assigned migration targets. "
    ) if manifest["files"] else ""
