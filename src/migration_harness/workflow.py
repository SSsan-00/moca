from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from contextlib import closing
from contextvars import ContextVar
from dataclasses import dataclass
import codecs
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from threading import RLock
import uuid
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Iterator, TypedDict

# チェックポイントの読込に使う型を制限する設定は、LangGraphのimport前に適用する。
os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

run_observer: ContextVar[Callable[[dict[str, Any]], None] | None] = ContextVar("run_observer", default=None)
worktree_source_lock = RLock()


class PrepareState(TypedDict, total=False):
    """準備グラフのノード間で受け渡す項目を定義する。"""
    config: dict[str, Any]
    unit_id: str
    run_id: str
    worktree_path: str
    run_path: str
    context_path: str
    context_paths: list[str]
    context_part_chars: list[int]
    context_chars: int
    capsule_path: str
    capsule_count: int
    source_boundary_review_required: bool


class RunInterrupted(KeyboardInterrupt):
    """中断後の案内に使うRun IDを保持する。"""

    def __init__(self, run_id: str) -> None:
        """再開対象のRun IDを例外に関連付ける。"""
        super().__init__(run_id)
        self.run_id = run_id


def _unit(config: dict[str, Any], unit_id: str) -> dict[str, Any]:
    """設定で許可された移行単位だけを解決する。"""
    for unit in config["units"]:
        if unit["id"] == unit_id:
            return unit
    raise ValueError(f"unknown migration unit: {unit_id}")


def _target_paths(unit: dict[str, Any]) -> tuple[Path, ...]:
    """単一移行先を指定した旧設定も同じ順序付き形式で扱う。"""
    return tuple(Path(name) for name in unit.get("targets", [unit["target"]]))


def _has_symlink_component(root: Path, relative_path: Path) -> bool:
    """中間ディレクトリも含めてリンクの有無を調べる。"""
    current = root
    for part in relative_path.parts:
        current /= part
        if current.is_symlink():
            return True
    return False


def _validate(state: PrepareState) -> dict[str, Any]:
    """作業場所を作る前に入力ファイルがアプリ内にあることを確認する。"""
    config = state["config"]
    unit = _unit(config, state["unit_id"])
    root = Path(config["project_root"])
    for source in unit["inputs"]:
        path = (root / source["path"]).resolve()
        if not path.is_relative_to(root):
            raise ValueError(f"input escapes project root: {source['path']}")
        if not path.is_file():
            raise ValueError(f"input file not found: {source['path']}")
    return {}


def _create_worktree(state: PrepareState) -> dict[str, Any]:
    """指定リビジョンの分離したworktreeと初期Run記録を用意する。"""
    config = state["config"]
    root = Path(config["project_root"])
    worktree = Path(config["worktree_root"]) / state["run_id"]
    worktree.parent.mkdir(parents=True, exist_ok=True)
    if worktree.exists():
        raise FileExistsError(f"worktree path already exists: {worktree}")
    subprocess.run(
        ["git", "-C", str(root), "worktree", "add", "--detach", str(worktree), config["base_ref"]],
        check=True,
        capture_output=True,
        text=True,
    )
    unit = _unit(config, state["unit_id"])
    run_path = Path(state["run_path"])
    record = {
        "run_id": state["run_id"],
        "unit_id": unit["id"],
        "status": "prepared",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "project_root": config["project_root"],
        "worktree_path": str(worktree),
        "capsule_path": str(run_path / "capsule"),
        "context_chars": state["context_chars"],
        "context_part_chars": state["context_part_chars"],
        "context_capsules": state["capsule_count"],
        "max_context_chars": config["max_context_chars"],
        "junie_max_steps": config["junie_max_steps"],
        "resume_attempts": 0,
        "segmentation_policy": "line_with_character_fallback_and_overlap",
        "source_boundary_review_required": state["capsule_count"] > 1,
        "target": unit["target"],
        "completed_capsules": 0,
        "current_capsule": 1,
    }
    if "targets" in unit:
        record["targets"] = unit["targets"]
    _write_run_record(run_path / "run.json", record)
    return {"worktree_path": str(worktree)}


def _text_fingerprint(path: Path, encoding: str) -> bytes | None:
    """厳密にデコードできる場合だけ、文字列を比較用に要約する。"""
    decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(64 * 1024):
                digest.update(decoder.decode(chunk).encode("utf-8"))
        digest.update(decoder.decode(b"", final=True).encode("utf-8"))
    except UnicodeDecodeError:
        return None
    return digest.digest()


def _input_encoding(path: Path, requested: str) -> str:
    """BOMを優先し、UTF-8とEUC-JPの判定が曖昧な場合は明示指定を求める。"""
    if requested != "auto":
        return requested
    with path.open("rb") as stream:
        has_utf8_bom = stream.read(3) == b"\xef\xbb\xbf"
    if has_utf8_bom:
        return "utf-8-sig"
    utf8_fingerprint = _text_fingerprint(path, "utf-8")
    euc_jp_fingerprint = _text_fingerprint(path, "euc-jp")
    if utf8_fingerprint is None and euc_jp_fingerprint is None:
        raise ValueError(f"{path}: input is neither valid UTF-8 nor valid EUC-JP; set encoding explicitly")
    if utf8_fingerprint is None:
        return "euc-jp"
    if euc_jp_fingerprint is None:
        return "utf-8"
    if utf8_fingerprint == euc_jp_fingerprint:
        return "utf-8"
    raise ValueError(f"{path}: encoding is ambiguous between UTF-8 and EUC-JP; set input encoding explicitly")


def _selected_lines(path: Path, start: int, end: int, encoding: str) -> Iterator[tuple[int, str]]:
    """行末を保持したまま、元ファイルの行番号付きで指定範囲を返す。"""
    selected_count = 0
    reached_end = False
    with path.open("r", encoding=encoding, errors="strict", newline="") as stream:
        for number, line in enumerate(stream, start=1):
            if start <= number <= end:
                selected_count += 1
                yield number, line
            if number >= end:
                reached_end = True
                break
    if not selected_count:
        raise ValueError(f"requested lines {start}-{end} do not exist in {path}")
    if not reached_end:
        raise ValueError(f"requested line {end} does not exist in {path}")


def _context_header(unit: dict[str, Any]) -> str:
    """共通ヘッダに移行単位と編集を許可したファイルを記載する。"""
    return "\n".join([
        f"# Migration unit: {unit['id']}",
        "",
        "Targets in worktree: " + ", ".join(f"`{path.as_posix()}`" for path in _target_paths(unit)),
        "",
        "Only the listed excerpts are included. The application source is not copied into this public harness repository.",
        "",
    ])


def _max_backtick_run(text: str) -> int:
    """ソース中のバッククォートでMarkdownの囲みが閉じないよう最長の連続数を求める。"""
    return max((len(run) for run in re.findall(r"`+", text)), default=0)


def _group_markdown_frame(group: dict[str, Any]) -> tuple[str, str]:
    """予算の計算と出力に使うMarkdownの前後を組み立てる。"""
    item = group["item"]
    fence = "`" * max(3, group["max_backticks"] + 1)
    opening = (
        f"## {item['role']} — `{item['path']}` lines "
        f"{group['start_line']}-{group['end_line']}\n\n{fence}text\n"
    )
    closing = ("" if group["ends_with_newline"] else "\n") + f"{fence}\n\n"
    return opening, closing


def _group_size(group: dict[str, Any]) -> int:
    """本文を連結せず、見出しと囲みを含む出力文字数を求める。"""
    opening, closing = _group_markdown_frame(group)
    return len(opening) + group["content_chars"] + len(closing)


def _new_group(input_index: int, item: dict[str, Any], line_number: int, record: str) -> dict[str, Any]:
    """1件目の抜粋と、次の行を詰めるための文字数を保持する。"""
    return {
        "input_index": input_index,
        "item": item,
        "start_line": line_number,
        "end_line": line_number,
        "records": [record],
        "content_chars": len(record),
        "max_backticks": _max_backtick_run(record),
        "ends_with_newline": record.endswith(("\n", "\r")),
    }


