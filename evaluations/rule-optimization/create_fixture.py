"""実Junieによるスキル検証用。分割・矛盾時の保留・既存設定の分類を試す。"""

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


repository = Path(__file__).resolve().parents[2]
root = Path(tempfile.mkdtemp(prefix="harness-rule-skill-")).resolve()
harness, app = root / "harness", root / "app"
(harness / "tools").mkdir(parents=True)
(app / "docs").mkdir(parents=True)
shutil.copytree(repository / ".junie/skills/harness-optimize-rules", harness / ".junie/skills/harness-optimize-rules")
shutil.copyfile(repository / "tools/rule_pack.py", harness / "tools/rule_pack.py")
for name in ("rule_usage.py", "rule_review.py", "rule_meaning.py"):
    shutil.copyfile(repository / "tools" / name, harness / "tools" / name)
mixed = """# 散らばったメモ
[G01] 全移行で原文の値・順序・重複を維持する。推測で数値を補わない。

## DB処理の場合
[D01] 更新トランザクションが失敗したらrollbackする。
[D02] ただし参照専用処理ではトランザクションを開始しない。
[D03] DBタイムアウトは30秒。値を勝手に変えない。
```sql
SELECT 'A & B', 0.01, '0007';
```

## 共通の追記
[G02] 全移行でエラー本文は切り捨てずに記録する。
"""
views = """# 画面まわりのメモ
## Razorの表示
[V01] 動的な文字列はRazorの標準エンコードに任せる。
[V02] 信頼済みの固定HTMLだけは例外としてHtml.Rawを使える。

## 全体に関わるメモ
[G03] 全移行で文化依存の日時変換を使わない。UTC指定を維持する。
[G01] 全移行で原文の値・順序・重複を維持する。推測で数値を補わない。
"""
if "--conflict" in sys.argv:
    views += "\n## DBへの追記（優先順位は不明）\n[D04] DBタイムアウトは60秒とし、30秒は禁止する。\n"
for name, text in [("mixed.md", mixed), ("views.txt", views), ("untouched.md", "対象外のルール。変更しない。\n")]:
    (app / "docs" / name).write_text(text, encoding="utf-8")
pack = harness / "rule-workspace/LegacyApp"
(pack / "sources").mkdir(parents=True)
if "--loading" not in sys.argv and "--tail-required" not in sys.argv:
    for name in ("mixed.md", "views.txt"):
        shutil.copyfile(app / "docs" / name, pack / "sources" / name)
(app / "legacy").write_text("<?php echo 1;", encoding="utf-8")
(app / "must-not-run.py").write_text("from pathlib import Path\nPath('HOOK_WAS_RUN').write_text('unexpected')\n", encoding="utf-8")
config = '''# Keep this configuration comment.
[project]
root = "../app"
worktree_root = "../trees"
run_root = "../runs"

[[project.migration_rules]]
path = "docs/mixed.md"
[[project.migration_rules]]
path = "docs/views.txt"
[[project.migration_rules]]
path = "docs/untouched.md" # Keep this unrelated rule and comment.

[scripts]
before_prepare = [COMMAND]

[custom]
unchanged = "keep this value"

[[units]]
id = "example"
source = "legacy"
target = "Result.cs"
'''.replace("COMMAND", json.dumps([sys.executable, "must-not-run.py"]))
if "--loading" in sys.argv or "--tail-required" in sys.argv:
    documents = {
        "Agent.md": "# 共通の必須事項\n[G10] 全移行で原文の値・順序・重複を保持する。\n[G11] 全移行でエラー本文を切り捨てず記録する。\n",
        "db-details.md": "# DBアクセスの詳細\nDBアクセスの移行・修正にのみ適用する。\n[D10] 更新失敗時はrollbackする。ただし参照専用ではトランザクションを開始しない。\n"
            + "\n".join(f"DBの例{i}: 接続を開き、値'{i:04d}'をパラメーターで渡し、終了時に接続を破棄する。" for i in range(1, 25)) + "\n",
        "view-details.md": "# Razorの表示処理\n[V10] 動的文字列は標準エンコードに任せる。\n[V11] 信頼済みの固定HTMLだけHtml.Rawを使用できる。\n",
        "unclear.md": "# 確認事項\n[U10] 状態の引き継ぎを確認すること。\n",
    }
    if "--tail-required" in sys.argv:
        documents["view-details.md"] += "\n" + "".join(
            f"Razor display example {i:02d}: render the original value as encoded text.\n"
            for i in range(1, 60)
        ) + '\n## Project-wide obligations\n[T90] Every migration and repair must preserve error correlation IDs verbatim.\n'
    for name, content in documents.items():
        (app / "docs" / name).write_text(content, encoding="utf-8")
    old_rules = config[config.index("[[project.migration_rules]]"):config.index("[scripts]")]
    config = config.replace(old_rules, '''[[project.migration_rules]]
path = "docs/Agent.md" # Keep entry note.
usage = "reference"
description = "参考資料"
custom = "keep entry metadata"
[[project.migration_rules]]
path = "docs/db-details.md" # Keep DB note. No usage means required.
[[project.migration_rules]]
path = VIEW_PATH
usage = "reference"
description = "詳細"
[[project.migration_rules]]
path = "docs/unclear.md"
usage = "required"

'''.replace("VIEW_PATH", json.dumps((app / "docs/view-details.md").as_posix())))
    config += '''
[[units.inputs]]
role = "migration_rules"
path = "docs/db-details.md" # Keep legacy note.
usage = "required"
custom = 42

[checker]
command = ["python", "check.py"]
max_repair_attempts = 3
'''
(harness / "harness.local.toml").write_text(config, encoding="utf-8")
(root / "before.toml").write_text(config, encoding="utf-8")
subprocess.run(["git", "init", "-q", str(app)], check=True)
print(root)
