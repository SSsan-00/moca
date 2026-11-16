"""保存済み2万行ケースから、同じ原文を使うスキル比較用チェックアウトを作る。"""

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import tomlkit


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("baseline", type=Path, help="rule-loadingの2万行ケースの保存先")
parser.add_argument("--tools-from", type=Path, default=Path(__file__).resolve().parents[2],
                    help="比較するスキルとtoolsを持つハーネスのチェックアウト")
args = parser.parse_args()
baseline, repository = args.baseline.resolve(), args.tools_from.resolve()
root = Path(tempfile.mkdtemp(prefix="harness-skill-costs-")).resolve()
app, harness = root / "app", root / "harness"
shutil.copytree(baseline / "app", app, ignore=shutil.ignore_patterns(".git", "bin", "obj"))
harness.mkdir()
shutil.copytree(repository / ".junie/skills/harness-optimize-rules",
                harness / ".junie/skills/harness-optimize-rules")
(harness / "tools").mkdir()
for name in ("rule_pack.py", "rule_review.py", "rule_usage.py"):
    shutil.copyfile(repository / "tools" / name, harness / "tools" / name)

config = tomlkit.parse((baseline / "harness/harness.local.toml").read_text(encoding="utf-8"))
items = list(config["project"]["migration_rules"])
assert len(items) == 14
assert [item["path"] for item in items[2:4]] == ["benchmark-rules/03.md", "benchmark-rules/04.md"]
mixed = (app / "rules/database-queries.md").read_bytes()
lines = mixed.decode("utf-8").splitlines(keepends=True)
assert len(lines) == 53
# 前回は末尾の義務を取り出した生成物を使用。今回は同じ原文からスキルで整理し直す。
assert "".join(lines[51:]) in (app / items[2]["path"]).read_text(encoding="utf-8")
assert "".join(lines[:51]) in (app / items[3]["path"]).read_text(encoding="utf-8")
(app / "benchmark-rules/database-queries.md").write_bytes(mixed)
config["project"]["migration_rules"] = tomlkit.aot()
for number, item in enumerate(items):
    if number == 2:
        continue
    if number == 3:
        item["path"] = "benchmark-rules/database-queries.md"
    config["project"]["migration_rules"].append(item)
config["project"].update(root="../app", run_root="../runs", worktree_root="../trees")
(harness / "harness.local.toml").write_text(tomlkit.dumps(config), encoding="utf-8")
shutil.copyfile(harness / "harness.local.toml", root / "config.input.toml")
shutil.copyfile(baseline / "fixture.json", root / "fixture.json")
(root / "versions.json").write_text(json.dumps({
    "baseline": str(baseline), "tools_from": str(repository),
    "mixed_rule_sha256": hashlib.sha256(mixed).hexdigest(),
}, indent=2) + "\n", encoding="utf-8")
subprocess.run(["git", "init", "-q", str(app)], check=True)
subprocess.run(["git", "-C", str(app), "add", "."], check=True)
subprocess.run(["git", "-C", str(app), "-c", "user.name=Harness validation",
                "-c", "user.email=harness@example.invalid", "-c", "commit.gpgsign=false",
                "commit", "-qm", "Same large PHP and complete raw migration rules"], check=True)
print(root)
