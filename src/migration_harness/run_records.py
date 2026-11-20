"""Run記録の検証と、進捗・使用量・レビュー判断の保存。"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from time import sleep
from typing import Any, Literal

from . import junie_cli
from .config import find_unit, target_paths
from .timings import update_timings

run_observer: ContextVar[Callable[[dict[str, Any]], None] | None] = ContextVar("run_observer", default=None)


@contextmanager
def execution_lock(config: dict[str, Any], name: str, *, wait: bool = False) -> Iterator[None]:
    directory = Path(config["run_root"]) / ".execution-locks"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / (name + ".lock")).open("a+b") as lock:
        if os.name == "nt":
            import msvcrt
            # Windowsはロック領域の読み取りも拒否する。EOF以降もロックできるので初期化は不要。
            def acquire():
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            def release():
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            def acquire():
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            def release():
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        while True:
            try:
                acquire()
                break
            except OSError as exc:
                import errno
                if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise
                if not wait:
                    raise RuntimeError("この実行は別の処理で使用中です。終了してから再開してください。") from None
                from .execution import check_cancelled
                check_cancelled()
                sleep(.05)
        try:
            yield
        finally:
            release()


@contextmanager
def lock_run_record(path: Path) -> Iterator[None]:
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
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    for attempt in range(11):
        try:
            temporary.replace(path)
            return
        except PermissionError as exc:
            # An open reader on Windows can briefly prevent replacement of the old JSON.
            if getattr(exc, "winerror", None) not in {5, 32} or attempt == 10:
                raise
            sleep(.02)


def _notify_run_observer(record: dict[str, Any]) -> None:
    observer = run_observer.get()
    if observer is not None:
        observer(record)


def write_run_record(
    path: Path, record: dict[str, Any], *, timing_stage: int | None | Literal["status"] = "status",
    invalidate_migration_reviews: bool = False,
) -> None:
    update_timings(record, stage=timing_stage)
    with lock_run_record(path):
        if path.is_file():
            saved = json.loads(path.read_text(encoding="utf-8"))
            for field in ("token_usage", "migration_reviews", "migration_mappings", "hidden_from_dashboard"):
                if field in saved and not (invalidate_migration_reviews and field in {"migration_reviews", "migration_mappings"}):
                    record[field] = saved[field]
        replace_run_record(path, record)
    _notify_run_observer(record)


@contextmanager
def edit_run_record(path: Path) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
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


def validate_run_id(run_id: str) -> None:
    try:
        parsed_run_id = uuid.UUID(run_id)
    except ValueError as exc:
        raise ValueError("run ID must be a UUID printed by a previous harness command") from exc
    if str(parsed_run_id) != run_id:
        raise ValueError("run ID must use its canonical UUID format")


def _read_run_record(path: Path) -> Any:
    for attempt in range(11):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except PermissionError as exc:
            # Windows can refuse a reader during replacement; do not retry ordinary permissions errors.
            if getattr(exc, "winerror", None) not in {32, 33} or attempt == 10:
                raise
            sleep(.02)


def _load_run_identity(
    config: dict[str, Any], run_id: str,
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    validate_run_id(run_id)
    run_root = Path(config["run_root"]).resolve()
    run_path = run_root / run_id
    if run_path.is_symlink() or not run_path.is_dir() or run_path.resolve() != run_path:
        raise ValueError(f"run directory not found under configured run_root: {run_id}")
    record_path = run_path / "run.json"
    if record_path.is_symlink() or not record_path.is_file():
        raise ValueError("run record is missing or is a symlink")
    record = _read_run_record(record_path)
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
    return run_path, record, unit


def _saved_worktree(config: dict[str, Any], run_id: str, record: dict[str, Any]) -> Path:
    worktree_root = Path(config["worktree_root"]).resolve()
    if not isinstance(record.get("worktree_path"), str):
        raise ValueError("run record is missing its worktree path")
    worktree_path = Path(record["worktree_path"]).resolve()
    if worktree_path.parent != worktree_root or worktree_path.name != run_id:
        raise ValueError("run worktree path does not match the configured worktree root and run ID")
    return worktree_path


def load_saved_run(
    config: dict[str, Any], run_id: str,
) -> tuple[Path, Path, dict[str, Any], dict[str, Any]]:
    run_path, record, unit = _load_run_identity(config, run_id)
    worktree_path = _saved_worktree(config, run_id, record)
    return run_path, worktree_path, record, unit


def load_dashboard_record(config: dict[str, Any], run_id: str) -> tuple[Path, dict[str, Any]]:
    run_path, record, _ = _load_run_identity(config, run_id)
    validate_dashboard_worktree(config, run_id, record)
    return run_path, record


def validate_dashboard_worktree(config: dict[str, Any], run_id: str, record: dict[str, Any]) -> None:
    if "worktree_path" in record or record["status"] not in {"preparation_failed", "preparation_interrupted"}:
        _saved_worktree(config, run_id, record)
