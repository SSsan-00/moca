"""ソースと作業ファイルのパス・内容の照合。"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Callable


def harness_asset(name: str) -> Path:
    """ハーネスのチェックアウトまたはインストール先の付属ファイルを解決する。"""
    path = Path(__file__).resolve().parents[2] / name
    return path if path.is_file() else Path(sys.prefix) / "share" / "php-to-csharp-harness" / name


def has_symlink_component(root: Path, relative_path: Path) -> bool:
    """中間ディレクトリも含めてリンクの有無を調べる。"""
    current = root
    for part in relative_path.parts:
        current /= part
        if current.is_symlink():
            return True
    return False


def sha256_file(path: Path, *, check: Callable[[], None] | None = None) -> str:
    """ファイル全体を一度に保持せず、保存バイト列を照合するハッシュを計算する。"""
    digest = hashlib.sha256()
    if check is not None:
        check()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(64 * 1024)
            if check is not None:
                check()
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def safe_target(root: Path, relative: Path) -> Path:
    """作業場所の内側にある通常ファイルのパスを返す。新規作成前のパスも許可する。"""
    target = root / relative
    if has_symlink_component(root, relative) or not target.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"target escapes its workspace or is a symlink: {relative}")
    if target.exists() and not target.is_file():
        raise ValueError(f"target is not a regular file: {relative}")
    return target
