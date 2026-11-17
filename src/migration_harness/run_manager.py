"""ダッシュボードのRun一覧と、同時に5件までの実行待ち行列。"""

from __future__ import annotations

import copy
import json
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from threading import Thread
from time import monotonic

from .config import find_unit
from .junie_conversation import JunieConversations
from .run_records import edit_run_record, execution_lock, load_dashboard_record, load_saved_run, validate_run_id


class RunManager:
    limit = 5

    def __init__(self, config, default):
        """Run一覧、キュー、ワーカー、会話管理を初期化する。"""
        self.config = config
        self.default = default
        self.condition = default.condition
        self.views = {}
        self.jobs = {}
        self.pending = deque()
        self.workers = {}
        self.results = {}
        self.closing = False
        self._queue_retry_at = None
        self._queue_retry_delay = 1.0
        self.owner = None
        self.root = None
        self.scope = None
        self.blocked_conversations = {}
        self.conversations = JunieConversations(self)
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
            jobs, cancelled, blocked = self._read_saved_queue(config, root)
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
        self._queue_retry_at = None
        self._queue_retry_delay = 1.0
        if updated is not None:
            self.config.update(updated)
        self.blocked_conversations = blocked
        if not root:
            return
        self.conversations.cancelled = cancelled
        self._restore_saved_jobs(jobs)

    def _read_saved_queue(self, config, root):
        """保存済みキューを読み、復元前にRunと設定の整合性を確認する。"""
        path = Path(root) / "dashboard-queue.json" if root else None
        saved = json.loads(path.read_text(encoding="utf-8")) if path and path.exists() else {}
        jobs = saved.get("jobs", [])
        cancelled = saved.get("cancelled_conversations", {})
        blocked = saved.get("blocked_conversations", {})
        for run_id, message in blocked.items():
            validate_run_id(run_id)
            if not isinstance(message, str) or not message:
                raise ValueError("保存済みの停止理由が不正です。")
        if (jobs or cancelled or blocked) and saved.get("project_root") != config.get("project_root"):
            raise ValueError("保存済みの実行待ち一覧とプロジェクトが異なります。保存先を確認してください。")
        for job in jobs:
            if str(uuid.UUID(job["run_id"])) != job["run_id"]:
                raise ValueError("実行待ち一覧のRun IDが不正です。")
            find_unit(config, job["unit_id"])
            if not job.get("running") and job["action"] not in {"run", "prepare"}:
                load_saved_run(config, job["run_id"])
        return jobs, cancelled, blocked

    def _restore_saved_jobs(self, jobs):
        """未起動ジョブだけをFIFOへ戻し、起動済み・保留中Runは止めて表示する。"""
        for job in jobs:
            run_id = job["run_id"]
            if job.get("running"):
                # 起動済みの工程は繰り返さず、保存されたチェックポイントから明示的な再開を待つ。
                message = self.blocked_conversations.setdefault(
                    run_id, "前回の処理の終了を確認できません。ログと残存プロセスを確認してから指示を送信し直してください。",
                )
                try:
                    view = self.get(run_id)
                    view.publish({**view.state, "message": message})
                except (OSError, ValueError, KeyError):
                    view = self.new_view(run_id, job["unit_id"])
                    view.publish({**view.state, "status": "preparation_interrupted", "ready": True,
                                  "message": message})
                continue
            if run_id in self.blocked_conversations:
                continue
            view = self.get(run_id) if job["action"] not in {"run", "prepare"} else self.new_view(run_id, job["unit_id"])
            self.enqueue(view, job, operation_id=job.get("operation_id"))
        self.persist()
        if self.conversations.thread:
            self.conversations.runs.clear()
            self.conversations.scan()

    @property
    def queue_path(self):
        """実行待ちデータの保存先を返す。"""
        return Path(self.config["run_root"]) / "dashboard-queue.json"

    @property
    def busy(self):
        """実行中または待機中のRunがあるか判定する。"""
        return bool(self.jobs)

    def persist(self):
        """ジョブと会話保留状態をキュー保存先へ記録する。"""
        if not self.root:
            return
        jobs = [{key: value for key, value in job.items() if key not in {"reply", "config"}}
                for job in self.jobs.values()]
        temporary = self.queue_path.with_suffix(".tmp")
        temporary.write_text(json.dumps({
            "project_root": self.config["project_root"], "jobs": jobs,
            "cancelled_conversations": self.conversations.cancelled,
            "blocked_conversations": self.blocked_conversations,
        }, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.queue_path)

    def new_view(self, run_id, unit_id):
        """新しいRunの画面状態を作成する。"""
        view = type(self.default)()
        view.condition = self.condition
        view.publish({"run_id": run_id, "unit_id": unit_id, "status": "queued", "ready": False,
                      "created_at": datetime.now(timezone.utc).isoformat(),
                      "diff_viewer_enabled": bool(self.config.get("diff_viewer_html"))})
        self.views[run_id] = view
        return view

    def get(self, run_id=None):
        """Run IDに対応する画面状態を取得または復元する。"""
        with self.condition:
            if not run_id:
                return self.default
            if run_id not in self.views:
                _, record = load_dashboard_record(self.config, run_id)
                view = type(self.default)()
                view.condition = self.condition
                view.observe(record)
                view.ready(self.config, record, self.blocked_conversations.get(run_id, ""))
                self.views[run_id] = view
            return self.views[run_id]

    def enqueue(self, view, action, *, operation_id=None):
        """新規・再開・復元を同じFIFOキューと画面状態へ登録する。"""
        run_id = view.state["run_id"]
        self.jobs[run_id] = {
            **action, "run_id": run_id, "unit_id": view.state["unit_id"],
            "operation_id": operation_id or uuid.uuid4().hex,
        }
        view.publish({**view.state, "ready": False, "queued": True, "cancellable": False,
                      "operation_id": None, "stop_requested": False,
                      "generating_anchors": action["action"] == "anchors"})
        self.pending.append(run_id)

    def cancel_queued(self, view):
        """まだ開始していないRunの操作を取り消す。"""
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
        if identifier := job.get("conversation_id"):
            self.conversations.cancelled[run_id] = identifier
            self.conversations.wake.set()
        self.persist()
        return {"run_id": run_id}

    def hide_run(self, run_id):
        """Runを一覧から隠し、記録ファイルは保持する。"""
        validate_run_id(run_id)
        candidate = Path(self.config["run_root"]).resolve() / run_id
        record_path = candidate / "run.json"
        with execution_lock(self.config, run_id):
            if candidate.is_symlink() or record_path.is_symlink() or record_path.exists():
                run_path, _ = load_dashboard_record(self.config, run_id)
                with edit_run_record(run_path / "run.json") as (_, changes):
                    changes["hidden_from_dashboard"] = True
            else:
                self.views.pop(run_id, None)
                self.results.pop(run_id, None)
        if self.default.state.get("run_id") == run_id:
            self.default = type(self.default)()
            self.default.condition = self.condition
            self.default.publish({"status": "idle", "ready": True})
        return {"run_id": run_id}

    def submit(self, action, selected=None):
        """HTTP操作を検証し、Runを実行待ちへ登録する。"""
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
                if run_id in self.blocked_conversations:
                    raise ValueError(self.blocked_conversations[run_id])
                if kind == "hide-run":
                    return self.hide_run(run_id)
                if any(job["unit_id"] == view.state["unit_id"] for job in self.jobs.values()):
                    raise ValueError("この移行単位は別のRunで実行中または実行待ちです。終了してから再開してください。")
                if kind == "anchors" and not view.state.get("worktree_path"):
                    raise ValueError("worktreeの準備が終わってから、もう一度お試しください。")
                if kind in {"resume", "resume-run", "build-response"}:
                    _, record = load_dashboard_record(self.config, run_id)
                    if not can_resume_run(self.config, record):
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
        """空きワーカー枠にFIFO順で処理を割り当てる。"""
        with self.condition:
            if self._queue_retry_at is not None:
                return
            while self.pending and len(self.workers) < self.limit and not self.closing:
                run_id = self.pending[0]
                job = self.jobs[run_id]
                job["config"] = copy.deepcopy(self.config)
                job["running"] = True
                try:
                    self.persist()
                except OSError:
                    # 起動印を保存できるまで、待機位置と画面の状態を維持する。
                    del job["running"], job["config"]
                    self._defer_queue_retry()
                    return
                self.pending.popleft()
                view = self.views[run_id]
                view.publish({**view.state, "queued": False})
                view.begin_operation(job["operation_id"])
                thread = Thread(target=self.execute, args=(run_id,), daemon=True, name=f"migration-{run_id[:8]}")
                self.workers[run_id] = thread
                thread.start()

    def _defer_queue_retry(self):
        """既存の巡回で共有キューの保存を再試行するよう予約する。"""
        if not self.closing:
            self._queue_retry_at = monotonic() + self._queue_retry_delay
            self._queue_retry_delay = min(2 * self._queue_retry_delay, 8.0)
            self.conversations.start()

    def retry_queue(self):
        """共有キューを保存し、空き枠の割り当てを再開する。"""
        with self.condition:
            if self.closing or self._queue_retry_at is None or monotonic() < self._queue_retry_at:
                return
            try:
                self.persist()
            except OSError:
                self._defer_queue_retry()
                return
            self._queue_retry_at = None
            self.start_pending()
            if self._queue_retry_at is None:
                self._queue_retry_delay = 1.0

    def block_conversations(self, run_id, message):
        """終了未確認のRunを会話処理から保留する。"""
        with self.condition:
            self.blocked_conversations[run_id] = message
            self.conversations.notify(run_id)

    def execute(self, run_id):
        """割り当て済みRunのworkflowを実行して結果を反映する。"""
        from .dashboard import execute_dashboard_action
        try:
            job = self.jobs[run_id]
            view = self.views[run_id]
            view.manager = self
            self.results[run_id] = execute_dashboard_action(view, job["config"], job)
        finally:
            with self.condition:
                self.jobs.pop(run_id, None)
                self.workers.pop(run_id, None)
                try:
                    self.persist()
                except OSError:
                    self._defer_queue_retry()
                    if run_id not in self.blocked_conversations:
                        raise
                    # The last persisted queue still identifies this as a started job.
                    view.publish({**view.state, "message": view.state.get("message", "") +
                                  "\n待機指示の停止を保存できません。残存プロセスと保存先を確認してください。"})
                else:
                    self.start_pending()
                finally:
                    self.condition.notify_all()

    def workspace(self, *, include_usage=False):
        """Runの保存状態と画面操作情報をまとめて返す。"""
        with self.condition:
            units = [unit["id"] for unit in self.config.get("units", [])]
            records = Path(self.config["run_root"]).glob("*/run.json") if units else ()
            rows = {}
            hidden = set()
            for path in records:
                try:
                    _, record = load_dashboard_record(self.config, path.parent.name)
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
        """Run一覧に表示する要約を作成する。"""
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
        """新しい処理を止め、ワーカーと資源を終了する。"""
        with self.condition:
            self.closing = True
            self.conversations.wake.set()
            for view in self.views.values():
                if view.control:
                    view.request_stop({"operation_id": view.state.get("operation_id")})
            threads = list(self.workers.values())
        for thread in threads:
            thread.join()
        if self.conversations.thread:
            self.conversations.thread.join()
        with self.condition:
            for view in [self.default, *self.views.values()]:
                view.closed = True
            self.condition.notify_all()
        if self.owner:
            self.owner.__exit__(None, None, None)
            self.owner = None
