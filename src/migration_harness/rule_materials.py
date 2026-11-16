"""必須事項を起動時の指示に渡し、詳細・例は原文への索引で案内する。"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


def write_rule_files(directory: Path, sources: list[tuple[dict, str]]) -> list[dict]:
    """自動要約しない。分類のない既存ルールは全文を必須事項として残す。"""
    if directory.is_symlink():
        raise ValueError("unsafe rule material directory")
    directory.mkdir(exist_ok=True)
    entries = []
    for item, text in sources:
        name = f"rule-{len(entries) + 1:04d}.txt"
        if (directory / name).is_symlink():
            raise ValueError(f"unsafe rule material: {name}")
        content = text.encode("utf-8")
        (directory / name).write_bytes(content)
        entries.append({
            "file": name, "path": item["path"], "usage": item.get("usage", "required"),
            "description": item.get("description", ""), "sha256": hashlib.sha256(content).hexdigest(),
        })
    if (directory / "rules.json").is_symlink():
        raise ValueError("unsafe rule material index")
    (directory / "rules.json").write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    return entries


def render_guidelines(directory: Path, manifest: dict) -> str:
    """照合済みの保存資料だけを使う。旧Runの断片も順に結合し省略しない。"""
    required, details = [], []
    if "rule_files" in manifest:
        entries = json.loads((directory / "rules.json").read_text(encoding="utf-8")) if manifest["rule_files"] else []
        if entries != manifest["rule_files"]:
            raise ValueError("saved rule index differs from manifest; restore the original run materials")
    else:
        entries = manifest.get("rule_chunks", [])
    for entry in entries:
        name = entry["file"]
        if not re.fullmatch(r"rule-\d+\.txt", name):
            raise ValueError("invalid saved rule filename")
        content = (directory / name).read_bytes()
        if hashlib.sha256(content).hexdigest() != entry["sha256"]:
            raise ValueError(f"saved rule changed: {name}")
        usage = entry.get("usage", "required")
        if usage == "required":
            required.append(content.decode("utf-8"))
        elif usage == "reference" and isinstance(entry.get("description"), str) and entry["description"].strip():
            details.append(json.dumps({
                "source": entry["path"], "when_to_read": entry["description"],
                "file": f".migration-materials/{name}",
            }, ensure_ascii=False))
        else:
            raise ValueError(f"invalid saved rule usage or description: {name}")
    # 断片方式より前のRunのshared.mdには必須ルールが混在するため全文を渡す。
    legacy_shared = "rule_files" not in manifest and "rule_chunks" not in manifest and "shared.md" in manifest.get("materials", {})
    if legacy_shared:
        required.append((directory / "shared.md").read_bytes().decode("utf-8"))
    if not required and not details:
        return ""
    return (
        "# Project migration instructions\n\n"
        "The mandatory text below is already provided as Junie guidelines for this task. "
        "Apply it to every target, including repairs and resumed work. It takes precedence over generic "
        "migration hints. Do not run a separate reader or re-open these guidelines just to acknowledge them. "
        "Before completion, check your changes against all applicable requirements and exceptions.\n\n"
        "## Mandatory text (verbatim saved excerpts)\n\n" + "".join(required) +
        ("\n\n## Detail and example references\n\n"
         "The following descriptions are an index, not a substitute for the original details. "
         "When relevant to the source, targets or diagnostic, read the listed file before implementing or repairing. "
         "Re-evaluate applicability for each new capsule, repair or resume. Existing target code is not a "
         "substitute for reading the details of the behavior you are about to change. "
         "If applicability is unclear, inspect it. Use bounded ranges for long files and follow relevant links "
         "within the provided materials. Do not read unrelated examples merely for acknowledgement.\n\n" +
         "\n".join(details) if details else "")
    )
