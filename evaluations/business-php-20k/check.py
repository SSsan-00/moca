"""Compare independently recorded PHP results with the built .NET executable."""
import json
from pathlib import Path
import subprocess
import sys


def difference(expected, actual, label="result"):
    if type(expected) is not type(actual):
        return f"{label}: expected {type(expected).__name__}, got {type(actual).__name__}"
    if isinstance(expected, dict):
        if expected.keys() != actual.keys():
            return f"{label}: missing {sorted(expected.keys() - actual.keys())}, extra {sorted(actual.keys() - expected.keys())}"
        for key, value in expected.items():
            issue = difference(value, actual[key], f"{label}.{key}")
            if issue:
                return issue
    elif isinstance(expected, list):
        if len(expected) != len(actual):
            return f"{label}: expected {len(expected)} items, got {len(actual)}"
        for index, (left, right) in enumerate(zip(expected, actual, strict=True)):
            issue = difference(left, right, f"{label}[{index}]")
            if issue:
                return issue
    elif expected != actual:
        return f"{label}: expected {repr(expected)[:180]}, got {repr(actual)[:180]}"
    return None


def compare(expected, actual):
    if not isinstance(actual, list) or len(expected) != len(actual):
        return ["Number of scenarios does not match."]
    issues = []
    for left, right in zip(expected, actual, strict=True):
        issue = difference(left, right, f"scenario {left['id']}")
        if issue:
            issues.append(issue)
    return issues


def diagnostic(message):
    print(f"BEHAVIOR Pages/Business/BusinessService.cs:1 error: {message}")


def run(cases, oracle):
    expected = json.loads(oracle.read_bytes())
    result = subprocess.run(
        ["dotnet", "bin/Debug/net9.0/Case.dll", str(cases), "verification/schema.sql"],
        capture_output=True, timeout=60, check=False,
    )
    if result.returncode:
        failure = " ".join(result.stderr.decode("utf-8", "replace").split())
        diagnostic(f"C# fixture exited with {result.returncode}: {failure[:1200]}")
        return 1
    try:
        actual = json.loads(result.stdout)
    except ValueError:
        diagnostic("C# fixture did not produce valid JSON")
        return 1
    issues = compare(expected, actual)
    if issues:
        for issue in issues[:8]:
            diagnostic(issue)
        return 1
    operations = sum(len(case["steps"]) for case in expected)
    print(f"PASS: {operations} operation results and {len(expected)} SQLite state snapshots matched PHP.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run(*(Path(arg) for arg in sys.argv[1:])))
