from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any


def _outside(root: Path, path: Path, name: str) -> Path:
    resolved = path.expanduser().resolve()
    if resolved == root or resolved.is_relative_to(root):
        raise ValueError(f"{name} must be outside the application repository: {resolved}")
    return resolved


def load_config(path: Path) -> dict[str, Any]:
    config_path = path.expanduser().resolve()
    with config_path.open("rb") as stream:
        raw = tomllib.load(stream)

    project = raw.get("project", {})
    root_value = project.get("root")
    if not isinstance(root_value, str) or not root_value.strip():
        raise ValueError("[project].root is required")

    root = Path(root_value).expanduser()
    if not root.is_absolute():
        root = (config_path.parent / root).resolve()
    else:
        root = root.resolve()
    if not root.is_dir() or not (root / ".git").exists():
        raise ValueError(f"project root must be an existing Git worktree: {root}")

    base_ref = project.get("base_ref", "HEAD")
    if not isinstance(base_ref, str) or not base_ref.strip():
        raise ValueError("[project].base_ref must be a non-empty Git ref")

    worktree_value = project.get("worktree_root")
    run_value = project.get("run_root")
    if not isinstance(worktree_value, str) or not isinstance(run_value, str):
        raise ValueError("[project].worktree_root and [project].run_root are required")

    def resolve_output(value: str) -> Path:
        candidate = Path(value).expanduser()
        return (config_path.parent / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()

    # 移行元の作業ツリーと生成データを分離し、誤変更や公開リポジトリへの混入を防ぐ。
    worktree_root = _outside(root, resolve_output(worktree_value), "worktree_root")
    run_root = _outside(root, resolve_output(run_value), "run_root")
    if worktree_root == run_root or worktree_root.is_relative_to(run_root) or run_root.is_relative_to(worktree_root):
        raise ValueError("worktree_root and run_root must be separate directories")

    max_context_chars = project.get("max_context_chars", 100000)
    if not isinstance(max_context_chars, int) or max_context_chars < 1000:
        raise ValueError("[project].max_context_chars must be an integer of at least 1000")

    build = raw.get("build", {})
    if not isinstance(build, dict):
        raise ValueError("[build] must be a table")
    build_project = build.get("project")
    if build_project is not None:
        if not isinstance(build_project, str) or not build_project.strip():
            raise ValueError("[build].project must be a path relative to the worktree")
        build_path = Path(build_project)
        if build_path.anchor or ".." in build_path.parts or build_path.suffix.lower() not in {".csproj", ".sln", ".slnx"}:
            raise ValueError("[build].project must name a .csproj, .sln or .slnx inside the worktree")

    checker = raw.get("checker", {})
    if not isinstance(checker, dict):
        raise ValueError("[checker] must be a table")
    checker_command = checker.get("command")
    if "command" in checker:
        validate_checker_command(checker_command)
    max_repair_attempts = checker.get("max_repair_attempts", 0)
    if type(max_repair_attempts) is not int or max_repair_attempts < 0:
        raise ValueError("[checker].max_repair_attempts must be a non-negative integer")

    viewer = raw.get("diff_viewer", {})
    if not isinstance(viewer, dict):
        raise ValueError("[diff_viewer] must be a table")
    viewer_html = viewer.get("html", "tools/dist/index.min.html")
    if not isinstance(viewer_html, str) or not viewer_html.strip():
        raise ValueError("[diff_viewer].html must be a path inside the application repository")
    viewer_path = Path(viewer_html)
    if viewer_path.anchor or ".." in viewer_path.parts or viewer_path.suffix.lower() != ".html":
        raise ValueError("[diff_viewer].html must name a relative .html file inside the application repository")
    anchor_reference = viewer.get("anchor_reference")
    if anchor_reference is not None and (
        not isinstance(anchor_reference, str) or not anchor_reference.strip()
    ):
        raise ValueError("[diff_viewer].anchor_reference must name a workspace JSON file")

    junie = raw.get("junie", {})
    if not isinstance(junie, dict):
        raise ValueError("[junie] must be a table")
    junie_max_steps = junie.get("max_steps")
    if junie_max_steps is not None and (
        isinstance(junie_max_steps, bool) or not isinstance(junie_max_steps, int) or junie_max_steps < 1
    ):
        raise ValueError("[junie].max_steps must be a positive integer when specified")
    units = raw.get("units")
    if not isinstance(units, list) or not units:
        raise ValueError("at least one [[units]] entry is required")

    ids = [unit.get("id") for unit in units if isinstance(unit, dict)]
    if len(ids) != len(units) or any(not isinstance(unit_id, str) or not unit_id.strip() for unit_id in ids):
        raise ValueError("every [[units]] entry needs a non-empty id")
    if len(ids) != len(set(ids)):
        raise ValueError("migration unit IDs must be unique")

    normalized_units = []
    for unit in units:
        targets = unit.get("targets", [unit.get("target")])
        if "targets" in unit and "target" in unit:
            raise ValueError(f"unit {unit['id']}: specify target or targets")
        if not isinstance(targets, list) or not targets or any(
            not isinstance(target, str) or not target.strip() or not Path(target).parts
            or Path(target).anchor or ".." in Path(target).parts
            for target in targets
        ):
            raise ValueError(f"unit {unit['id']}: targets must be safe repository-relative paths")
        target_paths = [Path(target) for target in targets]
        if len(set(target_paths)) != len(target_paths) or any(
            parent in target_paths for target in target_paths for parent in target.parents
        ):
            raise ValueError(f"unit {unit['id']}: target paths must be distinct files")

        inputs = unit.get("inputs")
        if not isinstance(inputs, list) or not inputs:
            raise ValueError(f"unit {unit['id']}: at least one [[units.inputs]] entry is required")
        # リポジトリ全体を走査せず、設定した行範囲だけをAIに渡す。
        normalized_inputs = []
        for item in inputs:
            item_path = item.get("path")
            role = item.get("role")
            encoding = item.get("encoding", "auto")
            symbol = item.get("symbol")
            shared = item.get("shared", False)
            start = item.get("start_line")
            end = item.get("end_line")
            if not isinstance(item_path, str) or Path(item_path).is_absolute() or ".." in Path(item_path).parts:
                raise ValueError(f"unit {unit['id']}: input path must be repository-relative")
            if not isinstance(role, str) or not role.strip():
                raise ValueError(f"unit {unit['id']}: each input needs a role")
            if symbol is not None and (not isinstance(symbol, str) or not symbol.strip()):
                raise ValueError(f"unit {unit['id']}: input symbol must be a non-empty string")
            if not isinstance(encoding, str) or encoding not in {"auto", "utf-8", "utf-8-sig", "euc-jp"}:
                raise ValueError(f"unit {unit['id']}: input encoding must be auto, utf-8, utf-8-sig, or euc-jp")
            if not isinstance(shared, bool):
                raise ValueError(f"unit {unit['id']}: input shared must be a boolean")
            if not isinstance(start, int) or not isinstance(end, int) or start < 1 or end < start:
                raise ValueError(f"unit {unit['id']}: input line ranges must be positive and ordered")
            normalized_inputs.append({
                "role": role,
                "path": item_path,
                "encoding": encoding,
                "shared": shared,
                "start_line": start,
                "end_line": end,
            })
            if symbol is not None:
                normalized_inputs[-1]["symbol"] = symbol.strip()

        normalized = {"id": unit["id"], "target": targets[0], "inputs": normalized_inputs}
        if "targets" in unit:
            normalized["targets"] = [target.as_posix() for target in target_paths]
            normalized["target"] = normalized["targets"][0]
        normalized_units.append(normalized)

    return {
        "config_path": str(config_path),
        "project_root": str(root),
        "base_ref": base_ref,
        "worktree_root": str(worktree_root),
        "run_root": str(run_root),
        "max_context_chars": max_context_chars,
        "build_project": build_project,
        "checker_command": checker_command,
        "diff_viewer_html": viewer_html,
        "diff_viewer_anchor_reference": resolve_output(anchor_reference) if anchor_reference is not None else None,
        "max_repair_attempts": max_repair_attempts,
        "junie_max_steps": junie_max_steps,
        "units": normalized_units,
    }


def validate_checker_command(command: Any) -> None:
    if (
        not isinstance(command, list)
        or not command
        or any(not isinstance(arg, str) or "\0" in arg for arg in command)
        or not command[0].strip()
    ):
        raise ValueError("checker command must be a non-empty array with an executable followed by arguments")
