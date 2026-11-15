from __future__ import annotations

from collections import Counter
from collections.abc import Callable
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
    source_boundary_review_required: bool
    error: str


class RunInterrupted(KeyboardInterrupt):
    def __init__(self, run_id: str) -> None:
        super().__init__(run_id)
        self.run_id = run_id


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
        "junie_max_steps": config["junie_max_steps"],
        "resume_attempts": 0,
        "segmentation_policy": "line_with_character_fallback_and_overlap",
        "source_boundary_review_required": state["capsule_count"] > 1,
        "target": unit["target"],
        "completed_capsules": 0,
        "current_capsule": 1,
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


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(64 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _write_run_record(path: Path, record: dict[str, Any]) -> None:
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _apply_agent_target(
    agent_root: Path,
    agent_target: Path,
    worktree_target: Path,
    relative_target: Path,
    record: dict[str, Any],
    record_path: Path,
) -> None:
    if (
        _has_symlink_component(agent_root, relative_target)
        or not agent_target.resolve().is_relative_to(agent_root.resolve())
    ):
        raise ValueError(f"Junie target escapes its workspace or is a symlink: {relative_target}")
    if not agent_target.is_file():
        return
    worktree_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(agent_target, worktree_target)
    record["target_applied"] = True
    record["target_sha256"] = _sha256_file(worktree_target)
    _write_run_record(record_path, record)


def _has_completion_record(path: Path, token: str) -> bool:
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
    command: list[str], completion_path: Path, token: str, target: Path,
) -> int:
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
                    and target.is_file()
                    and not _has_symlink_component(completion_path.parent, target.relative_to(completion_path.parent))
                    and target.resolve().is_relative_to(completion_path.parent.resolve())
                ):
                    return _close_junie(process)
    except KeyboardInterrupt:
        _close_junie(process)
        raise
    finally:
        if process.poll() is not None:
            restore_terminal()


