"""Run記録の検証と、進捗・使用量・レビュー判断の保存。"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Literal

from . import junie_cli
from .config import find_unit, target_paths
from .timings import update_timings

run_observer: ContextVar[Callable[[dict[str, Any]], None] | None] = ContextVar("run_observer", default=None)


@contextmanager
def lock_run_record(path: Path) -> Iterator[None]:
    """Run単位で、ダッシュボードと別CLIの読み取り・更新を直列化する。"""
    # 置き換わるJSON本体とは別のファイルをロックする。run.jsonとeditor.jsonは別々にロックする。
    with path.with_suffix(".lock").open("ab") as lock:
        if os.name == "nt":
            import msvcrt

            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            yield


def replace_run_record(path: Path, record: dict[str, Any]) -> None:
    """呼び出し元がロックした状態で、JSONファイルを置き換える。"""
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _notify_run_observer(record: dict[str, Any]) -> None:
    observer = run_observer.get()
    if observer is not None:
        observer(record)


def write_run_record(
    path: Path, record: dict[str, Any], *, timing_stage: int | None | Literal["status"] = "status",
) -> None:
    """工程の進捗を保存し、独立して更新された使用量と台帳を引き継ぐ。"""
    update_timings(record, stage=timing_stage)
    with lock_run_record(path):
        if path.is_file():
            saved = json.loads(path.read_text(encoding="utf-8"))
            for field in ("token_usage", "migration_reviews", "migration_mappings"):
                if field in saved:
                    record[field] = saved[field]
        replace_run_record(path, record)
    _notify_run_observer(record)


@contextmanager
def edit_run_record(path: Path) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
    """最新のRun記録を読み、指定された項目だけを同じロック内で更新する。"""
    with lock_run_record(path):
        record = json.loads(path.read_text(encoding="utf-8"))
        changes: dict[str, Any] = {}
        yield record, changes
        if not changes:
            return
        record.update(changes)
        update_timings(record, stage=record.get("active_stage"))
        replace_run_record(path, record)
    _notify_run_observer(record)


def junie_usage_recorder(
    run_path: Path, phase: str, capsule: int | None = None,
) -> Callable[[list[int]], None]:
    """同じ工程・カプセルの使用量を保存するコールバックを作る。"""
    def report(delta: list[int]) -> None:
        with edit_run_record(run_path / "run.json") as (record, changes):
            rows = record.get("token_usage", [])
            row = next((row for row in rows if row["phase"] == phase and row.get("capsule") == capsule), None)
            if row is None:
                row = {"phase": phase, "capsule": capsule, **dict.fromkeys(junie_cli.USAGE_FIELDS, 0)}
                rows.append(row)
            elif not any(delta):
                return
            for field, value in zip(junie_cli.USAGE_FIELDS, delta, strict=True):
                row[field] += value
            changes["token_usage"] = rows

    return report


def load_saved_run(
    config: dict[str, Any], run_id: str,
) -> tuple[Path, Path, dict[str, Any], dict[str, Any]]:
    """保存済みのプロジェクト・移行先・作業場所を現在の設定と照合する。"""
    try:
        parsed_run_id = uuid.UUID(run_id)
    except ValueError as exc:
        raise ValueError("run ID must be a UUID printed by a previous harness command") from exc
    if str(parsed_run_id) != run_id:
        raise ValueError("run ID must use its canonical UUID format")

    run_root = Path(config["run_root"]).resolve()
    run_path = (run_root / run_id).resolve()
    if run_path.parent != run_root or not run_path.is_dir():
        raise ValueError(f"run directory not found under configured run_root: {run_id}")
    record_path = run_path / "run.json"
    if record_path.is_symlink() or not record_path.is_file():
        raise ValueError("run record is missing or is a symlink")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    if not isinstance(record, dict):
        raise ValueError("run record must contain a JSON object")
    if record.get("run_id") != run_id or record.get("project_root") != config["project_root"]:
        raise ValueError("run record does not match the configured project")
    unit_id = record.get("unit_id")
    if not isinstance(unit_id, str):
        raise ValueError("run record is missing its migration unit ID")
    unit = find_unit(config, unit_id)
    saved_targets = record.get("targets", [record.get("target")])
    if not isinstance(saved_targets, list) or any(not isinstance(path, str) for path in saved_targets) or (
        tuple(Path(path) for path in saved_targets) != target_paths(unit)
    ):
        raise ValueError("configured targets differ from those recorded for this run")
    worktree_root = Path(config["worktree_root"]).resolve()
    if not isinstance(record.get("worktree_path"), str):
        raise ValueError("run record is missing its worktree path")
    worktree_path = Path(record["worktree_path"]).resolve()
    if worktree_path.parent != worktree_root or worktree_path.name != run_id:
        raise ValueError("run worktree path does not match the configured worktree root and run ID")
    return run_path, worktree_path, record, unit
