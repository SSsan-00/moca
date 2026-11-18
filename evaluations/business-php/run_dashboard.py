"""Drive the ordinary dashboard with real Junie; requires Playwright and Chrome.

Usage: PYTHONPATH=/absolute/harness/src python run_dashboard.py FIXTURE_ROOT OUTPUT_DIR
Only the isolated evaluation's trust/headless retry prompts are accepted.
"""
import json
from pathlib import Path
import subprocess
import sys
from threading import Thread
import time


def serve(root, run_id=None):
    """評価用設定で、指定されたRunまたはダッシュボードを開く。"""
    from migration_harness.config import load_config
    from migration_harness.dashboard import run_dashboard
    run_dashboard(load_config(root / "harness.toml"), **({"run_id": run_id} if run_id else {"unit_id": "business-scale"}))


def check(root, output):
    """評価の実行状態を観測し、保存された結果を確認する。"""
    from playwright.sync_api import expect, sync_playwright
    output.mkdir(parents=True, exist_ok=True)
    existing = set((root / "runs").glob("*/run.json"))
    start = time.monotonic()
    process = subprocess.Popen([sys.executable, "-u", __file__, "--server", str(root)], stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True)
    record, run_path, page_errors, transitions = None, None, [], []
    try:
        for line in process.stdout:
            print(line, end="", flush=True)
            if line.startswith("Progress dashboard: "):
                url = line.removeprefix("Progress dashboard: ").strip()
                break
        else:
            raise RuntimeError("dashboard did not start")
        def drain():
            """ダッシュボードの標準出力を読み、実行中の出力を回収する。"""
            for line in process.stdout:
                print(line, end="", flush=True)
        Thread(target=drain, daemon=True).start()
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome")
            page = browser.new_page(locale="ja-JP", viewport={"width": 1440, "height": 960})
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            page.goto(url)
            last = None
            for _ in range(9000):
                for name in ("信頼して続行", "今回のみ非対話モードで再試行", "今回のみ許可"):
                    button = page.get_by_role("button", name=name, exact=True)
                    if button.is_visible():
                        button.click()
                        print("APPROVED", name, flush=True)
                if run_path is None:
                    found = set((root / "runs").glob("*/run.json")) - existing
                    if found:
                        assert len(found) == 1, found
                        run_path, = found
                if run_path is not None:
                    record = json.loads(run_path.read_text(encoding="utf-8"))
                    state = (record["status"], record.get("current_capsule"), len(record.get("repair_history", [])))
                    if state != last:
                        transitions.append({"seconds": round(time.monotonic()-start, 3), "state": state})
                        print("STATE", state, "run", record["run_id"], flush=True)
                        last = state
                    if state[0] == "checker_succeeded":
                        break
                    if state[0] in {"checker_needs_review", "build_failed", "junie_failed", "script_failed", "interrupted"}:
                        page.wait_for_timeout(1500)
                        if json.loads(run_path.read_text(encoding="utf-8"))["status"] == state[0]:
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
            record = json.loads(run_path.read_text(encoding="utf-8"))
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
        check(Path(sys.argv[1]), Path(sys.argv[2]))
