"""登録済み移行ルールの全範囲を確認してから、設定案を反映する。"""

import argparse
import hashlib
import json
import stat
import sys
import tomllib
from collections import Counter
from pathlib import Path
from tempfile import NamedTemporaryFile

from rule_meaning import evidence_inputs, validate_evidence
from rule_pack import source_files, verify
from rule_usage import inspect_rules

PAGE_CHARS = 2800


def save_new(path: Path, data: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(data)


def pages_for(report: dict, *, verify_source: bool = False) -> list:
    pages = []
    for item in report["rules"]:
        source = Path(item["path"])
        data = source.read_bytes()
        if verify_source and _digest(data) != item["sha256"]:
            raise ValueError(f"確認用ページ作成中に原文が変更されました: {source}")
        lines = data.decode(item["encoding"]).splitlines()
        page = []
        size = 0
        for number, line in enumerate(lines, 1):
            # 長い物理行も省略せず、同じ行番号で続きの断片を渡す。
            for offset in range(0, max(len(line), 1), PAGE_CHARS - 24):
                fragment = line[offset:offset + PAGE_CHARS - 24]
                cost = len(fragment) + 24
                if size + cost > PAGE_CHARS:
                    pages.append((source, page))
                    page = []
                    size = 0
                page.append((number, fragment))
                size += cost
        if page:
            pages.append((source, page))
    return pages


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _encode(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _page_tail(page) -> str:
    return next((text for _, text in reversed(page) if text.strip()), "")


def _page_index(pages) -> list:
    return [{
        "source": str(source), "first": page[0][0], "last": page[-1][0],
        "tail_sha256": _digest(_page_tail(page).encode("utf-8")), "sha256": _digest(_encode(page)),
    } for source, page in pages]


class ReviewPages:
    def __init__(self, pack: Path, index: list, live_pages=None):
        self.pack = pack
        self.index = index
        self.live_pages = live_pages

    def __len__(self) -> int:
        return len(self.index)

    def body(self, index: int):
        if self.live_pages is not None:
            return self.live_pages[index - 1][1]
        data = (self.pack / "review-pages" / f"{index}.json").read_bytes()
        if _digest(data) != self.index[index - 1]["sha256"]:
            raise ValueError(f"確認用の固定ページ {index} が変更されました。")
        return json.loads(data)


def _snapshot_pages(pack: Path, report: dict) -> ReviewPages:
    pages = pages_for(report, verify_source=True)
    index = _page_index(pages)
    root = pack / "review-pages"
    root.mkdir()
    for number, (_, page) in enumerate(pages, 1):
        save_new(root / f"{number}.json", _encode(page))
    save_new(pack / "review-pages.json", _encode({
        "format_version": 1, "inventory_sha256": _digest((pack / "loading-before.json").read_bytes()),
        "pages": index,
    }))
    return ReviewPages(pack, index)


def inventory(pack: Path, *, final: bool = True) -> tuple[Path, bytes, ReviewPages]:
    inventory_data = (pack / "loading-before.json").read_bytes()
    report = json.loads(inventory_data)
    config = Path(report["config_path"])
    original = (pack / "config.before.toml").read_bytes()
    if config.read_bytes() != original:
        raise ValueError("設定か原文が確認開始後に変更されました。新しいPACKで確認してください。")
    saved = pack / "review-pages.json"
    if not saved.exists() and (pack / "review-pages").exists():
        raise ValueError("確認用ページの索引がありません。")
    index = None
    if saved.exists():
        state = json.loads(saved.read_bytes())
        if state.get("format_version") != 1 or state.get("inventory_sha256") != _digest(inventory_data):
            raise ValueError("確認用ページの索引と元の棚卸しが一致しません。")
        index = state["pages"]
    if index is None or final:
        if inspect_rules(config) != report:
            raise ValueError("設定か原文が確認開始後に変更されました。新しいPACKで確認してください。")
        pages = pages_for(report, verify_source=True)
        fresh_index = _page_index(pages)
        if index is not None and index != fresh_index:
            raise ValueError("確認用ページの索引と設定・原文から再生成した索引が一致しません。")
        return config, original, ReviewPages(pack, fresh_index, pages)
    return config, original, ReviewPages(pack, index)


def print_index(pages: ReviewPages, start: int, limit: int) -> None:
    if start < 1 or start > max(1, len(pages)) or not 1 <= limit <= 20:
        raise ValueError("索引は有効な開始ページと1〜20件の件数を指定してください。")
    end = min(len(pages), start + limit - 1)
    for number, item in enumerate(pages.index[start - 1:end], start):
        print(f"{number}: {item['source']} ({item['first']}–{item['last']})")
    suffix = f"。次は index --start {end + 1}" if end < len(pages) else ""
    print(f"全 {len(pages)} ページ{suffix}")


def review_records(pack: Path) -> dict:
    review_path = pack / "reading-review.json"
    review = json.loads(review_path.read_text(encoding="utf-8")) if review_path.exists() else {}
    if not isinstance(review, dict):
        raise ValueError("reading-review.json はページ番号をキーにしたJSONオブジェクトにしてください。")
    return review


def check_review(pack: Path, pages: ReviewPages, *, records: dict | None = None,
                 numbers: set[int] | None = None) -> set[tuple[Path, int]]:
    review = review_records(pack) if records is None else records
    if set(review) - {str(i) for i in range(1, len(pages) + 1)}:
        raise ValueError("確認記録に不明なページ番号があります。")
    mandatory = set()
    missing = []
    reasons = []
    for i, descriptor in enumerate(pages.index, 1):
        if numbers is not None and i not in numbers:
            continue
        entry = review.get(str(i), {})
        end = entry.get("end") if isinstance(entry, dict) else None
        end_matches = isinstance(end, str) and _digest(end.encode("utf-8")) == descriptor["tail_sha256"]
        if (
            not isinstance(entry, dict)
            or not (pack / "shown" / str(i)).is_file()
            or not end_matches
            or not isinstance(entry.get("note"), str)
            or not entry["note"].strip()
            or not isinstance(entry.get("required_lines"), list)
        ):
            missing.append(i)
            if isinstance(entry, dict) and "end" in entry and not end_matches:
                reasons.append(f"ページ {i}: 末尾の引用が原文と一致しません。該当ページを再読して訂正してください。")
            continue
        for number in entry["required_lines"]:
            if type(number) != int or not descriptor["first"] <= number <= descriptor["last"]:
                raise ValueError(f"ページ {i} の必須行番号が範囲外です。")
            mandatory.add((Path(descriptor["source"]), number))
    if missing:
        raise ValueError(f"未確認のページ: {', '.join(map(str, missing))}" +
                         ("\n" + "\n".join(reasons) if reasons else ""))
    return mandatory


def show_pages(pack: Path, pages: ReviewPages, numbers: list[int]) -> None:
    # 全文を1回で出すとツール側で省略される。小さい束でも確認記録はページごとに残す。
    for index in numbers:
        page = pages.body(index)
        source = pages.index[index - 1]["source"]
        print(f"ページ {index}/{len(pages)}: {source}")
        for number, text in page:
            print(f"{number:>6} | {text}")
        print(f"ページ {index} 終端", flush=True)
        (pack / "shown" / str(index)).touch()


def without_rules(data: bytes) -> dict:
    doc = tomllib.loads(data.decode("utf-8"))
    doc["project"].pop("migration_rules", None)
    for unit in doc.get("units", []):
        if "inputs" in unit:
            unit["inputs"] = [item for item in unit["inputs"] if item.get("role") != "migration_rules"]
            if not unit["inputs"]:
                unit.pop("inputs")
    return doc


def apply(pack: Path, config: Path, original: bytes, mandatory: set, candidate: Path, *,
          collect_evidence: bool = False, register_new: bool = False) -> None:
    pack = pack.resolve()
    data = candidate.read_bytes()
    if without_rules(data) != without_rules(original):
        raise ValueError("移行ルール以外の設定が変更されています。")
    # 相対パスの解決基準を保ち、検証済みの設定案だけを置き換える。
    with NamedTemporaryFile(dir=config.parent, suffix=".toml", delete=False) as stream:
        stream.write(data)
        temporary = Path(stream.name)
    try:
        after = inspect_rules(temporary)
        before = json.loads((pack / "loading-before.json").read_text(encoding="utf-8"))
        source_text = {
            Path(item["path"]): Path(item["path"]).read_bytes().decode(item["encoding"])
            for item in before["rules"]
        }
        old_paths = source_text.keys()
        new_rules = {Path(item["path"]): item["usage"] for item in after["rules"]}
        if register_new:
            unchanged = {item["path"]: item for item in after["rules"] if Path(item["path"]) in old_paths}
            if unchanged != {item["path"]: item for item in before["rules"]} or old_paths == new_rules.keys():
                raise ValueError("新規登録では既存ルールを変更せず、新しいルールを追加してください。")
            generated = pack / "generated"
            registered = {path for path in new_rules.keys() - old_paths if path.is_relative_to(generated)}
            if registered:
                verify(pack)
                expected = tomllib.loads((generated / "rules.toml").read_text(encoding="utf-8"))
                if {Path(row["path"]): row["usage"] for row in expected["project"]["migration_rules"]} != {
                    path: new_rules[path] for path in registered
                }:
                    raise ValueError("新規生成した資料の登録が生成結果と一致しません。")
        elif old_paths != new_rules.keys():
            verify(pack)
            _, split_sources = source_files(pack)
            removed = Counter(source_text[path] for path in old_paths - new_rules.keys())
            if removed != Counter("".join(lines) for lines in split_sources.values()):
                raise ValueError("分割元には、置換する確認済みルールの原文だけを使ってください。")
            generated = pack.resolve() / "generated"
            registration = tomllib.loads((generated / "rules.toml").read_text(encoding="utf-8"))
            expected = {
                Path(item["path"]): item["usage"] for item in registration["project"]["migration_rules"]
            }
            if expected != {path: new_rules[path] for path in new_rules.keys() - old_paths}:
                raise ValueError("分割した必須・詳細ファイルの登録が生成結果と一致しません。")
        required = {
            Path(rule["path"]): set(Path(rule["path"]).read_bytes().decode(rule["encoding"]).splitlines())
            for rule in after["rules"] if rule["usage"] == "required"
        }
        for path, number in sorted(mandatory):
            line = source_text[path].splitlines()[number - 1]
            if not any(line in lines for lines in required.values()):
                raise ValueError(f"必須登録に原文がありません: {path}:{number}")
        evidence = evidence_inputs(pack, data, before, after, register_new)
        if collect_evidence:
            versions, _, _, placements, _, relevant = evidence
            print(json.dumps({"format_version": 2, "versions": versions, "unresolved": [], "entries": []},
                             ensure_ascii=False, indent=2))
            print("確認が必要な配置先: " + ", ".join(sorted(relevant)), file=sys.stderr)
            print(json.dumps(placements, ensure_ascii=False, indent=2), file=sys.stderr)
            return
        if not (pack / "meaning-review.json").is_file() and evidence[-1]:
            raise ValueError("意味レビューがありません。meaningで現行版を確認してください。")
        validate_evidence(pack, evidence, mandatory)
        fresh = inspect_rules(temporary)
        if (fresh != after or config.read_bytes() != original
                or evidence_inputs(pack, data, before, fresh, register_new)[0] != evidence[0]):
            raise ValueError("設定または資料が同時に変更されました。")
        temporary.chmod(stat.S_IMODE(config.stat().st_mode))
        temporary.replace(config)
        after["config_path"] = str(config)
        print(json.dumps(after, ensure_ascii=False, indent=2))
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for name in ("start", "index", "read", "record", "check", "meaning", "apply"):
        command = commands.add_parser(name)
        command.add_argument("pack", type=Path)
        if name == "start":
            command.add_argument("config", type=Path)
        if name == "index":
            command.add_argument("--start", type=int, default=1)
            command.add_argument("--limit", type=int, default=10)
        if name == "read":
            command.add_argument("pages", type=int, nargs="+")
        if name == "record":
            command.add_argument("records", help="読んだ1〜2ページの確認記録（JSON、または@JSONファイル）")
            command.add_argument("--next", action="store_true", help="記録後に次の未読2ページを表示")
        if name in {"meaning", "apply"}:
            command.add_argument("candidate", type=Path)
            command.add_argument("--register-new", action="store_true")
    args = parser.parse_args()
    pack = args.pack
    try:
        if args.action == "start":
            report = inspect_rules(args.config)
            pack.mkdir(parents=True, exist_ok=True)
            (pack / "shown").mkdir()
            save_new(pack / "config.before.toml", Path(report["config_path"]).read_bytes())
            save_new(pack / "loading-before.json", json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8"))
            print_index(_snapshot_pages(pack, report), 1, 10)
            return
        config, original, pages = inventory(pack, final=args.action in {"check", "meaning", "apply"})
        if args.action == "index":
            print_index(pages, args.start, args.limit)
            return
        if args.action == "read":
            if not 1 <= len(args.pages) <= 2 or len(set(args.pages)) != len(args.pages):
                raise ValueError("一度に指定できるのは重複のない1〜2ページです。")
            if any(not 1 <= number <= len(pages) for number in args.pages):
                raise ValueError("ページ番号が範囲外です。")
            # 読み直しは許すが、未記録の束を積み残したまま次のページには進めない。
            previous = {i for i in range(1, len(pages) + 1) if (pack / "shown" / str(i)).is_file()}
            if set(args.pages) - previous:
                check_review(pack, pages, numbers=previous)
            show_pages(pack, pages, args.pages)
            return
        if args.action == "record":
            data = (Path(args.records[1:]).read_text(encoding="utf-8-sig")
                    if args.records.startswith("@") else args.records)
            entries = json.loads(data)
            if not isinstance(entries, dict) or not 1 <= len(entries) <= 2:
                raise ValueError("読んだ1〜2ページの記録をJSONオブジェクトで指定してください。")
            records = review_records(pack) | entries
            check_review(pack, pages, records=records, numbers={int(key) for key in entries})
            shown = {i for i in range(1, len(pages) + 1) if (pack / "shown" / str(i)).is_file()}
            if args.next:
                check_review(pack, pages, records=records, numbers=shown)
            with NamedTemporaryFile(dir=pack, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(json.dumps(records, ensure_ascii=False, indent=2).encode("utf-8"))
            temporary.replace(pack / "reading-review.json")
            print("確認記録を保存: " + ", ".join(entries))
            if args.next:
                show_pages(pack, pages, sorted(set(range(1, len(pages) + 1)) - shown)[:2])
            return
        mandatory = check_review(pack, pages)
        if args.action in {"meaning", "apply"}:
            apply(pack, config, original, mandatory, args.candidate,
                  collect_evidence=args.action == "meaning", register_new=args.register_new)
        else:
            print(f"全 {len(pages)} ページ確認済み")
    except (OSError, ValueError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
