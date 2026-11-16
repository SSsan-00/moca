"""登録済み移行ルールの全範囲を確認してから、設定案を反映する。"""

import argparse
import json
import stat
import tomllib
from collections import Counter
from pathlib import Path
from tempfile import NamedTemporaryFile

from rule_pack import source_files, verify
from rule_usage import inspect_rules

PAGE_CHARS = 2800


def save_new(path: Path, data: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(data)


def pages_for(report: dict) -> list:
    pages = []
    for item in report["rules"]:
        source = Path(item["path"])
        lines = source.read_bytes().decode(item["encoding"]).splitlines()
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


def inventory(pack: Path) -> tuple[Path, bytes, list]:
    report = json.loads((pack / "loading-before.json").read_text(encoding="utf-8"))
    config = Path(report["config_path"])
    original = (pack / "config.before.toml").read_bytes()
    if config.read_bytes() != original or inspect_rules(config) != report:
        raise ValueError("設定か原文が確認開始後に変更されました。新しいPACKで確認してください。")
    return config, original, pages_for(report)


def check_review(pack: Path, pages: list) -> set[tuple[Path, int]]:
    review_path = pack / "reading-review.json"
    review = json.loads(review_path.read_text(encoding="utf-8")) if review_path.exists() else {}
    if not isinstance(review, dict):
        raise ValueError("reading-review.json はページ番号をキーにしたJSONオブジェクトにしてください。")
    if set(review) - {str(i) for i in range(1, len(pages) + 1)}:
        raise ValueError("確認記録に不明なページ番号があります。")
    mandatory = set()
    missing = []
    for i, (source, page) in enumerate(pages, 1):
        entry = review.get(str(i), {})
        last = next((text for _, text in reversed(page) if text.strip()), "")
        if (
            not isinstance(entry, dict)
            or not (pack / "shown" / str(i)).is_file()
            or entry.get("end") != last
            or not isinstance(entry.get("note"), str)
            or not entry["note"].strip()
            or not isinstance(entry.get("required_lines"), list)
        ):
            missing.append(i)
            continue
        for number in entry["required_lines"]:
            if type(number) != int or not any(n == number for n, _ in page):
                raise ValueError(f"ページ {i} の必須行番号が範囲外です。")
            mandatory.add((source, number))
    if missing:
        raise ValueError(f"未確認のページ: {', '.join(map(str, missing))}")
    return mandatory


def without_rules(data: bytes) -> dict:
    doc = tomllib.loads(data.decode("utf-8"))
    doc["project"].pop("migration_rules", None)
    for unit in doc.get("units", []):
        if "inputs" in unit:
            unit["inputs"] = [item for item in unit["inputs"] if item.get("role") != "migration_rules"]
            if not unit["inputs"]:
                unit.pop("inputs")
    return doc


def apply(pack: Path, config: Path, original: bytes, mandatory: set, candidate: Path) -> None:
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
        if old_paths != new_rules.keys():
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
        if config.read_bytes() != original:
            raise ValueError("設定が同時に変更されました。")
        temporary.chmod(stat.S_IMODE(config.stat().st_mode))
        temporary.replace(config)
        after["config_path"] = str(config)
        print(json.dumps(after, ensure_ascii=False, indent=2))
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for name in ("start", "read", "check", "apply"):
        command = commands.add_parser(name)
        command.add_argument("pack", type=Path)
        if name == "start":
            command.add_argument("config", type=Path)
        if name == "read":
            command.add_argument("page", type=int)
        if name == "apply":
            command.add_argument("candidate", type=Path)
    args = parser.parse_args()
    pack = args.pack
    try:
        if args.action == "start":
            report = inspect_rules(args.config)
            pack.mkdir(parents=True, exist_ok=True)
            (pack / "shown").mkdir()
            save_new(pack / "config.before.toml", Path(report["config_path"]).read_bytes())
            save_new(pack / "loading-before.json", json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8"))
            for i, (source, page) in enumerate(pages_for(report), 1):
                print(f"{i}: {source} ({page[0][0]}–{page[-1][0]})")
            return
        config, original, pages = inventory(pack)
        if args.action == "read":
            if not 1 <= args.page <= len(pages):
                raise ValueError("ページ番号が範囲外です。")
            source, page = pages[args.page - 1]
            print(f"ページ {args.page}/{len(pages)}: {source}")
            for number, text in page:
                print(f"{number:>6} | {text}")
            print(f"ページ {args.page} 終端", flush=True)
            (pack / "shown" / str(args.page)).touch()
            return
        mandatory = check_review(pack, pages)
        if args.action == "apply":
            apply(pack, config, original, mandatory, args.candidate)
        else:
            print(f"全 {len(pages)} ページ確認済み")
    except (OSError, ValueError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