def _append_context_record(
    groups: list[dict[str, Any]],
    input_index: int,
    item: dict[str, Any],
    line_number: int,
    record: str,
    base_chars: int,
    max_chars: int,
) -> bool:
    """上限内に収まる場合だけ抜粋を追加し、入らない場合は編集中のグループを保つ。"""
    record_backticks = _max_backtick_run(record)
    last = groups[-1] if groups and groups[-1]["input_index"] == input_index else None
    if last and line_number <= last["end_line"] + 1:
        candidate = {
            **last,
            "end_line": max(last["end_line"], line_number),
            "content_chars": last["content_chars"] + len(record),
            "max_backticks": max(last["max_backticks"], record_backticks),
            "ends_with_newline": record.endswith(("\n", "\r")),
        }
        total_chars = base_chars + sum(_group_size(group) for group in groups[:-1]) + _group_size(candidate)
        if total_chars > max_chars:
            return False
        candidate["records"].append(record)
        groups[-1] = candidate
        return True

    candidate = _new_group(input_index, item, line_number, record)
    if base_chars + sum(_group_size(group) for group in groups) + _group_size(candidate) > max_chars:
        return False
    groups.append(candidate)
    return True


def _render_groups(groups: list[dict[str, Any]]) -> str:
    """行末を保持して表示用の抜粋を連結する。"""
    parts = []
    for group in groups:
        opening, closing = _group_markdown_frame(group)
        parts.extend((opening, *group["records"], closing))
    return "".join(parts)


def _manifest_source(group: dict[str, Any]) -> dict[str, Any]:
    """抜粋を元のファイル・行範囲・行内の担当範囲に結び付ける。"""
    return {
        "role": group["item"]["role"],
        "path": group["item"]["path"],
        **({"symbol": group["item"]["symbol"]} if "symbol" in group["item"] else {}),
        "encoding": group["item"]["resolved_encoding"],
        "start_line": group["start_line"],
        "end_line": group["end_line"],
        "character_slices": [
            {
                "line": int(match[1]),
                "focus_start_column": int(match[2]),
                "focus_end_column": int(match[3]),
                "context_start_column": int(match[4]),
                "context_end_column": int(match[5]),
            }
            for record in group["records"]
            if (match := re.match(r"\s*(\d+) focus chars (\d+)-(\d+) context chars (\d+)-(\d+) \|", record))
        ],
    }


def _prepare_inputs(
    root: Path, capsule: Path, inputs: list[dict[str, Any]],
) -> tuple[list[tuple[dict[str, Any], Path]], list[dict[str, str]]]:
    """PHPをRunに固定し、入力ごとの文字コードを確定する。"""
    prepared = []
    php_sources: dict[str, dict[str, str]] = {}
    for item in inputs:
        name = item["path"]
        source_path = (root / name).resolve()
        if not source_path.is_relative_to(root):
            raise ValueError(f"input escapes project root: {name}")
        pin = None
        if Path(name).suffix.lower() == ".php":
            pin = php_sources.get(name)
            if pin is None:
                relative = f"php-sources/{len(php_sources) + 1:04d}.php"
                snapshot = capsule / relative
                snapshot.parent.mkdir(exist_ok=True)
                before = source_path.stat()
                shutil.copyfile(source_path, snapshot)
                after = (root / name).stat()
                if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
                    raise ValueError(f"PHP input changed while preparing its snapshot: {name}")
                pin = {"path": name, "snapshot": relative, "sha256": _sha256_file(snapshot)}
                php_sources[name] = pin
            # カプセルと比較に同じPHPバイト列を使う。
            source_path = capsule / pin["snapshot"]
        encoding = _input_encoding(source_path, item["encoding"])
        if pin is not None:
            pin.setdefault("encoding", encoding)
        prepared.append(({**item, "resolved_encoding": encoding}, source_path))
    return prepared, list(php_sources.values())


