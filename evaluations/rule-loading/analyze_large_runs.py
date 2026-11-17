"""実Junieの保存済みRunから、初回の正しさ・資料の選択・使用量を照合する。"""

import argparse
import json
import re
from pathlib import Path


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("root", type=Path)
parser.add_argument("labels", nargs="+")
parser.add_argument("--expect-initial-failure", action="append", default=[],
                    help="初回失敗した比較・中間試行も、除外せず結果へ記録する")
args = parser.parse_args()
root = args.root.resolve()
labels = args.labels
assert set(args.expect_initial_failure) <= set(labels)
results = []
rule_hashes = None
capsule_hashes = None
for label in labels:
    timing = json.loads((root / f"{label}.json").read_text(encoding="utf-8"))
    run_path = Path(timing["path"])
    saved = root / f"{label}-run.json"
    run = json.loads((saved if saved.exists() else run_path / "run.json").read_text(encoding="utf-8"))
    manifest = json.loads((run_path / "capsule/manifest.json").read_text(encoding="utf-8"))
    entries = manifest["rule_files"]
    current_hashes = [entry["sha256"] for entry in entries]
    current_capsules = [part["sha256"] for part in manifest["parts"]]
    if rule_hashes is None:
        rule_hashes, capsule_hashes = current_hashes, current_capsules
    assert current_hashes == rule_hashes and current_capsules == capsule_hashes
    expected_failure = label in args.expect_initial_failure
    assert len(manifest["parts"]) == 2
    assert run["completed_capsules"] <= 2 if expected_failure else run["completed_capsules"] == 2
    assert run["status"] in ({"build_failed", "junie_failed", "checker_succeeded"} if expected_failure else {"checker_succeeded"})
    decisions = run.get("detail_applicability")
    selected = None
    allowed = set()
    workspace = run_path / "agent-workspace"
    if decisions is None:
        assert expected_failure and run["completed_capsules"] == 0
        assert {row["phase"] for row in run["token_usage"]} == {"rule_review"}
    else:
        assert len(decisions) == 12
        selected = [name for name, value in decisions.items() if value["applies"]]
        assert selected == ["rule-0002.txt"], decisions
        allowed = set(selected) | {entry["file"] for entry in entries if entry["usage"] == "required"}
        staged = {p.name for p in workspace.rglob("rule-*.txt")}
        assert staged == allowed, staged
        guidelines = (workspace / ".migration-materials/guidelines.md").read_text(encoding="utf-8")
        for entry in entries:
            text = (run_path / "capsule/materials" / entry["file"]).read_text(encoding="utf-8")
            assert (text in guidelines) == (entry["file"] in allowed), entry["file"]
    worktree = Path(run["worktree_path"])
    initial_file = worktree / ".initial-verification.json"
    initial = json.loads(initial_file.read_text(encoding="utf-8")) if initial_file.exists() else None
    initial_passed = initial is not None and initial["build_exit_code"] == initial["checker_exit_code"] == 0
    assert initial_passed != expected_failure, (label, initial)
    if initial is not None:
        assert initial["injected_faults"] == initial_passed
    if initial_passed:
        assert initial == {"build_exit_code": 0, "checker_exit_code": 0, "injected_faults": True}, initial
    initial_helpers_used = None
    checked_files = [".before-repair.cs.txt"] if initial is not None else []
    if run["status"] == "checker_succeeded":
        checked_files.append("Pages/Orders/OrderPage.cs")
    for filename in checked_files:
        text = (worktree / filename).read_text(encoding="utf-8")
        for number in range(1, 5):
            for edge in ("Start", "End"):
                assert re.search(r'public\s+const\s+string\s+Rule' + str(number) + edge
                                 + r'\s*=\s*"R' + str(number) + "-" + edge.upper() + '"', text)
        for name, value in (("OrderDetailVersion", "order-contract-v7"),
                            ("MigrationCorrelationMode", "correlation-verbatim-k29")):
            assert re.search(r'public\s+const\s+string\s+' + name + r'\s*=\s*"' + value + '"', text)
        helpers_used = "LocalLabels.Prefix" in text and "SharedSuffix.Value" in text
        if initial_passed or filename != ".before-repair.cs.txt":
            assert helpers_used
        if filename == ".before-repair.cs.txt":
            initial_helpers_used = helpers_used
    # 合格時は無出力。保存済みの旧ベンチマークの成功ログも再解析できる。
    expected = {"", "PASS: 19997 ordered values, 10 rule markers, both adjacent helpers"}
    if initial_passed:
        assert (worktree / ".initial-check.log").read_text(encoding="utf-8").strip() in expected
        assert (run_path / "checker.log").read_text(encoding="utf-8").strip() in expected
        assert len(run["repair_history"]) == 1
        assert [line.split()[0] for line in (run_path / "checker-before-repair-1.log").read_text(encoding="utf-8").splitlines()] == ["R4", "ORD", "T90"]
    if run["status"] == "checker_succeeded":
        assert (run_path / "checker.log").read_text(encoding="utf-8").strip() in expected

    sessions = []
    for path in Path.home().glob(".junie/sessions/*/events.jsonl"):
        if path.stat().st_mtime < timing["start"]:
            continue
        raw = path.read_text(encoding="utf-8", errors="replace")
        if run["run_id"] not in raw:
            continue
        events = [json.loads(line) for line in raw.splitlines()]
        if not any(run["run_id"] in event.get("event", {}).get("agentEvent", {}).get("currentDirectory", "")
                   for event in events):
            continue
        reviewing = any("/rule-review-" in event.get("event", {}).get("agentEvent", {}).get("currentDirectory", "")
                        for event in events)
        starts = [event for event in events if event["kind"] == "TaskStartedEvent"
                  and timing["start"] * 1000 <= event["timestampMs"] <= timing["end"] * 1000]
        if not starts:
            continue
        seconds = 0
        reads = set()
        helper_commands = set()
        for start in starts:
            task = [event for event in events if event.get("taskId") == start["taskId"]
                    and event["timestampMs"] <= timing["end"] * 1000]
            seconds += (task[-1]["timestampMs"] - start["timestampMs"]) / 1000
            for event in task:
                agent = event.get("event", {}).get("agentEvent", {})
                if agent.get("kind") == "ViewFilesBlockUpdatedEvent" and agent.get("status") == "COMPLETED":
                    reads.update(Path(file["relativePath"]).name for file in agent.get("files", []))
                if agent.get("kind") == "TerminalBlockUpdatedEvent":
                    # 事前確認のJSONには除外したIDも書く。IDの記録と本文の参照を混同しない。
                    if not reviewing:
                        assert set(re.findall(r"rule-\d+\.txt", agent.get("command", ""))) <= allowed
                    if agent.get("status") == "COMPLETED" and "php_literals" in agent.get("command", ""):
                        helper_commands.add(agent["stepId"])
        assert {name for name in reads if re.fullmatch(r"rule-\d+\.txt", name)} <= allowed
        sessions.append({"session_id": path.parent.name, "start": starts[0]["timestampMs"],
                         "junie_seconds": round(seconds, 3), "read_files": sorted(reads),
                         "source_helper_commands": len(helper_commands)})
    sessions.sort(key=lambda item: item["start"])
    usage = run["token_usage"]
    assert len(sessions) == len(usage), (label, len(sessions), len(usage))
    phases = [{**row, **session, "normal_input": row["input"] - row["cache_read"] - row["cache_write"]}
              for row, session in zip(usage, sessions, strict=True)]
    totals = {key: sum(row[key] for row in phases)
              for key in ("input", "output", "cache_read", "cache_write", "normal_input", "responses", "junie_seconds")}
    totals["junie_seconds"] = round(totals["junie_seconds"], 3)
    results.append({"label": label, "run_id": run["run_id"], "status": run["status"], "initial_verification": initial,
                    "completed_capsules": run["completed_capsules"],
                    "initial_helpers_used": initial_helpers_used,
                    "selected_details": selected, "excluded_detail_count": 11 if selected is not None else None,
                    "completed_repairs": len(run.get("repair_history", [])),
                    "wall_seconds_including_trust_waits": timing["wall_seconds"], "totals": totals, "phases": phases})
output = {"fixture": json.loads((root / "fixture.json").read_text(encoding="utf-8")),
          "versions": json.loads((root / "versions.json").read_text(encoding="utf-8")),
          "rule_snapshot_sha256": rule_hashes, "capsule_sha256": capsule_hashes, "runs": results}
(root / "large-comparison.json").write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
for result in results:
    print(result["label"], result["run_id"], result["totals"])
    print("review", {key: result["phases"][0][key] for key in ("input", "output", "cache_read", "junie_seconds")})
print("PASS: trial results verified, including explicitly expected initial failures")
