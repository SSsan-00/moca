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


def _trial_evidence(root, transcript):
    if not transcript:
        return False, False, ""
    project = None
    starts = finishes = 0
    active = completed = False
    reads = set()
    submitted = ""
    with transcript.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            logger, _, message = line.rstrip().partition(" - ")
            if logger.endswith("JunieCli") and message.startswith("Using the specified project directory: "):
                project = Path(message.removeprefix("Using the specified project directory: "))
            if not logger.endswith("AnalyticsClientImpl") or not message.startswith("[ANALYTICS:EVENT] type="):
                continue
            event = message.removeprefix("[ANALYTICS:EVENT] type=")
            if event.startswith("task_request_started properties="):
                starts += 1
                active = event.startswith("task_request_started properties=prompt=/harness-optimize-rules ")
            elif event.startswith("task_request_finished properties="):
                finishes += 1
                completed = active and event.split(",", 1)[0].endswith("properties=status=Success")
                active = False
            elif (active and event.startswith("agent_step_finished properties=step_type=")
                  and event.endswith("step_result=SUBMIT")):
                step = event.split("step_type=", 1)[1].split(" ", 1)[0]
                if step not in {"agent_skill_read_doc", "submit"}:
                    continue
                _, _, data = event.partition("rawJsonObject=")
                try:
                    args, _ = json.JSONDecoder().raw_decode(data)
                except ValueError:
                    continue
                if not isinstance(args, dict):
                    continue
                if step == "submit":
                    submitted = args.get("solution_summary", "")
                elif args.get("name") == "harness-optimize-rules":
                    path = args.get("path", "SKILL.md")
                    if isinstance(path, str):
                        reads.add(path)
    same_trial = project == root.resolve() and starts == 1
    return (same_trial and {"SKILL.md", "references/loading-policy.md"} <= reads,
            same_trial and finishes == 1 and completed, submitted if isinstance(submitted, str) else "")


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
    skill_read, completed, submitted = _trial_evidence(root, transcript)
    outcome = result if skill_read and completed else "unverified_trial"
    if outcome == "justified_hold":
        quote = (signals or {}).get("reviewed_hold_reason")
        if not (isinstance(quote, str) and quote in submitted and
                all(ref in quote for ref in ("export.md:92", "export-rehearsal.md:2"))):
            outcome = "hold_reason_needs_review"
    return {
        "scenario": scenario, "expected": expected, "observed": observed, "artifact_outcome": result,
        "outcome": outcome,
        "failures": failures, "skill_read_verified": skill_read, "trial_completed": completed,
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