def _prepare_context(state: PrepareState) -> dict[str, Any]:
    """共通資料の予算を先に取り、残りに移行対象を詰める。"""
    config = state["config"]
    unit = _unit(config, state["unit_id"])
    root = Path(config["project_root"])
    run_path = Path(config["run_root"]) / state["run_id"]
    capsule = run_path / "capsule"
    capsule.mkdir(parents=True, exist_ok=False)

    max_chars = config["max_context_chars"]
    inputs, php_sources = _prepare_inputs(root, capsule, unit["inputs"])
    header = _context_header(unit)
    shared_groups: list[dict[str, Any]] = []
    segment_inputs = []
    shared_intro = (
        "# Shared context\n\n"
        "Apply the shared rules and constraints to every segment. Read these excerpts as reference.\n\n"
    )
    segment_intro = "# Segment inputs\n\n"
    shared_base_chars = len(header) + len(shared_intro) + len(segment_intro)
    for input_index, (item, source_path) in enumerate(inputs):
        if item["role"] != "migration_rules" and not item.get("shared", False):
            segment_inputs.append((input_index, item, source_path))
            continue
        lines = _selected_lines(source_path, item["start_line"], item["end_line"], item["resolved_encoding"])
        for number, line in lines:
            if not _append_context_record(
                shared_groups, input_index, item, number, f"{number:>6} | {line}", shared_base_chars, max_chars,
            ):
                raise ValueError(f"shared context exceeds max_context_chars ({max_chars}): {item['path']}")
    if not segment_inputs:
        raise ValueError("migration unit requires at least one input outside shared context")
    shared_context = shared_intro + _render_groups(shared_groups) + segment_intro if shared_groups else ""
    prefix = header + shared_context
    base_chars = len(prefix)
    overlap_chars = min(512, (max_chars - len(shared_context)) // 20)
    budget_error = (
        "max_context_chars is too small for a segment input after including shared context"
        if shared_groups else "max_context_chars is too small for the context headers"
    )
    groups: list[dict[str, Any]] = []
    context_paths: list[str] = []
    context_part_chars: list[int] = []
    manifest_parts: list[dict[str, Any]] = []
    split_line_coverage: list[dict[str, Any]] = []

    def flush_capsule() -> None:
        """確定した抜粋と実際の文字数を保存して、次のカプセルを空にする。"""
        if not groups:
            return
        context = prefix + _render_groups(groups)
        if len(context) > max_chars:
            raise RuntimeError("internal error: generated context capsule exceeds its configured limit")
        part_number = len(context_paths) + 1
        part_dir = capsule / f"part-{part_number:04d}"
        part_dir.mkdir()
        context_path = part_dir / "context.md"
        # 原文の改行と文字数をWindowsでも保持する。
        context_path.write_text(context, encoding="utf-8", newline="")
        context_paths.append(str(context_path))
        context_part_chars.append(len(context))
        manifest_parts.append({
            "index": part_number,
            "path": str(context_path),
            "chars": len(context),
            "sources": [_manifest_source(group) for group in groups],
        })
        groups.clear()

    for input_index, item, source_path in segment_inputs:
        lines = _selected_lines(source_path, item["start_line"], item["end_line"], item["resolved_encoding"])
        for number, line in lines:
            record = f"{number:>6} | {line}"
            if _append_context_record(groups, input_index, item, number, record, base_chars, max_chars):
                continue
            if groups:
                flush_capsule()
                if _append_context_record(groups, input_index, item, number, record, base_chars, max_chars):
                    continue

            line_body = line.rstrip("\r\n")
            line_ending = line[len(line_body):]
            if not line_body:
                raise ValueError(budget_error)

            offset = 0
            line_overlap = overlap_chars
            focus_ranges: list[tuple[int, int]] = []
            while offset < len(line_body):
                low = 1
                high = len(line_body) - offset
                best_length = 0
                best_record = ""
                while low <= high:
                    piece_length = (low + high) // 2
                    end_column = offset + piece_length
                    context_start = max(0, offset - line_overlap)
                    context_end = min(len(line_body), end_column + line_overlap)
                    suffix = (line_ending or "\n") if end_column == len(line_body) else "\n"
                    candidate_record = (
                        f"{number:>6} focus chars {offset + 1}-{end_column} "
                        f"context chars {context_start + 1}-{context_end} | "
                        f"{line_body[context_start:context_end]}{suffix}"
                    )
                    if _append_context_record([], input_index, item, number, candidate_record, base_chars, max_chars):
                        best_length = piece_length
                        best_record = candidate_record
                        low = piece_length + 1
                    else:
                        high = piece_length - 1
                if best_length == 0:
                    if line_overlap:
                        line_overlap = 0
                        continue
                    raise ValueError(budget_error)
                if not _append_context_record(groups, input_index, item, number, best_record, base_chars, max_chars):
                    flush_capsule()
                    if not _append_context_record(groups, input_index, item, number, best_record, base_chars, max_chars):
                        raise RuntimeError("internal error: could not fit a split source line")
                focus_ranges.append((offset + 1, offset + best_length))
                offset += best_length
            expected_column = 1
            for start_column, end_column in focus_ranges:
                if start_column != expected_column or end_column < start_column:
                    raise RuntimeError(f"internal error: source line {number} has a gap or overlap in its focus ranges")
                expected_column = end_column + 1
            if expected_column != len(line_body) + 1:
                raise RuntimeError(f"internal error: source line {number} is not fully covered by its focus ranges")
            split_line_coverage.append({
                "path": item["path"],
                "line": number,
                "characters": len(line_body),
                "slices": len(focus_ranges),
            })

    flush_capsule()
    manifest = {
        "unit_id": unit["id"],
        "max_context_chars": max_chars,
        "segmentation_policy": "line_with_character_fallback_and_overlap",
        "syntax_boundaries_analyzed": False,
        "split_line_coverage": split_line_coverage,
        "total_context_chars": sum(context_part_chars),
        "shared_sources": [_manifest_source(group) for group in shared_groups],
        "php_sources": php_sources,
        "parts": manifest_parts,
    }
    (capsule / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return {
        "run_path": str(run_path),
        "context_path": context_paths[0],
        "context_paths": context_paths,
        "context_part_chars": context_part_chars,
        "context_chars": sum(context_part_chars),
        "capsule_path": str(capsule),
        "capsule_count": len(context_paths),
        "source_boundary_review_required": len(context_paths) > 1,
    }


def _graph():
    """入力と上限の検証を終えてからworktreeを作る準備グラフを構成する。"""
    builder = StateGraph(PrepareState)
    builder.add_node("validate", _validate)
    builder.add_node("create_worktree", _create_worktree)
    builder.add_node("prepare_context", _prepare_context)
    builder.add_edge(START, "validate")
    # コンテキスト作成と上限確認が成功してからworktreeを作る。
    builder.add_edge("validate", "prepare_context")
    builder.add_edge("prepare_context", "create_worktree")
    builder.add_edge("create_worktree", END)
    return builder


def prepare_run(config: dict[str, Any], unit_id: str) -> dict[str, Any]:
    """Run IDを発行し、準備グラフとチェックポイントの寿命を管理する。"""
    run_id = str(uuid.uuid4())
    run_root = Path(config["run_root"])
    run_root.mkdir(parents=True, exist_ok=True)
    checkpoint_db = run_root / "checkpoints.sqlite"
    with closing(sqlite3.connect(checkpoint_db, check_same_thread=False)) as connection, connection:
        checkpointer = SqliteSaver(connection)
        app = _graph().compile(checkpointer=checkpointer)
        result = app.invoke(
            {"config": config, "unit_id": unit_id, "run_id": run_id},
            {"configurable": {"thread_id": run_id}},
        )
    return {
        "run_id": run_id,
        "worktree_path": result["worktree_path"],
        "context_path": result["context_path"],
        "context_paths": result["context_paths"],
        "context_part_chars": result["context_part_chars"],
        "context_chars": result["context_chars"],
        "capsule_path": result["capsule_path"],
        "capsule_count": result["capsule_count"],
        "source_boundary_review_required": result["source_boundary_review_required"],
        "max_context_chars": config["max_context_chars"],
        "run_path": str(run_root / run_id),
    }


def _sha256_file(path: Path) -> str:
    """ファイル全体を一度に保持せず、保存バイト列を照合するハッシュを計算する。"""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(64 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _write_run_record(path: Path, record: dict[str, Any]) -> None:
    """Run記録を置き換えてから、進捗の購読側へ状態を通知する。"""
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    observer = run_observer.get()
    if observer is not None:
        observer(record)


def _safe_target(root: Path, relative: Path) -> Path:
    """シンボリックリンクや作業場所外への書き込みを防ぐ。"""
    target = root / relative
    if _has_symlink_component(root, relative) or not target.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"target escapes its workspace or is a symlink: {relative}")
    if target.exists() and not target.is_file():
        raise ValueError(f"target is not a regular file: {relative}")
    return target


def _target_hash(path: Path) -> str | None:
    """移行先の未作成状態をハッシュの不一致と区別して扱う。"""
    return _sha256_file(path) if path.is_file() else None


def _finish_pending_targets(
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
            source = _safe_target(agent_root, relative[name])
            destination = _safe_target(worktree, relative[name])
            current = _target_hash(destination)
            if current == version["after"]:
                continue
            if current != version["before"] or _target_hash(source) != version["after"]:
                raise ValueError(f"cannot recover target application after an external change: {name}")
            remaining.append((source, destination, version))
        # ファイル単位で置き換え、途中で停止しても記録したハッシュを使って残りを反映する。
        for source, destination, version in remaining:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(dir=destination.parent, delete=False) as stream:
                temporary = Path(stream.name)
            try:
                shutil.copyfile(source, temporary)
                if _target_hash(temporary) != version["after"] or _target_hash(destination) != version["before"]:
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
        _write_run_record(record_path, record)


def _apply_agent_targets(
    agent_root: Path, worktree: Path, targets: tuple[Path, ...],
    before: dict[str, str | None], record: dict[str, Any], record_path: Path,
) -> None:
    """外部編集を検出し、複数ファイルを反映する前に復旧用の記録を残す。"""
    with worktree_source_lock:
        for name, digest in before.items():
            if _target_hash(_safe_target(worktree, Path(name))) != digest:
                raise ValueError(f"worktree target changed during Junie execution: {name}")
        pending = {}
        for relative in targets:
            name = relative.as_posix()
            after = _target_hash(_safe_target(agent_root, relative))
            if after is not None and after != before[name]:
                pending[name] = {"before": before[name], "after": after}
        if pending:
            record["target_apply_pending"] = pending
            _write_run_record(record_path, record)
            _finish_pending_targets(agent_root, worktree, targets, record, record_path)


def _unlisted_workspace_files(root: Path, allowed: set[Path]) -> dict[Path, str]:
    """編集対象外のファイルを記録し、Junie呼び出し前後で差分を検出できるようにする。"""
    files = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if relative in allowed:
            continue
        if path.is_symlink():
            files[relative] = "symlink:" + os.readlink(path)
        elif path.is_file():
            files[relative] = _sha256_file(path)
    return files


def _has_completion_record(path: Path, token: str) -> bool:
    """現在の呼び出しが残した完了トークンだけを受理する。"""
    if path.is_symlink() or not path.is_file():
        return False
    expected = token.encode("ascii")
    try:
        with path.open("rb") as stream:
            value = stream.read(len(expected) + 3)
    except OSError:
        return False
    return value in (expected, expected + b"\n", expected + b"\r\n")


def _close_junie(process: subprocess.Popen) -> int:
    """Junieの終了を待ち、次のカプセルと端末操作が重ならないようにする。"""
    if process.poll() is not None:
        return process.returncode
    try:
        if os.name == "nt":
            # Windowsのランチャーが起動した子プロセスも閉じてから次へ進む。
            taskkill = Path(os.environ["SystemRoot"]) / "System32" / "taskkill.exe"
            subprocess.run(
                [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                check=True, capture_output=True, timeout=10,
            )
        else:
            process.terminate()
        return process.wait(timeout=10)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        if process.poll() is not None:
            return process.returncode
        raise OSError("Could not close Junie. Close the current Junie session before resuming.") from exc


def _run_interactive_junie(
    command: list[str], completion_path: Path, token: str, target: Path | tuple[Path, ...],
) -> int:
    """対話用の端末を引き継ぎ、成果物と完了記録がそろえばセッションを閉じる。"""
    targets = (target,) if isinstance(target, Path) else target
    root = completion_path.parent
    restore_terminal = _save_terminal_mode()
    # 標準入出力を引き継ぎ、承認や追加入力はJunieの画面で受け付ける。
    process = subprocess.Popen(command)
    try:
        while True:
            try:
                return process.wait(timeout=0.25)
            except subprocess.TimeoutExpired:
                if (
                    _has_completion_record(completion_path, token)
                    and all(
                        path.is_file() and not _has_symlink_component(root, path.relative_to(root))
                        and path.resolve().is_relative_to(root.resolve())
                        for path in targets
                    )
                ):
                    return _close_junie(process)
    except KeyboardInterrupt:
        _close_junie(process)
        raise
    finally:
        if process.poll() is not None:
            restore_terminal()


def _save_terminal_mode() -> Callable[[], None]:
    """強制終了で端末設定が残る場合に備えて、起動前の復元処理を作る。"""
    if os.name == "nt":
        import ctypes
        import msvcrt
        from ctypes import wintypes

        kernel = ctypes.windll.kernel32
        modes = []
        for stream in (sys.stdin, sys.stdout, sys.stderr):
            if stream.isatty():
                handle = wintypes.HANDLE(msvcrt.get_osfhandle(stream.fileno()))
                mode = wintypes.DWORD()
                if kernel.GetConsoleMode(handle, ctypes.byref(mode)):
                    modes.append((handle, mode))

        def restore() -> None:
            """Junieを起動する前の端末設定に戻す。"""
            for handle, mode in modes:
                kernel.SetConsoleMode(handle, mode)

        return restore

    import termios

    fd = sys.stdin.fileno()
    try:
        mode = termios.tcgetattr(fd)
    except termios.error:
        return lambda: None

    def restore() -> None:
        """Junieを起動する前の端末設定に戻す。"""
        termios.tcsetattr(fd, termios.TCSANOW, mode)

    return restore


def _run_prepared_with_junie(
    config: dict[str, Any],
    unit: dict[str, Any],
    run_path: Path,
    result: dict[str, Any],
    record: dict[str, Any],
    *,
    resuming: bool,
) -> dict[str, Any]:
    """保存済みの編集と進捗から未完了のカプセルを順に処理する。"""
    worktree_root = Path(result["worktree_path"]).resolve()
    if not worktree_root.is_dir() or not worktree_root.is_relative_to(Path(config["worktree_root"]).resolve()):
        raise ValueError(f"run worktree is missing or outside configured worktree root: {worktree_root}")
    relative_targets = _target_paths(unit)
    for relative in relative_targets:
        first = relative.parts[0].lower()
        if first == ".migration-context.md" or first.startswith(("migration-complete-", "checker-findings-")):
            raise ValueError("target path conflicts with a reserved file in the agent workspace")
    agent_root = run_path / "agent-workspace"
    if agent_root.is_symlink() or not agent_root.resolve().is_relative_to(run_path.resolve()):
        raise ValueError("agent workspace path escapes the run directory")
    record_path = run_path / "run.json"
    resume_attempts = record.get("resume_attempts", 0)
    if resuming and (type(resume_attempts) is not int or resume_attempts < 0):
        raise ValueError("run record contains an invalid resume-attempt count")
    if agent_root.exists() and (not agent_root.is_dir() or not resuming and record["status"] != "prepared"):
        raise ValueError("agent workspace is invalid or already exists for this run")
    if not agent_root.exists():
        if record["status"] != "prepared":
            raise ValueError("agent workspace is missing; this run cannot be resumed safely")
        agent_root.mkdir()

    _finish_pending_targets(agent_root, worktree_root, relative_targets, record, record_path)
    worktree_targets = tuple(_safe_target(worktree_root, path) for path in relative_targets)
    agent_targets = tuple(_safe_target(agent_root, path) for path in relative_targets)
    hashes = {path.as_posix(): _target_hash(file) for path, file in zip(relative_targets, worktree_targets, strict=True)}
    if resuming:
        prior_hashes = record.get("target_hashes", {relative_targets[0].as_posix(): record.get("target_sha256")})
        if hashes != prior_hashes:
            raise ValueError("worktree targets changed since the last recorded Junie result; inspect and reconcile before resuming")
    for worktree_target, agent_target in zip(worktree_targets, agent_targets, strict=True):
        agent_target.parent.mkdir(parents=True, exist_ok=True)
        if not agent_target.exists() and worktree_target.is_file():
            shutil.copyfile(worktree_target, agent_target)

    record["agent_workspace_path"] = str(agent_root)
    if resuming:
        record["resume_attempts"] = resume_attempts + 1
    record["status"] = "junie_running"
    record.setdefault("completed_capsules", 0)
    record.setdefault("current_capsule", record["completed_capsules"] + 1)
    record["target_hashes"] = hashes
    if not resuming:
        record["target_sha256"] = hashes[relative_targets[0].as_posix()]
        record["target_applied"] = False
    _write_run_record(record_path, record)

    agent_context = agent_root / ".migration-context.md"
    exit_code = None
    start_index = record["completed_capsules"] + 1
    for index, context_path in enumerate(result["context_paths"][start_index - 1 :], start=start_index):
        shutil.copyfile(context_path, agent_context)
        completion_token = uuid.uuid4().hex
        # 隠しファイルはJunieが書き込み前に承認を求めるため、通常の名前を使う。
        completion_path = agent_root / f"migration-complete-{completion_token}.txt"
        resume_instruction = ""
        if resuming and index == start_index:
            resume_instruction = (
                "This capsule was interrupted. Compare its source ranges with the saved target, "
                "keep completed edits, and migrate only what is still missing. "
            )
        prompt = (
            f"Read `{agent_context}`. This is context segment {index} of {result['capsule_count']} "
            f"for migration unit `{unit['id']}`. The configured target files are "
            + ", ".join(f"`{path.as_posix()}`" for path in relative_targets) + ". "
            "Preserve work from earlier segments and continue only with this segment. "
            "Edit only the configured targets and the completion record described below. "
            "Ensure every configured target exists before writing a completion record. "
            f"{resume_instruction}"
            "The source excerpt may begin or end inside a function or control structure. Use its original line numbers; "
            "do not treat the excerpt boundary as a PHP statement boundary or add closing syntax just for the excerpt end. "
            "For `focus chars START-END context chars CONTEXT_START-CONTEXT_END` records, migrate only the focus range. "
            "The surrounding context range overlaps adjacent segments and is reference-only; do not migrate it twice. "
            "These records are slices of one physical source line, and displayed line breaks are not source newlines. "
            "Do not infer text outside the context range. "
            "Do not access files outside this workspace. Do not run builds, tests, or external checkers. "
            "After verifying that all required changes for this capsule are saved, write exactly "
            f"`{completion_token}` as plain text to `{completion_path}` as your final file action. "
            "Leave this record absent while anything remains, including when blocked or at the step limit. "
            "The harness closes this session when it sees the record. Finish all edits and checks before writing it. "
            "If interrupted, preserve the edits and report what remains. "
            "Claim the entire migration is complete only after the final capsule."
        )
        command = ["junie", "--project", str(agent_root)]
        max_steps = record.get("junie_max_steps", config["junie_max_steps"])
        if max_steps is not None:
            command.extend(["--max-steps", str(max_steps)])
        command.extend(["--prompt", prompt])
        record["current_capsule"] = index
        _write_run_record(record_path, record)
        allowed = {*relative_targets, completion_path.relative_to(agent_root)}
        other_files = _unlisted_workspace_files(agent_root, allowed)
        interrupted = False
        try:
            try:
                exit_code = _run_interactive_junie(
                    command, completion_path, completion_token, agent_targets[0] if len(agent_targets) == 1 else agent_targets,
                )
            except KeyboardInterrupt:
                interrupted = True
            if _unlisted_workspace_files(agent_root, allowed) != other_files:
                raise ValueError("Junie changed files outside the configured targets; inspect the agent workspace")
            _apply_agent_targets(
                agent_root, worktree_root, relative_targets,
                record["target_hashes"], record, record_path,
            )
        except (OSError, ValueError) as exc:
            record.update(status="junie_failed", error=str(exc))
            _write_run_record(record_path, record)
            raise
        if interrupted:
            record.update(status="interrupted", error="interrupted by user; current capsule can be resumed")
            _write_run_record(record_path, record)
            raise RunInterrupted(record["run_id"])

        record["junie_exit_code"] = exit_code
        missing = [path.as_posix() for path, file in zip(relative_targets, agent_targets, strict=True) if not file.is_file()]
        if missing or not _has_completion_record(completion_path, completion_token):
            record["status"] = "interrupted" if len(missing) < len(agent_targets) else "junie_failed"
            record["error"] = (
                f"Junie did not produce configured targets: {', '.join(missing)}" if missing
                else "Junie did not leave a valid completion record for this capsule"
            )
            _write_run_record(record_path, record)
            break

        record["completed_capsules"] = index
        record["current_capsule"] = min(index + 1, result["capsule_count"])
        record["status"] = "junie_finished" if index == result["capsule_count"] else "junie_running"
        record.pop("error", None)
        _write_run_record(record_path, record)

    if exit_code is None:
        raise RuntimeError("context preparation produced no capsules")
    result = {
        **result,
        "junie_exit_code": exit_code,
        "agent_workspace_path": str(agent_root),
        "completed_capsules": record["completed_capsules"],
        "status": record["status"],
        "current_capsule": record["current_capsule"],
        "error": record.get("error"),
    }
    if record["status"] == "junie_finished":
        return _verify_worktree(config, unit, result, record)
    return result


def _build_worktree(config: dict[str, Any], result: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    """対象worktreeをビルドし、以前のチェッカー結果を更新対象から外す。"""
    worktree = Path(result["worktree_path"])
    run_path = Path(result["run_path"])
    record_path = run_path / "run.json"
    log_path = run_path / "build.log"
    build_project = config["build_project"]
    command = ["dotnet", "build"]

    record.update(status="building", build_exit_code=None, build_log_path=str(log_path))
    for key in ("error", "checker_exit_code", "checker_log_path", "review_file_hashes"):
        record.pop(key, None)
    _write_run_record(record_path, record)
    print(f"Building in {worktree} (log: {log_path})", flush=True)
    try:
        # MSBuildの出力は実行データ側に残し、アプリの差分に混ぜない。
        with log_path.open("wb") as log:
            try:
                if build_project is not None:
                    target = (worktree / build_project).resolve()
                    if not target.is_relative_to(worktree.resolve()) or not target.is_file():
                        raise ValueError(f"build project is missing or outside the worktree: {build_project}")
                    command.append(str(target))
                completed = subprocess.run(command, cwd=worktree, stdout=log, stderr=subprocess.STDOUT, check=False)
            except (OSError, ValueError) as exc:
                log.write(f"{exc}\n".encode("utf-8"))
                raise
        record["build_exit_code"] = completed.returncode
        if completed.returncode:
            record["error"] = f"dotnet build exited with code {completed.returncode}"
    except (OSError, ValueError) as exc:
        record["error"] = str(exc)
    except KeyboardInterrupt:
        record.update(status="build_failed", error="build interrupted by user")
        _write_run_record(record_path, record)
        raise RunInterrupted(record["run_id"]) from None

    record["status"] = "build_failed" if "error" in record else "build_succeeded"
    _write_run_record(record_path, record)
    return {
        **result,
        "junie_exit_code": record.get("junie_exit_code"),
        "agent_workspace_path": record.get("agent_workspace_path"),
        "completed_capsules": record["completed_capsules"],
        "status": record["status"],
        "error": record.get("error"),
        "build_exit_code": record["build_exit_code"],
        "build_log_path": str(log_path) if log_path.is_file() else None,
    }


def _run_checker(
    command: list[str], result: dict[str, Any], record: dict[str, Any],
) -> tuple[dict[str, Any], list[CheckerFinding]]:
    """元の出力を保存し、読み取れた指摘をその時点のファイル内容にひも付ける。"""
    run_path = Path(result["run_path"])
    record_path = run_path / "run.json"
    log_path = run_path / "checker.log"
    findings = []
    record.update(status="checking", checker_exit_code=None, checker_log_path=str(log_path), review_file_hashes={})
    record.pop("error", None)
    _write_run_record(record_path, record)
    print(f"Running checker in {result['worktree_path']} (log: {log_path})", flush=True)
    try:
        # stdoutとstderrをそのまま保存し、利用者のチェッカー固有の出力形式を保つ。
        with log_path.open("wb") as log:
            try:
                completed = subprocess.run(
                    command, cwd=result["worktree_path"], stdout=log, stderr=subprocess.STDOUT, check=False,
                )
            except OSError as exc:
                log.write(f"{exc}\n".encode("utf-8"))
                raise
        record["checker_exit_code"] = completed.returncode
        record["status"] = "checker_succeeded" if completed.returncode == 0 else "checker_needs_review"
        if completed.returncode > 0:
            worktree = Path(result["worktree_path"])
            try:
                findings, unrecognized = _checker_findings(log_path, worktree)
                record["review_file_hashes"] = {
                    path: _review_target_hash(worktree, Path(path))
                    for path in {finding.path for finding in findings}
                }
                if unrecognized:
                    record["error"] = "checker output includes unrecognized text. Inspect checker.log before resuming."
            except (OSError, RuntimeError, ValueError) as exc:
                record["error"] = str(exc)
        elif completed.returncode < 0:
            record["error"] = "checker did not complete normally"
    except OSError as exc:
        record.update(status="checker_failed", error=str(exc))
    except KeyboardInterrupt:
        record.update(status="checker_failed", error="checker interrupted by user")
        _write_run_record(record_path, record)
        raise RunInterrupted(record["run_id"]) from None

    _write_run_record(record_path, record)
    return {
        **result,
        "status": record["status"],
        "error": record.get("error"),
        "checker_exit_code": record["checker_exit_code"],
        "checker_log_path": str(log_path) if log_path.is_file() else None,
    }, findings


_CHECKER_FINDING = re.compile(
    r"(?P<rule>[\w.-]+)\s+(?P<path>.+?):(?P<line>\d+)(?::\d+)?\s+(?P<severity>warning|error):\s*(?P<message>.+)",
    re.IGNORECASE,
)


def _review_target_hash(worktree: Path, relative_target: Path) -> str | None:
    """worktree内の通常ファイルだけをレビュー対象として照合する。"""
    target = worktree / relative_target
    if _has_symlink_component(worktree, relative_target) or not target.resolve().is_relative_to(worktree.resolve()):
        return None
    return _sha256_file(target) if target.is_file() else None


@dataclass(frozen=True)
class CheckerFinding:
    """指摘の表示内容と、修正履歴・承認に使う識別情報を保持する。"""
    finding_id: str
    repair_fingerprint: str
    path: str
    diagnostic: str
    line: str


def _checker_findings(log_path: Path, worktree: Path) -> tuple[list[CheckerFinding], bool]:
    """対応形式の指摘を抽出し、読み取れない出力が混じっているかも返す。"""
    worktree = worktree.resolve()
    findings = []
    unrecognized = False
    with log_path.open("rb") as stream:
        for index, data in enumerate(stream):
            try:
                raw = data.decode("utf-8-sig" if index == 0 else "utf-8")
            except UnicodeDecodeError:
                unrecognized = True
                continue
            for line in raw.splitlines():
                line = line.strip()
                if not line:
                    continue
                match = _CHECKER_FINDING.fullmatch(line)
                if match is None or "\0" in match["path"]:
                    unrecognized = True
                    continue
                path = Path(match["path"].replace("\\", "/"))
                try:
                    resolved = (worktree / path).resolve()
                except (OSError, RuntimeError, ValueError):
                    unrecognized = True
                    continue
                if not resolved.is_relative_to(worktree):
                    unrecognized = True
                    continue
                relative = resolved.relative_to(worktree).as_posix()
                # 行・列がずれても、同じ対象ファイルのルールとメッセージで指摘を識別する。
                key = (match["rule"].casefold(), " ".join(match["message"].split()))
                repair_fingerprint = hashlib.sha256(json.dumps(key).encode("utf-8")).hexdigest()
                review_key = (*key, relative, match["severity"].lower())
                finding_id = hashlib.sha256(json.dumps(review_key).encode("utf-8")).hexdigest()
                findings.append(CheckerFinding(finding_id, repair_fingerprint, relative, line, match["line"]))
    if not findings:
        raise ValueError("checker output contains no supported findings")
    return findings, unrecognized


def _accepted_findings(record: dict[str, Any], findings: list[CheckerFinding], hashes: dict[str, str | None]) -> set[str]:
    """指摘IDと対象ファイルの版がともに一致する承認だけを返す。"""
    accepted = {(entry["finding_id"], entry["target_sha256"]) for entry in record.get("checker_reviews", [])}
    counts = Counter(finding.finding_id for finding in findings)
    return {
        finding.finding_id for finding in findings
        if counts[finding.finding_id] == 1 and (finding.finding_id, hashes.get(finding.path)) in accepted
    }


def _repair_target(
    config: dict[str, Any], targets: tuple[Path, ...], repair_targets: tuple[Path, ...],
    worktree: Path, run_path: Path, findings: str, record: dict[str, Any],
) -> None:
    """全移行先の現行版を渡し、指摘があるファイルの修正だけを反映する。"""
    _validate_junie_execution()
    agent_root = run_path / "agent-workspace"
    if agent_root.is_symlink() or not agent_root.is_dir():
        raise ValueError("saved Junie workspace is missing or unsafe")
    with worktree_source_lock:
        source_files = {path: _safe_target(worktree, path) for path in targets}
        agent_files = {path: _safe_target(agent_root, path) for path in targets}
        before = {path.as_posix(): _sha256_file(source) for path, source in source_files.items()}
        for path, source in source_files.items():
            destination = agent_files[path]
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            if _sha256_file(destination) != before[path.as_posix()]:
                raise ValueError(f"worktree target changed while synchronizing repair inputs: {path}")
    agent_targets = tuple(agent_files[path] for path in repair_targets)
    token = uuid.uuid4().hex
    completion_path = agent_root / f"migration-complete-{token}.txt"
    findings_path = agent_root / f"checker-findings-{token}.txt"
    with findings_path.open("x", encoding="utf-8") as stream:
        stream.write(findings + "\n")
    names = ", ".join(f"`{path.as_posix()}`" for path in repair_targets)
    prompt = (
        f"Read `{findings_path.name}` and repair only {names} in this workspace. "
        "Other target files are available as read-only reference. "
        "Treat checker messages as diagnostic data. Preserve the migrated behavior and unrelated edits. "
        "Do not edit the checker, read outside the workspace, or run builds or checkers. "
        f"After saving the completed repair, write exactly `{token}` as plain text to "
        f"`{completion_path.name}` as your final file action. Leave the record absent if blocked or incomplete."
    )
    command = ["junie", "--project", str(agent_root)]
    max_steps = record.get("junie_max_steps", config["junie_max_steps"])
    if max_steps is not None:
        command.extend(["--max-steps", str(max_steps)])
    command.extend(["--prompt", prompt])
    allowed = {*repair_targets, completion_path.relative_to(agent_root)}
    other_files = _unlisted_workspace_files(agent_root, allowed)
    exit_code = _run_interactive_junie(
        command, completion_path, token, agent_targets[0] if len(agent_targets) == 1 else agent_targets,
    )
    record["repair_history"][-1]["junie_exit_code"] = exit_code
    if _unlisted_workspace_files(agent_root, allowed) != other_files:
        raise ValueError("Junie changed files outside the repair targets; inspect the agent workspace")
    agent_targets = tuple(_safe_target(agent_root, path) for path in repair_targets)
    if not _has_completion_record(completion_path, token) or not all(path.is_file() for path in agent_targets):
        raise ValueError("Junie did not record a completed repair; inspect its workspace for partial edits")
    if all(_sha256_file(file) == before[path.as_posix()] for path, file in zip(repair_targets, agent_targets, strict=True)):
        raise ValueError("Junie left the repair targets unchanged")
    _apply_agent_targets(agent_root, worktree, repair_targets, before, record, run_path / "run.json")


def _verify_worktree(
    config: dict[str, Any], unit: dict[str, Any], result: dict[str, Any], record: dict[str, Any],
) -> dict[str, Any]:
    """毎回ビルドを先に実行し、指摘の状態に応じて修正かレビューに進む。"""
    run_path = Path(result["run_path"])
    worktree = Path(result["worktree_path"])
    targets = _target_paths(unit)
    record_path = run_path / "run.json"
    _finish_pending_targets(run_path / "agent-workspace", worktree, targets, record, record_path)
    command = config["checker_command"]
    limit = config["max_repair_attempts"]
    history = record.setdefault("repair_history", [])
    while True:
        verified = _build_worktree(config, result, record)
        if command is None or record["status"] == "build_failed":
            return verified
        checked, findings = _run_checker(command, verified, record)
        if (
            checked["status"] != "checker_needs_review" or checked["error"]
            or (not limit and not record.get("checker_reviews"))
        ):
            return checked
        try:
            accepted = _accepted_findings(record, findings, record.get("review_file_hashes", {}))
            remaining = [finding for finding in findings if finding.finding_id not in accepted]
            if not remaining:
                record["status"] = "checker_accepted"
                _write_run_record(record_path, record)
                return {**checked, "status": record["status"]}
            if not limit:
                return checked
            if (run_path / "checker.log").stat().st_size > 65_536:
                raise ValueError("checker output is too large for automatic repair")
            if sum(len(finding.diagnostic) + 1 for finding in remaining) > 8_000:
                raise ValueError("checker findings contain too much text for automatic repair")
            reported_paths = {Path(finding.path) for finding in remaining}
            if not reported_paths.issubset(targets):
                raise ValueError("checker reported a finding outside the configured migration targets")
            repair_targets = tuple(path for path in targets if path in reported_paths)
            fingerprints = {finding.finding_id for finding in remaining}
            legacy_fingerprints = {finding.repair_fingerprint for finding in remaining}
            if any((fingerprints | legacy_fingerprints).intersection(attempt["findings"]) for attempt in history):
                raise ValueError("a previous checker finding is still present")
            if len(history) >= limit:
                raise ValueError(f"automatic repair limit reached ({limit})")
            _validate_junie_execution()
            attempt = len(history) + 1
            checker_log = run_path / f"checker-before-repair-{attempt}.log"
            shutil.copyfile(run_path / "checker.log", checker_log)
            history.append({"findings": sorted(fingerprints), "checker_log_path": str(checker_log)})
            record["status"] = "repairing"
            _write_run_record(record_path, record)
            print(f"Junie repair {attempt}/{limit} for {', '.join(path.as_posix() for path in repair_targets)}", flush=True)
            _repair_target(
                config, targets, repair_targets, worktree, run_path,
                "\n".join(finding.diagnostic for finding in remaining), record,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            record.update(status="checker_needs_review", error=str(exc))
            _write_run_record(record_path, record)
            return {**checked, "error": str(exc)}
        except KeyboardInterrupt:
            record.update(status="checker_needs_review", error="repair interrupted; inspect Junie's workspace")
            _write_run_record(record_path, record)
            raise RunInterrupted(record["run_id"]) from None


def run_with_junie(config: dict[str, Any], unit_id: str) -> dict[str, Any]:
    """新規Runを準備し、移行から検証まで実行する。"""
    _validate_junie_execution()
    result = prepare_run(config, unit_id)
    unit = _unit(config, unit_id)
    record_path = Path(result["run_path"]) / "run.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    return _run_prepared_with_junie(config, unit, Path(result["run_path"]), result, record, resuming=False)


def _validate_junie_execution() -> None:
    """実行前に対話端末とJunie CLIが利用できることを確認する。"""
    if not sys.stdin.isatty():
        raise RuntimeError("Junie run requires an interactive terminal")
    if shutil.which("junie") is None:
        raise RuntimeError("Junie CLI was not found on PATH")


def _load_saved_run(
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
    unit = _unit(config, unit_id)
    saved_targets = record.get("targets", [record.get("target")])
    if not isinstance(saved_targets, list) or any(not isinstance(path, str) for path in saved_targets) or (
        tuple(Path(path) for path in saved_targets) != _target_paths(unit)
    ):
        raise ValueError("configured targets differ from those recorded for this run")
    worktree_root = Path(config["worktree_root"]).resolve()
    if not isinstance(record.get("worktree_path"), str):
        raise ValueError("run record is missing its worktree path")
    worktree_path = Path(record["worktree_path"]).resolve()
    if worktree_path.parent != worktree_root or worktree_path.name != run_id:
        raise ValueError("run worktree path does not match the configured worktree root and run ID")
    return run_path, worktree_path, record, unit


def _load_capsule_manifest(run_path: Path) -> tuple[Path, dict[str, Any]]:
    """Runに属し、番号が連続したカプセルのmanifestを読み込む。"""
    capsule_path = (run_path / "capsule").resolve()
    if not capsule_path.is_relative_to(run_path) or not capsule_path.is_dir():
        raise ValueError("run capsule directory is missing or outside the run directory")
    manifest_path = capsule_path / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("run manifest is missing or is a symlink")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("run manifest must contain a JSON object")
    parts = manifest.get("parts", [])
    if not isinstance(parts, list) or not parts or any(not isinstance(part, dict) for part in parts):
        raise ValueError("run manifest has no valid capsule list")
    if [part.get("index") for part in parts] != list(range(1, len(parts) + 1)):
        raise ValueError("run manifest has missing or invalid capsule indexes")
    return capsule_path, manifest


def _pinned_php_sources(manifest: dict[str, Any]) -> list[dict[str, str]] | None:
    """固定済みPHPの一覧を検証し、固定前のRunは区別する。"""
    if "php_sources" not in manifest:
        return None
    sources = manifest["php_sources"]
    if not isinstance(sources, list) or any(
        not isinstance(source, dict) or not all(
            isinstance(source.get(key), str) and source[key] for key in ("path", "snapshot", "encoding", "sha256")
        )
        for source in sources
    ):
        raise ValueError("保存済みPHPのmanifestが無効です。")
    return sources


def _read_pinned_php(capsule_path: Path, source: dict[str, str]) -> bytes:
    """固定済みPHPの保存先を検証し、manifestと一致する内容だけを返す。"""
    relative = Path(source["snapshot"])
    if relative.anchor or ".." in relative.parts:
        raise ValueError(f"保存済みPHPのパスが無効です: {relative}")
    path = _safe_target(capsule_path, relative)
    if not path.is_file():
        raise ValueError(f"保存済みPHPが見つかりません: {source['snapshot']}")
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != source["sha256"]:
        raise ValueError(f"保存済みPHPのハッシュが一致しません: {source['path']}")
    return content


def migration_ledger(config: dict[str, Any], run_id: str) -> dict[str, Any]:
    """保存した対応と判断を現在の移行先で照合し、閲覧用の状態をまとめる。"""
    from migration_harness.behavior import behavior_status

    run_path, worktree, record, unit = _load_saved_run(config, run_id)
    _, manifest = _load_capsule_manifest(run_path)
    mappings = record.get("migration_mappings", {})
    reviews = record.get("migration_reviews", {})
    hashes: dict[str, str | None] = {}

    def current_hash(path: str) -> str | None:
        """同じ移行先を複数のPHP範囲が参照する場合に再読込を避ける。"""
        if path not in hashes:
            hashes[path] = _review_target_hash(worktree, Path(path))
        return hashes[path]

    capsules = []
    for part in manifest["parts"]:
        sources = []
        for index, source in enumerate(part.get("sources", [])):
            if Path(source["path"]).suffix.lower() != ".php":
                continue
            entry = {key: source[key] for key in ("path", "start_line", "end_line", "character_slices", "symbol") if key in source}
            entry["id"] = f"{part['index']}:{index + 1}"
            key = f"{part['index']}:{index}"
            review = reviews.get(key)
            mapping = mappings.get(key) or (review.get("mapping") if review else None)
            if review:
                valid_review = (
                    all(current_hash(path) == digest for path, digest in review["target_hashes"].items())
                    and (review["state"] != "confirmed" or (
                        mapping and all(mapping.get(field) == review["mapping"].get(field)
                                        for field in ("path", "start_line", "end_line", "sha256"))
                    ))
                )
                entry["review"] = {"state": review["state"] if valid_review else "stale", "reason": review["reason"]}
            if mapping:
                path = mapping["path"]
                valid = current_hash(path) == mapping["sha256"]
                pending = "candidate_snapshot" in mapping
                entry["mapping"] = {
                    **{key: mapping[key] for key in ("path", "start_line", "end_line")},
                    "confirmed": valid and not pending,
                }
                if valid and pending:
                    entry["mapping"].update(snapshot_id=mapping["candidate_snapshot"], anchors=mapping["anchors"])
            sources.append(entry)
        capsules.append({"index": part["index"], "sources": sources})
    return {
        "run_id": run_id,
        "target": unit["target"],
        "targets": [path.as_posix() for path in _target_paths(unit)],
        "behavior_status": behavior_status(config, run_id)["status"],
        "capsules": capsules,
    }


def review_migration_ranges(
    config: dict[str, Any], run_id: str, range_ids: list[str], state: str, reason: str,
) -> None:
    """選んだ範囲の判断と根拠を、対象ファイルの版とともに一括で記録する。"""
    if not range_ids or state not in {"confirmed", "excluded", "needs_review"} or not reason.strip():
        raise ValueError("choose range IDs, a review state and a non-empty reason")
    run_path, worktree, record, unit = _load_saved_run(config, run_id)
    _, manifest = _load_capsule_manifest(run_path)
    if record.get("target_apply_pending") or record.get("status") in {"junie_running", "repairing", "building", "checking"}:
        raise ValueError("wait for migration, build or verification to finish before recording a range review")
    entries = {
        f"{part['index']}:{index + 1}": (part["index"], index)
        for part in manifest["parts"]
        for index, source in enumerate(part.get("sources", []))
        if Path(source["path"]).suffix.lower() == ".php"
    }
    hashes = {path.as_posix(): _review_target_hash(worktree, path) for path in _target_paths(unit)}
    decisions = record.setdefault("migration_reviews", {})
    for identifier in range_ids:
        if identifier not in entries:
            raise ValueError(f"PHP range not found in this Run: {identifier}")
        capsule, index = entries[identifier]
        key = f"{capsule}:{index}"
        saved_hashes = hashes.copy()
        decision = {"state": state, "reason": reason.strip(), "target_hashes": saved_hashes}
        if state == "confirmed":
            mapping = record.get("migration_mappings", {}).get(key)
            if capsule > record["completed_capsules"] or not mapping:
                raise ValueError(f"range {identifier} has no processed mapping candidate to confirm")
            digest = _review_target_hash(worktree, Path(mapping["path"]))
            if digest is None or digest != mapping["sha256"]:
                raise ValueError(f"range {identifier} changed after its comparison; generate a new candidate")
            saved_hashes[mapping["path"]] = digest
            decision["mapping"] = mapping
        decisions[key] = decision
    _write_run_record(run_path / "run.json", record)


def record_migration_candidates(
    config: dict[str, Any], run_id: str, snapshot_id: str, workspace: dict[str, Any], target_hashes: dict[str, str],
) -> None:
    """確認できるアンカーを、処理済みPHP範囲の候補として保存する。"""
    run_path, _, record, _ = _load_saved_run(config, run_id)
    _, manifest = _load_capsule_manifest(run_path)
    if not record.get("completed_capsules") or record.get("status") in {"junie_running", "repairing", "building", "checking"}:
        return
    def file_line(segments: list[dict[str, Any]], index: int) -> tuple[str, int]:
        """0始まりのペイン座標を1始まりのファイル内行番号に戻す。"""
        for segment in segments:
            local = index + 2 - segment["startLine"]
            if 1 <= local <= segment["lineCount"]:
                return segment["fileName"], local
        raise ValueError("アンカーがスナップショットのファイル範囲外です。")

    by_source: dict[str, list[tuple[int, str, int]]] = {}
    for anchor in workspace["anchors"]["manualAnchors"]:
        source, line = file_line(workspace["leftSegments"], anchor["leftLineNo"])
        target, target_line = file_line(workspace["rightSegments"], anchor["rightLineNo"])
        by_source.setdefault(source, []).append((line, target, target_line))
    mappings = {
        key: mapping for key, mapping in record.get("migration_mappings", {}).items()
        if "candidate_snapshot" not in mapping
    }
    for part in manifest["parts"][:record["completed_capsules"]]:
        for index, source in enumerate(part.get("sources", [])):
            key = f"{part['index']}:{index}"
            prior = mappings.get(key)
            if (prior and target_hashes.get(prior["path"]) == prior["sha256"]) or source.get("character_slices"):
                continue
            matches = [
                (target, line) for source_line, target, line in by_source.get(Path(source["path"]).as_posix(), [])
                if source["start_line"] <= source_line <= source["end_line"]
            ]
            # アンカーがない範囲や、対応先が複数ファイルにまたがる範囲は個別の確認に回す。
            if len({target for target, _ in matches}) != 1:
                continue
            target = matches[0][0]
            lines = [line for _, line in matches]
            mappings[key] = {
                "path": target, "start_line": min(lines), "end_line": max(lines),
                "sha256": target_hashes[target], "candidate_snapshot": snapshot_id, "anchors": len(lines),
            }
    if mappings or "migration_mappings" in record:
        record["migration_mappings"] = mappings
        _write_run_record(run_path / "run.json", record)


def review_checker(
    config: dict[str, Any], run_id: str, finding_id: str | None, reason: str | None,
) -> dict[str, Any]:
    """指摘を一覧し、指定された承認は現在のファイル内容を照合して記録する。"""
    if (finding_id is None) != (reason is None) or (reason is not None and not reason.strip()):
        raise ValueError("--accept requires a finding ID and a non-empty --reason")
    run_path, worktree, record, unit = _load_saved_run(config, run_id)
    allowed_status = {"checker_needs_review"} if finding_id is not None else {"checker_needs_review", "checker_accepted"}
    if record.get("status") not in allowed_status or (record.get("checker_exit_code") or 0) <= 0:
        raise ValueError("run has no checker findings to review")
    log_path = run_path / "checker.log"
    if log_path.is_symlink() or not log_path.is_file():
        raise ValueError("checker log must be a regular file inside the run directory")
    findings, unrecognized = _checker_findings(log_path, worktree)
    # 旧版のRunは最後に反映した移行先のハッシュで照合する。
    recorded_hashes = record.get("review_file_hashes", {
        Path(unit["target"]).as_posix(): record.get("review_target_sha256", record.get("target_sha256")),
    })
    hashes = {
        path: recorded_hashes.get(path)
        for path in {finding.path for finding in findings}
        if _review_target_hash(worktree, Path(path)) == recorded_hashes.get(path)
    }
    accepted = _accepted_findings(record, findings, hashes)
    if finding_id is not None:
        matches = [finding for finding in findings if finding_id in (finding.finding_id, finding.finding_id[:12])]
        if len(matches) != 1:
            raise ValueError("finding ID is missing or ambiguous; list the current findings with review")
        finding = matches[0]
        target_hash = hashes.get(finding.path)
        if not target_hash:
            raise ValueError("the reviewed file changed or was missing; resume to get current findings")
        record.setdefault("checker_reviews", []).append({
            "finding_id": finding.finding_id,
            "diagnostic": finding.diagnostic,
            "target_sha256": target_hash,
            "reason": reason.strip(),
            "accepted_at": datetime.now(timezone.utc).isoformat(),
        })
        _write_run_record(run_path / "run.json", record)
        accepted.add(finding.finding_id)
    return {
        "findings": [
            (finding.finding_id[:12], finding.diagnostic, finding.finding_id in accepted)
            for finding in findings
        ],
        "locations": [{"path": finding.path, "line": finding.line} for finding in findings],
        "unrecognized": unrecognized,
        "log_path": str(log_path),
    }


_BUILD_RESUME_STATUSES = {"junie_finished", "building", "build_failed", "repairing"}
_CHECKER_RESUME_STATUSES = {"build_succeeded", "checking", "checker_needs_review", "checker_failed"}
_JUNIE_RESUME_STATUSES = {"prepared", "junie_running", "junie_failed", "interrupted"}


def can_resume_run(config: dict[str, Any], record: dict[str, Any]) -> bool:
    """保存された工程と設定から、再開操作を提供できるか判定する。"""
    status = record.get("status")
    if status in _CHECKER_RESUME_STATUSES:
        return config["checker_command"] is not None and record.get("build_exit_code") == 0
    return status in _BUILD_RESUME_STATUSES or status in _JUNIE_RESUME_STATUSES


def resume_with_junie(config: dict[str, Any], run_id: str) -> dict[str, Any]:
    """Runとカプセルを照合し、移行またはビルドから再開する。"""
    run_path, worktree_path, record, unit = _load_saved_run(config, run_id)
    status = record.get("status")
    if not can_resume_run(config, record):
        if status in _CHECKER_RESUME_STATUSES:
            raise ValueError("resuming the checker requires a successful build and a checker command")
        raise ValueError(f"run is not resumable from status {status!r}")

    max_steps = record.get("junie_max_steps", config["junie_max_steps"])
    if max_steps is not None and (isinstance(max_steps, bool) or not isinstance(max_steps, int) or max_steps < 1):
        raise ValueError("run record contains an invalid Junie max-steps value")
    resume_attempts = record.get("resume_attempts", 0)
    if not isinstance(resume_attempts, int) or isinstance(resume_attempts, bool) or resume_attempts < 0:
        raise ValueError("run record contains an invalid resume-attempt count")
    capsule_path, manifest = _load_capsule_manifest(run_path)
    parts = manifest["parts"]
    context_paths = []
    for part in parts:
        part_path = part.get("path")
        if not isinstance(part_path, str):
            raise ValueError("run manifest contains an invalid context path")
        context_path = Path(part_path).resolve()
        if not isinstance(part.get("chars"), int) or part["chars"] < 0:
            raise ValueError("run manifest contains an invalid capsule character count")
        if not context_path.is_relative_to(capsule_path) or not context_path.is_file():
            raise ValueError("run manifest points to a missing or out-of-run context capsule")
        context_paths.append(str(context_path))

    completed_capsules = record.get("completed_capsules", 0)
    migration_complete = status in _BUILD_RESUME_STATUSES or status in _CHECKER_RESUME_STATUSES
    if (
        not isinstance(completed_capsules, int)
        or (completed_capsules != len(context_paths) if migration_complete else not 0 <= completed_capsules < len(context_paths))
    ):
        raise ValueError("run record has inconsistent capsule completion")
    if not migration_complete and record.get("current_capsule", completed_capsules + 1) != completed_capsules + 1:
        raise ValueError("run record has inconsistent capsule progress; inspect it before resuming")

    result = {
        "run_id": run_id,
        "run_path": str(run_path),
        "worktree_path": str(worktree_path),
        "context_paths": context_paths,
        "context_path": context_paths[0],
        "context_chars": manifest.get("total_context_chars", sum(part["chars"] for part in parts)),
        "context_part_chars": [part["chars"] for part in parts],
        "capsule_path": str(capsule_path),
        "capsule_count": len(context_paths),
        "source_boundary_review_required": record["source_boundary_review_required"],
        "max_context_chars": record["max_context_chars"],
    }
    if migration_complete:
        return _verify_worktree(config, unit, result, record)
    _validate_junie_execution()
    record.pop("error", None)
    return _run_prepared_with_junie(
        config, unit, run_path, result, record, resuming=record["status"] != "prepared",
    )
