from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, SimpleQueue
from threading import Event, Lock, Thread
from urllib.parse import urlencode
from xml.etree.ElementTree import Element, SubElement, tostring


@dataclass(frozen=True)
class RequestKey:
    run_id: str
    operation_id: str
    request_id: str


def pending_requests(dashboard, manager):
    with dashboard.condition:
        views = manager.views.values() if manager else (dashboard,)
        result = {}
        for view in sorted(views, key=lambda view: view.junie_request_sequence):
            state = view.state
            request = state.get("junie_request")
            if (not request or state.get("stop_requested") or state.get("ready")
                    or not state.get("run_id") or not state.get("operation_id")):
                continue
            key = RequestKey(state["run_id"], state["operation_id"], request["id"])
            result[key] = {**request, "run_id": key.run_id, "operation_id": key.operation_id,
                           "unit_id": state.get("unit_id", "")}
        return result


def answer_pending(dashboard, manager, action):
    ids = [action.get(name) for name in ("run_id", "operation_id", "request_id")]
    if not all(isinstance(value, str) and value for value in (*ids, action.get("option_id"))):
        return False
    key = RequestKey(*ids)
    with dashboard.condition:
        if key not in pending_requests(dashboard, manager):
            return False
        view = manager.views[key.run_id] if manager else dashboard
        return view.answer_junie(action)


def notification_settings(raw):
    if not isinstance(raw, dict) or raw.keys() - {"enabled", "interval_seconds", "repeats", "milestone_after_seconds"}:
        raise ValueError("[junie_notifications] must contain enabled, interval_seconds, repeats or milestone_after_seconds")
    enabled = raw.get("enabled", True)
    interval = raw.get("interval_seconds", 300)
    repeats = raw.get("repeats", 2)
    if type(enabled) is not bool or type(interval) is not int or not 1 <= interval <= 86400:
        raise ValueError("[junie_notifications] expects a boolean enabled and 1–86400 interval_seconds")
    if type(repeats) is not int or not 0 <= repeats <= 10:
        raise ValueError("[junie_notifications].repeats must be between 0 and 10")
    milestone = raw.get("milestone_after_seconds", 0)
    if type(milestone) is not int or not 0 <= milestone <= 86400:
        raise ValueError("[junie_notifications].milestone_after_seconds must be between 0 and 86400")
    return {"enabled": enabled, "interval_seconds": interval, "repeats": repeats,
            "milestone_after_seconds": milestone}


@dataclass(frozen=True)
class RunNotice:
    run_id: str
    status: str
    kind: str


def run_notice(run_id, action, result, *, failed=False):
    if action == "anchors":
        return None
    status = result.get("status", "")
    if failed:
        kind = "failed"
    elif (result.get("last_stop") or {}).get("saved_status") == status:
        return None
    elif status == "checker_needs_review" or (status == "editor_checkpoint" and result.get("operator_question")):
        kind = "review"
    elif status in {"build_failed", "checker_failed", "junie_failed", "script_failed", "preparation_failed"}:
        kind = "failed"
    elif status in {"interrupted", "preparation_interrupted", "cancelled"}:
        return None
    elif result.get("requested_range_complete"):
        kind = "migrated"
    elif result.get("error") or result.get("error_details"):
        kind = "failed"
    elif status in {"checker_succeeded", "checker_accepted"}:
        verification = result.get("verification") or {}
        kind = "recheck" if verification.get("inputs_changed_during_verification") else "verified"
    elif status == "build_succeeded":
        verification = result.get("verification") or {}
        kind = "recheck" if verification.get("inputs_changed_during_verification") else "built"
    elif status == "junie_finished":
        kind = "migrated"
    else:
        return None
    return RunNotice(run_id, status, kind)


@dataclass
class PendingToast:
    token: str
    next_at: float
    emitted: int = 0


