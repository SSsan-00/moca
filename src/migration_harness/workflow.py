from __future__ import annotations

import codecs
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, TypedDict

os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph


class PrepareState(TypedDict, total=False):
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
    error: str


def _unit(config: dict[str, Any], unit_id: str) -> dict[str, Any]:
    for unit in config["units"]:
        if unit["id"] == unit_id:
            return unit
    raise ValueError(f"unknown migration unit: {unit_id}")


def _has_symlink_component(root: Path, relative_path: Path) -> bool:
    current = root
    for part in relative_path.parts:
        current /= part
        if current.is_symlink():
            return True
    return False


def _validate(state: PrepareState) -> dict[str, Any]:
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
    config = state["config"]
    root = Path(config["project_root"])
    worktree = Path(config["worktree_root"]) / state["run_id"]
    worktree.parent.mkdir(parents=True, exist_ok=True)
    if worktree.exists():
        raise FileExistsError(f"worktree path already exists: {worktree}")
    # 実行ごとに指定した基準リビジョンから独立した作業場所を作る。
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
        "segmentation_policy": "line_with_character_fallback_and_overlap",
        "source_boundary_review_required": state["capsule_count"] > 1,
        "target": unit["target"],
    }
    (run_path / "run.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return {"worktree_path": str(worktree)}


def _text_fingerprint(path: Path, encoding: str) -> bytes | None:
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
    return "\n".join([
        f"# Migration unit: {unit['id']}",
        "",
        f"Target in worktree: `{unit['target']}`",
        "",
        "Only the listed excerpts are included. The application source is not copied into this public harness repository.",
        "",
    ])


def _max_backtick_run(text: str) -> int:
    return max((len(run) for run in re.findall(r"`+", text)), default=0)


def _group_size(group: dict[str, Any]) -> int:
    item = group["item"]
    fence = "`" * max(3, group["max_backticks"] + 1)
    heading = (
        f"## {item['role']} — `{item['path']}` lines "
        f"{group['start_line']}-{group['end_line']}\n\n"
    )
    opening = f"{fence}text\n"
    closing = ("" if group["ends_with_newline"] else "\n") + f"{fence}\n\n"
    return len(heading) + len(opening) + group["content_chars"] + len(closing)


def _new_group(input_index: int, item: dict[str, Any], line_number: int, record: str) -> dict[str, Any]:
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
        last["end_line"] = candidate["end_line"]
        last["records"].append(record)
        last["content_chars"] = candidate["content_chars"]
        last["max_backticks"] = candidate["max_backticks"]
        last["ends_with_newline"] = candidate["ends_with_newline"]
        return True

    candidate = _new_group(input_index, item, line_number, record)
    if base_chars + sum(_group_size(group) for group in groups) + _group_size(candidate) > max_chars:
        return False
    groups.append(candidate)
    return True


def _render_context(unit: dict[str, Any], groups: list[dict[str, Any]]) -> str:
    parts = [_context_header(unit)]
    for group in groups:
        item = group["item"]
        fence = "`" * max(3, group["max_backticks"] + 1)
        parts.extend([
            f"## {item['role']} — `{item['path']}` lines {group['start_line']}-{group['end_line']}\n\n",
            f"{fence}text\n",
            *group["records"],
            "" if group["ends_with_newline"] else "\n",
            f"{fence}\n\n",
        ])
    return "".join(parts)


def _prepare_context(state: PrepareState) -> dict[str, Any]:
    config = state["config"]
    unit = _unit(config, state["unit_id"])
    root = Path(config["project_root"])
    run_path = Path(config["run_root"]) / state["run_id"]
    capsule = run_path / "capsule"
    capsule.mkdir(parents=True, exist_ok=False)

    max_chars = config["max_context_chars"]
    base_chars = len(_context_header(unit))
    groups: list[dict[str, Any]] = []
    context_paths: list[str] = []
    context_part_chars: list[int] = []
    manifest_parts: list[dict[str, Any]] = []
    split_line_coverage: list[dict[str, Any]] = []

    def flush_capsule() -> None:
        if not groups:
            return
        context = _render_context(unit, groups)
        if len(context) > max_chars:
            raise RuntimeError("internal error: generated context capsule exceeds its configured limit")
        part_number = len(context_paths) + 1
        part_dir = capsule / f"part-{part_number:04d}"
        part_dir.mkdir()
        context_path = part_dir / "context.md"
        context_path.write_text(context, encoding="utf-8")
        context_paths.append(str(context_path))
        context_part_chars.append(len(context))
        manifest_parts.append({
            "index": part_number,
            "path": str(context_path),
            "chars": len(context),
            "sources": [
                {
                    "role": group["item"]["role"],
                    "path": group["item"]["path"],
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
                for group in groups
            ],
        })
        groups.clear()

    for input_index, item in enumerate(unit["inputs"]):
        source_path = (root / item["path"]).resolve()
        encoding = _input_encoding(source_path, item["encoding"])
        context_item = {**item, "resolved_encoding": encoding}
        lines = _selected_lines(source_path, item["start_line"], item["end_line"], encoding)
        for number, line in lines:
            record = f"{number:>6} | {line}"
            if _append_context_record(groups, input_index, context_item, number, record, base_chars, max_chars):
                continue
            if groups:
                flush_capsule()
                if _append_context_record(groups, input_index, context_item, number, record, base_chars, max_chars):
                    continue

            line_body = line.rstrip("\r\n")
            line_ending = line[len(line_body):]
            record = f"{number:>6} | {line}"
            if _append_context_record(groups, input_index, context_item, number, record, base_chars, max_chars):
                continue
            if not line_body:
                raise ValueError("max_context_chars is too small for the context headers")

            offset = 0
            overlap_chars = min(512, max_chars // 20)
            focus_ranges: list[tuple[int, int]] = []
            while offset < len(line_body):
                low = 1
                high = len(line_body) - offset
                best_length = 0
                best_record = ""
                while low <= high:
                    piece_length = (low + high) // 2
                    end_column = offset + piece_length
                    context_start = max(0, offset - overlap_chars)
                    context_end = min(len(line_body), end_column + overlap_chars)
                    suffix = line_ending if end_column == len(line_body) else ""
                    if not suffix.endswith("\n"):
                        suffix += "\n"
                    candidate_record = (
                        f"{number:>6} focus chars {offset + 1}-{end_column} "
                        f"context chars {context_start + 1}-{context_end} | "
                        f"{line_body[context_start:context_end]}{suffix}"
                    )
                    if _append_context_record([], input_index, context_item, number, candidate_record, base_chars, max_chars):
                        best_length = piece_length
                        best_record = candidate_record
                        low = piece_length + 1
                    else:
                        high = piece_length - 1
                if best_length == 0:
                    raise ValueError("max_context_chars is too small for the context headers")
                if not _append_context_record(groups, input_index, context_item, number, best_record, base_chars, max_chars):
                    flush_capsule()
                    if not _append_context_record(groups, input_index, context_item, number, best_record, base_chars, max_chars):
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
    run_id = str(uuid.uuid4())
    run_root = Path(config["run_root"])
    run_root.mkdir(parents=True, exist_ok=True)
    checkpoint_db = run_root / "checkpoints.sqlite"
    with sqlite3.connect(checkpoint_db, check_same_thread=False) as connection:
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


def run_with_junie(config: dict[str, Any], unit_id: str) -> dict[str, Any]:
    if not sys.stdin.isatty():
        raise RuntimeError("Junie run requires an interactive terminal")
    if shutil.which("junie") is None:
        raise RuntimeError("Junie CLI was not found on PATH")
    result = prepare_run(config, unit_id)
    unit = _unit(config, unit_id)
    worktree_root = Path(result["worktree_path"]).resolve()
    run_path = Path(result["run_path"])
    agent_root = run_path / "agent-workspace"
    agent_root.mkdir()

    relative_target = Path(unit["target"])
    if relative_target == Path(".migration-context.md"):
        raise ValueError("target path conflicts with the staged migration context")
    worktree_target = worktree_root / relative_target
    if (
        _has_symlink_component(worktree_root, relative_target)
        or not worktree_target.resolve().is_relative_to(worktree_root)
    ):
        raise ValueError(f"target escapes the worktree or is a symlink: {unit['target']}")
    if worktree_target.exists() and not worktree_target.is_file():
        raise ValueError(f"target is not a regular file: {unit['target']}")

    agent_target = agent_root / relative_target
    agent_target.parent.mkdir(parents=True, exist_ok=True)
    if worktree_target.is_file():
        shutil.copyfile(worktree_target, agent_target)

    record_path = run_path / "run.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["agent_workspace_path"] = str(agent_root)
    record["status"] = "junie_running"
    record["completed_capsules"] = 0
    record["current_capsule"] = 1
    record_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")

    agent_context = agent_root / ".migration-context.md"
    completed = None
    for index, context_path in enumerate(result["context_paths"], start=1):
        shutil.copyfile(context_path, agent_context)
        prompt = (
            f"Read `{agent_context}`. This is context segment {index} of {result['capsule_count']} "
            f"for migration unit `{unit['id']}`. The target file `{unit['target']}` contains the work "
            "from earlier segments; preserve it and continue only with this segment. Edit only the configured target. "
            "The source excerpt may begin or end inside a function or control structure. Use its original line numbers; "
            "do not treat the excerpt boundary as a PHP statement boundary or add closing syntax just for the excerpt end. "
            "For `focus chars START-END context chars CONTEXT_START-CONTEXT_END` records, migrate only the focus range. "
            "The surrounding context range overlaps adjacent segments and is reference-only; do not migrate it twice. "
            "These records are slices of one physical source line, and displayed line breaks are not source newlines. "
            "Do not infer text outside the context range. "
            "Do not access files outside this workspace. Do not run builds, tests, or external checkers. "
            "If the step limit is reached, preserve current edits and state what remains. "
            "Claim the migration is complete only after the final segment."
        )
        command = ["junie", "--project", str(agent_root)]
        if config["junie_max_steps"] is not None:
            command.extend(["--max-steps", str(config["junie_max_steps"])])
        command.extend(["--prompt", prompt])
        record["current_capsule"] = index
        record_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        try:
            # 対話CLIで利用者のログイン状態とJunieのモデル選択を引き継ぐ。
            completed = subprocess.run(command, check=False)
        except OSError as exc:
            record["status"] = "junie_failed"
            record["error"] = str(exc)
            record_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
            raise

        if (
            _has_symlink_component(agent_root, relative_target)
            or not agent_target.resolve().is_relative_to(agent_root.resolve())
        ):
            record["status"] = "junie_failed"
            record["error"] = f"Junie target escapes its workspace or is a symlink: {unit['target']}"
            record_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
            raise ValueError(record["error"])
        if not agent_target.is_file():
            record["status"] = "junie_failed"
            record["error"] = f"Junie did not produce the configured target: {unit['target']}"
            record_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
            raise RuntimeError(record["error"])

        worktree_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(agent_target, worktree_target)
        record["target_applied"] = True
        record["junie_exit_code"] = completed.returncode
        if completed.returncode == 0:
            record["completed_capsules"] = index
        else:
            record["status"] = "junie_failed"
        record_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        if completed.returncode != 0:
            break

    if completed is None:
        raise RuntimeError("context preparation produced no capsules")
    all_capsules_completed = record["completed_capsules"] == result["capsule_count"]
    record["status"] = "junie_finished" if completed.returncode == 0 and all_capsules_completed else "junie_failed"
    record_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return {
        **result,
        "junie_exit_code": completed.returncode,
        "agent_workspace_path": str(agent_root),
        "completed_capsules": record["completed_capsules"],
    }
