"""ダッシュボードのフォームと、コメントを保持したTOMLの相互変換。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

import tomlkit
from tomlkit.exceptions import TOMLKitError

from .config import REFERENCE_ROLES, SCRIPT_PHASES, validate_rule_usage


_TABLE_FIELDS = {
    "project": ("root", "worktree_root", "run_root", "base_ref", "max_context_chars"),
    "build": ("project",),
    "checker": ("command", "max_repair_attempts"),
    "junie": ("max_steps", "model", "effort", "warn_input_bytes", "max_input_bytes"),
    "diff_viewer": ("html", "enabled", "anchor_reference"),
    "target_context": ("roots", "folders", "exclude", "recursive", "max_files", "max_bytes", "max_file_bytes"),
}
_NUMERIC_FIELDS = {
    "max_repair_attempts": (0, "自動修正回数"),
    "max_context_chars": (1000, "カプセルの文字数上限"),
    "max_steps": (1, "Junieのステップ数上限"),
    "start_line": (1, "開始行"),
    "end_line": (1, "終了行"),
    "max_files": (0, "参照ファイル数の上限"),
    "max_bytes": (0, "参照資料の合計容量の上限"),
    "max_file_bytes": (0, "参照ファイル1件の容量の上限"),
    "warn_input_bytes": (1, "Junie の入力容量の警告値"),
    "max_input_bytes": (1, "Junie の入力容量の停止値"),
}
_INPUT_FIELDS = ("path", "start_line", "end_line", "encoding", "symbol")
_RULE_FIELDS = ("usage", "description")
_UNIT_SETTINGS = ("id", "source", "targets", "checker_command", "draft_imports", "target_context")
_REFERENCE_TARGET_ROLES = ("reference_csharp", "reference_razor")


def _settings_integer(value: Any, key: str) -> int:
    minimum, label = _NUMERIC_FIELDS[key]
    if type(value) is not int and (not isinstance(value, str) or not re.fullmatch(r"[0-9]+", value)):
        raise ValueError(f"{label}は{minimum}以上の整数で入力してください。")
    number = int(value)
    if number < minimum:
        raise ValueError(f"{label}は{minimum}以上の整数で入力してください。")
    return number


def _material_form_entry(item: Any, index: int, key: str) -> dict[str, Any]:
    if isinstance(item, Mapping) and isinstance(item.get("path"), str):
        role = "migration_rules" if key == "migration_rules" else item.get("role")
        if key != "references" or role in REFERENCE_ROLES:
            fields = _INPUT_FIELDS + _RULE_FIELDS if role == "migration_rules" else _INPUT_FIELDS
            return {"index": index, "role": role or "", **{name: item[name] for name in fields if name in item}}
    # フォームに出せない旧設定も、元の位置を保持して保存時に残す。
    return {"index": index}


def _apply_material_edits(container: Mapping, key: str, edits: Any) -> None:
    """フォーム対応の入力だけを編集し、他の入力と既存テーブルの属性を保持する。"""
    inputs = container.get(key, [])
    common = key == "migration_rules"
    roles = ("migration_rules",) if common else REFERENCE_ROLES
    error = "移行ルール・参考実装の入力内容を確認してください。"
    if not isinstance(inputs, list) or not isinstance(edits, list):
        raise ValueError(error)
    retained = set()
    previous_index = -1
    ordered = []
    for edit in edits:
        if not isinstance(edit, dict):
            raise ValueError(error)
        index = edit.get("index")
        if index is not None:
            if type(index) is not int or not previous_index < index < len(inputs):
                raise ValueError(error)
            previous_index = index
            retained.add(index)
            item = _material_form_entry(inputs[index], index, key)
            if "path" not in item:
                if set(edit) != {"index"}:
                    raise ValueError(error)
                ordered.append((index, None))
                continue
            if edit.get("role") != item["role"] and not (
                edit.get("role") in _REFERENCE_TARGET_ROLES and item["role"] in _REFERENCE_TARGET_ROLES
            ):
                raise ValueError(error)
        role, path = edit.get("role"), edit.get("path")
        if not isinstance(path, str) or role not in roles:
            raise ValueError(error)
        values = {"path": path}
        if common:
            previous = inputs[index] if index is not None else {}
            validate_rule_usage(
                edit.get("usage", previous.get("usage", "required")),
                edit.get("description", previous.get("description", "")),
            )
            values.update({name: edit[name] for name in _RULE_FIELDS if name in edit})
        if not common:
            values["role"] = role
        for name in _INPUT_FIELDS[1:]:
            if name not in edit:
                continue
            value = edit[name]
            if name in ("start_line", "end_line"):
                value = _settings_integer(value, name) if value != "" else None
            elif not isinstance(value, str):
                raise ValueError(error)
            values[name] = value if value != "" else None
        ordered.append((index, values))

    for index in reversed(range(len(inputs))):
        if index not in retained:
            if "path" not in _material_form_entry(inputs[index], index, key):
                raise ValueError(error)
            del inputs[index]
    if not ordered:
        container.pop(key, None)
        return
    inputs = container.setdefault(key, tomlkit.aot())
    for position, (index, values) in enumerate(ordered):
        if values is None:
            continue
        if index is None:
            item = tomlkit.inline_table() if isinstance(inputs, tomlkit.items.Array) else tomlkit.table()
            inputs.insert(position, item)
        else:
            item = inputs[position]
        for name, value in values.items():
            if value is None:
                item.pop(name, None)
            elif item.get(name) != value:
                item[name] = value


def _promote_legacy_materials(document: Any, units: list) -> bool:
    """旧設定の共通資料を移し、行範囲、文字コード、コメントを保持する。"""
    promoted = False
    for unit in units:
        inputs = unit.get("inputs")
        if not isinstance(inputs, list):
            continue
        legacy = []
        for index, item in enumerate(inputs):
            if not isinstance(item, Mapping):
                continue
            role = item.get("role")
            if role == "migration_rules":
                key = "migration_rules"
                item.pop("role")
            elif role in REFERENCE_ROLES and item.get("shared") is True:
                key = "references"
            else:
                continue
            legacy.append(index)
            item.pop("shared", None)
            project = document.setdefault("project", tomlkit.table())
            materials = project.setdefault(key, tomlkit.aot())
            if key == "migration_rules" and item in materials:
                continue
            if isinstance(materials, tomlkit.items.Array):
                destination = tomlkit.inline_table()
                destination.update(item.unwrap())
                item = destination
            elif isinstance(item, tomlkit.items.InlineTable):
                destination = tomlkit.table()
                destination.update(item)
                item = destination
            materials.append(item)
        for index in reversed(legacy):
            del inputs[index]
        if legacy and not inputs:
            unit.pop("inputs")
        promoted = promoted or bool(legacy)
    return promoted


def _apply_table_edits(document: Any, changes: dict[str, Any]) -> None:
    for section, keys in _TABLE_FIELDS.items():
        _apply_table_fields(document, section, keys, changes.get(section, {}))


def _table_field_value(section, key, value):
    if key == "command":
        if not isinstance(value, list) or any(not isinstance(arg, str) for arg in value):
            raise ValueError("チェッカーのコマンドと引数を確認してください。")
    elif key in {"anchor_reference", "roots", "folders", "exclude"} and value != "":
        if not isinstance(value, list) or any(not isinstance(path, str) or not path.strip() for path in value):
            raise ValueError("パスやパターンは1行に1つ入力してください。")
    elif not isinstance(value, str):
        raise ValueError("設定フォームの入力内容を確認してください。")
    if key in _NUMERIC_FIELDS and value:
        value = _settings_integer(value, key)
    if section == "diff_viewer" and key == "enabled" and value:
        if value != "false":
            raise ValueError("差分ビューアーの使用設定を確認してください。")
        value = False
    if key == "recursive" and value:
        if value not in {"true", "false"}:
            raise ValueError("参照資料の再帰収集の設定を確認してください。")
        value = value == "true"
    return value


def _apply_table_fields(document, section, keys, edits, *, preserve_empty=False):
    if not isinstance(edits, dict) or edits.keys() - set(keys):
        raise ValueError("設定フォームの入力内容を確認してください。")
    for key, raw_value in edits.items():
        value = _table_field_value(section, key, raw_value)
        clear_value = value == "" or (value == [] and key != "roots" and not preserve_empty)
        if clear_value and key not in {"root", "worktree_root", "run_root"}:
            if section in document:
                document[section].pop(key, None)
        else:
            document.setdefault(section, tomlkit.table())[key] = value


def _apply_unit_edits(document: Any, unit_changes: dict[str, Any]) -> None:
    units = document.setdefault("units", tomlkit.aot())
    for index_text, fields in unit_changes.items():
        if not index_text.isascii() or not index_text.isdecimal() or not isinstance(fields, dict):
            raise ValueError("移行単位を選び直してください。")
        index = int(index_text)
        if not 0 <= index <= len(units):
            raise ValueError("移行単位を選び直してください。")
        if index == len(units):
            units.append(tomlkit.table())
        unit = units[index]
        for key, value in fields.items():
            if key not in _UNIT_SETTINGS:
                raise ValueError("移行単位の入力内容を確認してください。")
            elif key == "target_context":
                _apply_table_fields(unit, key, _TABLE_FIELDS[key], value, preserve_empty=True)
            elif key == "checker_command":
                if not isinstance(value, list) or any(not isinstance(arg, str) for arg in value):
                    raise ValueError("移行単位のチェッカーのコマンドと引数を確認してください。")
                if value:
                    unit[key] = value
                else:
                    unit.pop(key, None)
            elif key == "draft_imports":
                if not isinstance(value, list) or any(not isinstance(path, str) or not path.strip() for path in value):
                    raise ValueError("仮C#のコピー元は1行に1ファイル指定してください。")
                if value:
                    unit[key] = value
                else:
                    unit.pop(key, None)
            elif key == "targets":
                if not isinstance(value, list) or any(not isinstance(path, str) for path in value):
                    raise ValueError("移行先は1行に1ファイル指定してください。")
                if len(value) == 1 and "targets" not in unit:
                    unit["target"] = value[0]
                else:
                    # 単数の設定を配列へ変えるときも、元の行末コメントを引き継ぐ。
                    previous = unit.pop("target", None)
                    targets = tomlkit.item(value)
                    if previous is not None:
                        targets.trivia.comment = previous.trivia.comment
                        targets.trivia.comment_ws = previous.trivia.comment_ws
                    unit["targets"] = targets
            elif not isinstance(value, str):
                raise ValueError("移行単位の入力内容を確認してください。")
            elif key == "source" and not value:
                unit.pop(key, None)
            else:
                unit[key] = value


def _remove_empty_units(units: list, indices: Any) -> None:
    if not isinstance(indices, list) or any(
        type(index) is not int or not 0 <= index < len(units) for index in indices
    ):
        raise ValueError("空欄の移行単位を確認してください。")
    for index in sorted(set(indices), reverse=True):
        unit = units[index]
        if (
            unit.keys() - {"id", "source", "target", "targets", "target_context"}
            or any(not isinstance(value, str) or value.strip()
                   for key, value in unit.items() if key not in {"targets", "target_context"})
            or unit.get("targets", []) != [] or unit.get("target_context", {}) != {}
        ):
            raise ValueError("入力済みの移行単位は削除できません。")
        del units[index]


def _apply_script_edits(document: Any, edits: Any) -> None:
    scripts = document.get("scripts", {})
    if not isinstance(edits, list):
        raise ValueError("スクリプトの工程とコマンドを確認してください。")
    if edits:
        scripts = document.setdefault("scripts", tomlkit.table())
    commands_by_phase = {phase: [] for phase in SCRIPT_PHASES}
    for entry in edits:
        if not isinstance(entry, dict):
            raise ValueError("スクリプトの工程とコマンドを確認してください。")
        phase, command = entry.get("phase"), entry.get("command")
        if not isinstance(phase, str) or phase not in commands_by_phase or not isinstance(command, list) or any(
            not isinstance(arg, str) for arg in command
        ):
            raise ValueError("スクリプトの工程とコマンドを確認してください。")
        commands_by_phase[phase].append(command)
    for phase, commands in commands_by_phase.items():
        if commands and scripts.get(phase) != commands:
            scripts[phase] = commands
        elif not commands:
            scripts.pop(phase, None)


def _settings_form(document: Any) -> dict[str, Any]:
    scripts = document.get("scripts", {})
    units = document.get("units", [])
    project = document.get("project", {})
    return {
        **{
            section: {
                key: (
                    str(value) if key in _NUMERIC_FIELDS else
                    ("false" if value is False else "") if key == "enabled" else
                    ("true" if value is True else "false" if value is False else "") if key == "recursive" else value
                )
                for key in keys
                if (value := document.get(section, {}).get(key, [] if key == "command" else "")) is not None
            }
            for section, keys in _TABLE_FIELDS.items()
        },
        "migrationRules": [
            _material_form_entry(item, index, "migration_rules")
            for index, item in enumerate(project.get("migration_rules", []))
        ],
        "references": [
            _material_form_entry(item, index, "references")
            for index, item in enumerate(project.get("references", []))
        ],
        "scripts": [
            {"phase": phase, "command": command}
            for phase in SCRIPT_PHASES if isinstance(commands := scripts.get(phase), list)
            for command in commands if isinstance(command, list) and all(isinstance(arg, str) for arg in command)
        ],
        "units": [{
            **{key: value for key in ("id", "source") if isinstance(value := unit.get(key), str)},
            "target_context": {
                key: (str(value).lower() if key in _NUMERIC_FIELDS or key == "recursive" else value)
                for key in _TABLE_FIELDS["target_context"]
                if isinstance(context := unit.get("target_context"), Mapping)
                and (value := context.get(key)) is not None
            },
            "checker_command": unit.get("checker_command", []),
            "draft_imports": unit.get("draft_imports", []),
            "targets": [path for path in unit.get("targets", [unit.get("target")]) if isinstance(path, str)]
            if isinstance(unit.get("targets", []), list) else [],
        } for unit in units],
    }


def build_settings_draft(content: str, changes: dict[str, Any] | None = None) -> dict[str, Any]:
    """フォームで指定したキーだけを更新し、残りのTOMLを保持する。"""
    try:
        document = tomlkit.parse(content)
        project = document.get("project", {})
        units = document.get("units", [])
        scripts = document.get("scripts", {})
        if (
            not isinstance(project, Mapping) or not isinstance(units, list)
            or any(not isinstance(unit, Mapping) for unit in units)
            or not isinstance(scripts, Mapping)
            or any(not isinstance(document.get(section, {}), Mapping) for section in _TABLE_FIELDS)
            or not isinstance(project.get("migration_rules", []), list)
            or not isinstance(project.get("references", []), list)
        ):
            raise ValueError("invalid settings form data")
    except (TOMLKitError, ValueError):
        if changes:
            raise ValueError("TOMLを直接編集し、書式を修正してください。") from None
        return {"content": content, "form": None}

    promoted = _promote_legacy_materials(document, units)
    if changes:
        unit_changes = changes.get("units", {})
        if not isinstance(unit_changes, dict):
            raise ValueError("フォームの入力内容を確認してください。")
        _apply_table_edits(document, changes)
        for field, key in (("migrationRules", "migration_rules"), ("references", "references")):
            if field in changes:
                _apply_material_edits(document.setdefault("project", tomlkit.table()), key, changes[field])
        if unit_changes:
            _apply_unit_edits(document, unit_changes)
        if "removeEmptyUnits" in changes:
            _remove_empty_units(document.get("units", []), changes["removeEmptyUnits"])
        if "scripts" in changes:
            _apply_script_edits(document, changes["scripts"])
    if changes or promoted:
        content = tomlkit.dumps(document)

    return {"content": content, "form": _settings_form(document)}
