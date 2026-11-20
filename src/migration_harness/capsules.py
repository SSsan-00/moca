"""移行元の固定、コンテキストの分割、カプセルの読み戻し。"""

from __future__ import annotations

import codecs
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path
from itertools import chain
from typing import Any, Iterator

from .config import REFERENCE_ROLES, target_paths
from .execution import check_cancelled, report_activity
from .files import safe_target, sha256_file
from .php_boundaries import PhpFunction, function_boundaries
from .rule_materials import write_rule_files


def is_php_input(source: dict[str, Any]) -> bool:
    return source.get("role") in {"php", "reference_php"} or Path(source["path"]).suffix.lower() == ".php"


def migration_php_encodings(manifest: dict[str, Any]) -> dict[str, str]:
    # 固定済みPHPの一覧には、共通資料として登録した参考PHPも含まれる。
    encodings: dict[str, str] = {}
    for part in manifest["parts"]:
        for source in part["sources"]:
            if is_php_input(source):
                encodings.setdefault(source["path"], source["encoding"])
    return encodings


def _text_fingerprint(path: Path, encoding: str) -> bytes | None:
    """厳密にデコードできる場合だけ、文字列を比較用に要約する。"""
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


def input_encoding(path: Path, requested: str) -> str:
    """BOMを優先し、UTF-8とEUC-JPの判定が曖昧な場合は明示指定を求める。"""
    if requested != "auto":
        return codecs.lookup(requested).name.replace("_", "-")
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


def _selected_lines(path: Path, start: int, end: int | None, encoding: str) -> Iterator[tuple[int, str]]:
    """行末と元の行番号を保持し、終端指定がなければファイル末尾まで読む。"""
    selected_count = 0
    reached_end = False
    with path.open("r", encoding=encoding, errors="strict", newline="") as stream:
        for number, line in enumerate(stream, start=1):
            if number % 256 == 0:
                check_cancelled()
            if number >= start:
                selected_count += 1
                yield number, line
            if number == end:
                reached_end = True
                break
    if not selected_count:
        if end is None:
            raise ValueError(f"input file has no lines: {path}")
        raise ValueError(f"requested lines {start}-{end} do not exist in {path}")
    if end is not None and not reached_end:
        raise ValueError(f"requested line {end} does not exist in {path}")


def _context_header(unit: dict[str, Any]) -> str:
    return "\n".join([
        f"# Migration unit: {unit['id']}",
        "",
        "Targets in worktree: " + ", ".join(f"`{path.as_posix()}`" for path in target_paths(unit)),
        "",
        "Only the listed excerpts are included. The application source is not copied into this public harness repository.",
        "",
    ])


def _max_backtick_run(text: str) -> int:
    return max((len(run) for run in re.findall(r"`+", text)), default=0)


def _group_markdown_frame(group: dict[str, Any]) -> tuple[str, str]:
    item = group["item"]
    fence = "`" * max(3, group["max_backticks"] + 1)
    opening = (
        f"## {item['role']} — `{item['path']}` lines "
        f"{group['start_line']}-{group['end_line']}\n\n{fence}text\n"
    )
    closing = ("" if group["ends_with_newline"] else "\n") + f"{fence}\n\n"
    return opening, closing


def _group_size(group: dict[str, Any]) -> int:
    """本文を連結せず、見出しと囲みを含む出力文字数を求める。"""
    opening, closing = _group_markdown_frame(group)
    return len(opening) + group["content_chars"] + len(closing)


def _new_group(input_index: int, item: dict[str, Any], line_number: int, record: str) -> dict[str, Any]:
    """1件目の抜粋と、次の行を詰めるための文字数を保持する。"""
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
    """上限内に収まる場合だけ抜粋を追加し、入らない場合は編集中のグループを保つ。"""
    if base_chars + len(record) > max_chars:
        return False
    last = groups[-1] if groups else None
    if last and last["input_index"] == input_index and line_number <= last["end_line"] + 1:
        candidate = {
            **last,
            "end_line": max(last["end_line"], line_number),
            "content_chars": last["content_chars"] + len(record),
            "max_backticks": max(last["max_backticks"], _max_backtick_run(record)),
            "ends_with_newline": record.endswith(("\n", "\r")),
        }
        total_chars = base_chars + sum(_group_size(group) for group in groups[:-1]) + _group_size(candidate)
        if total_chars > max_chars:
            return False
        # 収まると確定するまでは、候補と共有している行の一覧を変更しない。
        candidate["records"].append(record)
        groups[-1] = candidate
        return True

    candidate = _new_group(input_index, item, line_number, record)
    if base_chars + sum(_group_size(group) for group in groups) + _group_size(candidate) > max_chars:
        return False
    groups.append(candidate)
    return True


