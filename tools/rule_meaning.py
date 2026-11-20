import hashlib
import json
from pathlib import Path

from rule_pack import read_json, source_files

KINDS = {"obligation", "condition", "exception", "priority", "description"}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _rule_text(item: dict) -> list[str]:
    data = Path(item["path"]).read_bytes()
    if _sha(data) != item["sha256"]:
        raise ValueError(f"資料の版が変更されました: {item['path']}")
    return data.decode(item["encoding"]).splitlines()


def evidence_inputs(pack: Path, candidate: bytes, before: dict, after: dict, additions: bool):
    old = {item["path"]: item for item in before["rules"]}
    new = {item["path"]: item for item in after["rules"]}
    relevant = {path for path, item in new.items() if old.get(path) != item}
    sources = {path: _rule_text(item) for path, item in old.items()}
    destinations = {path: _rule_text(new[path]) for path in relevant}
    placements = {path: [(path, 1, len(lines), 1)] for path, lines in sources.items() if path in new}
    generated = pack / "generated"
    plan_versions = {}
    if any(Path(path).is_relative_to(generated) for path in relevant):
        coverage = read_json(generated / "coverage.json")
        inventory, texts = source_files(pack)
        positions = {}
        for entry in coverage["placements"]:
            name = entry["source"]
            source = str(pack / "sources" / name)
            source_lines = [line.splitlines()[0] if line.splitlines() else "" for line in texts[name]]
            if not additions:
                matches = [path for path, lines in sources.items() if path not in new and lines == source_lines]
                if len(matches) != 1:
                    raise ValueError(f"分割元を確認済み原文に一意に照合できません: {name}")
                source = matches[0]
            else:
                sources[source] = source_lines
            path = str(generated / entry["destination"])
            marker = f"出典: {json.dumps(name, ensure_ascii=False)} / {entry['start']}–{entry['end']}行"
            following = positions.get(path, 0)
            index = destinations[path].index(marker, following) + 2
            positions[path] = index + entry["end"] - entry["start"] + 1
            placements.setdefault(path, []).append((source, entry["start"], entry["end"], index + 1))
        plan_versions = {"plan": _sha((pack / "plan.json").read_bytes()),
                         "coverage": _sha((generated / "coverage.json").read_bytes()),
                         "snapshots": inventory}
    if additions:
        for path, lines in destinations.items():
            if path not in placements:
                sources[path] = lines
                placements[path] = [(path, 1, len(lines), 1)]
    versions = {
        "before": _sha((pack / "loading-before.json").read_bytes()),
        "setting": _sha((pack / "config.before.toml").read_bytes()),
        "reading": _sha((pack / "reading-review.json").read_bytes()) if (pack / "reading-review.json").is_file() else None,
        "candidate": _sha(candidate), "destinations": {path: item["sha256"] for path, item in new.items()},
        "register_new": additions, **plan_versions,
    }
    return versions, sources, destinations, placements, new, relevant


def _quote(entry: object, texts: dict[str, list[str]], name: str):
    if not isinstance(entry, dict):
        raise ValueError(f"{name}の出典位置と引用が必要です。")
    path, line, quote = entry.get("path"), entry.get("line"), entry.get("quote")
    if (not isinstance(path, str) or path not in texts or type(line) is not int
            or not 1 <= line <= len(texts[path]) or not isinstance(quote, str)
            or not quote.strip() or texts[path][line - 1] != quote):
        raise ValueError(f"{name}の引用が対象の原文と一致しません: {path}:{line}")
    return path, line


def validate_evidence(pack: Path, snapshot, mandatory):
    versions, sources, destinations, placements, new, relevant = snapshot
    if not relevant:
        return
    review = read_json(pack / "meaning-review.json")
    if review.get("format_version") != 1 or review.get("versions") != versions:
        raise ValueError("意味レビューが候補や原文の現行版と一致しません。")
    unresolved = review.get("unresolved")
    if not isinstance(unresolved, list) or unresolved:
        raise ValueError(f"意味レビューに未解決事項があります: {unresolved}")
    entries = review.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ValueError("意味レビューに原文と配置先の根拠が必要です。")
    covered, described, retained = set(), set(), set()
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("kind") not in KINDS:
            raise ValueError("意味レビューの種類が不正です。")
        detail = entry.get("reason")
        if not isinstance(detail, str) or not detail.strip():
            raise ValueError("判断理由を記録してください。")
        source, source_line = _quote(entry.get("source"), sources, "原文")
        destination, destination_line = _quote(entry.get("destination"), destinations, "配置先")
        if not any(source == original and start <= source_line <= end
                   and destination_line == first + source_line - start
                   for original, start, end, first in placements.get(destination, ())):
            raise ValueError("原文と配置先が実際の配置に対応していません。")
        covered.add(destination)
        if entry.get("kind") == "description":
            if not new[destination]["description"] or entry.get("description") != new[destination]["description"]:
                raise ValueError("読む場面の説明が候補設定と一致しません。")
            described.add(destination)
        if new[destination]["usage"] == "required":
            retained.add((Path(source), source_line))
    if relevant - covered:
        raise ValueError("根拠がない配置先があります: " + ", ".join(sorted(relevant - covered)))
    missing_descriptions = {path for path in relevant if new[path]["description"]} - described
    if missing_descriptions:
        raise ValueError("読む場面の根拠がありません: " + ", ".join(sorted(missing_descriptions)))
    unchanged_sources = {Path(path) for path in new if path not in relevant and new[path]["usage"] == "required"}
    if any((path, line) not in retained and path not in unchanged_sources for path, line in mandatory):
        raise ValueError("必須登録に原文の配置根拠がありません。")
