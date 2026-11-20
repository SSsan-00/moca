"""Join dashboard measurements with real Junie session events (no cache double count).

Usage: python collect.py baseline/result.json improved/result.json > results.json
"""
import hashlib
import json
from pathlib import Path
import sys


def collect(paths):
    trials = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    sessions = {trial["run_id"]: [] for trial in trials}
    for path in (Path.home() / ".junie/sessions").glob("*/events.jsonl"):
        text = path.read_text(encoding="utf-8")
        matches = [run_id for run_id in sessions if run_id in text]
        if not matches:
            continue
        # JSON strings may legally contain Unicode line separators. Only LF delimits events.
        rows = [json.loads(line) for line in text.split("\n") if line]
        starts = [row["timestampMs"] for row in rows if row["kind"] == "TaskStartedEvent"]
        if not starts:
            continue
        assert len(starts) == 1, path
        events = [row.get("event", {}).get("agentEvent", {}) for row in rows]
        models = {usage["model"] for event in events for usage in event.get("modelUsage", [])}
        usage = dict.fromkeys(("input", "output", "cache_read", "cache_write", "responses"), 0)
        for event in events:
            if event.get("kind") != "LlmResponseMetadataEvent":
                continue
            for item in event.get("modelUsage", []):
                if "inputTokens" not in item or "outputTokens" not in item:
                    continue
                usage["input"] += item["inputTokens"] + item.get("cacheInputTokens", 0) + item.get("cacheCreateTokens", 0)
                usage["output"] += item["outputTokens"]
                usage["cache_read"] += item.get("cacheInputTokens", 0)
                usage["cache_write"] += item.get("cacheCreateTokens", 0)
                usage["responses"] += 1
        value = dict(session_id=path.parent.name, start_ms=starts[0],
            junie_seconds=(max(row["timestampMs"] for row in rows) - starts[0]) / 1000,
            tool_steps=len({event.get("stepId") for event in events if event.get("kind") == "ToolBlockUpdatedEvent"}),
            models=sorted(models), usage=usage)
        for run_id in matches:
            sessions[run_id].append(value)
    output = []
    for path, trial in zip(paths, trials):
        root = Path(trial["run_path"])
        manifest = json.loads((root / "capsule/manifest.json").read_text(encoding="utf-8"))
        chronological = sorted(sessions[trial["run_id"]], key=lambda item: item["start_ms"])
        phases = []
        # Repeated build repairs are aggregated into one usage entry by the harness.
        for usage in trial["token_usage"]:
            grouped, summed = [], dict.fromkeys(("input", "output", "cache_read", "cache_write", "responses"), 0)
            while summed["responses"] < usage["responses"]:
                session = chronological.pop(0)
                grouped.append(session)
                for key in summed:
                    summed[key] += session["usage"][key]
            assert all(summed[key] == usage[key] for key in summed), (trial["run_id"], usage, summed)
            phases.append(usage | dict(sessions=grouped, junie_seconds=sum(s["junie_seconds"] for s in grouped),
                                      tool_steps=sum(s["tool_steps"] for s in grouped)))
        assert not chronological, trial["run_id"]
        record = json.loads((root / "run.json").read_text(encoding="utf-8"))
        worktree = Path(record["worktree_path"])
        result = {key: trial[key] for key in ("run_id", "status", "wall_seconds", "build_exit_code", "checker_exit_code",
                                             "completed_capsules", "context_capsules", "page_errors", "check") if key in trial}
        result.update(label=path.parent.name, phases=phases,
            totals={key: sum(phase[key] for phase in phases) for key in ("input", "output", "cache_read", "cache_write", "responses", "junie_seconds", "tool_steps")},
            stage_seconds={k: v["elapsed_seconds"] for k, v in trial["stage_timings"].items()},
            selected_details=[key for key, value in trial["detail_applicability"].items() if value["applies"]],
            repair_kinds=[row.get("kind", "checker") for row in trial["repair_history"] or []],
            capsule_hashes=[part["sha256"] for part in manifest["parts"]],
            target_sha256={name: hashlib.sha256((worktree / name).read_bytes()).hexdigest()
                           for name in ("Pages/Business/BusinessService.cs", "Pages/Business/PolicyCatalog.cs")})
        output.append(result)
    assert all(trial["capsule_hashes"] == output[0]["capsule_hashes"] for trial in output)
    assert all(trial["fixture"] == trials[0]["fixture"] for trial in trials)
    return dict(fixture=trials[0]["fixture"], trials=output)


if __name__ == "__main__":
    print(json.dumps(collect([Path(value) for value in sys.argv[1:]]), ensure_ascii=False, indent=2))
