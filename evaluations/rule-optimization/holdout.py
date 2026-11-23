import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from migration_harness.config import load_config
from migration_harness.rules.rule_materials import render_guidelines, write_rule_files


REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools"))
from rule_review import without_rules

REQUIRED = "Every migration and repair must retain the delivery audit ID."
DETAIL = "Only for CSV exports, preserve the original quoting for fields containing tabs."
EXCEPTION = "For the opt-out CSV sample, skip quoting the empty suffix field."
DECOY = "CSV name rehearsal: the test report's tabs are unrelated to exported fields."
PRIORITY = "The CSV opt-out exception overrides the ordinary CSV quoting requirement."
CONFLICT = "Every export must replace the delivery audit ID with a new value."
CONFIG = "# Keep this synthetic trial comment.\n" + '''[project]
root = "app"
worktree_root = "worktrees"
run_root = "runs"
[[project.migration_rules]]
path = "export.md"
[[project.migration_rules]]
path = "export-rehearsal.md"
[[units]]
id = "test"
source = "fixture.php"
target = "Example.cs"
'''


def originals(scenario):
    primary = "\n".join([DETAIL, EXCEPTION, PRIORITY] + ["Illustrative read-only export sample."] * 88 + [REQUIRED])
    secondary = DECOY + ("\n" + CONFLICT if scenario == "conflict" else "")
    return {"export.md": primary + "\n", "export-rehearsal.md": secondary + "\n"}


def prepare(root, scenario):
    root.mkdir()
    app = root / "app"
    app.mkdir()
    subprocess.run(["git", "init", "-q", str(app)], check=True)
    for name, body in originals(scenario).items():
        (root / "app" / name).write_bytes(body.encode())
    (app / "fixture.php").write_bytes(b"<?php echo 'synthetic sample';\n")
    (root / "harness.local.toml").write_bytes(CONFIG.encode())
    shutil.copytree(REPO / ".junie/skills/harness-optimize-rules", root / ".junie/skills/harness-optimize-rules")
    tools = root / "tools"
    tools.mkdir()
    for name in ("rule_review.py", "rule_usage.py", "rule_pack.py", "rule_meaning.py"):
        shutil.copyfile(REPO / "tools" / name, tools / name)


def _runtime(settings):
    project = Path(settings["project_root"])
    items = [
        (item, (project / item["path"]).read_bytes().decode("utf-8-sig"))
        for item in settings["units"][0]["inputs"] if item["role"] == "migration_rules"
    ]
    with tempfile.TemporaryDirectory() as temp:
        directory = Path(temp)
        entries = write_rule_files(directory, items)
        manifest = {"rule_files": entries}
        selected = frozenset(row["file"] for row, (_, body) in zip(entries, items, strict=True) if DETAIL in body)
        return (render_guidelines(directory, manifest, selected=frozenset()),
                render_guidelines(directory, manifest, selected=selected))


def score(root, scenario, transcript=None, signals=None):
    expected = "hold" if scenario == "conflict" else "apply"
    config = root / "harness.local.toml"
    current = config.read_bytes()
    unchanged = current == CONFIG.encode()
    observed = "hold" if unchanged else "apply"
    failures = []
    if any((root / "app" / name).read_bytes() != body.encode() for name, body in originals(scenario).items()):
        failures.append("original source bytes changed")
    try:
        if without_rules(current) != without_rules(CONFIG.encode()) or b"# Keep this synthetic trial comment." not in current:
            failures.append("unrelated settings or the trial comment changed")
        if observed == "apply" and expected == "apply":
            mandatory, selected = _runtime(load_config(config))
            if REQUIRED not in mandatory:
                failures.append("late project-wide obligation is not supplied by default")
            if any(line in mandatory for line in (DETAIL, EXCEPTION, PRIORITY, DECOY)):
                failures.append("unrelated or CSV-only instructions became mandatory")
            if any(line not in selected for line in (DETAIL, EXCEPTION, PRIORITY)) or DECOY in selected:
                failures.append("CSV selection split its exception/priority or included an unrelated document")
    except (OSError, ValueError, TypeError, KeyError) as error:
        failures.append(f"could not evaluate runtime configuration: {error}")
    result = {
        ("apply", "apply"): "correct_apply" if not failures else "incorrect_apply",
        ("hold", "apply"): "incorrect_apply",
        ("apply", "hold"): "unnecessary_hold",
        ("hold", "hold"): "justified_hold" if not failures else "invalid_hold",
    }[expected, observed]
    trace = transcript.read_text(encoding="utf-8", errors="replace") if transcript else ""
    required_reads = ("SKILL.md", "loading-policy.md")
    skill_read = all(any("agent_skill_read_doc" in line and "harness-optimize-rules" in line and name in line
                         for line in trace.splitlines())
                     for name in required_reads)
    return {
        "scenario": scenario, "expected": expected, "observed": observed, "outcome": result,
        "failures": failures, "skill_read_verified": skill_read,
        "elapsed_seconds": (signals or {}).get("elapsed_seconds"),
        "input_tokens": (signals or {}).get("input_tokens"),
        "cached_input_tokens": (signals or {}).get("cached_input_tokens"),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("prepare", "score"))
    parser.add_argument("root", type=Path)
    parser.add_argument("--scenario", required=True, choices=("conditional", "conflict"))
    parser.add_argument("--transcript", type=Path)
    parser.add_argument("--signals", type=Path)
    args = parser.parse_args()
    if args.action == "prepare":
        prepare(args.root, args.scenario)
    else:
        signals = json.loads(args.signals.read_bytes()) if args.signals else None
        print(json.dumps(score(args.root, args.scenario, args.transcript, signals), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
