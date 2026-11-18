"""必須事項と対象が明記された詳細を渡し、ほかの詳細は索引で案内する。"""

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


def _target_mentions(targets: tuple[Path, ...]) -> re.Pattern[str] | None:
    """詳細ルールに移行先ファイルの記載があるか判定する。"""
    mentions = []
    for target in targets:
        path = r"[\\/]".join(re.escape(part) for part in target.parts)
        mentions.append(rf"(?<![\w./\\-]){path}(?![\w/\\-]|\.[\w-])")
        # RazorのPageModelは .cshtml.cs で、型名には .cshtml が付かない。
        name = target.stem.removesuffix(".cshtml")
        if target.suffix == ".cs" and name.isidentifier():
            mentions.append(rf"(?<!\w){re.escape(name)}\.[A-Z]\w*\b")
    return re.compile("|".join(mentions)) if mentions else None


def detail_index(manifest: dict) -> list[dict]:
    """詳細ルールの索引と用途説明を作成する。"""
    return [
        {"file": entry["file"], "source": entry["path"], "when_to_read": entry["description"]}
        for entry in manifest.get("rule_files", ()) if entry.get("usage") == "reference"
    ]


def selected_details(index: list[dict], decisions: object, line_counts: dict[str, int] | None = None) -> frozenset[str]:
    """確認漏れや不明な資料のIDがあれば、移行に進めない。"""
    if not isinstance(decisions, dict) or decisions.keys() != {item["file"] for item in index}:
        raise ValueError("detail review must contain exactly one decision for every saved detail")
    selected = set()
    for name, decision in decisions.items():
        if (
            not isinstance(decision, dict)
            or type(decision.get("applies")) is not bool
            or not isinstance(decision.get("reason"), str)
            or not decision["reason"].strip()
        ):
            raise ValueError(f"invalid applicability decision for saved detail: {name}")
        evidence = decision.get("rule_lines")
        if evidence is not None and (
            not isinstance(evidence, list) or len(evidence) != 2 or any(type(line) is not int for line in evidence)
            or not 1 <= evidence[0] <= evidence[1] <= (line_counts[name] if line_counts is not None else evidence[1])
        ):
            raise ValueError(f"invalid applicability evidence lines for saved detail: {name}")
        if decision["applies"]:
            selected.add(name)
    return frozenset(selected)


def effective_details(manifest: dict, record: dict) -> frozenset[str]:
    selected = set(selected_details(detail_index(manifest), record["detail_applicability"]))
    reference_hashes = {item["file"]: item["sha256"] for item in manifest.get("rule_files", [])
                        if item.get("usage") == "reference"}
    for name, decision in record.get("detail_overrides", {}).items():
        if name not in reference_hashes or decision.get("sha256") != reference_hashes[name] or type(decision.get("applies")) is not bool:
            raise ValueError(f"operator decision differs from saved rule: {name}")
        if decision["applies"]:
            selected.add(name)
        else:
            selected.discard(name)
    return frozenset(selected)


def render_guidelines(
    directory: Path, manifest: dict, *, targets: tuple[Path, ...] = (), selected: frozenset[str] | None = None,
    supplied_rules: list[dict] | None = None, excluded: frozenset[str] = frozenset(),
) -> str:
    """照合済みの保存資料だけを使う。旧Runの断片も順に結合し省略しない。"""
    required, matched, details = [], [], []
    mentions = _target_mentions(targets)
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
            body = content.decode("utf-8")
            required.append(body)
        elif usage == "reference" and isinstance(entry.get("description"), str) and entry["description"].strip():
            body = content.decode("utf-8")
            if name not in excluded and (
                (selected is not None and name in selected)
                or (mentions and (mentions.search(entry["description"]) or mentions.search(body)))
            ):
                matched.append(body)
            else:
                if selected is None and name not in excluded:
                    details.append(json.dumps({
                        "source": entry["path"], "when_to_read": entry["description"],
                        "file": f".migration-materials/{name}",
                    }, ensure_ascii=False))
                continue
        else:
            raise ValueError(f"invalid saved rule usage or description: {name}")
        if supplied_rules is not None:
            supplied_rules.append({"file": name, "path": entry.get("path", name), "usage": usage,
                                   "bytes": len(content), "characters": len(body)})
    # 断片方式より前のRunのshared.mdには必須ルールが混在するため全文を渡す。
    legacy_shared = "rule_files" not in manifest and "rule_chunks" not in manifest and "shared.md" in manifest.get("materials", {})
    if legacy_shared:
        shared = (directory / "shared.md").read_bytes()
        required.append(shared.decode("utf-8"))
        if supplied_rules is not None:
            supplied_rules.append({"file": "shared.md", "path": "shared.md", "usage": "required",
                                   "bytes": len(shared), "characters": len(required[-1])})
    if not required and not matched and not details:
        return ""
    return (
        "# Project migration instructions\n\n"
        "The mandatory text below is already provided as Junie guidelines for this task. "
        "Apply it to every target, including repairs and resumed work. It takes precedence over generic "
        "migration hints. Do not run a separate reader or re-open these guidelines just to acknowledge them. "
        "Before completion, check your changes against all applicable requirements and exceptions.\n\n"
        "## Mandatory text (verbatim saved excerpts)\n\n" + "".join(required) +
        ("\n\n## Applicable details (verbatim saved excerpts)\n\n"
         "Read these supplied details before editing. Apply their requirements and exceptions when their conditions hold.\n\n"
         + "\n".join(matched) if matched else "") +
        ("\n\n## Detail and example references\n\n"
         "The following descriptions are an index, not a substitute for the original details. "
         "Before editing, review this index against the current source, targets and any diagnostic. "
         "Read each applicable file before implementing or repairing. "
         "Re-evaluate applicability for each new capsule, repair or resume. Existing target code is not a "
         "substitute for reading the details of the behavior you are about to change. "
         "If applicability is unclear, inspect it. Use bounded ranges for long files and follow relevant links "
         "within the provided materials. Do not read unrelated examples merely for acknowledgement.\n\n" +
         "\n".join(details) if details else "")
    )