class JunieNotifications:
    def __init__(self, pending, settings, origin, prefix, backend=None, now=time.monotonic):
        self.pending = pending
        self.settings = settings
        self.origin = origin
        self.prefix = prefix
        self.backend = backend if backend is not None else (WindowsToast() if os.name == "nt" else None)
        self.now = now
        self.active = {}
        self.links = {}
        self.run_queue = SimpleQueue()
        self.completed = set()
        self.milestones = set()
        self.visible_at = None
        self.link_lock = Lock()
        self.stop = Event()
        self.thread = None

    def start(self):
        if self.backend is not None:
            self.thread = Thread(target=self.run, name="junie-notifications", daemon=True)
            self.thread.start()

    def run(self):
        while not self.stop.is_set():
            self.tick()
            self.stop.wait(1)

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join()
        for toast in self.active.values():
            if toast.emitted:
                self.try_backend("remove", self.tag(toast), "")
        self.active.clear()

    @staticmethod
    def tag(toast):
        return hashlib.sha256(toast.token.encode("ascii")).hexdigest()[:16]

    def post(self, notice):
        if notice and self.backend is not None and not self.stop.is_set():
            self.run_queue.put(notice)

    def mark_visible(self):
        self.visible_at = self.now()

    def remember(self, token, key):
        with self.link_lock:
            self.links[token] = key
            if len(self.links) > 256:
                active_tokens = {entry.token for entry in self.active.values()}
                expired = next((item for item in self.links if item not in active_tokens), None)
                if expired:
                    del self.links[expired]

    def tick(self):
        policy = self.settings()
        visible = set(self.pending()) if policy["enabled"] else set()
        for key in list(self.active.keys() - visible):
            toast = self.active.pop(key)
            if toast.emitted:
                self.try_backend("remove", self.tag(toast), "")
        now = self.now()
        for key in sorted(visible, key=lambda key: (key.run_id, key.operation_id, key.request_id)):
            toast = self.active.get(key)
            if toast is None:
                toast = self.active[key] = PendingToast(secrets.token_urlsafe(24), now)
                self.remember(toast.token, key)
            if toast.emitted <= policy["repeats"] and toast.next_at <= now and key in self.pending():
                if self.stop.is_set():
                    return
                toast.emitted += 1
                toast.next_at = now + policy["interval_seconds"]
                self.try_backend("show", self.tag(toast), f"{self.origin}/junie-open/{toast.token}")

        notices = []
        while not self.stop.is_set():
            try:
                notices.append(self.run_queue.get_nowait())
            except Empty:
                break
        if not policy["enabled"]:
            return
        terminal = {notice.run_id for notice in notices if notice.kind != "milestone"}
        for notice in notices:
            if self.stop.is_set():
                return
            if notice.kind == "milestone":
                if (notice.run_id in terminal or notice.run_id in self.milestones
                        or self.visible_at is not None and self.now() - self.visible_at < 3):
                    continue
                self.milestones.add(notice.run_id)
            if notice.kind in {"migrated", "built", "verified"}:
                if notice.run_id in self.completed:
                    continue
                self.completed.add(notice.run_id)
            toast = PendingToast(secrets.token_urlsafe(24), now)
            self.remember(toast.token, notice)
            self.try_backend("show", self.tag(toast), f"{self.origin}/junie-open/{toast.token}", notice.kind)

    def try_backend(self, action, tag, url, kind=None):
        try:
            self.backend(action, tag, url, kind) if kind else self.backend(action, tag, url)
        except (OSError, subprocess.SubprocessError):
            # A disabled or unavailable Windows notifier must not interrupt the dashboard.
            pass

    def location(self, token):
        with self.link_lock:
            key = self.links.get(token)
        if key is None:
            return None
        if isinstance(key, RunNotice):
            return self.prefix + "?" + urlencode({"run": key.run_id, "notice": key.kind, "status": key.status})
        return self.prefix + "?" + urlencode({"run": key.run_id, "junie_request": key.request_id,
                                               "junie_operation": key.operation_id})


_RUN_MESSAGES = {
    "review": ("確認待ち", "処理が確認待ちです。対象の Run を開きます。"),
    "failed": ("処理停止", "処理が停止しました。対象の Run で工程と理由を確認できます。"),
    "migrated": ("移行処理完了", "指定の移行処理が終わりました。今回のビルド・チェッカーは未実施です。"),
    "built": ("ビルド完了", "ビルドが終わりました。チェッカーは未実施です。"),
    "verified": ("ビルド・チェック完了", "検証処理が終わりました。移行台帳や判断結果は対象の Run で確認できます。"),
    "recheck": ("再確認が必要", "検証中に入力が変わりました。対象の Run で再検証してください。"),
    "milestone": ("進捗", "移行処理が終わり、ビルド・検証に進みました。"),
}


def toast_xml(url, kind=None):
    toast = Element("toast", activationType="protocol", launch=url)
    binding = SubElement(SubElement(toast, "visual"), "binding", template="ToastGeneric")
    heading, text = _RUN_MESSAGES.get(kind, ("確認待ち", "Junie が操作の確認を待っています。クリックして確認を開きます。"))
    SubElement(binding, "text").text = f"移行ハーネス: {heading}"
    SubElement(binding, "text").text = text
    return tostring(toast, encoding="unicode")


class WindowsToast:
    def __call__(self, action, tag, url, kind=None):
        command = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        script = r"""
$ErrorActionPreference = 'Stop'
$data = [Console]::In.ReadToEnd() | ConvertFrom-Json
# Use only the installed Windows PowerShell entry, with no registry changes.
$app = Get-StartApps | Where-Object { $_.AppID -match '\\WindowsPowerShell\\v1\.0\\powershell\.exe$' } | Select-Object -First 1
if (-not $app) { exit 2 }
$manager = [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime]
if ($data.action -eq 'remove') {
    $history = $manager::History
    $history.Remove($data.tag, 'MigrationHarness', $app.AppID)
} elseif ($data.action -eq 'show') {
    $notifier = $manager::CreateToastNotifier($app.AppID)
    # Some Windows PowerShell WinRT projections do not expose Setting.
    # When it is absent, Windows still applies its notification policies in Show.
    $setting = $notifier.Setting
    if ($null -ne $setting -and [string]$setting -ne 'Enabled') { exit 3 }
    $xml = [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime]::new()
    $xml.LoadXml($data.xml)
    $item = [Windows.UI.Notifications.ToastNotification, Windows.UI.Notifications, ContentType=WindowsRuntime]::new($xml)
    $item.Tag = $data.tag
    $item.Group = 'MigrationHarness'
    $notifier.Show($item)
}
"""
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        payload = json.dumps({"action": action, "tag": tag, "xml": toast_xml(url, kind) if url else ""}).encode("ascii")
        args = [str(command), "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]
        for attempt in range(2):
            try:
                subprocess.run(args, input=payload, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=15 if attempt else 5)
                return
            except subprocess.TimeoutExpired:
                # Reuse the tag so a completed first Show is replaced, not duplicated.
                if action != "show" or attempt:
                    raise
