"""保存済みの大量ルールを変えず、2万行のPHPと初回成果物の検証を用意する。"""

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import tomlkit


source_config = Path(sys.argv[1]).resolve()
config = tomlkit.parse(source_config.read_text(encoding="utf-8"))
source_app = (source_config.parent / config["project"]["root"]).resolve()
root = Path(tempfile.mkdtemp(prefix="harness-large-rules-")).resolve()
app, harness = root / "app", root / "harness"
harness.mkdir()
shutil.copytree(source_app, app, ignore=shutil.ignore_patterns(".git", "bin", "obj"))

# 設定外にあるスキル生成物もコピーし、検証中に元資料を変更しない。
rule_hashes = []
for number, rule in enumerate(config["project"]["migration_rules"], 1):
    source = source_app / rule["path"]
    destination = app / "benchmark-rules" / f"{number:02d}.md"
    destination.parent.mkdir(exist_ok=True)
    shutil.copyfile(source, destination)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    assert hashlib.sha256(destination.read_bytes()).hexdigest() == digest
    rule_hashes.append(digest)
    rule["path"] = destination.relative_to(app).as_posix()
assert len(rule_hashes) == 14
config["project"].update(root="../app", run_root="../runs", worktree_root="../trees", max_context_chars=500_000)
config["checker"]["command"] = [sys.executable, "check.py"]
config["scripts"]["after_migration"] = [[sys.executable, "break.py"]]

values = []
for number in range(19_997):
    value = (f"order-{number:02d}-{number * number + 13}" if number < 24 else
             "order-" + hashlib.sha256(f"large-rules:{number}".encode()).hexdigest()[:16])
    if number and number % 997 == 0:
        value = values[number - 1]
    if number in (24, 9998, 10000, 15001, 19996):
        value = f'edge-{number}-"<&>/\\日本'
    values.append(value)
php = "<?php\n$values = [];\n" + "".join(
    "$values[] = " + json.dumps(value, ensure_ascii=False) + ";\n" for value in values
) + "echo json_encode($values);\n"
assert len(php.splitlines()) == 20_000
assert [json.loads(line.removeprefix("$values[] = ").removesuffix(";"))
        for line in php.splitlines()[2:-1]] == values
(app / "legacy/order").write_text(php, encoding="utf-8")
(app / "expected.json").write_text(json.dumps(["ORDER:" + v + ":END" for v in values], ensure_ascii=False), encoding="utf-8")
checker = (app / "check.py").read_text(encoding="utf-8").replace("24 ordered values", "19997 ordered values")
(app / "check.py").write_text(checker, encoding="utf-8")

# 修正前のビルドと実出力を保存する。初回失敗時はその指摘を自動修正へ渡す。
(app / "break.py").write_text('''import json
import subprocess
import sys
from pathlib import Path

target = Path("Pages/Orders/OrderPage.cs")
text = target.read_text(encoding="utf-8")
Path(".before-repair.cs.txt").write_text(text, encoding="utf-8")
build = subprocess.run(["dotnet", "build", "Case.csproj"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
Path(".initial-build.log").write_bytes(build.stdout)
check = None
if build.returncode == 0:
    check = subprocess.run([sys.executable, "check.py"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    Path(".initial-check.log").write_bytes(check.stdout)
passed = build.returncode == 0 and check is not None and check.returncode == 0
Path(".initial-verification.json").write_text(json.dumps({
    "build_exit_code": build.returncode, "checker_exit_code": check.returncode if check else None,
    "injected_faults": passed,
}), encoding="utf-8")
if passed:
    target.write_text(text.replace("R4-END", "R4-BROKEN")
        .replace("order-contract-v7", "order-contract-broken")
        .replace("correlation-verbatim-k29", "correlation-broken"), encoding="utf-8")
''', encoding="utf-8")

(harness / "harness.local.toml").write_text(tomlkit.dumps(config), encoding="utf-8")
(root / "fixture.json").write_text(json.dumps({
    "lines": 20_000, "values": len(values), "rule_source_sha256": rule_hashes,
    "php_sha256": hashlib.sha256(php.encode()).hexdigest(),
    "expected_sha256": hashlib.sha256((app / "expected.json").read_bytes()).hexdigest(),
}, indent=2) + "\n", encoding="utf-8")
subprocess.run(["git", "init", "-q", str(app)], check=True)
subprocess.run(["git", "-C", str(app), "add", "."], check=True)
subprocess.run(["git", "-C", str(app), "-c", "user.name=Harness validation",
                "-c", "user.email=harness@example.invalid", "-c", "commit.gpgsign=false",
                "commit", "-qm", "Fixed 20000-line rule validation case"], check=True)
print(root)
