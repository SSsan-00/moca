import hashlib
import json
import re
from pathlib import Path

from .files import safe_target
from .execution import check_cancelled
from .source_context import target_context_snapshot


def stage_review_evidence(review_root, capsule, manifest, pinned_context):
    inventory = {}

    def copy(source_root, relative, destination, digest, origin):
        check_cancelled()
        source = safe_target(source_root, relative)
        target = safe_target(review_root, destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        sha = hashlib.sha256()
        lines, last_byte = 0, b""
        with source.open("rb") as input_file, target.open("xb") as output_file:
            for chunk in iter(lambda: input_file.read(64 * 1024), b""):
                check_cancelled()
                output_file.write(chunk)
                sha.update(chunk)
                lines += chunk.count(b"\n")
                last_byte = chunk[-1:]
        if sha.hexdigest() != digest:
            raise ValueError(f"saved detail review evidence changed: {origin}")
        lines += int(bool(last_byte) and last_byte != b"\n")
        target.chmod(0o444)
        inventory[destination.as_posix()] = {"path": origin, "sha256": digest, "file_lines": lines}

    materials = manifest.get("materials", {})
    required = {item["file"]: item for item in manifest["rule_files"] if item.get("usage") == "required"}
    for name, digest in materials.items():
        if name not in required and not re.fullmatch(r"shared\.md|references\.json|reference-\d+\.txt", name):
            continue
        if name in required and not re.fullmatch(r"rule-\d+\.txt", name):
            raise ValueError("invalid saved rule filename")
        origin = required[name]["path"] if name in required else name
        copy(capsule / "materials", Path(name), Path("evidence/materials") / name, digest, origin)
    if pinned_context is not None:
        snapshot = target_context_snapshot(capsule)
        entries = json.loads(safe_target(snapshot, Path("manifest.json")).read_text(encoding="utf-8"))["files"]
        for entry in entries:
            relative = Path(entry["path"])
            copy(pinned_context, relative, Path("evidence/target") / relative, entry["sha256"], entry["path"])
    if inventory:
        index = review_root / "evidence.json"
        index.write_text(json.dumps(inventory, ensure_ascii=False, indent=2), encoding="utf-8")
        index.chmod(0o444)
    return inventory
