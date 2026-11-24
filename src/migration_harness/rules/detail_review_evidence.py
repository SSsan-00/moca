import codecs
import hashlib
import io
import json
import re
from pathlib import Path

from ..files import safe_target
from ..execution import check_cancelled
from ..context.source_context import target_context_snapshot
from ..context.target_evidence import pinned_text_encoding


_COPY_CHUNK_BYTES = 64 * 1024


def _copy_and_count(source, target):
    sha = hashlib.sha256()
    lines, last_character = 0, "\n"
    with source.open("rb") as input_file, target.open("xb") as output_file:
        encoding = pinned_text_encoding(input_file.peek(4))
        decoder = io.IncrementalNewlineDecoder(codecs.getincrementaldecoder(encoding)(), True)
        while True:
            check_cancelled()
            chunk = input_file.read(_COPY_CHUNK_BYTES)
            if chunk:
                output_file.write(chunk)
                sha.update(chunk)
            if decoder is not None:
                try:
                    text = decoder.decode(chunk, final=not chunk)
                except UnicodeDecodeError:
                    decoder = None
                else:
                    if text:
                        lines += text.count("\n")
                        last_character = text[-1]
            if not chunk:
                break
    # 周辺資料にはテキスト以外も含まれる。コピーは残すが、復号できない資料の行は引用できない。
    file_lines = lines + (last_character != "\n") if decoder is not None else 0
    return sha.hexdigest(), file_lines


def stage_review_evidence(review_root, capsule, manifest, pinned_context):
    inventory = {}

    def copy(source_root, relative, destination, digest, origin):
        check_cancelled()
        source = safe_target(source_root, relative)
        target = safe_target(review_root, destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        actual_digest, lines = _copy_and_count(source, target)
        if actual_digest != digest:
            raise ValueError(f"saved detail review evidence changed: {origin}")
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