def _save_terminal_mode() -> Callable[[], None]:
    # 自動終了でJunieの後処理が走らない場合に備え、起動前の端末設定を復元する。
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
    worktree_root = Path(result["worktree_path"]).resolve()
    if not worktree_root.is_dir() or not worktree_root.is_relative_to(Path(config["worktree_root"]).resolve()):
        raise ValueError(f"run worktree is missing or outside configured worktree root: {worktree_root}")
    relative_target = Path(unit["target"])
    if relative_target.parts and (
        Path(relative_target.parts[0]) == Path(".migration-context.md")
        or relative_target.parts[0].lower().startswith("migration-complete-")
    ):
        raise ValueError("target path conflicts with a reserved file in the agent workspace")
    if _has_symlink_component(worktree_root, relative_target):
        raise ValueError(f"target path contains a symlink: {unit['target']}")
    agent_root = run_path / "agent-workspace"
    if agent_root.is_symlink() or not agent_root.resolve().is_relative_to(run_path.resolve()):
        raise ValueError("agent workspace path escapes the run directory")
    resume_attempts = record.get("resume_attempts", 0)
    if resuming:
        if not isinstance(resume_attempts, int) or isinstance(resume_attempts, bool) or resume_attempts < 0:
            raise ValueError("run record contains an invalid resume-attempt count")
    if resuming:
        if agent_root.exists():
            if not agent_root.is_dir():
                raise ValueError("agent workspace path is not a directory")
            worktree_target_check = worktree_root / unit["target"]
            actual_hash = _sha256_file(worktree_target_check) if worktree_target_check.is_file() else None
            if actual_hash != record.get("target_sha256"):
                raise ValueError("worktree target changed since the last recorded Junie result; inspect and reconcile it before resuming")
        elif record.get("status") != "prepared":
            raise ValueError("agent workspace is missing; this run cannot be resumed safely")
        else:
            agent_root.mkdir()
    else:
        if agent_root.exists():
            if record.get("status") != "prepared" or not agent_root.is_dir():
                raise ValueError("agent workspace already exists for this run")
        else:
            agent_root.mkdir()

    worktree_target = worktree_root / relative_target
    if (
        _has_symlink_component(worktree_root, relative_target)
        or not worktree_target.resolve().is_relative_to(worktree_root)
    ):
        raise ValueError(f"target escapes the worktree or is a symlink: {unit['target']}")
    if worktree_target.exists() and not worktree_target.is_file():
        raise ValueError(f"target is not a regular file: {unit['target']}")

    agent_target = agent_root / relative_target
    if _has_symlink_component(agent_root, relative_target):
        raise ValueError(f"agent target path contains a symlink: {unit['target']}")
    agent_target.parent.mkdir(parents=True, exist_ok=True)
    if not agent_target.exists() and worktree_target.is_file():
        shutil.copyfile(worktree_target, agent_target)

    record_path = run_path / "run.json"
    record["agent_workspace_path"] = str(agent_root)
    if resuming:
        record["resume_attempts"] = resume_attempts + 1
    record["status"] = "junie_running"
    record.setdefault("completed_capsules", 0)
    record.setdefault("current_capsule", record["completed_capsules"] + 1)
    if not resuming:
        record["target_sha256"] = _sha256_file(worktree_target) if worktree_target.is_file() else None
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
            f"for migration unit `{unit['id']}`. The target file `{unit['target']}` contains the work "
            "from earlier segments; preserve it and continue only with this segment. "
            "Edit only the configured target and the completion record described below. "
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
        try:
            exit_code = _run_interactive_junie(command, completion_path, completion_token, agent_target)
        except KeyboardInterrupt:
            try:
                _apply_agent_target(
                    agent_root, agent_target, worktree_target,
                    relative_target, record, record_path,
                )
            finally:
                record["status"] = "interrupted"
                record["error"] = "interrupted by user; current capsule can be resumed"
                _write_run_record(record_path, record)
            raise RunInterrupted(record["run_id"]) from None
        except OSError as exc:
            record["status"] = "junie_failed"
            record["error"] = str(exc)
            _write_run_record(record_path, record)
            raise

        if (
            _has_symlink_component(agent_root, relative_target)
            or not agent_target.resolve().is_relative_to(agent_root.resolve())
        ):
            record["status"] = "junie_failed"
            record["error"] = f"Junie target escapes its workspace or is a symlink: {unit['target']}"
            _write_run_record(record_path, record)
            raise ValueError(record["error"])
        target_exists = agent_target.is_file()
        _apply_agent_target(
            agent_root, agent_target, worktree_target,
            relative_target, record, record_path,
        )
        record["junie_exit_code"] = exit_code
        if not target_exists or not _has_completion_record(completion_path, completion_token):
            record["status"] = "interrupted" if target_exists else "junie_failed"
            record["error"] = (
                "Junie did not leave a valid completion record for this capsule" if target_exists
                else f"Junie did not produce the configured target: {unit['target']}"
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
    worktree = Path(result["worktree_path"])
    run_path = Path(result["run_path"])
    record_path = run_path / "run.json"
    log_path = run_path / "build.log"
    build_project = config["build_project"]
    command = ["dotnet", "build"]

    record.update(status="building", build_exit_code=None, build_log_path=str(log_path))
    record.pop("error", None)
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


def _run_checker(command: list[str], result: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    run_path = Path(result["run_path"])
    record_path = run_path / "run.json"
    log_path = run_path / "checker.log"
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
                findings, _ = _checker_findings(log_path, worktree)
                record["review_file_hashes"] = {
                    path: _review_target_hash(worktree, Path(path))
                    for path in {finding.path for finding in findings}
                }
            except (OSError, RuntimeError, ValueError):
                # 解析できないチェッカー出力も、元の終了コードとログを保持する。
                pass
    except OSError as exc:
        record.update(status="checker_failed", error=str(exc))
    except KeyboardInterrupt:
        record.update(status="checker_failed", error="checker interrupted by user")
        _write_run_record(record_path, record)
        raise RunInterrupted(record["run_id"]) from None

    _write_run_record(record_path, record)
    return {
        **result,
        "junie_exit_code": record.get("junie_exit_code"),
        "agent_workspace_path": record.get("agent_workspace_path"),
        "completed_capsules": record["completed_capsules"],
        "status": record["status"],
        "error": record.get("error"),
        "build_exit_code": record["build_exit_code"],
        "build_log_path": record.get("build_log_path"),
        "checker_exit_code": record["checker_exit_code"],
        "checker_log_path": str(log_path) if log_path.is_file() else None,
    }


_CHECKER_FINDING = re.compile(
    r"(?P<rule>[\w.-]+)\s+(?P<path>.+?):\d+(?::\d+)?\s+(?P<severity>warning|error):\s*(?P<message>.+)",
    re.IGNORECASE,
)


def _review_target_hash(worktree: Path, relative_target: Path) -> str | None:
    target = worktree / relative_target
    if _has_symlink_component(worktree, relative_target) or not target.resolve().is_relative_to(worktree.resolve()):
        return None
    return _sha256_file(target) if target.is_file() else None


@dataclass(frozen=True)
class CheckerFinding:
    finding_id: str
    repair_fingerprint: str
    path: str
    diagnostic: str


def _checker_findings(log_path: Path, worktree: Path) -> tuple[list[CheckerFinding], bool]:
    worktree = worktree.resolve()
    with log_path.open("rb") as stream:
        output = stream.read(65_537)
    if len(output) > 65_536:
        raise ValueError("checker output is too large for automatic repair")
    findings = []
    unrecognized = False
    for line in output.decode("utf-8-sig").splitlines():
        if not line.strip():
            continue
        match = _CHECKER_FINDING.fullmatch(line.strip())
        if match is None:
            unrecognized = True
            continue
        path = Path(match["path"].replace("\\", "/"))
        resolved = (worktree / path).resolve()
        if not resolved.is_relative_to(worktree):
            unrecognized = True
            continue
        relative = resolved.relative_to(worktree).as_posix()
        # 行・列がずれても、同じ対象ファイルのルールとメッセージで指摘を識別する。
        key = (match["rule"].casefold(), " ".join(match["message"].split()))
        repair_fingerprint = hashlib.sha256(json.dumps(key).encode("utf-8")).hexdigest()
        review_key = (*key, relative, match["severity"].lower())
        finding_id = hashlib.sha256(json.dumps(review_key).encode("utf-8")).hexdigest()
        findings.append(CheckerFinding(finding_id, repair_fingerprint, relative, line.strip()))
    if not findings or sum(len(finding.diagnostic) + 1 for finding in findings) > 8_000:
        raise ValueError("checker output contains no supported findings or too much text for automatic repair")
    return findings, unrecognized


def _accepted_findings(record: dict[str, Any], findings: list[CheckerFinding], hashes: dict[str, str | None]) -> set[str]:
    accepted = {(entry["finding_id"], entry["target_sha256"]) for entry in record.get("checker_reviews", [])}
    counts = Counter(finding.finding_id for finding in findings)
    return {
        finding.finding_id for finding in findings
        if counts[finding.finding_id] == 1 and (finding.finding_id, hashes.get(finding.path)) in accepted
    }


def _repair_target(
    config: dict[str, Any], relative_target: Path, worktree: Path, run_path: Path,
    findings: str, record: dict[str, Any],
) -> None:
    _validate_junie_execution()
    agent_root = run_path / "agent-workspace"
    if agent_root.is_symlink() or not agent_root.is_dir():
        raise ValueError("saved Junie workspace is missing or unsafe")
    agent_target = agent_root / relative_target
    worktree_target = worktree / relative_target
    for root, target in ((agent_root, agent_target), (worktree, worktree_target)):
        if _has_symlink_component(root, relative_target) or not target.resolve().is_relative_to(root.resolve()):
            raise ValueError("repair target escapes its workspace or is a symlink")
    before = _sha256_file(worktree_target)
    shutil.copyfile(worktree_target, agent_target)
    token = uuid.uuid4().hex
    completion_path = agent_root / f"migration-complete-{token}.txt"
    findings_path = agent_root / f"checker-findings-{token}.txt"
    with findings_path.open("x", encoding="utf-8") as stream:
        stream.write(findings + "\n")
    prompt = (
        f"Read `{findings_path.name}` and repair only `{relative_target.as_posix()}` in this workspace. "
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
    exit_code = _run_interactive_junie(command, completion_path, token, agent_target)
    record["repair_history"][-1]["junie_exit_code"] = exit_code
    if not _has_completion_record(completion_path, token) or not agent_target.is_file():
        raise ValueError("Junie did not record a completed repair; inspect its workspace for partial edits")
    if not agent_target.resolve().is_relative_to(agent_root.resolve()) or _has_symlink_component(agent_root, relative_target):
        raise ValueError("repair output escapes Junie's workspace or is a symlink")
    if _sha256_file(agent_target) == before:
        raise ValueError("Junie left the repair target unchanged")
    if (
        _has_symlink_component(worktree, relative_target)
        or not worktree_target.resolve().is_relative_to(worktree.resolve())
        or _sha256_file(worktree_target) != before
    ):
        raise ValueError("the worktree target changed during the repair")
    _apply_agent_target(agent_root, agent_target, worktree_target, relative_target, record, run_path / "run.json")


def _verify_worktree(
    config: dict[str, Any], unit: dict[str, Any], result: dict[str, Any], record: dict[str, Any],
    *, build_first: bool = True,
) -> dict[str, Any]:
    run_path = Path(result["run_path"])
    worktree = Path(result["worktree_path"])
    target = Path(unit["target"])
    record_path = run_path / "run.json"
    command = config["checker_command"]
    limit = config["max_repair_attempts"]
    history = record.setdefault("repair_history", [])
    while True:
        verified = _build_worktree(config, result, record) if build_first else result
        if command is None or record["status"] == "build_failed":
            return verified
        checked = _run_checker(command, verified, record)
        if checked["status"] != "checker_needs_review" or (not limit and not record.get("checker_reviews")):
            return checked
        try:
            if checked["checker_exit_code"] <= 0:
                raise ValueError("checker did not complete normally")
            findings, unrecognized = _checker_findings(run_path / "checker.log", worktree)
            accepted = _accepted_findings(record, findings, record.get("review_file_hashes", {}))
            remaining = [finding for finding in findings if finding.finding_id not in accepted]
            if not remaining:
                if unrecognized:
                    raise ValueError("checker output includes unrecognized text that cannot be accepted")
                record["status"] = "checker_accepted"
                _write_run_record(record_path, record)
                return {**checked, "status": record["status"]}
            if not limit:
                return checked
            if any(Path(finding.path) != target for finding in remaining):
                raise ValueError("checker reported a finding outside the configured migration target")
            fingerprints = {finding.repair_fingerprint for finding in remaining}
            if any(fingerprints.intersection(attempt["findings"]) for attempt in history):
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
            print(f"Junie repair {attempt}/{limit} for {target}", flush=True)
            _repair_target(config, target, worktree, run_path, "\n".join(finding.diagnostic for finding in remaining), record)
        except (OSError, RuntimeError, ValueError) as exc:
            record.update(status="checker_needs_review", error=str(exc))
            _write_run_record(record_path, record)
            return {**checked, "error": str(exc)}
        except KeyboardInterrupt:
            record.update(status="checker_needs_review", error="repair interrupted; inspect Junie's workspace")
            _write_run_record(record_path, record)
            raise RunInterrupted(record["run_id"]) from None
        build_first = True


def run_with_junie(config: dict[str, Any], unit_id: str) -> dict[str, Any]:
    _validate_junie_execution()
    result = prepare_run(config, unit_id)
    unit = _unit(config, unit_id)
    record_path = Path(result["run_path"]) / "run.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    return _run_prepared_with_junie(config, unit, Path(result["run_path"]), result, record, resuming=False)


def _validate_junie_execution() -> None:
    if not sys.stdin.isatty():
        raise RuntimeError("Junie run requires an interactive terminal")
    if shutil.which("junie") is None:
        raise RuntimeError("Junie CLI was not found on PATH")


def _load_saved_run(
    config: dict[str, Any], run_id: str,
) -> tuple[Path, Path, dict[str, Any], dict[str, Any]]:
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
    if unit["target"] != record.get("target"):
        raise ValueError("configured target differs from the target recorded for this run")
    worktree_root = Path(config["worktree_root"]).resolve()
    if not isinstance(record.get("worktree_path"), str):
        raise ValueError("run record is missing its worktree path")
    worktree_path = Path(record["worktree_path"]).resolve()
    if worktree_path.parent != worktree_root or worktree_path.name != run_id:
        raise ValueError("run worktree path does not match the configured worktree root and run ID")
    return run_path, worktree_path, record, unit


def review_checker(
    config: dict[str, Any], run_id: str, finding_id: str | None, reason: str | None,
) -> dict[str, Any]:
    if (finding_id is None) != (reason is None) or (reason is not None and not reason.strip()):
        raise ValueError("--accept requires a finding ID and a non-empty --reason")
    run_path, worktree, record, unit = _load_saved_run(config, run_id)
    allowed_status = {"checker_needs_review"} if finding_id is not None else {"checker_needs_review", "checker_accepted"}
    if record.get("status") not in allowed_status or (record.get("checker_exit_code") or 0) <= 0:
        raise ValueError("run has no checker findings to review")
    log_path = run_path / "checker.log"
    if log_path.is_symlink():
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
        "unrecognized": unrecognized,
        "log_path": str(log_path),
    }


def resume_with_junie(config: dict[str, Any], run_id: str) -> dict[str, Any]:
    run_path, worktree_path, record, unit = _load_saved_run(config, run_id)
    build_only = record.get("status") in {"junie_finished", "building", "build_failed", "repairing"}
    checker_only = record.get("status") in {"build_succeeded", "checking", "checker_needs_review", "checker_failed"}
    if checker_only and (config["checker_command"] is None or record.get("build_exit_code") != 0):
        raise ValueError("resuming the checker requires a successful build and a checker command")
    if not (build_only or checker_only) and record.get("status") not in {"prepared", "junie_running", "junie_failed", "interrupted"}:
        raise ValueError(f"run is not resumable from status {record.get('status')!r}")

    max_steps = record.get("junie_max_steps", config["junie_max_steps"])
    if max_steps is not None and (isinstance(max_steps, bool) or not isinstance(max_steps, int) or max_steps < 1):
        raise ValueError("run record contains an invalid Junie max-steps value")
    resume_attempts = record.get("resume_attempts", 0)
    if not isinstance(resume_attempts, int) or isinstance(resume_attempts, bool) or resume_attempts < 0:
        raise ValueError("run record contains an invalid resume-attempt count")
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
    migration_complete = build_only or checker_only
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
    if checker_only:
        # 人が修正した後に再開する場合も、修正後の成果物をチェッカーに渡す。
        return _verify_worktree(
            config, unit, result, record, build_first=bool(record.get("repair_history") or record.get("checker_reviews")),
        )
    if build_only:
        return _verify_worktree(config, unit, result, record)
    _validate_junie_execution()
    record.pop("error", None)
    return _run_prepared_with_junie(
        config, unit, run_path, result, record, resuming=record["status"] != "prepared",
    )
