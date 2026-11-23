from concurrent.futures import Future
from pathlib import Path
from tempfile import mkdtemp
import json
import subprocess
import sys
import time

from migration_harness import workflow
from migration_harness.config import load_config, target_paths
from migration_harness.execution import ExecutionControl, control_scope
from migration_harness.context.source_context import stage_target_context

variant, output = sys.argv[1], Path(sys.argv[2])
root = Path(mkdtemp(prefix="harness-live-111-"))
print("START", variant, root.name, flush=True)

def on_request(event):
    if event is None:
        return
    choices = event["options"]
    choice = next((o for o in choices if o["kind"] == "reject_once"), None)
    if any(o["id"] == "trust" for o in choices):
        path = Path(event["detail"].split("\n")[-1]).resolve()
        if path.is_relative_to(root):
            choice = next(o for o in choices if o["id"] == "trust")
    if choice:
        f = Future()
        f.set_result(choice["id"])
        return f

def case(flag, example_ready=True, helper_available=True):
    started = time.monotonic()
    case_dir = root / f"target-{str(flag).lower()}-example-{str(example_ready).lower()}-helper-{str(helper_available).lower()}"
    app = case_dir / "app"
    app.mkdir(parents=True)
    def put(name, body):
        path = app / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body.encode())
    helper = "function display_payment_amount($amount) { return number_format($amount, 2, '.', ''); }\n"
    put("Legacy.php", "<?php\n" + (helper if helper_available else "")
        + "function render_total($amount) { return display_payment_amount($amount); }\n")
    put("Page.cs", "public static class Page {}\n")
    put("refs/PaymentCapability.cs", "public static class PaymentCapability { public const bool ExportBadge = " + str(flag).lower() + "; }\n")
    put("Rules/Required.md", "# Mandatory selection policy\nWhen PHP calls display_payment_amount, read the registered PHP/C# ReceiptExample pair before deciding how it should expose the badge. For that helper the BadgeFormat detail is applicable only when BOTH conditions hold: the pair marks the helper as BADGE_READY and the target platform constant PaymentCapability.ExportBadge is true. Independently, always select AuditTag when that PHP helper is called.\n")
    put("Rules/BadgeFormat.md", "# BadgeFormat\nIf the mandatory selection policy's two conditions for the payment display badge are satisfied, display the badge after the amount. Otherwise do not apply this detail.\n")
    put("Rules/AuditTag.md", "# AuditTag\nOnly apply this document when the mandatory selection policy explicitly selects it. Add the approved audit tag; do not change payment totals.\n")
    put("Rules/Email.md", "# Email\nApply only to legacy PHP that sends email over SMTP.\n")
    put("Examples/ReceiptExample.php", "<?php\nfunction receipt_preview($amount) { return display_payment_amount($amount); }\n")
    marker = "BADGE_READY" if example_ready else "BADGE_DISABLED"
    put("Examples/ReceiptExample.cs", f"// ReceiptExample compatibility marker for display_payment_amount: {marker}\npublic static class ReceiptExample {{}}\n")
    for args in (("init", "-q"), ("add", "."), ("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "synthetic")):
        subprocess.run(["git", "-C", str(app), *args], check=True, capture_output=True)
    cfg = case_dir / "harness.toml"
    cfg.write_text('''[project]
root = "app"
worktree_root = "worktrees"
run_root = "runs"
[[project.migration_rules]]
path = "Rules/Required.md"
usage = "required"
[[project.migration_rules]]
path = "Rules/BadgeFormat.md"
usage = "reference"
description = "BadgeFormat for the payment amount display helper; check the mandatory policy criteria and both referenced facts."
[[project.migration_rules]]
path = "Rules/AuditTag.md"
usage = "reference"
description = "AuditTag when the mandatory selection policy selects it."
[[project.migration_rules]]
path = "Rules/Email.md"
usage = "reference"
description = "SMTP mail only"
[[project.references]]
role = "reference_php"
path = "Examples/ReceiptExample.php"
[[project.references]]
role = "reference_csharp"
path = "Examples/ReceiptExample.cs"
[target_context]
roots = []
folders = ["refs"]
[junie]
model = "gpt-6-luna"
effort = "medium"
max_steps = 25
[[units]]
id = "sample"
source = "Legacy.php"
target = "Page.cs"
''', encoding="utf-8")
    config = load_config(cfg)
    prepared = workflow.prepare_run(config, "sample")
    run, worktree, record, unit = workflow.load_saved_run(config, prepared["run_id"])
    capsule, manifest = workflow.load_capsule_manifest(run)
    pinned = run / "agent-workspace"
    pinned.mkdir()
    targets = target_paths(unit)
    stage_target_context(worktree, targets, capsule, pinned, policy=unit.get("target_context"))
    selection_at = time.monotonic()
    selected = workflow._review_detail_applicability(
        run, capsule, manifest, targets, record, 25, model="gpt-6-luna", effort="medium", config=config, pinned_context=pinned,
    )
    selection_seconds = time.monotonic() - selection_at
    saved = json.loads((run / "run.json").read_bytes())
    usage_before = saved.get("token_usage", [])
    reuse_at = time.monotonic()
    reuse = workflow._review_detail_applicability(
        run, capsule, manifest, targets, saved, 25, model="gpt-6-luna", effort="medium", config=config, pinned_context=pinned,
    )
    reuse_ms = (time.monotonic() - reuse_at) * 1000
    usage_after = json.loads((run / "run.json").read_bytes()).get("token_usage", [])
    index = {r["file"]: r["path"] for r in manifest["rule_files"]}
    evidence = saved.get("detail_review_evidence", {})
    decisions = {}
    for key, value in saved["detail_applicability"].items():
        spans = [value["context_evidence"]] if value.get("context_evidence") else []
        spans += value.get("additional_context_evidence", [])
        citations = []
        for span in spans:
            entry = evidence[span["file"]]
            assert len(entry["sha256"]) == 64
            citations.append({"path": entry["path"], "lines": span["lines"], "sha256": entry["sha256"]})
        decisions[index[key]] = {"applies": value["applies"], "insufficient": value.get("insufficient_context", False),
                                 "citations": citations}
    assert reuse == selected and usage_before == usage_after
    expected = {"Rules/AuditTag.md"} | ({"Rules/BadgeFormat.md"} if flag and example_ready else set())
    names = {index[key] for key in selected}
    result = {"variant": variant, "flag": flag, "example_ready": example_ready, "helper_available": helper_available,
              "quality_ok": names == expected, "selected": sorted(names),
              "decisions": decisions, "selection_seconds": round(selection_seconds, 3),
              "reuse_ms": round(reuse_ms, 3), "wall_seconds": round(time.monotonic()-started, 3),
              "input_bytes": saved.get("last_junie_input", {}).get("bytes"), "usage": usage_after}
    print("RESULT", json.dumps(result, ensure_ascii=False), flush=True)
    return result

with control_scope(ExecutionControl(lambda e: print("RETRY", e["message"], flush=True)
                                    if "再試行" in e["message"] else None, on_junie_request=on_request)):
    cases = {"true": (True, True), "false": (False, True), "not-ready": (True, False),
             "original-true": (True, True, False), "original-false": (False, True, False)}
    results = []
    for name in sys.argv[3:] or ("true", "false", "not-ready"):
        try:
            result = case(*cases[name])
        except workflow._MalformedDetailReviewReply:
            result = {"quality_ok": False, "error": "malformed_rule_review_response_after_retry"}
        results.append({**result, "case": name})
        output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    if not all(result["quality_ok"] for result in results):
        raise SystemExit(1)
