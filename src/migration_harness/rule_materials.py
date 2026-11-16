"""必須ルールを省略なしで小分けに読み、起動ごとの読み取り記録を照合する。"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path


RULE_CHARS = 3000


def write_rule_chunks(directory: Path, sources: list[tuple[str, str]]) -> list[dict]:
    """本文を切り捨てず分割する。長い1行も文字位置で分割して全量を残す。"""
    chunks = []
    if directory.is_symlink():
        raise ValueError("unsafe rule material directory")
    directory.mkdir(exist_ok=True)
    for path, text in sources:
        offset = 0
        while offset < len(text):
            end = min(offset + RULE_CHARS, len(text))
            if end < len(text):
                newline = text.rfind("\n", offset, end)
                if newline >= offset:
                    end = newline + 1
            name = f"rule-{len(chunks) + 1:04d}.txt"
            content = text[offset:end].encode("utf-8")
            if (directory / name).is_symlink():
                raise ValueError(f"unsafe rule material: {name}")
            (directory / name).write_bytes(content)
            chunks.append({
                "file": name, "path": path, "start_char": offset, "end_char": end,
                "sha256": hashlib.sha256(content).hexdigest(),
            })
            offset = end
    if (directory / "rules.json").is_symlink():
        raise ValueError("unsafe rule material index")
    (directory / "rules.json").write_text(json.dumps(chunks, ensure_ascii=False, indent=2), encoding="utf-8")
    return chunks


def receipt_name(token: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{32}", token):
        raise ValueError("invalid rule reading token")
    return f"migration-rule-reads-{token}.jsonl"


def missing_rule_reads(root: Path, token: str, chunks: list[dict]) -> list[str]:
    """前のカプセルの記録や内容の違う記録では、今回の読み取りを代替しない。"""
    receipt = root / receipt_name(token)
    read = set()
    if receipt.is_file() and not receipt.is_symlink():
        try:
            for line in receipt.read_text(encoding="utf-8").splitlines():
                entry = json.loads(line)
                if isinstance(entry, dict) and entry.get("token") == token:
                    read.add((entry.get("file"), entry.get("sha256")))
        except (ValueError, TypeError):
            return [chunk["file"] for chunk in chunks]
    return [chunk["file"] for chunk in chunks if (chunk["file"], chunk["sha256"]) not in read]


def read_rule(root: Path, token: str, name: str) -> None:
    """Junieのツール出力が巨大にならないよう、1回で1断片だけを返す。"""
    receipt = root / receipt_name(token)
    if not re.fullmatch(r"rule-\d+\.txt", name):
        raise ValueError("specify exactly one rule-NNNN.txt per command")
    directory = root / ".migration-materials"
    if directory.is_symlink() or receipt.is_symlink():
        raise ValueError("unsafe rule reading path")
    chunks = json.loads((directory / "rules.json").read_text(encoding="utf-8"))
    chunk = next(item for item in chunks if item["file"] == name)
    path = directory / name
    if path.is_symlink():
        raise ValueError("unsafe rule file")
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != chunk["sha256"]:
        raise ValueError(f"saved rule changed: {name}")
    text = content.decode("utf-8")
    if len(text) > RULE_CHARS:
        raise ValueError("rule chunk exceeds the reading limit")
    print(f"{name} — {chunk['path']} ({chunk['start_char']}:{chunk['end_char']})\n{text}", flush=True)
    with receipt.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"token": token, "file": name, "sha256": chunk["sha256"]}) + "\n")


if __name__ == "__main__":
    # このファイルを作業フォルダに複製して実行する。ハーネスのインストールは不要。
    sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) != 3:
        raise SystemExit("usage: migration-read-rules.py TOKEN rule-NNNN.txt")
    read_rule(Path(__file__).resolve().parent, *sys.argv[1:])
