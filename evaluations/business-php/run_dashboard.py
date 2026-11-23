"""Drive the ordinary dashboard with real Junie; requires Playwright and Chrome.

Usage: PYTHONPATH=/absolute/harness/src python run_dashboard.py FIXTURE_ROOT OUTPUT_DIR
Accept trust/headless retry and confirmed completion receipts only for the isolated evaluation.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from threading import Thread
import time
from uuid import UUID


def serve(root, run_id=None):
    from migration_harness.config import load_config
    from migration_harness.web.dashboard import run_dashboard
    run_dashboard(load_config(root / "harness.toml"), **({"run_id": run_id} if run_id else {"unit_id": "business-scale"}))


def read_record(path):
    # A Windows reader can briefly lose the file while the writer atomically replaces it.
    for attempt in range(20):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (PermissionError, FileNotFoundError):
            if attempt == 19:
                raise
            time.sleep(0.05)


def allow_fixture_receipt(root, run_id, detail):
    if not run_id:
        return False
    lines = detail.strip().splitlines()
    if not lines or lines[0] != "Allow changes in this file?":
        return False
    path = Path(lines[-1])
    if not path.is_absolute():
        return False
    workspace = root.resolve() / "runs" / run_id / "agent-workspace"
    if workspace.resolve() != workspace:
        return False
    if re.fullmatch(r"migration-complete-[a-f0-9]{32}\.txt", path.name):
        return path.resolve().parent == workspace
    if re.fullmatch(r"steering-[a-f0-9]{32}\.txt", path.name):
        return path.resolve().parent == workspace / ".migration-scratch"
    return False


def check(root, output, run_id=None):
    from playwright.sync_api import TimeoutError as PlaywrightTimeout, expect, sync_playwright
    run_id = str(UUID(run_id)) if run_id else None
    output.mkdir(parents=True, exist_ok=True)
    existing = set((root / "runs").glob("*/run.json"))
    start = time.monotonic()
    environment = os.environ.copy()
    environment["PATH"] = os.pathsep.join((str(Path(sys.executable).parent), environment.get("PATH", "")))
    process = subprocess.Popen([sys.executable, "-u", __file__, "--server", str(root), *([run_id] if run_id else [])], stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, env=environment)
    run_path = root / "runs" / run_id / "run.json" if run_id else None
    record, page_errors, transitions = None, [], []
    try:
        for line in process.stdout:
            print(line, end="", flush=True)
            if line.startswith("Progress dashboard: "):
                url = line.removeprefix("Progress dashboard: ").strip()
                break
        else:
            raise RuntimeError("dashboard did not start")
        def drain():
            for line in process.stdout:
                print(line, end="", flush=True)
        Thread(target=drain, daemon=True).start()
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome")
            page = browser.new_page(locale="ja-JP", viewport={"width": 1440, "height": 960})
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            page.goto(url + ("?run=" + run_id if run_id else ""))
            if run_id:
                saved_status = read_record(run_path)["status"]
                close_selectors = {"checker_needs_review": "#review-close", "build_failed": "#build-review-close"}
                if selector := close_selectors.get(saved_status):
                    try:
                        page.locator(selector).click(timeout=3000)
                    except PlaywrightTimeout:
                        pass

                page.locator("#tab-runs").click(timeout=20_000)
                page.locator(f'tr[data-run-id="{run_id}"] button[data-action="resume-run"]').click(timeout=20_000)
            last = None
            for _ in range(9000):
                for name in ("信頼して続行", "今回のみ非対話モードで再試行"):
                    button = page.get_by_role("button", name=name, exact=True)
                    if button.is_visible() and button.is_enabled():
                        try:
                            button.click(timeout=1500)
                        except PlaywrightTimeout:
                            continue
                        print("APPROVED", name, flush=True)
                if run_path is None:
                    found = set((root / "runs").glob("*/run.json")) - existing
                    if found:
                        assert len(found) == 1, found
                        run_path, = found
                if run_path is not None:
                    popup = page.locator("#junie-confirmation[open]")
                    shown_run = popup.locator("#junie-confirmation-run")
                    detail = popup.locator("#junie-confirmation-detail")
                    approve = popup.get_by_role("button", name=re.compile(r"^今回のみ許可(?:（.*）)?$"))
                    current_id = run_path.parent.name
                    try:
                        if (approve.is_visible() and approve.is_enabled() and detail.is_visible()
                                and shown_run.inner_text(timeout=1500).endswith(f"Run {current_id}")
                                and allow_fixture_receipt(root, current_id, detail.inner_text(timeout=1500))):
                            approve.click(timeout=1500)
                            print("APPROVED scoped fixture receipt once", flush=True)
                    except PlaywrightTimeout:
                        pass
                    record = read_record(run_path)
                    state = (record["status"], record.get("current_capsule"), len(record.get("repair_history", [])))
                    if state != last:
                        transitions.append({"seconds": round(time.monotonic()-start, 3), "state": state})
                        print("STATE", state, "run", record["run_id"], flush=True)
                        last = state
                    if state[0] == "checker_succeeded":
                        break
                    if state[0] in {"checker_needs_review", "build_failed", "junie_failed", "script_failed", "interrupted"}:
                        page.wait_for_timeout(1500)
                        if read_record(run_path)["status"] == state[0]:
                            break
                page.wait_for_timeout(400)
            else:
                raise TimeoutError("dashboard verification exceeded one hour")
            if record["status"] == "checker_succeeded":
                expect(page.locator("#activity")).to_contain_text("チェッカーの終了コード 0", timeout=15000)
            page.screenshot(path=str(output / "dashboard.png"), full_page=True)
            browser.close()
    finally:
        elapsed = time.monotonic()-start
        if run_path:
            record = read_record(run_path)
            worktree = Path(record["worktree_path"])
            evidence = dict(run_id=record["run_id"], run_path=str(run_path.parent), status=record["status"],
                wall_seconds=round(elapsed, 3), transitions=transitions, page_errors=page_errors,
                fixture=json.loads((root/"fixture.json").read_text(encoding="utf-8")),
                stage_timings=record.get("stage_timings"), token_usage=record.get("token_usage"),
                repair_history=record.get("repair_history"), build_exit_code=record.get("build_exit_code"),
                checker_exit_code=record.get("checker_exit_code"), completed_capsules=record.get("completed_capsules"),
                context_capsules=record.get("context_capsules"), detail_applicability=record.get("detail_applicability"),
                error=record.get("error"))
            if (worktree/"verification/last-check.json").exists():
                evidence["check"] = json.loads((worktree/"verification/last-check.json").read_text(encoding="utf-8"))
            (output / "result.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
        process.terminate()
        process.wait(timeout=15)
    assert record["status"] == "checker_succeeded", record.get("error", record["status"])
    assert not page_errors, page_errors
    assert record["build_exit_code"] == record["checker_exit_code"] == 0
    assert record["completed_capsules"] == record["context_capsules"]
    selected = sorted(key for key, value in record["detail_applicability"].items() if value["applies"])
    assert selected == evidence["fixture"]["expected_selected_details"], selected
    print("PASS", record["run_id"], evidence["wall_seconds"], flush=True)


if __name__ == "__main__":
    if sys.argv[1] == "--server":
        serve(Path(sys.argv[2]), sys.argv[3] if len(sys.argv) > 3 else None)
    else:
        check(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3] if len(sys.argv) > 3 else None)
