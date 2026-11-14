from __future__ import annotations

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
from typing import Any, TypedDict

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
        "target": unit["target"],
    }
    (run_path / "run.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return {"worktree_path": str(worktree)}


def _selected_lines(path: Path, start: int, end: int) -> list[tuple[int, str]]:
    selected: list[tuple[int, str]] = []
    with path.open("r", encoding="utf-8", newline="") as stream:
        for number, line in enumerate(stream, start=1):
            if start <= number <= end:
                selected.append((number, line))
            if number >= end:
                break
    if not selected:
        raise ValueError(f"requested lines {start}-{end} do not exist in {path}")
    if selected[-1][0] < end:
        raise ValueError(f"requested line {end} does not exist in {path}")
    return selected


def _prepare_context(state: PrepareState) -> dict[str, Any]:
    config = state["config"]
    unit = _unit(config, state["unit_id"])
    root = Path(config["project_root"])
    run_path = Path(config["run_root"]) / state["run_id"]
    capsule = run_path / "capsule"
    capsule.mkdir(parents=True, exist_ok=False)

    parts = [
        f"# Migration unit: {unit['id']}",
        "",
        f"Target in worktree: `{unit['target']}`",
        "",
        "Only the listed excerpts are included. The application source is not copied into this public harness repository.",
        "",
    ]
    source_files = []
    for item in unit["inputs"]:
        source_path = (root / item["path"]).resolve()
        lines = _selected_lines(source_path, item["start_line"], item["end_line"])
        excerpt = "".join(f"{number:>6} | {line}" for number, line in lines)
        backtick_fence = "`" * max(3, max((len(run) + 1 for run in re.findall(r"`+", excerpt)), default=0))
        parts.extend([
            f"## {item['role']} — `{item['path']}` lines {item['start_line']}-{item['end_line']}",
            "",
            f"{backtick_fence}text",
            excerpt.rstrip("\n"),
            backtick_fence,
            "",
        ])
        source_files.append({
            "role": item["role"],
            "path": item["path"],
            "start_line": item["start_line"],
            "end_line": item["end_line"],
        })

    context = "\n".join(parts)
    # 文字数上限はトークン上限の代用ではなく、巨大な入力範囲を早期に拒否する目安。
    if len(context) > config["max_context_chars"]:
        raise ValueError(
            f"selected context is {len(context)} characters; limit is {config['max_context_chars']}. "
            "Narrow the configured line ranges."
        )

    context_path = capsule / "context.md"
    context_path.write_text(context, encoding="utf-8")
    return {
        "run_path": str(run_path),
        "context_path": str(context_path),
        "context_chars": len(context),
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


def prepare_run(config: dict[str, Any], unit_id: str) -> dict[str, str]:
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
        "run_path": str(run_root / run_id),
    }


def run_with_junie(config: dict[str, Any], unit_id: str) -> dict[str, str | int]:
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

    agent_context = agent_root / ".migration-context.md"
    shutil.copyfile(result["context_path"], agent_context)
    prompt = (
        f"Read `{agent_context}`. This workspace contains the allowed context and target file. "
        f"Do not access files outside this workspace. Make one implementation pass and edit only `{unit['target']}`. "
        "Do not run builds, tests, or external checkers. If the step limit is reached first, state what remains "
        "and preserve the current work so the user can continue in this session. Do not claim completion."
    )
    command = ["junie", "--project", str(agent_root)]
    if config["junie_max_steps"] is not None:
        command.extend(["--max-steps", str(config["junie_max_steps"])])
    command.extend(["--prompt", prompt])
    # 対話CLIで利用者のログイン状態とJunieのモデル選択を引き継ぐ。
    completed = subprocess.run(command, check=False)

    if (
        _has_symlink_component(agent_root, relative_target)
        or not agent_target.resolve().is_relative_to(agent_root.resolve())
    ):
        raise ValueError(f"Junie target escapes its workspace or is a symlink: {unit['target']}")
    if not agent_target.is_file():
        raise RuntimeError(f"Junie did not produce the configured target: {unit['target']}")
    worktree_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(agent_target, worktree_target)

    record_path = run_path / "run.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["status"] = "junie_finished" if completed.returncode == 0 else "junie_failed"
    record["junie_exit_code"] = completed.returncode
    record["agent_workspace_path"] = str(agent_root)
    record["target_applied"] = True
    record_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return {**result, "junie_exit_code": completed.returncode, "agent_workspace_path": str(agent_root)}
