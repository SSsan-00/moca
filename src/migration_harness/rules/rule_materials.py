"""必須事項と対象が明記された詳細を渡し、ほかの詳細は索引で案内する。"""

from __future__ import annotations

import hashlib
import json
import re
from io import StringIO
from pathlib import Path

from ..execution import check_cancelled


COMPACT_RULE_GUIDELINES = "compact-numbered-v1"
_NUMBERED_RULE_FRAME = re.compile(r"([1-9][0-9]{0,11})-([1-9][0-9]{0,11})\n\n(`{3,})text\n")


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
    mentions = []
    for target in targets:
        path = r"[\\/]".join(re.escape(part) for part in target.parts)
        mentions.append(rf"(?<![\w./\\-]){path}(?![\w/\\-]|\.[\w-])")
        # RazorのPageModelは .cshtml.cs で、型名には .cshtml が付かない。
        name = target.stem.removesuffix(".cshtml")
        if target.suffix == ".cs" and name.isidentifier():
            mentions.append(rf"(?<!\w){re.escape(name)}\.[A-Z]\w*\b")
    return re.compile("|".join(mentions)) if mentions else None


def saved_rule_entries(manifest: dict) -> list[dict]:
    entries = manifest.get("rule_files")
    if "rule_chunks" in manifest or not isinstance(entries, list):
        raise ValueError("capsule/manifest.json: missing or invalid rule_files; inspect the saved rules and start a new Run")
    return entries


def detail_index(manifest: dict) -> list[dict]:
    return [
        {"file": entry["file"], "source": entry["path"], "when_to_read": entry["description"]}
        for entry in saved_rule_entries(manifest) if entry.get("usage") == "reference"
    ]


class InvalidDetailContextEvidence(ValueError):
    pass


def _detail_context_span(reference, name, inventory):
    file = reference.get("file") if isinstance(reference, dict) else None
    lines = reference.get("lines") if isinstance(reference, dict) else None
    if (not isinstance(file, str) or not file.startswith("evidence/")
            or not isinstance(lines, list) or len(lines) != 2
            or any(type(line) is not int for line in lines) or not 1 <= lines[0] <= lines[1]
            or inventory is not None and (file not in inventory or lines[1] > inventory[file]["file_lines"])):
        raise InvalidDetailContextEvidence(f"invalid review context evidence for saved detail: {name}")
    return file, *lines


def selected_details(
    index: list[dict], decisions: object, line_counts: dict[str, int] | None = None,
    evidence_inventory: dict[str, dict] | None = None,
) -> frozenset[str]:
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
        insufficient = decision.get("insufficient_context", False)
        if type(insufficient) is not bool or insufficient and not decision["applies"]:
            raise ValueError(f"insufficient context cannot justify excluding saved detail: {name}")
        reference = decision.get("context_evidence")
        related = decision.get("additional_context_evidence", [])
        if not isinstance(related, list) or len(related) > 2 or related and reference is None:
            raise InvalidDetailContextEvidence(f"invalid review context evidence for saved detail: {name}")
        if reference is not None:
            spans = [_detail_context_span(item, name, evidence_inventory) for item in (reference, *related)]
            if len(spans) != len(set(spans)):
                raise InvalidDetailContextEvidence(f"invalid review context evidence for saved detail: {name}")
        if decision["applies"]:
            selected.add(name)
    return frozenset(selected)


def effective_details(manifest: dict, record: dict) -> frozenset[str]:
    """保存済みルールの版を照合し、利用者が上書きした採否を反映する。"""
    selected = set(selected_details(
        detail_index(manifest), record["detail_applicability"], evidence_inventory=record.get("detail_review_evidence"),
    ))
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


def _inline_rule(entry: dict, body: str) -> str:
    if not isinstance(source_path := entry.get("path"), str):
        return body
    opening = f"## migration_rules — `{source_path}` lines "
    if not body.startswith(opening):
        return body
    frame = _NUMBERED_RULE_FRAME.match(body, len(opening))
    if not frame:
        return body
    first, last = int(frame[1]), int(frame[2])
    end_frame = f"{frame[3]}\n\n"
    if last < first or not body.endswith(end_frame):
        return body
    start = frame.end()
    end = len(body) - len(end_frame)
    content = []
    number = first - 1
    for number, line in enumerate(StringIO(body[start:end], newline=""), first):
        if number % 256 == 0:
            check_cancelled()
        prefix = f"{number:>6} | "
        if number > last or not line.startswith(prefix) or not line.endswith(("\r", "\n")):
            return body
        content.append(line[len(prefix):])
    if number != last:
        return body
    inline = (
        f"{opening}{first}-{last} (.migration-materials/{entry['file']})\n\n{frame[3]}text\n"
        + "".join(content) + end_frame
    )
    return inline if len(inline) < len(body) else body


def render_guidelines(
    directory: Path, manifest: dict, *, targets: tuple[Path, ...] = (), selected: frozenset[str] | None = None,
    supplied_rules: list[dict] | None = None, excluded: frozenset[str] = frozenset(),
) -> str:
    required, matched, details = [], [], []
    mentions = _target_mentions(targets)
    entries = saved_rule_entries(manifest)
    if entries and json.loads((directory / "rules.json").read_text(encoding="utf-8")) != entries:
        raise ValueError("saved rule index differs from manifest; restore the original run materials")
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
            destination = required
        elif usage == "reference" and isinstance(entry.get("description"), str) and entry["description"].strip():
            body = content.decode("utf-8")
            if name not in excluded and (
                (selected is not None and name in selected)
                or (mentions and (mentions.search(entry["description"]) or mentions.search(body)))
            ):
                destination = matched
            else:
                if selected is None and name not in excluded:
                    details.append(json.dumps({
                        "source": entry["path"], "when_to_read": entry["description"],
                        "file": f".migration-materials/{name}",
                    }, ensure_ascii=False))
                continue
        else:
            raise ValueError(f"invalid saved rule usage or description: {name}")
        inline = _inline_rule(entry, body)
        destination.append(inline)
        if supplied_rules is not None:
            supplied_rules.append({"file": name, "path": entry.get("path", name), "usage": usage,
                                   "bytes": len(inline.encode("utf-8")) if inline is not body else len(content),
                                   "characters": len(inline)})
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