def _render_groups(groups: list[dict[str, Any]]) -> str:
    """行末を保持して表示用の抜粋を連結する。"""
    parts = []
    for group in groups:
        opening, closing = _group_markdown_frame(group)
        parts.extend((opening, *group["records"], closing))
    return "".join(parts)


def _manifest_source(group: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": group["item"]["role"],
        "path": group["item"]["path"],
        **({"symbol": group["item"]["symbol"]} if "symbol" in group["item"] else {}),
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


def _prepare_inputs(
    root: Path, capsule: Path, inputs: list[dict[str, Any]],
) -> tuple[list[tuple[dict[str, Any], Path]], list[dict[str, str]]]:
    """PHPをRunに固定し、入力ごとの文字コードを確定する。"""
    prepared = []
    php_sources: dict[str, dict[str, str]] = {}
    encoding_roles: dict[str, str] = {}
    for item in inputs:
        check_cancelled()
        name = Path(item["path"]).as_posix()
        source_path = (root / name).resolve()
        pin = None
        if is_php_input(item):
            pin = php_sources.get(name)
            if pin is None:
                relative = f"php-sources/{len(php_sources) + 1:04d}.php"
                snapshot = capsule / relative
                snapshot.parent.mkdir(exist_ok=True)
                before = source_path.stat()
                shutil.copyfile(source_path, snapshot)
                after = (root / name).stat()
                if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
                    raise ValueError(f"PHP input changed while preparing its snapshot: {name}")
                pin = {"path": name, "snapshot": relative, "sha256": sha256_file(snapshot)}
                php_sources[name] = pin
            # カプセルと比較に同じPHPバイト列を使う。
            source_path = capsule / pin["snapshot"]
        requested = item["encoding"]
        encoding = input_encoding(source_path, requested)
        if pin is not None:
            if "encoding" not in pin:
                pin["encoding"] = encoding
                encoding_roles[name] = f"{item['role']} ({requested})"
            elif encoding != pin["encoding"]:
                original = _text_fingerprint(source_path, pin["encoding"])
                if original is None or _text_fingerprint(source_path, encoding) != original:
                    raise ValueError(
                        f"{name}: inconsistent PHP input encoding between {encoding_roles[name]} "
                        f"and {item['role']} ({requested})",
                    )
                encoding = pin["encoding"]
        prepared.append(({**item, "path": name, "resolved_encoding": encoding}, source_path))
    return prepared, list(php_sources.values())


def prepare_context(config: dict[str, Any], unit: dict[str, Any], run_path: Path) -> dict[str, Any]:
    """共通資料の予算を先に取り、残りに移行対象を詰める。"""
    root = Path(config["project_root"])
    capsule = run_path / "capsule"
    capsule.mkdir(parents=True, exist_ok=False)

    max_chars = config["max_context_chars"]
    inputs, php_sources = _prepare_inputs(root, capsule, unit["inputs"])
    header = _context_header(unit)
    shared_groups: list[dict[str, Any]] = []
    rule_sources: list[tuple[dict[str, Any], str]] = []
    rule_ranges: list[dict[str, Any]] = []
    segment_inputs = []
    materials: dict[str, str] = {}
    reference_index = []
    material_dir = capsule / "materials"
    shared_intro = (
        "# Shared context\n\n"
        "Apply the shared rules and constraints to every segment. Read these excerpts as reference.\n\n"
    )
    segment_intro = "# Segment inputs\n\n"
    shared_base_chars = len(header) + len(shared_intro) + len(segment_intro)
    for input_index, (item, source_path) in enumerate(inputs):
        if item["role"] in REFERENCE_ROLES and item.get("shared", False):
            material_dir.mkdir(exist_ok=True)
            name = f"reference-{len(reference_index) + 1:04d}.txt"
            snapshot = material_dir / name
            with snapshot.open("w", encoding="utf-8", newline="") as stream:
                for number, line in _selected_lines(
                    source_path, item["start_line"], item["end_line"], item["resolved_encoding"],
                ):
                    stream.write(f"{number:>6} | {line}")
            materials[name] = sha256_file(snapshot)
            reference_index.append({
                "role": item["role"], "path": item["path"], "file": name,
                "start_line": item["start_line"], "end_line": number,
            })
            continue
        if item["role"] != "migration_rules" and not item.get("shared", False):
            segment_inputs.append((input_index, item, source_path))
            continue
        lines = _selected_lines(source_path, item["start_line"], item["end_line"], item["resolved_encoding"])
        # ルールはファイル単位で保存する。既に読んだ全資料のサイズを毎行数え直さない。
        rule_groups: list[dict[str, Any]] = []
        for number, line in lines:
            if item["role"] == "migration_rules":
                _append_context_record(rule_groups, input_index, item, number, f"{number:>6} | {line}", 0, sys.maxsize)
                continue
            if not _append_context_record(
                shared_groups, input_index, item, number, f"{number:>6} | {line}", shared_base_chars, max_chars,
            ):
                raise ValueError(f"shared context exceeds max_context_chars ({max_chars}): {item['path']}")
        rule_sources.extend((group["item"], _render_groups([group])) for group in rule_groups)
        rule_ranges.extend(_manifest_source(group) for group in rule_groups)
    if not segment_inputs:
        raise ValueError("migration unit requires at least one input outside shared context")
    rule_files = []
    if rule_sources:
        rule_files = write_rule_files(material_dir, rule_sources)
        materials.update({chunk["file"]: chunk["sha256"] for chunk in rule_files})
        materials["rules.json"] = sha256_file(material_dir / "rules.json")
        report_activity(f"共通ルールを省略せず保存しました（{len(rule_files)}ファイル）。")
    shared_context = ""
    if shared_groups:
        excerpts = _render_groups(shared_groups)
        shared_context = shared_intro + excerpts + segment_intro
        material_dir.mkdir(exist_ok=True)
        shared_file = material_dir / "shared.md"
        shared_file.write_text(excerpts, encoding="utf-8", newline="")
        materials[shared_file.name] = sha256_file(shared_file)
    if reference_index:
        index = material_dir / "references.json"
        index.write_text(json.dumps(reference_index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        materials[index.name] = sha256_file(index)
    prefix = header + shared_context
    overlap_chars = min(512, (max_chars - len(shared_context)) // 20)
    budget_error = (
        "max_context_chars is too small for a segment input after including shared context"
        if shared_groups else "max_context_chars is too small for the context headers"
    )
    scan_lines: dict[str, int | None] = {}
    for _, item, _ in segment_inputs:
        if not is_php_input(item):
            continue
        name, end = item["path"], item["end_line"]
        if scan_lines.get(name, 0) is not None:
            scan_lines[name] = None if end is None else max(scan_lines.get(name, 0), end)
    syntax_by_path = {
        pin["path"]: function_boundaries(
            capsule / pin["snapshot"], pin["encoding"], through_line=scan_lines.get(pin["path"]),
        )
        for pin in php_sources
    }
    for name, (_, status) in syntax_by_path.items():
        if status != "named functions, lexical boundaries only":
            report_activity(f"PHP 構文境界: {name} — {status}")
    manifest_parts, split_line_coverage = _write_segments(
        capsule, segment_inputs, prefix, max_chars, overlap_chars, budget_error, syntax_by_path,
    )
    syntax_boundaries = _syntax_manifest(manifest_parts, syntax_by_path)

    context_paths = [part["path"] for part in manifest_parts]
    context_part_chars = [part["chars"] for part in manifest_parts]
    total_chars = sum(context_part_chars)
    manifest = {
        "unit_id": unit["id"],
        "max_context_chars": max_chars,
        "segmentation_policy": (
            "php_function_boundaries_with_line_and_character_fallback"
            if any(item["functions"] for item in syntax_boundaries.values())
            else "line_with_character_fallback_and_overlap"
        ),
        "syntax_boundaries_analyzed": any(item["functions"] for item in syntax_boundaries.values()),
        "syntax_boundaries": syntax_boundaries,
        "split_line_coverage": split_line_coverage,
        "total_context_chars": total_chars,
        "shared_sources": rule_ranges + [_manifest_source(group) for group in shared_groups],
        "segment_offset": len(prefix),
        "materials": materials,
        "rule_files": rule_files,
        "reference_sources": reference_index,
        "php_sources": php_sources,
        "parts": manifest_parts,
    }
    (capsule / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return {
        "context_path": context_paths[0],
        "context_paths": context_paths,
        "context_part_chars": context_part_chars,
        "context_chars": total_chars,
        "capsule_path": str(capsule),
        "capsule_count": len(context_paths),
        "source_boundary_review_required": len(context_paths) > 1,
    }


def _write_segments(
    capsule: Path, segment_inputs: list[tuple[int, dict[str, Any], Path]], prefix: str,
    max_chars: int, overlap_chars: int, budget_error: str,
    syntax_by_path: dict[str, tuple[tuple[PhpFunction, ...], str]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """分割した本文と読み取り用の境界資料を保存する。"""
    base_chars = len(prefix)
    groups: list[dict[str, Any]] = []
    manifest_parts: list[dict[str, Any]] = []
    split_line_coverage: list[dict[str, Any]] = []
    previous_tail: tuple[int, str] | None = None
    boundary_chars = min(1200, max_chars // 10)

    def flush_capsule() -> None:
        nonlocal previous_tail
        if not groups:
            return
        context = prefix + _render_groups(groups)
        if len(context) > max_chars:
            raise RuntimeError("internal error: generated context capsule exceeds its configured limit")
        part_number = len(manifest_parts) + 1
        part_dir = capsule / f"part-{part_number:04d}"
        part_dir.mkdir()
        context_path = part_dir / "context.md"
        # 原文の改行と文字数をWindowsでも保持する。
        context_path.write_text(context, encoding="utf-8", newline="")
        manifest_parts.append({
            "index": part_number,
            "path": str(context_path),
            "chars": len(context),
            "sha256": sha256_file(context_path),
            "segment_sha256": hashlib.sha256(context[base_chars:].encode("utf-8")).hexdigest(),
            "sources": [_manifest_source(group) for group in groups],
        })
        check_cancelled()
        report_activity(f"カプセル {part_number} を準備しました（{len(context):,}文字）。")
        first, last = groups[0], groups[-1]
        if previous_tail and previous_tail[0] == first["input_index"]:
            previous_dir = Path(manifest_parts[-2]["path"]).parent
            next_lines = "".join(record[:boundary_chars] for record in first["records"][:12])[:boundary_chars]
            with (previous_dir / "boundary.md").open("a", encoding="utf-8", newline="") as stream:
                stream.write(f"\nFollowing excerpt (read-only)\n\n{next_lines}\n")
            (part_dir / "boundary.md").write_text(
                f"Previous excerpt (read-only)\n\n{previous_tail[1]}\n", encoding="utf-8", newline="",
            )
        tail = ""
        for line in reversed(last["records"][-12:]):
            if tail and len(line) + len(tail) > boundary_chars:
                break
            tail = line[:boundary_chars] + tail
        previous_tail = last["input_index"], tail
        groups.clear()

    for input_index, item, source_path in segment_inputs:
        lines = _selected_lines(source_path, item["start_line"], item["end_line"], item["resolved_encoding"])
        functions = {
            part.start_line: part for part in syntax_by_path.get(item["path"], ((), ""))[0]
            if part.start_line >= item["start_line"]
            and (item["end_line"] is None or part.end_line <= item["end_line"])
        }
        while (source_line := next(lines, None)) is not None:
            number, line = source_line
            function = functions.get(number)
            if function is not None:
                block = [source_line, *(next(lines) for _ in range(function.end_line - number))]
                candidate: list[dict[str, Any]] = []
                for row, body in block:
                    if not _append_context_record(
                        candidate, input_index, item, row, f"{row:>6} | {body}", base_chars, max_chars,
                    ):
                        lines = chain(block[1:], lines)
                        break
                else:
                    group = candidate[0]
                    if not _append_segment_group(groups, group, base_chars, max_chars):
                        flush_capsule()
                        groups.append(group)
                    continue
            record = f"{number:>6} | {line}"
            if _append_context_record(groups, input_index, item, number, record, base_chars, max_chars):
                continue
            if groups:
                flush_capsule()
                if _append_context_record(groups, input_index, item, number, record, base_chars, max_chars):
                    continue

            # 空のカプセルにも収まらない行だけ文字で分割する。
            line_body = line.rstrip("\r\n")
            line_ending = line[len(line_body):]
            if not line_body:
                raise ValueError(budget_error)

            offset = 0
            line_overlap = overlap_chars
            focus_ranges: list[tuple[int, int]] = []
            while offset < len(line_body):
                low = 1
                high = min(len(line_body) - offset, max_chars - base_chars)
                best_length = 0
                best_record = ""
                # 行番号・範囲ラベル・Markdownのフェンスも含めて収まる幅を探す。
                while low <= high:
                    piece_length = (low + high) // 2
                    end_column = offset + piece_length
                    context_start = max(0, offset - line_overlap)
                    context_end = min(len(line_body), end_column + line_overlap)
                    suffix = (line_ending or "\n") if end_column == len(line_body) else "\n"
                    candidate_record = (
                        f"{number:>6} focus chars {offset + 1}-{end_column} "
                        f"context chars {context_start + 1}-{context_end} | "
                        f"{line_body[context_start:context_end]}{suffix}"
                    )
                    if _append_context_record([], input_index, item, number, candidate_record, base_chars, max_chars):
                        best_length = piece_length
                        best_record = candidate_record
                        low = piece_length + 1
                    else:
                        high = piece_length - 1
                if best_length == 0:
                    if line_overlap:
                        line_overlap = 0
                        continue
                    raise ValueError(budget_error)
                if not _append_context_record(groups, input_index, item, number, best_record, base_chars, max_chars):
                    flush_capsule()
                    if not _append_context_record(groups, input_index, item, number, best_record, base_chars, max_chars):
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
    for part in manifest_parts:
        boundary = Path(part["path"]).with_name("boundary.md")
        if boundary.is_file():
            part["boundary_sha256"] = sha256_file(boundary)
    return manifest_parts, split_line_coverage



def _append_segment_group(
    groups: list[dict[str, Any]], incoming: dict[str, Any], base_chars: int, max_chars: int,
) -> bool:
    tail = groups[-1] if groups and groups[-1]["input_index"] == incoming["input_index"] else None
    if tail is None:
        if base_chars + sum(map(_group_size, groups)) + _group_size(incoming) > max_chars:
            return False
        groups.append(incoming)
        return True
    merged = {
        **tail,
        "end_line": incoming["end_line"],
        "content_chars": tail["content_chars"] + incoming["content_chars"],
        "max_backticks": max(tail["max_backticks"], incoming["max_backticks"]),
        "ends_with_newline": incoming["ends_with_newline"],
    }
    if base_chars + sum(map(_group_size, groups[:-1])) + _group_size(merged) > max_chars:
        return False
    merged["records"].extend(incoming["records"])
    groups[-1] = merged
    return True


def _syntax_manifest(
    parts: list[dict[str, Any]],
    syntax_by_path: dict[str, tuple[tuple[PhpFunction, ...], str]],
) -> dict[str, Any]:
    by_path: dict[str, list[tuple[int, int, int]]] = {}
    for part in parts:
        for source in part["sources"]:
            by_path.setdefault(source["path"], []).append(
                (source["start_line"], source["end_line"], part["index"]),
            )
    return {
        path: {
            "scanner": "php-lexical-functions-v1",
            "status": status,
            "functions": [
                {"name": fn.name, "start_line": fn.start_line, "end_line": fn.end_line, "capsules": capsules}
                for fn in functions
                if (capsules := sorted({
                    index for start, end, index in by_path.get(path, ())
                    if start <= fn.end_line and end >= fn.start_line
                }))
            ],
        }
        for path, (functions, status) in syntax_by_path.items()
    }

def load_capsule_manifest(run_path: Path) -> tuple[Path, dict[str, Any]]:
    run_path = run_path.resolve()
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
    return capsule_path, manifest


def pinned_php_sources(manifest: dict[str, Any]) -> list[dict[str, str]] | None:
    """固定済みPHPの一覧を検証し、固定前のRunは区別する。"""
    if "php_sources" not in manifest:
        return None
    sources = manifest["php_sources"]
    if not isinstance(sources, list) or any(
        not isinstance(source, dict) or not all(
            isinstance(source.get(key), str) and source[key] for key in ("path", "snapshot", "encoding", "sha256")
        )
        for source in sources
    ):
        raise ValueError("保存済みPHPのmanifestが無効です。")
    return sources


def read_pinned_php(capsule_path: Path, source: dict[str, str]) -> bytes:
    """固定済みPHPの保存先を検証し、manifestと一致する内容だけを返す。"""
    relative = Path(source["snapshot"])
    if relative.anchor or ".." in relative.parts:
        raise ValueError(f"保存済みPHPのパスが無効です: {relative}")
    path = safe_target(capsule_path, relative)
    if not path.is_file():
        raise ValueError(f"保存済みPHPが見つかりません: {source['snapshot']}")
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != source["sha256"]:
        raise ValueError(f"保存済みPHPのハッシュが一致しません: {source['path']}")
    return content
