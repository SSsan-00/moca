from __future__ import annotations

import codecs
import tomllib
from pathlib import Path
from typing import Any

_INPUT_ENCODINGS = {"auto", "utf-8", "utf-8-sig", "euc-jp"}
SCRIPT_PHASES = (
    "before_prepare", "after_prepare", "before_migration", "after_migration",
    "before_build", "after_build", "before_checker", "after_checker",
)
REFERENCE_ROLES = ("reference_php", "reference_csharp", "reference_razor")



def _input_encoding(value: Any, scope: str) -> str:
    """許可する入力文字コードの別名を標準名にそろえる。"""
    if isinstance(value, str):
        try:
            encoding = "auto" if value == "auto" else codecs.lookup(value).name.replace("_", "-")
        except (LookupError, ValueError):
            pass
        else:
            if encoding in _INPUT_ENCODINGS:
                return encoding
    raise ValueError(f"{scope} must be auto, utf-8, utf-8-sig, or euc-jp")


def _draft_extension(path: Path) -> str:
    """仮移行先の種類を判定する複合拡張子を返す。"""
    if path.name.lower().endswith(".cshtml.cs"):
        return ".cshtml.cs"
    return path.suffix.lower()


def _draft_import_commands(unit: dict[str, Any], targets: list[Path], root: Path) -> list[list[str]]:
    """移行単位の仮成果物コピー設定をコマンドへ変換する。"""
    sources = unit.get("draft_imports", [])
    scope = f"移行単位「{unit['id']}」の仮C#取り込み"
    if not isinstance(sources, list):
        raise ValueError(f"{scope}はコピー元パスの配列で指定してください。")
    commands = []
    used_targets = set()
    for source in sources:
        path = root / _file_path(source, root, scope)
        if len(targets) == 1:
            destination = targets[0]
        else:
            extension = _draft_extension(Path(source))
            matches = [target for target in targets if extension and _draft_extension(target) == extension]
            if not matches and extension == ".cs":
                matches = [target for target in targets if _draft_extension(target) == ".cshtml.cs"]
            if len(matches) != 1:
                reason = "ありません" if not matches else "複数あります"
                raise ValueError(
                    f"{scope}で、コピー元「{source}」の拡張子に対応する移行先が{reason}。"
                    "コピー元と移行先の拡張子を確認し、対応する移行先を1つにしてください。"
                )
            destination = matches[0]
        if destination in used_targets:
            raise ValueError(
                f"{scope}で、移行先「{destination.as_posix()}」に対応するコピー元が複数あります。"
                "コピー元を1つにしてください。"
            )
        used_targets.add(destination)
        commands.append(["@copy-migration-draft", str(path), destination.as_posix()])
    return commands


def _file_path(value: Any, root: Path, scope: str, *, inside: bool = False) -> str:
    """入力は外部ファイルも許可し、編集対象は元リポジトリ内の相対パスにそろえる。"""
    if not isinstance(value, str) or not value.strip() or "\0" in value or not Path(value).parts:
        raise ValueError(f"{scope}: specify a file path")
    path = (root / Path(value).expanduser()).resolve()
    if path == root:
        raise ValueError(f"{scope}: specify a file, not the application repository root")
    if path.is_relative_to(root):
        return path.relative_to(root).as_posix()
    if inside:
        raise ValueError(f"{scope}: file must be inside the application repository: {path}")
    return path.as_posix()


def _outside(root: Path, path: Path, name: str) -> Path:
    """アプリのリポジトリと実行データが混ざらないよう、保存先を解決する。"""
    resolved = path.expanduser().resolve()
    if resolved == root or resolved.is_relative_to(root):
        raise ValueError(f"{name} must be outside the application repository: {resolved}")
    return resolved


