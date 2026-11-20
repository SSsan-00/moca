"""原文の全行を照合しながら、必須事項と用途別のMarkdownへ配置する。"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tempfile
import tomllib
from pathlib import Path


def json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def read_json(path: Path) -> dict:
    if path.is_symlink():
        raise ValueError(f"シンボリックリンクは使えません: {path}")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"JSONの最上位はオブジェクトにしてください: {path}")
    return value


def regular_files(directory: Path):
    # rglobは読めないフォルダを省略するため、列挙失敗を伝播させる。
    for path in sorted(directory.iterdir()):
        if path.is_symlink():
            raise ValueError(f"リンクは使えません: {path}")
        if path.is_dir():
            yield from regular_files(path)
        elif path.is_file():
            yield path
        else:
            raise ValueError(f"通常のファイルではありません: {path}")


def source_files(pack: Path) -> tuple[list[dict], dict[str, list[str]]]:
    directory = pack / "sources"
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("sources/ に原文のコピーを配置してください。リンクは使えません。")
    inventory, texts = [], {}
    for path in regular_files(directory):
        if path.suffix.lower() not in {".md", ".markdown", ".txt"}:
            raise ValueError(f"原文はUTF-8のMarkdownかテキストにしてください: {path}")
        name = path.relative_to(directory).as_posix()
        data = path.read_bytes()
        text = data.decode("utf-8-sig")
        if "\0" in text:
            raise ValueError(f"バイナリの内容は変換できません: {name}")
        lines = text.splitlines(keepends=True)
        inventory.append({"path": name, "sha256": hashlib.sha256(data).hexdigest(), "lines": len(lines)})
        texts[name] = lines
    if not inventory:
        raise ValueError("sources/ に原文がありません。")
    return inventory, texts


def snapshot(pack: Path) -> None:
    inventory, _ = source_files(pack)
    # 採用済みの原文を別内容で上書きして検査をすり抜けないよう、初回だけ作る。
    with (pack / "sources.json").open("x", encoding="utf-8", newline="") as stream:
        stream.write(json_text({"sources": inventory}))


def label(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or any(c in value for c in "\r\n\0"):
        raise ValueError(f"{name}は空でない1行の文字列にしてください。")
    return value


def render(pack: Path) -> dict[str, bytes]:
    inventory, texts = source_files(pack)
    if read_json(pack / "sources.json") != {"sources": inventory}:
        raise ValueError("原文の変更・追加・削除を検出しました。新しい作業領域でsnapshotからやり直してください。")
    plan = read_json(pack / "plan.json")
    # 既存PACKの検証結果を変えず、新しい参照案内は明示した計画にだけ適用する。
    version = plan.get("format_version", 1)
    if type(version) is not int or version not in (1, 2):
        raise ValueError("format_versionは1または2にしてください。")
    documents = plan.get("documents")
    if not isinstance(documents, list) or not documents:
        raise ValueError("plan.jsonにdocumentsの配列が必要です。")
    placements = {name: [None] * len(lines) for name, lines in texts.items()}
    output, coverage, details = {}, [], []
    for document in documents:
        if not isinstance(document, dict):
            raise ValueError("documentsの要素はオブジェクトにしてください。")
        name = document.get("path")
        if not isinstance(name, str) or not (name == "Agent.md" or re.fullmatch(r"details/[a-z0-9][a-z0-9_-]*\.md", name)):
            raise ValueError("出力先はAgent.md、またはdetails/用途名.md（英小文字・数字・ハイフン等）にしてください。")
        if re.fullmatch(r"con|prn|aux|nul|com[1-9]|lpt[1-9]", Path(name).stem):
            raise ValueError(f"Windowsの予約名は使えません: {name}")
        if name in output:
            raise ValueError(f"出力先が重複しています: {name}")
        title = label(document.get("title"), f"{name}のtitle")
        if name != "Agent.md":
            description = label(document.get("description"), f"{name}のdescription（読む場面・内容）")
            details.append((name, description))
        parts = document.get("parts")
        if not isinstance(parts, list):
            raise ValueError(f"{name}のpartsは配列にしてください。")
        content = [f"# {title}\n\n"]
        for part in parts:
            if not isinstance(part, dict) or not isinstance(part.get("source"), str) or part["source"] not in texts:
                raise ValueError(f"未登録の原文です: {part}")
            source, start, end = part["source"], part.get("start"), part.get("end")
            if type(start) is not int or type(end) is not int or not 1 <= start <= end <= len(texts[source]):
                raise ValueError(f"行範囲が不正です: {part}")
            for index in range(start - 1, end):
                if placements[source][index] is not None:
                    raise ValueError(f"原文の行が重複しています: {source}:{index + 1}")
                placements[source][index] = name
            # 元の改行・空白・例外・コードをそのまま配置する。文章の生成や要約は行わない。
            excerpt = "".join(texts[source][start - 1:end])
            content.extend([f"出典: {json.dumps(source, ensure_ascii=False)} / {start}–{end}行\n\n", excerpt, "\n\n"])
            coverage.append({"source": source, "start": start, "end": end, "destination": name})
        output[name] = "".join(content)
    if "Agent.md" not in output:
        raise ValueError("入口のAgent.mdが必要です。")
    missing = [f"{name}:{i + 1}" for name, lines in placements.items() for i, destination in enumerate(lines) if destination is None]
    if missing:
        raise ValueError(f"未配置の原文が{len(missing)}行あります: {', '.join(missing[:20])}")
    routing = (
        "\n## 詳細を読むとき\n\n"
        "この入口の必須事項は移行・修正・再開で適用します。下の資料は対象の処理に該当する場合、"
        "不明な場合、関連するエラーを直す場合に原文を読み、条件と例外を合わせて確認してください。\n\n"
        "ハーネス実行中は、Junie起動時のDetail and example referencesのsourceで資料を照合し、"
        "fileにある保存済みRunのファイルを読んでください。元の絶対パスや下の相対リンクを"
        "作業フォルダから直接開く必要はありません。\n\n"
    )
    if version == 2:
        routing = (
            "\n## 詳細を読むとき\n\n"
            "移行・修正・再開では、今回提供された必須事項と該当する詳細の条件・例外を適用します。"
            "起動時のMandatory textとApplicable detailsにある原文を先に確認し、"
            "同じ本文を確認済みの記録のためだけにファイルから読み直す必要はありません。"
            "編集後は適用する義務と変更内容を照合してください。"
            "参照資料と付属ヘルパーは読み取り専用です。独自の抽出・照合スクリプトや中間データは"
            ".migration-scratch/に置き、既存ヘルパーを書き換えずに利用してください。\n\n"
            "必要な本文が未提供なら、Detail and example referencesのsourceとfileを照合して"
            "保存済みRunの資料を読みます。適用判断が不明な資料も確認します。"
            "提供済み本文は索引だけの説明とは区別し、説明で原文を代用しないでください。"
            "下のリンクはPACK内の索引です。Runに同じ相対パスがあるとは限らず、"
            "無関係として除外された資料をリンクだけで探し直す必要はありません。\n\n"
        )
    for name, description in details:
        routing += f"- [{name}]({name}): {description}\n"
    output["Agent.md"] += routing
    rules = []
    for name, description in [("Agent.md", ""), *details]:
        rules.extend(["[[project.migration_rules]]", f"path = {toml_string((pack / 'generated' / name).as_posix())}",
                      f'usage = "{"required" if name == "Agent.md" else "reference"}"'])
        if description:
            rules.append(f"description = {toml_string(description)}")
        rules.append("")
    output["rules.toml"] = "# ルール登録用の断片です。既存の設定全体を置き換えないでください。\n" + "\n".join(rules)
    tomllib.loads(output["rules.toml"])
    output["coverage.json"] = json_text({"sources": inventory, "placements": coverage})
    return {name: content.encode("utf-8") for name, content in output.items()}


def toml_string(value: str) -> str:
    # JSONの引用に加えて、TOMLが生の文字として許さないDELをエスケープする。
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


def build(pack: Path) -> None:
    output = render(pack)
    destination = pack / "generated"
    if destination.exists() or destination.is_symlink():
        raise ValueError("generated/は既にあります。修正は別名の作業領域で行い、採用済みの資料を保持してください。")
    # 検査が通った全ファイルだけを公開する。失敗途中の出力をTOMLへ登録させない。
    with tempfile.TemporaryDirectory(dir=pack) as temporary:
        staged = Path(temporary) / "generated"
        staged.mkdir()
        for name, data in output.items():
            path = staged / name
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(data)
        staged.rename(destination)


def verify(pack: Path) -> None:
    expected = render(pack)
    directory = pack / "generated"
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("generated/がありません。buildを実行してください。")
    actual = {}
    for path in regular_files(directory):
        actual[path.relative_to(directory).as_posix()] = path.read_bytes()
    if actual != expected:
        changed = sorted(name for name in actual.keys() | expected.keys() if actual.get(name) != expected.get(name))
        raise ValueError("出力の変更・欠落・追加を検出しました: " + ", ".join(changed))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("snapshot", "build", "verify"))
    parser.add_argument("pack", type=Path, help="sources/とplan.jsonを置く作業領域")
    args = parser.parse_args()
    pack = args.pack.expanduser().resolve()
    try:
        {"snapshot": snapshot, "build": build, "verify": verify}[args.command](pack)
    except (ValueError, OSError) as exc:
        parser.exit(1, f"{type(exc).__name__}: {exc}\n")
    print(f"{args.command}: OK ({pack})")
    if args.command in {"build", "verify"}:
        print("原文の全行と配置先が一致しました。適用条件・優先順位・リンクの意味は別途確認してください。")


if __name__ == "__main__":
    main()
