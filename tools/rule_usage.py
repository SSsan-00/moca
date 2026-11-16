"""設定を変更せず、実際に適用される移行ルールと必須本文の文字数を調べる。"""

import argparse
import hashlib
import json
from pathlib import Path

from migration_harness.config import load_config
from migration_harness.workflow import _input_encoding


def inspect_rules(config_path: Path) -> dict:
    config = load_config(config_path)
    rules = []
    # 共通ルールは全unitに展開済み。全unitを足すと同じ資料を重複計上する。
    for item in config["units"][0]["inputs"]:
        if item["role"] != "migration_rules":
            continue
        path = (Path(config["project_root"]) / item["path"]).resolve()
        encoding = _input_encoding(path, item["encoding"])
        data = path.read_bytes()
        rules.append({
            "path": str(path), "usage": item["usage"], "description": item["description"],
            "encoding": encoding, "chars": len(data.decode(encoding)),
            "sha256": hashlib.sha256(data).hexdigest(),
        })
    return {
        "config_path": config["config_path"], "rules": rules,
        "required_chars": sum(rule["chars"] for rule in rules if rule["usage"] == "required"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("--output", type=Path, help="UTF-8で保存する新規ファイル。既存ファイルは上書きしない")
    args = parser.parse_args()
    result = json.dumps(inspect_rules(args.config), ensure_ascii=False, indent=2) + "\n"
    if args.output:
        with args.output.open("x", encoding="utf-8", newline="") as stream:
            stream.write(result)
    else:
        print(result, end="")


if __name__ == "__main__":
    main()
