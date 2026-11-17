"""設定コマンドのパス別名を、そのRunのworktree内の絶対パスへ展開する。"""

from __future__ import annotations

import re
from pathlib import Path

from .files import safe_target


_PATH_ALIAS = re.compile(r"\{(worktree|build_project|target)\}")


def expand_command_paths(
    command: list[str], worktree: Path | None, build_project: str | None, target: str | None,
) -> list[str]:
    """工程コマンド内のパス指定を実行時の値へ展開する。"""
    def replace(match: re.Match[str]) -> str:
        """1つのコマンド引数に含まれるパス指定を置き換える。"""
        alias = match.group(1)
        if worktree is None:
            raise ValueError(f"{{{alias}}} はworktree作成後の工程で使用してください（before_prepareでは使用できません）")
        root = worktree.resolve()
        if alias == "worktree":
            return str(root)
        relative = build_project if alias == "build_project" else target
        if not relative:
            setting = "[build].project" if alias == "build_project" else "移行先ファイル"
            raise ValueError(f"{{{alias}}} を使うには {setting} を設定してください")
        return str(safe_target(root, Path(relative)))

    # シェル展開せず、引数の境界と通常のJSON・コード中の波括弧を保つ。
    return [_PATH_ALIAS.sub(replace, arg) for arg in command]
