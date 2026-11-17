"""独立した6単位を、5件並列・1件待機で実行する検証用アプリを作る。"""

import json
import subprocess
import sys
from pathlib import Path


root = Path(sys.argv[1]).resolve()
app = root / "app"
app.mkdir(parents=True, exist_ok=False)
(app / "Case.csproj").write_text('<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><TargetFramework>net10.0</TargetFramework></PropertyGroup></Project>\n')
(app / "rules.md").write_text(
    "Translate PHP functions into a public static C# class with the same name as the target filename (without the .cs extension). "
    "Keep the public static method named Label returning the exact string literal from the PHP. No namespace. "
    "Only edit the specified target. Do not run business code or the PHP.\n"
)
(app / "check.py").write_text(
    "import pathlib,sys\nn=int(sys.argv[1])\ns=pathlib.Path(f'Unit{n}.cs').read_text()\n"
    "assert f'Unit{n}' in s and 'Label' in s and f'unit-{n}' in s, s\n"
)
config = f'''[project]
root = {json.dumps(str(app))}
worktree_root = {json.dumps(str(root / "trees"))}
run_root = {json.dumps(str(root / "runs"))}

[[project.migration_rules]]
path = "rules.md"
usage = "required"

[build]
project = "Case.csproj"

[checker]
max_repair_attempts = 1
'''
for n, label in enumerate(("注文一覧", "顧客詳細", "売上集計", "在庫照会", "請求一覧", "入金消込")):
    (app / f"source{n}.php").write_text(f'<?php\nfunction Label() {{ return "unit-{n}"; }}\n')
    config += f'''\n[[units]]
id = "{label}"
source = "source{n}.php"
target = "Unit{n}.cs"
checker_command = {json.dumps([sys.executable, "check.py", str(n)])}
'''
for args in (("init", "-q"), ("config", "user.name", "Parallel evaluation"),
             ("config", "user.email", "evaluation@example.invalid"), ("add", "."),
             ("-c", "commit.gpgsign=false", "commit", "-qm", "Parallel fixture")):
    subprocess.run(["git", "-C", str(app), *args], check=True)
path = root / "harness.toml"
path.write_text(config, encoding="utf-8")
print(path)
