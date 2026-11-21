"""Build an isolated 20,000-line PHP / 200-document real-Junie evaluation."""
from pathlib import Path
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile


root = Path(tempfile.mkdtemp(prefix="harness-scale-")).resolve()
app = root / "app"
for directory in ("Pages/Catalog", "Pages/inc", "legacy", "rules"):
    (app / directory).mkdir(parents=True)


def write(name, text):
    (app / name).write_text(text, encoding="utf-8", newline="")


write(".gitignore", "bin/\nobj/\n")
write("Case.csproj", '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><OutputType>Exe</OutputType>'
      '<TargetFramework>net10.0</TargetFramework><ImplicitUsings>enable</ImplicitUsings>'
      '<Nullable>enable</Nullable></PropertyGroup></Project>\n')
write("NuGet.Config", '<configuration><packageSources><clear/></packageSources></configuration>\n')
write("Program.cs", 'Console.WriteLine(System.Text.Json.JsonSerializer.Serialize('
      'Migration.CatalogPage.Values(args.Length == 0 ? "all" : args[0])));\n')
write("Pages/Catalog/CatalogPage.cs", 'namespace Migration;\npublic static class CatalogPage {\n'
      '    public static string[] Values(string mode) => [];\n}\n')
write("Pages/Catalog/CatalogLabels.cs", 'namespace Migration; public static class CatalogLabels '
      '{ public const string Prefix = "CAT:"; }\n')
write("Pages/inc/SharedSuffix.cs", 'namespace Migration; public static class SharedSuffix '
      '{ public const string Value = ":END"; }\n')

prefix = ["<?php", "function catalog_values(string $mode): array {", "    $values = [];"]
suffix = [
    "    // The last rule only applies to the empty mode.",
    "    if ($mode === 'empty') {", "        return [];", "    }",
    "    if ($mode === 'reverse') {", "        $values = array_reverse($values);", "    }",
    "    if ($mode === 'selected') {",
    "        $values = array_values(array_filter($values, static function ($value) {",
    "            return strpos($value, '特例') !== false;", "        }));", "    }",
    "    return array_map(static function ($value) {", "        return 'CAT:' . $value . ':END';",
    "    }, $values);", "}", "$mode = $argv[1] ?? 'all';",
    "echo json_encode(catalog_values($mode), JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);",
]
values = []
for i in range(20_000 - len(prefix) - len(suffix)):
    # Unique, non-arithmetic payloads catch sampling; duplicates and rare values cross capsules.
    digest = hashlib.sha256(f"fixture-catalog-{i}".encode()).hexdigest()[:20]
    value = f"item-{i:05d}-{digest}"
    if i % 137 == 0:
        value = "重複-特例-\"引用\"-C:\\資料\\末尾"
    elif i % 251 == 0:
        value += "-日本語-😀-特例"
    elif i % 347 == 0:
        value += "-apostrophe'-\\n-is-literal"
    values.append(value)
rows = ["    $values[] = '" + value.replace("\\", "\\\\").replace("'", "\\'") + "';" for value in values]
source = "\n".join(prefix + rows + suffix) + "\n"
assert len(source.splitlines()) == 20_000
write("legacy/catalog.php", source)

rules = [
    ("required-api.md", "required", "", "Implement Migration.CatalogPage.Values(string mode) returning string[]. "
     "Preserve every PHP result, including order, duplicates, escapes and all modes. "
     "Use the existing CatalogLabels.Prefix and SharedSuffix.Value, not copies of their literals. "
     "Add public const string PolicyVersion = \"2026-11\" to CatalogPage. "
     "Edit only the assigned target; the runner builds and tests it after all capsules.\n"),
    ("required-integrity.md", "required", "", "Convert the complete original sequence exactly. "
     "Do not infer missing values from examples, formulas or counts. "
     "A compact representation is allowed if decoding preserves all values and their order. "
     "Do not run PHP at runtime or read the legacy files at runtime.\n"),
]
applicable = {
    17: ("reverse", "When a mode reverses an ordered list while preserving duplicates.", "ReversePolicy", "reverse-all",
         "Reverse the complete list before applying its output formatting; preserve duplicates."),
    64: ("substring", "When filtering strings by the Unicode substring 特例.", "SelectionPolicy", "ordinal-substring",
         "Use ordinal substring matching. A value containing 特例 anywhere is selected, retaining original order."),
    113: ("wrapping", "When formatting returned identifiers with a prefix and suffix.", "WrappingPolicy", "shared-labels",
          "Use CatalogLabels.Prefix and SharedSuffix.Value from adjacent reference files."),
    165: ("literals", "When literal data contains quotes, backslashes, Japanese text or emoji.", "LiteralPolicy", "lossless-literals",
          "Preserve all literals exactly. Literal backslash-n must not become a newline; retain apostrophes and emoji."),
    198: ("empty", "When an empty mode returns an empty collection without formatted elements.", "EmptyPolicy", "empty-before-format",
          "The empty mode returns an empty string array. Do not wrap an empty value into a nonempty result."),
}
domains = ["SMTP delivery", "SQL transactions", "OAuth login", "PDF pagination", "image resizing", "ZIP upload",
           "payment refunds", "SFTP synchronization", "WebSocket reconnect", "session cookies", "CSV file import"]
