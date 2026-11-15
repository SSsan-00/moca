"""ソースと作業ファイルのパス・内容の照合。"""

from __future__ import annotations

import hashlib
from pathlib import Path


def has_symlink_component(root: Path, relative_path: Path) -> bool:
    """中間ディレクトリも含めてリンクの有無を調べる。"""
    current = root
    for part in relative_path.parts:
        current /= part
        if current.is_symlink():
            return True
    return False


def sha256_file(path: Path) -> str:
    """ファイル全体を一度に保持せず、保存バイト列を照合するハッシュを計算する。"""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(64 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
