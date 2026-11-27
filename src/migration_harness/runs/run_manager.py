"""ダッシュボードのRun一覧と、同時に5件までの実行待ち行列。"""

from __future__ import annotations

import copy
import json
import re
import uuid
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from stat import S_ISDIR, S_ISREG
from threading import Lock, Thread
from time import monotonic

from ..config import find_unit
from ..junie.junie_conversation import JunieConversations
from ..saved_state import nonempty_text, saved_object, versionless_record
from .run_records import (
    edit_run_record,
    execution_lock,
    load_dashboard_record,
    load_saved_run,
    validate_dashboard_worktree,
    validate_run_id,
)

_QUEUED_ACTIONS = frozenset({"run", "resume", "resume-run", "anchors", "build-response", "conversation", "accept", "recover-script"})


def _saved_operation_id(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value) is not None


def _validate_waiting_operation(job):
    action = job["action"]
    if action == "conversation" and not _saved_operation_id(job.get("conversation_id")):
        raise ValueError("保存済みの会話の指示IDが不正です。")
    if action == "accept" and not all(nonempty_text(job.get(key)) for key in ("finding_id", "reason")):
        raise ValueError("保存済みのチェッカー承認に指摘IDまたは理由がありません。")
    if "checker_instruction" in job and not nonempty_text(job["checker_instruction"]):
        raise ValueError("保存済みのチェッカー指示が不正です。")
    if "verification_request" in job and not isinstance(job["verification_request"], dict):
        raise ValueError("保存済みの再開計画が不正です。")