def load_config(path: Path) -> dict[str, Any]:
    """ローカルTOMLを検証し、準備と再開で共通の設定形式に整える。"""
    config_path = path.expanduser().resolve()
    with config_path.open("rb") as stream:
        raw = tomllib.load(stream)

    project = raw.get("project", {})
    if not isinstance(project, dict):
        raise ValueError("[project] must be a table")
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
        """相対指定の保存先を、設定ファイルのあるディレクトリから解決する。"""
        candidate = Path(value).expanduser()
        return (config_path.parent / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()

    worktree_root = _outside(root, resolve_output(worktree_value), "worktree_root")
    run_root = _outside(root, resolve_output(run_value), "run_root")
    if worktree_root == run_root or worktree_root.is_relative_to(run_root) or run_root.is_relative_to(worktree_root):
        raise ValueError("worktree_root and run_root must be separate directories")

    max_context_chars = project.get("max_context_chars", 500000)
    if not isinstance(max_context_chars, int) or max_context_chars < 1000:
        raise ValueError("[project].max_context_chars must be an integer of at least 1000")

    build = raw.get("build", {})
    if not isinstance(build, dict):
        raise ValueError("[build] must be a table")
    build_project = build.get("project")
    if build_project is not None:
        build_project = _file_path(build_project, root, "[build].project", inside=True)
        build_path = Path(build_project)
        if build_path.suffix.lower() not in {".csproj", ".sln", ".slnx"}:
            raise ValueError("[build].project must name a .csproj, .sln or .slnx inside the worktree")

    checker = raw.get("checker", {})
    if not isinstance(checker, dict):
        raise ValueError("[checker] must be a table")
    checker_command = checker.get("command")
    if "command" in checker:
        validate_checker_command(checker_command)
    max_repair_attempts = checker.get("max_repair_attempts", 2)
    if type(max_repair_attempts) is not int or max_repair_attempts < 0:
        raise ValueError("[checker].max_repair_attempts must be a non-negative integer")

    scripts = raw.get("scripts", {})
    if not isinstance(scripts, dict) or scripts.keys() - set(SCRIPT_PHASES):
        raise ValueError("[scripts] must contain only supported execution phases")
    for phase, commands in scripts.items():
        if not isinstance(commands, list) or any(not is_command(command) for command in commands):
            raise ValueError(f"[scripts].{phase} must list commands as arrays of an executable followed by arguments")
        for command in commands:
            if command[0] == "@copy-migration-draft" and phase == "before_migration":
                if len(command) != 3:
                    raise ValueError("@copy-migration-draft requires a source file and destination")
                command[1] = str(root / _file_path(command[1], root, "draft source"))
                command[2] = _file_path(command[2], root, "draft destination", inside=True)

    viewer = raw.get("diff_viewer", {})
    if not isinstance(viewer, dict):
        raise ValueError("[diff_viewer] must be a table")
    viewer_enabled = viewer.get("enabled", True)
    if type(viewer_enabled) is not bool:
        raise ValueError("[diff_viewer].enabled must be a boolean")
    viewer_html = viewer.get("html", "")
    if not isinstance(viewer_html, str):
        raise ValueError("[diff_viewer].html must be a file path")
    viewer_html = viewer_html.strip() or None
    if viewer_enabled and viewer_html:
        viewer_html = _file_path(viewer_html, root, "[diff_viewer].html")
        viewer_path = Path(viewer_html)
        if viewer_path.suffix.lower() != ".html":
            raise ValueError("[diff_viewer].html must name an .html file")
    anchor_references = viewer.get("anchor_reference", [])
    if isinstance(anchor_references, str):
        anchor_references = [anchor_references]
    if not isinstance(anchor_references, list) or any(
        not isinstance(path, str) or not path.strip() for path in anchor_references
    ):
        raise ValueError("[diff_viewer].anchor_reference must contain workspace JSON file paths")

    junie = raw.get("junie", {})
    if not isinstance(junie, dict):
        raise ValueError("[junie] must be a table")
    junie_model = junie.get("model")
    if junie_model is not None:
        if not isinstance(junie_model, str) or any(character.isspace() or ord(character) < 32 or ord(character) == 127
                                                   for character in junie_model) or junie_model.startswith("-"):
            raise ValueError("Junieのモデルは、空白を含まないモデルIDで指定してください。既定値に戻す場合は空欄にしてください。")
        junie_model = junie_model or None
    junie_effort = junie.get("effort")
    if junie_effort is not None:
        if not isinstance(junie_effort, str) or junie_effort not in ("", "none", "minimal", "low", "medium", "high", "xhigh", "max"):
            raise ValueError("推論の強さは none・minimal・low・medium・high・xhigh・max のいずれかを指定してください。")
        junie_effort = junie_effort or None
        if junie_effort and not junie_model:
            raise ValueError("推論の強さを指定する場合は、Junieで使うモデルも選択してください。")
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

    project_rules = project.get("migration_rules", [])
    if not isinstance(project_rules, list):
        raise ValueError("[[project.migration_rules]] must list rule files")
    common_rules = []
    for item in project_rules:
        rule = _normalize_input(item, "project migration rules", root=root, project_rule=True)
        if rule not in common_rules:
            common_rules.append(rule)
    project_references = project.get("references", [])
    if not isinstance(project_references, list):
        raise ValueError("[[project.references]] must list reference files")
    common_references = []
    for item in project_references:
        reference = _normalize_input(item, "project references", root=root, project_reference=True)
        common_references.append(reference)

    normalized_units = []
    for unit in units:
        targets = unit.get("targets", [unit.get("target")])
        if "targets" in unit and "target" in unit:
            raise ValueError(f"unit {unit['id']}: specify target or targets")
        if not isinstance(targets, list) or not targets:
            raise ValueError(f"unit {unit['id']}: targets must list file paths")
        targets = [_file_path(target, root, f"unit {unit['id']} targets", inside=True) for target in targets]
        target_paths = [Path(target) for target in targets]
        if len(set(target_paths)) != len(target_paths) or any(
            parent in target_paths for target in target_paths for parent in target.parents
        ):
            raise ValueError(f"unit {unit['id']}: target paths must be distinct files")
        for target in target_paths:
            name = target.name.lower()
            if name.endswith(".cshtml"):
                counterpart = target.with_name(f"{target.name}.cs")
            elif name.endswith(".cshtml.cs"):
                counterpart = target.with_suffix("")
            else:
                continue
            if counterpart not in target_paths:
                raise ValueError(
                    f"unit {unit['id']}: {target.as_posix()} の対応ファイル "
                    f"{counterpart.as_posix()} を targets に指定してください。"
                )

        normalized_inputs = []
        if "source" in unit:
            source = _file_path(unit["source"], root, f"unit {unit['id']} source")
            encoding = _input_encoding(unit.get("source_encoding", "auto"), f"unit {unit['id']}: source_encoding")
            normalized_inputs.append({
                "role": "php", "path": source, "encoding": encoding,
                "shared": False, "start_line": 1, "end_line": None,
            })

        inputs = unit.get("inputs", [])
        if not isinstance(inputs, list) or not (normalized_inputs or inputs):
            raise ValueError(f"unit {unit['id']}: specify source or at least one [[units.inputs]] entry")
        for item in inputs:
            normalized_input = _normalize_input(item, f"unit {unit['id']}", root=root)
            if normalized_input["role"] == "migration_rules":
                if normalized_input not in common_rules:
                    common_rules.append(normalized_input)
            elif normalized_input["role"] in REFERENCE_ROLES and normalized_input["shared"]:
                common_references.append(normalized_input)
            else:
                normalized_inputs.append(normalized_input)

        normalized = {"id": unit["id"], "target": targets[0], "inputs": normalized_inputs}
        if "draft_imports" in unit:
            normalized["draft_import_commands"] = _draft_import_commands(unit, target_paths, root)
        if "checker_command" in unit:
            try:
                validate_checker_command(unit["checker_command"])
            except ValueError as exc:
                raise ValueError(f"unit {unit['id']}: {exc}") from exc
            normalized["checker_command"] = unit["checker_command"]
        if "targets" in unit:
            normalized["targets"] = [target.as_posix() for target in target_paths]
            normalized["target"] = normalized["targets"][0]
        normalized_units.append(normalized)

    for unit in normalized_units:
        unit["inputs"] = common_rules + common_references + unit["inputs"]

    return {
        "config_path": str(config_path),
        "project_root": str(root),
        "base_ref": base_ref,
        "worktree_root": str(worktree_root),
        "run_root": str(run_root),
        "max_context_chars": max_context_chars,
        "build_project": build_project,
        "checker_command": checker_command,
        "scripts": scripts,
        "diff_viewer_html": viewer_html if viewer_enabled else None,
        "diff_viewer_anchor_reference": [resolve_output(path) for path in anchor_references],
        "max_repair_attempts": max_repair_attempts,
        "junie_max_steps": junie_max_steps,
        "junie_model": junie_model,
        "junie_effort": junie_effort,
        "units": normalized_units,
    }


def _normalize_input(
    item: Any, scope: str, *, root: Path, project_rule: bool = False, project_reference: bool = False,
) -> dict[str, Any]:
    """入力資料のパスと範囲を正規化する。"""
    if not isinstance(item, dict):
        raise ValueError(f"{scope}: each [[units.inputs]] entry must be a table")
    item_path = _file_path(item.get("path"), root, scope)
    role = "migration_rules" if project_rule else item.get("role")
    encoding = _input_encoding(item.get("encoding", "auto"), f"{scope}: input encoding")
    symbol = item.get("symbol")
    shared = item.get("shared", False)
    start = item.get("start_line")
    end = item.get("end_line")
    if not isinstance(role, str) or not role.strip():
        raise ValueError(f"{scope}: each input needs a role")
    if project_reference and role not in REFERENCE_ROLES:
        raise ValueError(f"{scope}: use reference_php, reference_csharp or reference_razor")
    if symbol is not None and (not isinstance(symbol, str) or not symbol.strip()):
        raise ValueError(f"{scope}: input symbol must be a non-empty string")
    if not isinstance(shared, bool):
        raise ValueError(f"{scope}: input shared must be a boolean")
    shared = shared or role == "migration_rules" or project_reference
    if role == "migration_rules" or (shared and role in REFERENCE_ROLES):
        start, end = 1, None
    elif start is None and end is None and shared:
        start = 1
    elif not isinstance(start, int) or not isinstance(end, int) or start < 1 or end < start:
        raise ValueError(f"{scope}: input line ranges must be positive and ordered")
    normalized = {
        "role": role,
        "path": item_path,
        "encoding": encoding,
        "shared": shared,
        "start_line": start,
        "end_line": end,
    }
    if symbol is not None:
        normalized["symbol"] = symbol.strip()
    if role == "migration_rules":
        usage = item.get("usage", "required")
        description = item.get("description", "")
        validate_rule_usage(usage, description)
        normalized.update(usage=usage, description=description)
    return normalized


def validate_rule_usage(usage: Any, description: Any) -> None:
    """ルールの用途と説明の設定を検証する。"""
    if not isinstance(usage, str) or usage not in {"required", "reference"}:
        raise ValueError(
            "移行ルールの読み込み方（usage）は「毎回適用する必須事項」（required）または"
            "「必要時に読む詳細・例」（reference）を指定してください。"
        )
    if not isinstance(description, str):
        raise ValueError("移行ルールの「読む場面・内容」（description）は文字列で入力してください。")
    if usage == "reference" and not description.strip():
        raise ValueError("「必要時に読む詳細・例」には「読む場面・内容」を入力してください。空白だけでは保存できません。")


def validate_checker_command(command: Any) -> None:
    """シェル文字列ではなく、実行ファイルと引数の配列を受理する。"""
    if not is_command(command):
        raise ValueError("checker command must be a non-empty array with an executable followed by arguments")


def is_command(command: Any) -> bool:
    """値が実行可能なコマンドとして設定されているか判定する。"""
    return (
        isinstance(command, list) and bool(command)
        and all(isinstance(arg, str) and "\0" not in arg for arg in command)
        and bool(command[0].strip())
    )


def find_unit(config: dict[str, Any], unit_id: str) -> dict[str, Any]:
    """設定で許可された移行単位だけを解決する。"""
    for unit in config["units"]:
        if unit["id"] == unit_id:
            return unit
    raise ValueError(f"unknown migration unit: {unit_id}")


def checker_command_for_unit(config: dict[str, Any], unit: dict[str, Any]) -> list[str] | None:
    """CLI指定、移行単位、プロジェクト共通の順でチェッカーを選ぶ。"""
    if config.get("checker_from_cli"):
        return config["checker_command"]
    return unit.get("checker_command", config["checker_command"])


def target_paths(unit: dict[str, Any]) -> tuple[Path, ...]:
    """単一移行先を指定した旧設定も同じ順序付き形式で扱う。"""
    return tuple(Path(name) for name in unit.get("targets", [unit["target"]]))
