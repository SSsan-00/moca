"""Fixture-specific static checks. Never execute PHP, business code, or a database."""
from collections import Counter
import json
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "verification"


def normalize_sql(sql):
    """SQLの字句を照合するため、比較用の形にそろえる。"""
    parameters = {}
    parts = re.split(r"('(?:[^']|'')*')", sql)
    for i in range(0, len(parts), 2):
        parts[i] = re.sub(r"[:@$][A-Za-z_]\w*", lambda m: parameters.setdefault(m[0], f"?{len(parameters)}"), parts[i])
        parts[i] = re.sub(r"\s+", "", parts[i]).lower()
    return "".join(parts)


def compare(expected, source, inspection):
    """固定した期待値と静的検査結果の違いを列挙する。"""
    issues = list(inspection["problems"])
    actual = inspection["policies"]
    ids = [row.get("Id") for row in actual]
    if Counter(ids) != Counter(row["Id"] for row in expected):
        issues.append("PolicySpec entries must include every original contract exactly once.")
    by_id = {row.get("Id"): row for row in actual}
    for row in expected:
        got = by_id.get(row["Id"], {})
        for name, value in row.items():
            if type(got.get(name)) is not type(value) or got.get(name) != value:
                issues.append(f"Contract {row['Id']} {name}: PHP={value!r}, C#={got.get(name)!r}.")
    # shortcut: this fixture uses literal parameterized SQL; use a SQL parser for other source styles.
    sql = set(re.findall(r"'((?:SELECT|INSERT|UPDATE|DELETE)\b[^']*)'", source))
    assert sql and all("\\" not in statement for statement in sql), "unsupported fixture SQL"
    found = {normalize_sql(text) for text in inspection["strings"]}
    for statement in sorted(sql):
        if normalize_sql(statement) not in found:
            issues.append(f"Original SQL structure missing or changed: {statement}")
    codes = set(re.findall(r"reject\('([^']+)'\)", source))
    for code in sorted(codes - set(inspection["strings"])):
        issues.append(f"Original rejection code missing: {code}")
    for name, message in {
        "transactionBinding": "Commands must bind their active transaction.",
        "nullBinding": "Null SQL parameters must use DBNull.Value.",
        "parameterBinding": "SQL values must use parameter binding.",
    }.items():
        if not inspection[name]:
            issues.append(message)
    return issues, dict(policies=len(expected), policy_fields=sum(len(row) for row in expected),
                        sql_structures=len(sql), rejection_codes=len(codes))


def check():
    """評価用の移行先を静的に検査し、差異を終了コードに反映する。"""
    start = time.monotonic()
    # This executable is a Roslyn source parser, not the migrated application.
    result = subprocess.run(["dotnet", "run", "--project", str(DATA / "inspector/Inspect.csproj"),
        "--verbosity", "quiet", "--", str(ROOT / "Pages/Business/BusinessService.cs"),
        str(ROOT / "Pages/Business/PolicyCatalog.cs"), str(ROOT / "Pages/inc/Contracts.cs")],
        capture_output=True, text=True, encoding="utf-8", timeout=120)
    if result.returncode:
        raise RuntimeError("static parser failed: " + result.stdout[-2000:] + result.stderr[-2000:])
    inspection = json.loads(result.stdout.strip().split("\n")[-1])
    expected = json.loads((DATA / "expected-policies.json").read_text(encoding="utf-8"))
    source = (ROOT / "legacy/business.php").read_text(encoding="utf-8")
    issues, counts = compare(expected, source, inspection)
    evidence = dict(validation="static_only", application_executed=False, database_accessed=False,
                    **counts, issues=len(issues), diagnostics=issues,
                    elapsed_seconds=round(time.monotonic()-start, 3))
    (DATA / "last-check.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for issue in issues[:20]:
        print(f"BUSINESS Pages/Business/BusinessService.cs:1 error: {issue}")
    if issues:
        print(f"Static check failed: {len(issues)} findings. Compare the original source and both targets.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(check())
