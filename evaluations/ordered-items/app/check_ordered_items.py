import argparse
from html.parser import HTMLParser
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parent
TARGET = "Pages/OrderedItems.cshtml"


class ListItems(HTMLParser):
    def __init__(self):
        """HTMLから順序付きのリスト要素を集める状態を初期化する。"""
        super().__init__()
        self.values = []
        self.text = None

    def handle_starttag(self, tag, attrs):
        """リスト項目の本文の収集を始める。"""
        if tag == "li":
            self.text = []

    def handle_data(self, data):
        """収集中のリスト項目へデコード済みの本文を加える。"""
        if self.text is not None:
            self.text.append(data)

    def handle_endtag(self, tag):
        """リスト項目を確定し、HTMLに出た順に結果を残す。"""
        if tag == "li" and self.text is not None:
            self.values.append("".join(self.text).strip())
            self.text = None


def main():
    """応答のリスト要素を期待した値と順序に照合する。"""
    parser = argparse.ArgumentParser(description="Compare rendered list items with the PHP fixture oracle.")
    parser.add_argument("--html-stdin", action="store_true", help="Check pre-rendered UTF-8 HTML from stdin")
    args = parser.parse_args()

    try:
        expected = json.loads((ROOT / "expected.json").read_text(encoding="utf-8"))
        if args.html_stdin:
            html = sys.stdin.buffer.read().decode("utf-8")
        else:
            result = subprocess.run(
                ["dotnet", "bin/Debug/net10.0/OrderedItems.dll"], cwd=ROOT,
                capture_output=True, check=True, timeout=30, encoding="utf-8",
            )
            html = result.stdout
    except subprocess.CalledProcessError as exc:
        print(f"FIXTURE_RUNTIME {TARGET}:1 error: dotnet exited {exc.returncode}: {exc.stderr.strip()!a}")
        return 2
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"FIXTURE_RUNTIME {TARGET}:1 error: {str(exc)!a}")
        return 2

    items = ListItems()
    items.feed(html)
    items.close()
    if items.values != expected:
        print(f"FIXTURE_SEQUENCE {TARGET}:1 error: expected {json.dumps(expected)}, got {json.dumps(items.values)}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
