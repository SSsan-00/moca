"""Run記録の検証と、進捗・使用量・レビュー判断の保存。"""

from __future__ import annotations

import errno
import json
import msvcrt
import math
import os
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date
from http.client import HTTPException
from pathlib import Path
from time import sleep
from typing import Any, Literal
from urllib.request import urlopen

from ..junie.usage import USAGE_FIELDS
from ..config import find_unit, target_paths
from ..saved_state import nonempty_text, saved_object, versionless_record
from .timings import update_timings

run_observer: ContextVar[Callable[[dict[str, Any]], None] | None] = ContextVar("run_observer", default=None)


@contextmanager
def execution_lock(config: dict[str, Any], name: str, *, wait: bool = False) -> Iterator[None]:
    directory = Path(config["run_root"]) / ".execution-locks"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / (name + ".lock")).open("a+b") as lock:
        # EOF以降の先頭1バイトをロックできるので、ファイルの初期化は不要。
        while True:
            try:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise
                if not wait:
                    raise RuntimeError("この実行は別の処理で使用中です。終了してから再開してください。") from None
                from ..execution import check_cancelled
                check_cancelled()
                sleep(.05)
        try:
            yield
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


@contextmanager
def lock_run_record(path: Path) -> Iterator[None]:
    # 置き換わるJSON本体とは別のファイルをロックする。run.jsonとeditor.jsonは別々にロックする。
    with path.with_suffix(".lock").open("ab") as lock:
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


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
    invalidate_migration_mappings: bool = False,
) -> None:
    update_timings(record, stage=timing_stage)
    with lock_run_record(path):
        if path.is_file():
            saved = read_run_record(path)
            if saved.get("junie_model_revision"):
                for field in ("junie_model", "junie_effort", "junie_model_revision"):
                    record[field] = saved[field]
            for field in ("token_usage", "cost_exchange", "migration_mappings", "hidden_from_dashboard"):
                if field in saved and not (invalidate_migration_mappings and field == "migration_mappings"):
                    record[field] = saved[field]
        replace_run_record(path, record)
    _notify_run_observer(record)


@contextmanager
def edit_run_record(path: Path) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
    with lock_run_record(path):
        record = read_run_record(path)
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
        lookup_rate = False
        with edit_run_record(run_path / "run.json") as (record, changes):
            rows = record.get("token_usage", [])
            if "cost_exchange" not in record:
                # 再開前の使用量には当時のレートがないため、現在のレートを過去に適用しない。
                prior_usage = any(row.get("responses", 0) for row in rows)
                changes["cost_exchange"] = {"status": "unavailable" if prior_usage else "pending"}
                lookup_rate = not prior_usage
            row = next((row for row in rows if row["phase"] == phase and row.get("capsule") == capsule), None)
            if row is None:
                row = {"phase": phase, "capsule": capsule, **dict.fromkeys(USAGE_FIELDS, 0)}
                rows.append(row)
            elif not any(delta):
                if not changes:
                    return
            previous_responses = row["responses"]
            for field, value in zip(USAGE_FIELDS, delta, strict=True):
                row[field] += value
            cost_known = getattr(delta, "cost_known", 0)
            if cost_known or "cost_known" in row:
                row["cost_usd"] = row.get("cost_usd", 0.0) + getattr(delta, "cost_usd", 0.0)
                row["cost_known"] = row.get("cost_known", 0) + cost_known
                row["cost_missing"] = row.get("cost_missing", previous_responses) + delta[-1] - cost_known
            changes["token_usage"] = rows
        if lookup_rate:
            rate = _exchange_rate()
            with edit_run_record(run_path / "run.json") as (record, changes):
                if record.get("cost_exchange", {}).get("status") == "pending":
                    changes["cost_exchange"] = rate

    return report


def _exchange_rate() -> dict[str, Any]:
    try:
        with urlopen("https://api.frankfurter.dev/v2/rate/usd/jpy", timeout=3) as response:
            data = json.loads(response.read(4096))
        rate = data["rate"]
        if data.get("base") != "USD" or data.get("quote") != "JPY" or type(rate) not in (int, float) or not 0 < rate < 10000 or not math.isfinite(rate):
            raise ValueError("invalid exchange rate")
        day = date.fromisoformat(data["date"]).isoformat()
        return {"status": "available", "rate": rate, "date": day, "source": "Frankfurter"}
    except (OSError, ValueError, TypeError, KeyError, HTTPException):
        return {"status": "unavailable"}


def validate_run_id(run_id: str) -> None:
    try:
        parsed_run_id = uuid.UUID(run_id)
    except ValueError as exc:
        raise ValueError("run ID must be a UUID printed by a previous harness command") from exc
    if str(parsed_run_id) != run_id:
        raise ValueError("run ID must use its canonical UUID format")


def read_run_record(path: Path) -> dict[str, Any]:
    for attempt in range(11):
        try:
            record = saved_object(path.read_text(encoding="utf-8"), "run.json")
            versionless_record(record, "run.json")
            _validate_run_fields(record)
            return record
        except UnicodeError as exc:
            raise ValueError("run.json の文字コードが無効です。元の記録は変更していません。") from exc
        except PermissionError as exc:
            # Windows can refuse a reader during replacement; do not retry ordinary permissions errors.
            if getattr(exc, "winerror", None) not in {32, 33} or attempt == 10:
                raise
            sleep(.02)


def run_model_options(run_path: Path, record: dict[str, Any], config: dict[str, Any]) -> dict[str, str | None]:
    saved = read_run_record(run_path / "run.json")
    return {"model": saved.get("junie_model", record.get("junie_model", config.get("junie_model"))),
            "effort": saved.get("junie_effort", record.get("junie_effort"))}


def _validate_run_fields(record: dict[str, Any]) -> None:
    if "status" in record and not nonempty_text(record["status"]):
        raise ValueError("run record contains an invalid status")
    counters = ("completed_capsules", "context_capsules", "current_capsule", "resume_attempts")
    for field in counters:
        if field in record and (type(record[field]) is not int or record[field] < (field == "current_capsule")):
            raise ValueError(f"run record has invalid capsule progress ({field})")
    for field, kind in (("verification", dict), ("target_apply_pending", dict), ("repair_pending", dict),
                        ("migration_mappings", dict), ("repair_history", list)):
        value = record.get(field)
        if value is not None and not isinstance(value, kind):
            raise ValueError(f"run record contains an invalid {field}")
    for field in ("build_exit_code", "checker_exit_code"):
        value = record.get(field)
        if value is not None and type(value) is not int:
            raise ValueError(f"run record contains an invalid {field}")
    if record.get("junie_model_revision") is not None:
        if not nonempty_text(record["junie_model_revision"]) or any(
            field not in record or (record[field] is not None and not isinstance(record[field], str))
            for field in ("junie_model", "junie_effort")
        ):
            raise ValueError("run record contains inconsistent Junie model options")


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
    record = read_run_record(record_path)
    if "status" not in record:
        raise ValueError("run record is missing its status")
    if record.get("run_id") != run_id or record.get("project_root") != config["project_root"]:
        raise ValueError("run record does not match the configured project")
    unit_id = record.get("unit_id")
    if not nonempty_text(unit_id):
        raise ValueError("run record is missing its migration unit ID")
    unit = find_unit(config, unit_id)
    saved_targets = record.get("targets", [record.get("target")])
    if (not nonempty_text(record.get("target")) or Path(record["target"]) != Path(unit["target"])
            or not isinstance(saved_targets, list) or any(not nonempty_text(path) for path in saved_targets)
            or tuple(Path(path) for path in saved_targets) != target_paths(unit)):
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