for i in range(1, 199):
    if i in applicable:
        title, description, marker, value, body = applicable[i]
        text = f"# {title}\n{body}\nAdd public const string {marker} = {json.dumps(value)} to the migrated class.\n"
    else:
        domain = domains[i % len(domains)]
        description = f"Procedure {i:03d}: requirements only for {domain}; no rules for pure in-memory lists."
        text = f"# Procedure {i:03d}: {domain}\nOnly applies to {domain}.\n"
        text += "".join(f"Step {j:03d}: keep the {domain} operation's existing retry, validation, failure and cleanup behavior; "
                        "do not introduce this operation into unrelated migrations.\n" for j in range(1, 49))
    rules.append((f"detail-{i:03d}.md", "reference", description, text))
for name, _, _, text in rules:
    write("rules/" + name, text)

php = shutil.which("php")
assert php, "PHP is required to generate an independent runtime oracle"
modes = ("all", "reverse", "selected", "empty", "unknown")
expected = {}
for mode in modes:
    expected[mode] = json.loads(subprocess.check_output([php, "-d", "xdebug.mode=off", str(app / "legacy/catalog.php"), mode]))
assert len(expected["all"]) == len(values)
assert expected["reverse"] == list(reversed(expected["all"]))
assert expected["selected"] == [value for value in expected["all"] if "特例" in value]
assert expected["empty"] == [] and expected["unknown"] == expected["all"]
write("expected.json", json.dumps(expected, ensure_ascii=False))
write("test.py", '''import json, pathlib, subprocess
expected = json.loads(pathlib.Path("expected.json").read_text(encoding="utf-8"))
for mode, values in expected.items():
    actual = json.loads(subprocess.check_output(["dotnet", "bin/Debug/net10.0/Case.dll", mode]))
    assert actual == values, (mode, len(actual), len(values), "values or order differ from PHP")
print(f"PASS: {len(expected)} modes; every value and position matches actual PHP output")
''')
markers = {v[2]: v[3] for v in applicable.values()} | {"PolicyVersion": "2026-11"}
write("check.py", '''import pathlib, re, sys
source = pathlib.Path("Pages/Catalog/CatalogPage.cs").read_text(encoding="utf-8")
errors = []
for name, value in ''' + repr(markers) + '''.items():
    if not re.search(r"public\\s+const\\s+string\\s+" + name + r'\\s*=\\s*"' + re.escape(value) + r'"\\s*;', source):
        errors.append(f"RULE Pages/Catalog/CatalogPage.cs:1 warning: missing {name} = {value}; consult original migration rules")
for helper in ("CatalogLabels.Prefix", "SharedSuffix.Value"):
    if helper not in source:
        errors.append(f"REF Pages/Catalog/CatalogPage.cs:1 warning: use {helper}")
if errors:
    print("\\n".join(errors))
sys.exit(bool(errors))
''')
write("break.py", '''from pathlib import Path
import re
path = Path("Pages/Catalog/CatalogPage.cs")
source = path.read_text(encoding="utf-8")
broken, count = re.subn(r'(PolicyVersion\\s*=\\s*")2026-11("\\s*;)', r'\\g<1>broken\\2', source)
assert count == 1, "expected PolicyVersion before intentional checker failure"
path.write_text(broken, encoding="utf-8", newline="")
''')

subprocess.run(["git", "init", "-q", str(app)], check=True)
for key, value in (("user.name", "Harness validation"), ("user.email", "harness@example.invalid"),
                   ("commit.gpgsign", "false"), ("core.autocrlf", "false")):
    subprocess.run(["git", "-C", str(app), "config", key, value], check=True)
subprocess.run(["git", "-C", str(app), "add", "."], check=True)
subprocess.run(["git", "-C", str(app), "commit", "-qm", "20k PHP lines and 200 rule documents"], check=True)
config = '[project]\nroot="app"\nrun_root="runs"\nworktree_root="trees"\nmax_context_chars=500000\n'
for name, usage, description, _ in rules:
    config += '\n[[project.migration_rules]]\npath=' + json.dumps("rules/" + name) + '\nusage=' + json.dumps(usage) + '\n'
    if description:
        config += 'description=' + json.dumps(description, ensure_ascii=False) + '\n'
config += '\n[build]\nproject="Case.csproj"\n[checker]\ncommand=' + json.dumps([sys.executable, "check.py"]) + '\nmax_repair_attempts=2\n'
config += '\n[scripts]\nafter_build=[' + json.dumps([sys.executable, "test.py"]) + ']\nafter_migration=[' + json.dumps([sys.executable, "break.py"]) + ']\n'
config += '\n[[units]]\nid="large-catalog"\nsource="legacy/catalog.php"\nsource_encoding="utf-8"\ntarget="Pages/Catalog/CatalogPage.cs"\n'
(root / "harness.toml").write_text(config, encoding="utf-8")
(root / "fixture.json").write_text(json.dumps({
    "php_lines": 20_000, "values": len(values), "php_bytes": len(source.encode()),
    "documents": len(rules), "document_bytes": sum(len(text.encode()) for _, _, _, text in rules),
    "expected_selected_details": [f"rule-{i + 2:04d}.txt" for i in applicable],
    "php_sha256": hashlib.sha256(source.encode()).hexdigest(), "modes": modes,
}, indent=2), encoding="utf-8")
print(root)
