"""ダッシュボードの停止要求と、実処理から届く状況報告をつなぐ。"""

from __future__ import annotations

import subprocess
import time
from concurrent.futures import Future
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, Lock
from typing import Callable, Iterator

from .windows_job import CREATE_SUSPENDED, WindowsJob


HEARTBEAT_SECONDS = 3


class OperationCancelled(KeyboardInterrupt):
    """端末のCtrl+Cと同じ保存・再開処理を通る、画面からの停止要求。"""


@dataclass
class ExecutionControl:
    on_activity: Callable[[dict], None]
    cancelled: Event = field(default_factory=Event)
    on_junie_request: Callable[[dict | None], Future[str] | None] | None = None
    trusted_junie_workspaces: set[str] = field(default_factory=set)
    junie_steering: bool = False
    operation_id: str | None = None
    junie_thoughts_path: Path | None = None
    _build_lock: Lock = field(default_factory=Lock, repr=False)
    _building: bool = False

    def request_build_stop(self) -> bool:
        with self._build_lock:
            if not self._building:
                return False
            self.cancelled.set()
            return True


_control: ContextVar[ExecutionControl | None] = ContextVar("execution_control", default=None)


@contextmanager
def control_scope(control: ExecutionControl | None) -> Iterator[None]:
    token = _control.set(control)
    try:
        yield
    finally:
        _control.reset(token)


def is_controlled() -> bool:
    """外部コマンドがハーネスの停止制御下にあるか判定する。"""
    return _control.get() is not None


def current_operation_id() -> str | None:
    control = _control.get()
    return control.operation_id if control is not None else None


def junie_dashboard_control() -> ExecutionControl | None:
    control = _control.get()
    return control if control is not None and control.on_junie_request is not None else None


def check_cancelled() -> None:
    """現在の操作に停止要求がある場合は中断する。"""
    if (control := _control.get()) is not None and control.cancelled.is_set():
        raise OperationCancelled("ダッシュボードから停止しました。")


@contextmanager
def build_process_scope() -> Iterator[None]:
    control = _control.get()
    if control is None:
        yield
        return
    with control._build_lock:
        check_cancelled()
        control._building = True
    try:
        yield
    finally:
        with control._build_lock:
            control._building = False
            check_cancelled()


def report_activity(message: str, *, kind: str = "activity", detail: str = "") -> None:
    """現在のRunの進捗と工程を通知する。"""
    if (control := _control.get()) is not None:
        entry = {"at": time.time(), "kind": kind, "message": message}
        if detail:
            entry["detail"] = detail
        control.on_activity(entry)


def run_process(command: list[str], *, label: str, cancellable: bool = True, **kwargs) -> subprocess.CompletedProcess:
    """待機中も停止を受け付ける。出力の内容と終了コードはsubprocess.runと同じ。"""
    if not is_controlled():
        return subprocess.run(command, **kwargs)
    check_cancelled()
    check = kwargs.pop("check", False)
    if kwargs.pop("capture_output", False):
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    with ExitStack() as stack:
        job = WindowsJob()
        stack.callback(job.close)
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | CREATE_SUSPENDED
        process = stack.enter_context(subprocess.Popen(command, **kwargs))
        try:
            job.start(process)
        except BaseException:
            process.kill()
            raise
        try:
            report_activity(f"{label}のプロセスを起動しました。")
            next_report = time.monotonic() + HEARTBEAT_SECONDS
            while True:
                if cancellable:
                    check_cancelled()
                try:
                    stdout, stderr = process.communicate(timeout=0.25)
                    break
                except subprocess.TimeoutExpired:
                    if time.monotonic() >= next_report:
                        report_activity(f"{label}のプロセスは起動中です。終了を待っています。", kind="heartbeat")
                        next_report = time.monotonic() + HEARTBEAT_SECONDS
        except BaseException:
            job.stop(time.monotonic() + 5)
            raise
        if check and process.returncode:
            raise subprocess.CalledProcessError(process.returncode, command, stdout, stderr)
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