class RunManager:
    limit = 5

    def __init__(self, config, default):
        self.config = config
        self.default = default
        self.condition = default.condition
        self.views = {}
        self._workspace_scan = Lock()
        self._workspace_cache = {}
        self._workspace_config = None
        self._disk_run_ids = set()
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
        with self.settings_switch(updated):
            pass

    @contextmanager
    def settings_switch(self, updated):
        with self.condition:
            config = self.config if updated is None else updated
            root = config.get("run_root")
            scope = tuple(config.get(key) for key in ("project_root", "run_root", "worktree_root"))
            if scope == self.scope:
                yield
                return
            if self.busy:
                raise ValueError("実行中の処理が終わってから設定を保存してください。")
            old_owner = self.owner
            owner = old_owner if root else None
            if root and root != self.root:
                owner = execution_lock(config, "dashboard")
                owner.__enter__()
            try:
                jobs, cancelled, blocked, existed = self._read_saved_queue(config, root)
                views, disk_ids, waiting = self._prepare_saved_jobs(config, jobs, blocked)
                tracked = (self.conversations.saved_runs(root, blocked)
                           if root and (updated is not None or self.conversations.thread) else None)
                # TOML の置換が成功するまで旧画面・所有権・待機列を公開したままにする。
                yield
            except BaseException:
                if owner is not old_owner and owner:
                    owner.__exit__(None, None, None)
                raise
            if self.scope is None:
                run_id = self.default.state.get("run_id")
                if run_id in views and views[run_id] is not self.default:
                    self.default.publish(views[run_id].state)
                    views[run_id] = self.default
            else:
                for view in [self.default, *self.views.values()]:
                    view.publish({"status": "idle", "ready": True})
                self.results.clear()
            self.views, self._disk_run_ids = views, disk_ids
            self.jobs, self.pending = waiting, deque(waiting)
            self.scope, self.root, self.owner = scope, root, owner
            self._queue_retry_at, self._queue_retry_delay = None, 1.0
            if updated is not None:
                self.config.update(updated)
            self.blocked_conversations = blocked
            self.conversations.cancelled = cancelled
            if old_owner is not owner and old_owner:
                old_owner.__exit__(None, None, None)
            if root:
                if self.conversations.thread:
                    self.conversations.runs = tracked
                    self.conversations.wake.set()
                if not existed or updated is not None or len(waiting) != len(jobs):
                    try:
                        self.persist()
                    except OSError:
                        self._defer_queue_retry(tracked)

    @staticmethod
    def _read_saved_queue(config, root):
        if not root:
            return [], {}, {}, False
        path = Path(root) / "dashboard-queue.json"
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            if path.with_suffix(".tmp").exists():
                raise ValueError("実行待ち一覧に途中保存だけが残っています。元の記録を確認してください。") from None
            return [], {}, {}, False
        except UnicodeError as error:
            raise ValueError("dashboard-queue.json の文字コードが不正です。元の記録は変更していません。") from error

        saved = saved_object(text, path.name)
        versionless_record(saved, path.name)
        jobs = saved.get("jobs")
        cancelled = saved.get("cancelled_conversations")
        blocked = saved.get("blocked_conversations")
        if (not isinstance(jobs, list) or not isinstance(cancelled, dict) or not isinstance(blocked, dict)
                or not nonempty_text(saved.get("project_root"))):
            raise ValueError("保存済みの実行待ち一覧の必須項目または型が不正です。")
        if (jobs or cancelled or blocked) and saved.get("project_root") != config.get("project_root"):
            raise ValueError("保存済みの実行待ち一覧とプロジェクトが異なります。保存先を確認してください。")
        for run_id, message in blocked.items():
            validate_run_id(run_id)
            if not nonempty_text(message):
                raise ValueError("保存済みの停止理由が不正です。")
        for run_id, conversation in cancelled.items():
            validate_run_id(run_id)
            if not _saved_operation_id(conversation):
                raise ValueError("保存済みの取消指示IDが不正です。")

        runs, units, operations = set(), set(), set()
        allowed_units = {unit["id"] for unit in config.get("units", ())}
        for job in jobs:
            if not isinstance(job, dict) or not all(nonempty_text(job.get(key)) for key in ("run_id", "unit_id", "action")):
                raise ValueError("保存済みの実行待ち操作にRun ID、移行単位、または操作名がありません。")
            run_id, unit, action = job["run_id"], job["unit_id"], job["action"]
            validate_run_id(run_id)
            if action not in _QUEUED_ACTIONS:
                raise ValueError(f"dashboard-queue.json に未対応の操作があります: {action}")
            if run_id in runs or unit in units:
                raise ValueError("保存済みの実行待ち一覧でRunまたは移行単位が重複しています。")
            if unit not in allowed_units:
                raise ValueError(f"unknown migration unit: {unit}")
            runs.add(run_id)
            units.add(unit)
            operation = job.get("operation_id")
            if not _saved_operation_id(operation) or operation in operations:
                raise ValueError("dashboard-queue.json の操作IDが未設定・不正・重複のいずれかです。元の記録を確認してください。")
            operations.add(operation)
            for flag in ("running", "junie_brave", "junie_brave_reconfirm"):
                if flag in job and type(job[flag]) is not bool:
                    raise ValueError(f"保存済みの実行待ち操作の {flag} が不正です。")
            if not job.get("running") and action != "run":
                _validate_waiting_operation(job)
                _, _, record, _ = load_saved_run(config, run_id)
                if record["unit_id"] != unit:
                    raise ValueError("保存済みの実行待ち操作とRunの移行単位が一致しません。")
        return jobs, cancelled, blocked, True

    def _prepare_saved_jobs(self, config, jobs, blocked):
        views = {} if self.scope is not None else self.views.copy()
        disk_ids, waiting = set(), {}
        for job in jobs:
            run_id, unit_id = job["run_id"], job["unit_id"]
            interrupted = job.get("running")
            if job["action"] == "recover-script" and not interrupted:
                _, record = load_dashboard_record(config, run_id)
                view = self._record_view(config, record, "")
                disk_ids.add(run_id)
                view.publish({**view.state, "queued": False, "ready": True,
                              "message": "待機操作を取り消しました。内容を確認してからやり直してください。"})
                views[run_id] = view
                continue
            reconfirm_brave = job.get("junie_brave_reconfirm") is True or "junie_brave" in job
            if interrupted:
                message = blocked.setdefault(
                    run_id, "前回の処理の終了を確認できません。ログと残存プロセスを確認してから指示を送信し直してください。",
                )
            elif run_id in blocked:
                continue
            if interrupted or job["action"] != "run":
                try:
                    _, record = load_dashboard_record(config, run_id)
                except (OSError, ValueError, KeyError):
                    if not interrupted:
                        raise
                    view = self._queued_view(config, run_id, unit_id)
                    view.publish({**view.state, "status": "preparation_interrupted", "ready": True})
                else:
                    if record["unit_id"] != unit_id:
                        raise ValueError("保存済みの起動印とRunの移行単位が一致しません。")
                    view = self._record_view(config, record, blocked.get(run_id, ""))
                    disk_ids.add(run_id)
            else:
                view = self._queued_view(config, run_id, unit_id)
            views[run_id] = view
            if interrupted:
                view.publish({**view.state, "message": message})
            elif reconfirm_brave:
                status = "preparation_interrupted" if job["action"] == "run" else view.state["status"]
                view.publish({**view.state, "status": status, "queued": False, "ready": True,
                              "message": "Brave モードの実行待ちは再起動後に引き継ぎません。この画面からもう一度実行してください。"})
            else:
                waiting[run_id] = self._queued_job(view, job, job["operation_id"])
        return views, disk_ids, waiting

    @property
    def queue_path(self):
        return Path(self.config["run_root"]) / "dashboard-queue.json"

    @property
    def busy(self):
        return bool(self.jobs)

    def persist(self):
        if not self.root:
            return
        jobs = []
        for job in self.jobs.values():
            saved = {key: value for key, value in job.items() if key not in {"reply", "config", "junie_brave"}}
            if job.get("junie_brave") is True:
                saved["junie_brave_reconfirm"] = True
            jobs.append(saved)
        temporary = self.queue_path.with_suffix(".tmp")
        temporary.write_text(json.dumps({
            "project_root": self.config["project_root"], "jobs": jobs,
            "cancelled_conversations": self.conversations.cancelled,
            "blocked_conversations": self.blocked_conversations,
        }, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.queue_path)

    def new_view(self, run_id, unit_id):
        view = self._queued_view(self.config, run_id, unit_id)
        self.views[run_id] = view
        return view

    def _queued_view(self, config, run_id, unit_id):
        view = type(self.default)()
        view.condition = self.condition
        view.publish({"run_id": run_id, "unit_id": unit_id, "status": "queued", "ready": False,
                      "created_at": datetime.now(timezone.utc).isoformat(),
                      "diff_viewer_enabled": bool(config.get("diff_viewer_html"))})
        return view

    def get(self, run_id=None):
        with self.condition:
            if not run_id:
                return self.default
            if run_id not in self.views:
                _, record = load_dashboard_record(self.config, run_id)
                self._restore_view(run_id, record)
                self._disk_run_ids.add(run_id)
            return self.views[run_id]

    def _restore_view(self, run_id, record):
        view = self._record_view(self.config, record, self.blocked_conversations.get(run_id, ""))
        self.views[run_id] = view
        return view

    def _record_view(self, config, record, message):
        view = type(self.default)()
        view.condition = self.condition
        view.observe(record)
        view.ready(config, record, message)
        return view

    def enqueue(self, view, action):
        run_id = view.state["run_id"]
        self.jobs[run_id] = self._queued_job(view, action, uuid.uuid4().hex)
        self.pending.append(run_id)

    @staticmethod
    def _queued_job(view, action, operation_id):
        view.publish({**view.state, "ready": False, "queued": True, "cancellable": False,
                      "operation_id": None, "stop_requested": False,
                      "generating_anchors": action["action"] == "anchors"})
        return {**action, "run_id": view.state["run_id"], "unit_id": view.state["unit_id"],
                "operation_id": operation_id}

    def save_enqueued(self):
        try:
            self.persist()
        except OSError:
            if self._queue_retry_at is None:
                self._defer_queue_retry()
        else:
            self.start_pending()

    def cancel_queued(self, view):
        run_id = view.state["run_id"]
        if run_id not in self.pending:
            raise ValueError("このRunはすでに実行されています。一覧を更新して停止してください。")
        job = self.jobs[run_id]
        new_run = job["action"] == "run"
        if not new_run:
            _, _, record, _ = load_saved_run(self.config, run_id)
        jobs, pending = self.jobs.copy(), self.pending.copy()
        self.pending.remove(run_id)
        del self.jobs[run_id]
        identifier = job.get("conversation_id")
        previous = self.conversations.cancelled.get(run_id)
        if identifier:
            self.conversations.cancelled[run_id] = identifier
        try:
            self.persist()
        except OSError as exc:
            self.jobs, self.pending = jobs, pending
            if identifier:
                if previous is None:
                    del self.conversations.cancelled[run_id]
                else:
                    self.conversations.cancelled[run_id] = previous
            raise ValueError("待機の取消を保存できませんでした。待機一覧を確認してもう一度お試しください。") from exc
        if new_run:
            view.publish({**view.state, "status": "cancelled", "queued": False, "ready": True})
        else:
            view.observe(record)
            view.ready(self.config, record)
            view.publish({**view.state, "queued": False})
        if (reply := job.get("reply")) is not None:
            reply.set_exception(ValueError("待機中のアンカー生成を取り消しました。"))
        if identifier:
            self.conversations.wake.set()
        return {"run_id": run_id}

    def hide_run(self, run_id):
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
        from .resume_plan import BUILD_THROUGH_STAGE, can_resume_build, can_resume_run

        with self.condition:
            if self.closing:
                raise ValueError("ダッシュボードを終了しています。再度起動してください。")
            kind = action["action"]
            if kind not in _QUEUED_ACTIONS | {"open-run", "cancel-queued", "hide-run"}:
                raise ValueError("この操作は利用できません。")
            brave = action.get("junie_brave", False)
            if type(brave) is not bool or (brave and kind not in {"run", "resume-run"}):
                raise ValueError("Brave モードは移行の実行、または再開方法を指定した場合にだけ選択できます。")
            if brave and kind == "resume-run" and action.get("verification_request", {}).get("start_stage") != "migration":
                raise ValueError("移行工程の再開計画を確認してから Brave モードを選択してください。")
            if kind == "run":
                units = action.get("unit_ids", [action.get("unit_id")])
                if not isinstance(units, list) or not units or len(units) > 100 or any(not isinstance(unit, str) for unit in units):
                    raise ValueError("移行単位を1件以上、100件以内で選択してください。")
                units = list(dict.fromkeys(units))
                if brave and len(units) != 1:
                    raise ValueError("Brave モードで実行する移行単位は1件だけ選択してください。")
                for unit in units:
                    find_unit(self.config, unit)
                if any(job["unit_id"] in units for job in self.jobs.values()):
                    raise ValueError("選択した移行単位は実行中または実行待ちです。一覧を確認してください。")
                ids = []
                for unit in units:
                    run_id = str(uuid.uuid4())
                    view = self.new_view(run_id, unit)
                    self.enqueue(view, {"action": kind, **({"junie_brave": True} if brave else {})})
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
                    stage = action.get("verification_request", {}).get("start_stage")
                    # 明示指定は受付時に計画を照合済みで、実行直前にも再照合する。
                    resumable = stage in {"migration", "checker"} or (
                        can_resume_build(record) if stage in {"build", BUILD_THROUGH_STAGE} else can_resume_run(self.config, record)
                    )
                    if not resumable:
                        raise ValueError("このRunは再開できません。新しく実行してください。")
                if kind == "build-response" and view.state.get("status") != "build_failed":
                    raise ValueError("ビルド結果が変わりました。進捗を確認してください。")
                if kind == "accept" and view.state.get("status") != "checker_needs_review":
                    raise ValueError("チェッカー結果が変わりました。進捗を確認してください。")
                if kind not in {"resume-run", "anchors"} and action.get("revision") != view.revision:
                    raise ValueError("実行状況が更新されました。内容を確認してもう一度操作してください。")
                self.enqueue(view, action)
                ids = [run_id]
            self.save_enqueued()
            return {"run_ids": ids, "run_id": ids[0]}

    def start_pending(self):
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

    def _defer_queue_retry(self, saved_runs=None):
        if not self.closing:
            self._queue_retry_at = monotonic() + self._queue_retry_delay
            self._queue_retry_delay = min(2 * self._queue_retry_delay, 8.0)
            self.conversations.start(saved_runs)

    def retry_queue(self):
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
        with self.condition:
            self.blocked_conversations[run_id] = message
            self.conversations.notify(run_id)

    def execute(self, run_id):
        from ..web.dashboard import execute_dashboard_action
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
        # Serialize only list scans. Requests to get, stop or update a Run use condition, not this lock.
        with self._workspace_scan:
            while True:
                with self.condition:
                    config = copy.deepcopy(self.config)
                    revisions = {run_id: (view, view.revision) for run_id, view in self.views.items()}
                units = [unit["id"] for unit in config.get("units", [])]
                records, seen = self._list_records(config) if units else ({}, set())
                rows = {}
                hidden = set()
                for run_id, record in records.items():
                    if include_usage:
                        rows[run_id] = {key: record[key] for key in ("run_id", "unit_id", "created_at", "token_usage", "cost_exchange")
                                        if key in record}
                    elif record.get("hidden_from_dashboard") is True:
                        hidden.add(run_id)
                    else:
                        with self.condition:
                            if config != self.config:
                                break
                            try:
                                self._refresh_list_view(run_id, record, revisions.get(run_id))
                            except (OSError, ValueError, KeyError, TypeError):
                                continue
                with self.condition:
                    if config != self.config:
                        continue
                    if not include_usage:
                        self._disk_run_ids.update(seen)
                        self._disk_run_ids.intersection_update(self.views)
                        visible = dict.fromkeys((*records, *self.views))
                        rows = {run_id: self.row(self.views[run_id]) for run_id in visible
                                if run_id in self.views and run_id not in hidden and
                                (run_id in records or run_id in self.jobs or run_id not in self._disk_run_ids
                                 or revisions.get(run_id) != (self.views[run_id], self.views[run_id].revision))}
                    limit, busy = self.limit, self.busy
                return {"units": units, "runs": sorted(rows.values(), key=lambda row: row.get("created_at", ""), reverse=True),
                        "parallel_limit": limit, "busy": busy}

    def _list_records(self, config):
        previous = self._workspace_cache if config == self._workspace_config else {}
        root = Path(config["run_root"]).resolve()
        cache, records, seen = {}, {}, set()
        for path in root.glob("*/run.json"):
            run_id = path.parent.name
            seen.add(run_id)
            try:
                stamp = self._list_stamp(path)
                cached = previous.get(run_id)
                if cached is not None and stamp == cached[0]:
                    record = cached[1]
                    validate_dashboard_worktree(config, run_id, record)
                else:
                    _, record = load_dashboard_record(config, run_id)
                    record = self.default.list_record(record)
                    if stamp != self._list_stamp(path):
                        continue
                records[run_id] = record
                cache[run_id] = stamp, record
            except (OSError, ValueError, KeyError, TypeError):
                continue
        self._workspace_config, self._workspace_cache = config, cache
        return records, seen

    @staticmethod
    def _list_stamp(path):
        directory = path.parent.lstat()
        file = path.lstat()
        if (not S_ISDIR(directory.st_mode) or not S_ISREG(file.st_mode)
                or getattr(directory, "st_reparse_tag", 0) or getattr(file, "st_reparse_tag", 0)):
            raise ValueError("linked or invalid run record")
        return (directory.st_dev, directory.st_ino, file.st_dev, file.st_ino,
                file.st_size, file.st_mtime_ns, file.st_ctime_ns)

    def _refresh_list_view(self, run_id, record, previous):
        view = self.views.get(run_id)
        if view is None:
            try:
                self._restore_view(run_id, record)
            except (OSError, ValueError, KeyError, TypeError):
                return
        elif run_id not in self.jobs and previous == (view, view.revision):
            revision = view.revision
            notice = {key: view.state[key] for key in ("message", "error_details") if key in view.state}
            unchanged = record.get("status") == view.state.get("status") and record.get("error") == view.state.get("error")
            view.observe(record)
            if view.revision != revision:
                view.ready(self.config, record)
                if unchanged and notice:
                    view.publish({**view.state, **notice})
            elif view.state.get("ready"):
                view.publish({**view.state, **view.available_actions(self.config, record)})

    def row(self, view):
        keys = ("run_id", "unit_id", "created_at", "status", "review_kind", "ready", "resumable", "build_resumable", "worktree_path", "queued",
                "operation_id", "cancellable", "stop_requested", "context_capsules", "current_capsule", "completed_capsules",
                "active_stage", "stage_timings", "timing_updated_at", "phase_activity", "error", "message")
        row = {key: copy.deepcopy(view.state[key]) for key in keys if key in view.state}
        row["awaiting_answer"] = bool(view.state.get("junie_request"))
        row["revision"] = view.revision
        if row.get("run_id") in self.pending:
            row["queue_position"] = list(self.pending).index(row["run_id"]) + 1
            row["queue_saving"] = self._queue_retry_at is not None
        return row

    def close(self):
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
