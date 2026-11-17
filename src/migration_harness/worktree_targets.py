"""移行先ソースの読み書き、Junie出力の反映、中断後の復旧。"""

from __future__ import annotations

import codecs
import hashlib
import os
import shutil
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import RLock
from typing import Any

from .files import has_symlink_component, safe_target, sha256_file
from .run_records import write_run_record

EDIT_BYTES = 8 * 1024 * 1024

# 同じプロセスの比較・手修正・Junie出力の反映を直列化する。
worktree_source_lock = RLock()


def target_hash(path: Path) -> str | None:
    """移行先の未作成状態をハッシュの不一致と区別して扱う。"""
    return sha256_file(path) if path.is_file() else None


def review_target_hash(worktree: Path, relative_target: Path) -> str | None:
    """worktree内の通常ファイルだけをレビュー対象として照合する。"""
    target = worktree / relative_target
    if has_symlink_component(worktree, relative_target) or not target.resolve().is_relative_to(worktree.resolve()):
        return None
    return sha256_file(target) if target.is_file() else None


def read_target(root: Path, target: Path) -> tuple[bytes | None, str]:
    """編集用にサイズとUTF-8を検証し、元バイト列と改行を保った本文を返す。"""
    path = safe_target(root, target)
    try:
        with path.open("rb") as stream:
            data = stream.read(EDIT_BYTES + 1)
    except FileNotFoundError:
        return None, ""
    if len(data) > EDIT_BYTES:
        raise ValueError("8 MiB を超える移行先ファイルは外部エディタで開いてください。")
    try:
        content = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("UTF-8 の移行先ファイルを選んでください。") from None
    if "\0" in content:
        raise ValueError("テキストファイルを選んでください。")
    return data, content


def target_newline(data: bytes) -> str:
    if b"\r\n" in data:
        return "\r\n"
    if b"\r" in data and b"\n" not in data:
        return "\r"
    return "\n"


def save_target_edit(worktree: Path, targets: tuple[Path, ...], edit: dict[str, Any]) -> None:
    """編集元の版を照合し、改行を保ってBOM付きUTF-8のファイルへ置き換える。"""
    with worktree_source_lock:
        if edit["target"] not in {target.as_posix() for target in targets}:
            raise ValueError("設定された移行対象ファイルを選んでください。")
        path = safe_target(worktree, Path(edit["target"]))
        if edit["version"] is None:
            original, version = b"", target_hash(path)
        else:
            original, _ = read_target(worktree, Path(edit["target"]))
            if original is None:
                raise ValueError("対象ファイルが見つかりません。作業ツリーで確認してください。")
            version = hashlib.sha256(original).hexdigest()
        # 画面を開いてから更新されたファイルを、古い編集内容で上書きしない。
        if version != edit["version"]:
            raise ValueError("編集を開いた後に対象ファイルが更新されました。最新の内容を読み込み、編集を反映し直してください。")
        # ブラウザーから来る改行を元の形式にそろえ、BOM付きUTF-8で保存する。
        text = edit["content"].replace("\r\n", "\n").replace("\r", "\n")
        text = text.replace("\n", target_newline(original))
        data = text.encode("utf-8-sig")
        if len(data) > EDIT_BYTES:
            raise ValueError("編集後のファイルが 8 MiB を超えています。作業ツリーで編集してください。")
        if data == original and version is not None:
            return
        # 同じディレクトリに書き終えてから置き換え、保存途中の内容が見えないようにする。
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
        try:
            temporary.write_bytes(data)
            if version is not None:
                shutil.copymode(path, temporary)
            if target_hash(safe_target(worktree, Path(edit["target"]))) != edit["version"]:
                raise ValueError("対象ファイルが更新されました。最新の内容を読み込んでください。")
            if version is None:
                # 新規ファイルは、保存の間に別の処理が作成した内容を上書きしない。
                os.link(temporary, path)
            else:
                temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def _ensure_utf8_bom(path: Path) -> None:
    data = path.read_bytes()
    try:
        data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError(f"移行先ファイルは UTF-8 で保存してください: {path.name}") from None
    if data.startswith(codecs.BOM_UTF8):
        return
    with NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
    try:
        temporary.write_bytes(codecs.BOM_UTF8 + data)
        shutil.copymode(path, temporary)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


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
            source = safe_target(agent_root, relative)
            if source.is_file():
                # 復旧用ハッシュと次回のJunie入力も、BOM付きの保存バイト列にそろえる。
                _ensure_utf8_bom(source)
            after = target_hash(source)
            if after is not None and after != before[name]:
                pending[name] = {"before": before[name], "after": after}
        if pending:
            record["target_apply_pending"] = pending
            write_run_record(record_path, record)
            finish_pending_targets(agent_root, worktree, targets, record, record_path)
