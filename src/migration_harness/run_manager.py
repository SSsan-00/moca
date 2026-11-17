"""ダッシュボードのRun一覧と、同時に5件までの実行待ち行列。"""

from __future__ import annotations

import copy
import json
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from threading import Thread

from .config import find_unit
from .run_records import edit_run_record, execution_lock, load_saved_run


class RunManager:
    limit = 5

    def __init__(self, config, default):
        self.config = config
        self.default = default
        self.condition = default.condition
        self.views = {}
        self.jobs = {}
        self.pending = deque()
        self.workers = {}
        self.results = {}
        self.closing = False
        self.owner = None
        self.root = None
        self.scope = None
        if default.state.get("run_id"):
            self.views[default.state["run_id"]] = default
        self.configure()

    def configure(self, updated=None):
        """実行中には設定を変えず、保存先を変えたときだけ一覧を読み直す。"""
        config = updated if updated is not None else self.config
        root = config.get("run_root")
        scope = tuple(config.get(key) for key in ("project_root", "run_root", "worktree_root"))
        if scope == self.scope:
            return
        if self.busy:
            raise ValueError("実行中の処理が終わってから設定を保存してください。")
        # 新しい保存先の所有権と待機データを先に検証し、失敗時は元の画面を保つ。
        owner = self.owner if root else None
        if root and root != self.root:
            owner = execution_lock(config, "dashboard")
            owner.__enter__()
        try:
            path = Path(root) / "dashboard-queue.json" if root else None
            saved = json.loads(path.read_text(encoding="utf-8")) if path and path.exists() else {}
            jobs = saved.get("jobs", [])
            if jobs and saved.get("project_root") != config.get("project_root"):
                raise ValueError("保存済みの実行待ち一覧とプロジェクトが異なります。保存先を確認してください。")
            for job in jobs:
                if str(uuid.UUID(job["run_id"])) != job["run_id"]:
                    raise ValueError("実行待ち一覧のRun IDが不正です。")
                find_unit(config, job["unit_id"])
                if not job.get("running") and job["action"] not in {"run", "prepare"}:
                    load_saved_run(config, job["run_id"])
        except BaseException:
            if owner is not self.owner and owner:
                owner.__exit__(None, None, None)
            raise
        if self.owner is not owner and self.owner:
            self.owner.__exit__(None, None, None)
        self.owner = owner
        if self.scope is not None:
            for view in [self.default, *self.views.values()]:
                view.publish({"status": "idle", "ready": True})
            self.views.clear()
            self.results.clear()
        self.scope = scope
        self.root = root
        if updated is not None:
            self.config.update(updated)
        if not root:
            return
        for job in jobs:
            run_id = job["run_id"]
            if job.get("running"):
                # 起動済みの工程は繰り返さない。保存されたチェックポイントから明示的に再開する。
                try:
                    view = self.get(run_id)
                    view.publish({**view.state, "message": "前回のダッシュボードが終了しました。内容を確認して再開してください。"})
                except (OSError, ValueError, KeyError):
                    view = self.new_view(run_id, job["unit_id"])
                    view.publish({**view.state, "status": "preparation_interrupted", "ready": True,
                                  "message": "準備が完了していません。移行単位を選んで新しく実行してください。"})
                continue
            view = self.get(run_id) if job["action"] not in {"run", "prepare"} else self.new_view(run_id, job["unit_id"])
            self.enqueue(view, job)
        self.persist()

    @property
    def queue_path(self):
        return Path(self.config["run_root"]) / "dashboard-queue.json"

    @property
    def busy(self):
        return bool(self.jobs)

    def persist(self):
        if not self.root:
            return
        jobs = [{key: value for key, value in job.items() if key not in {"reply", "config"}}
                for job in self.jobs.values()]
        temporary = self.queue_path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"project_root": self.config["project_root"], "jobs": jobs}, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.queue_path)

    def new_view(self, run_id, unit_id):
        view = type(self.default)()
        view.condition = self.condition
        view.publish({"run_id": run_id, "unit_id": unit_id, "status": "queued", "ready": False,
                      "created_at": datetime.now(timezone.utc).isoformat(),
                      "diff_viewer_enabled": bool(self.config.get("diff_viewer_html"))})
        self.views[run_id] = view
        return view

    def get(self, run_id=None):
        with self.condition:
            if not run_id:
                return self.default
            if run_id not in self.views:
                _, _, record, _ = load_saved_run(self.config, run_id)
                view = type(self.default)()
                view.condition = self.condition
                view.observe(record)
                view.ready(self.config, record)
                self.views[run_id] = view
            return self.views[run_id]

    def enqueue(self, view, action):
        """新規・再開・復元を同じFIFOキューと画面状態へ登録する。"""
        run_id = view.state["run_id"]
        self.jobs[run_id] = {**action, "run_id": run_id, "unit_id": view.state["unit_id"]}
        view.publish({**view.state, "ready": False, "queued": True, "cancellable": False,
                      "operation_id": None, "stop_requested": False,
                      "generating_anchors": action["action"] == "anchors"})
        self.pending.append(run_id)

    def cancel_queued(self, view):
        run_id = view.state["run_id"]
        if run_id not in self.pending:
            raise ValueError("このRunはすでに実行されています。一覧を更新して停止してください。")
        job = self.jobs[run_id]
        if job["action"] in {"run", "prepare"}:
            view.publish({**view.state, "status": "cancelled", "queued": False, "ready": True})
        else:
            _, _, record, _ = load_saved_run(self.config, run_id)
            view.observe(record)
            view.ready(self.config, record)
            view.publish({**view.state, "queued": False})
        self.pending.remove(run_id)
        del self.jobs[run_id]
        if (reply := job.get("reply")) is not None:
            reply.set_exception(ValueError("待機中のアンカー生成を取り消しました。"))
        self.persist()
        return {"run_id": run_id}

    def hide_run(self, run_id):
        record_path = Path(self.config["run_root"]) / run_id / "run.json"
        with execution_lock(self.config, run_id):
            if record_path.exists():
                run_path, _, _, _ = load_saved_run(self.config, run_id)
                with edit_run_record(run_path / "run.json") as (_, changes):
                    changes["hidden_from_dashboard"] = True
            else:
                self.views.pop(run_id, None)
                self.results.pop(run_id, None)
        return {"run_id": run_id}

    def submit(self, action, selected=None):
        from .workflow import can_resume_run

        with self.condition:
            if self.closing:
                raise ValueError("ダッシュボードを終了しています。再度起動してください。")
            kind = action["action"]
            if kind in {"run", "prepare"}:
                units = action.get("unit_ids", [action.get("unit_id")])
                if not isinstance(units, list) or not units or len(units) > 100 or any(not isinstance(unit, str) for unit in units):
                    raise ValueError("移行単位を1件以上、100件以内で選択してください。")
                units = list(dict.fromkeys(units))
                for unit in units:
                    find_unit(self.config, unit)
                if any(job["unit_id"] in units for job in self.jobs.values()):
                    raise ValueError("選択した移行単位は実行中または実行待ちです。一覧を確認してください。")
                ids = []
                for unit in units:
                    run_id = str(uuid.uuid4())
                    view = self.new_view(run_id, unit)
                    self.enqueue(view, {"action": kind})
                    ids.append(run_id)
            else:
                view = self.get(action.get("run_id")) if action.get("run_id") else selected or self.default
                run_id = view.state.get("run_id")
                if kind == "open-run":
                    return {"run_id": run_id}
                if kind == "cancel-queued":
                    return self.cancel_queued(view)
                if not run_id or run_id in self.jobs or not view.state.get("ready"):
                    raise ValueError("このRunは処理中です。終了してから操作してください。")
                if kind == "hide-run":
                    return self.hide_run(run_id)
                if any(job["unit_id"] == view.state["unit_id"] for job in self.jobs.values()):
                    raise ValueError("この移行単位は別のRunで実行中または実行待ちです。終了してから再開してください。")
                if kind == "anchors" and not view.state.get("worktree_path"):
                    raise ValueError("worktreeの準備が終わってから、もう一度お試しください。")
                if kind in {"resume", "resume-run", "build-response"} and not can_resume_run(self.config, view.state):
                    raise ValueError("このRunは再開できません。新しく実行してください。")
                if kind == "build-response" and view.state.get("status") != "build_failed":
                    raise ValueError("ビルド結果が変わりました。進捗を確認してください。")
                if kind == "accept" and view.state.get("status") != "checker_needs_review":
                    raise ValueError("チェッカー結果が変わりました。進捗を確認してください。")
                if kind not in {"resume-run", "anchors"} and action.get("revision") != view.revision:
                    raise ValueError("実行状況が更新されました。内容を確認してもう一度操作してください。")
                self.enqueue(view, action)
                ids = [run_id]
            self.persist()
            self.start_pending()
            return {"run_ids": ids, "run_id": ids[0]}

    def start_pending(self):
        with self.condition:
            while self.pending and len(self.workers) < self.limit and not self.closing:
                run_id = self.pending.popleft()
                job = self.jobs[run_id]
                job["running"] = True
                job["config"] = copy.deepcopy(self.config)
                view = self.views[run_id]
                view.publish({**view.state, "queued": False})
                view.begin_operation()
                self.persist()
                thread = Thread(target=self.execute, args=(run_id,), daemon=True, name=f"migration-{run_id[:8]}")
                self.workers[run_id] = thread
                thread.start()

    def execute(self, run_id):
        from .dashboard import execute_dashboard_action
        try:
            job = self.jobs[run_id]
            self.results[run_id] = execute_dashboard_action(self.views[run_id], job["config"], job)
        finally:
            with self.condition:
                self.jobs.pop(run_id, None)
                self.workers.pop(run_id, None)
                self.persist()
                self.start_pending()
                self.condition.notify_all()

    def workspace(self, *, include_usage=False):
        with self.condition:
            units = [unit["id"] for unit in self.config.get("units", [])]
            records = Path(self.config["run_root"]).glob("*/run.json") if units else ()
            rows = {}
            hidden = set()
            for path in records:
                try:
                    _, _, record, _ = load_saved_run(self.config, path.parent.name)
                    if include_usage:
                        rows[path.parent.name] = {key: record[key] for key in ("run_id", "unit_id", "created_at", "token_usage") if key in record}
                    elif record.get("hidden_from_dashboard") is True:
                        hidden.add(path.parent.name)
                    else:
                        view = self.get(path.parent.name)
                        if path.parent.name not in self.jobs:
                            revision = view.revision
                            notice = {key: view.state[key] for key in ("message", "error_details") if key in view.state}
                            unchanged = record.get("status") == view.state.get("status") and record.get("error") == view.state.get("error")
                            view.observe(record)
                            if view.revision != revision:
                                view.ready(self.config, record)
                                if unchanged and notice:
                                    view.publish({**view.state, **notice})
                        rows[path.parent.name] = self.row(view)
                except (OSError, ValueError, KeyError, TypeError):
                    continue
            if not include_usage:
                rows.update((run_id, self.row(view)) for run_id, view in self.views.items() if run_id not in hidden)
            return {"units": units, "runs": sorted(rows.values(), key=lambda row: row.get("created_at", ""), reverse=True),
                    "parallel_limit": self.limit, "busy": self.busy}

    def row(self, view):
        keys = ("run_id", "unit_id", "created_at", "status", "ready", "resumable", "worktree_path", "queued",
                "operation_id", "cancellable", "stop_requested", "context_capsules", "current_capsule", "completed_capsules",
                "active_stage", "stage_timings", "timing_updated_at", "phase_activity", "error", "message")
        row = {key: copy.deepcopy(view.state[key]) for key in keys if key in view.state}
        row["awaiting_answer"] = bool(view.state.get("junie_request"))
        row["revision"] = view.revision
        if row.get("run_id") in self.pending:
            row["queue_position"] = list(self.pending).index(row["run_id"]) + 1
        return row

    def close(self):
        with self.condition:
            self.closing = True
            for view in self.views.values():
                if view.control:
                    view.request_stop({"operation_id": view.state.get("operation_id")})
            threads = list(self.workers.values())
        for thread in threads:
            thread.join()
        with self.condition:
            for view in [self.default, *self.views.values()]:
                view.closed = True
            self.condition.notify_all()
        if self.owner:
            self.owner.__exit__(None, None, None)
            self.owner = None
