"""ダッシュボードの停止要求と、実処理から届く状況報告をつなぐ。"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from concurrent.futures import Future
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, Lock
from typing import Callable, Iterator


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
    return _control.get() is not None


def current_operation_id() -> str | None:
    control = _control.get()
    return control.operation_id if control is not None else None


def junie_dashboard_control() -> ExecutionControl | None:
    control = _control.get()
    return control if control is not None and control.on_junie_request is not None else None


def check_cancelled() -> None:
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
    if (control := _control.get()) is not None:
        entry = {"at": time.time(), "kind": kind, "message": message}
        if detail:
            entry["detail"] = detail
        control.on_activity(entry)


def _terminate_process_tree(process: subprocess.Popen) -> None:
    # 子プロセスがファイルを書き続けないよう、専用のプロセス群ごと止める。
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            process.kill()
    finally:
        # 親が先に終了しても、SIGTERMを無視する子を残さない。
        if os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait()


def run_process(command: list[str], *, label: str, cancellable: bool = True, **kwargs) -> subprocess.CompletedProcess:
    """待機中も停止を受け付ける。出力の内容と終了コードはsubprocess.runと同じ。"""
    if not is_controlled():
        return subprocess.run(command, **kwargs)
    check_cancelled()
    check = kwargs.pop("check", False)
    if kwargs.pop("capture_output", False):
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    with subprocess.Popen(command, **kwargs) as process:
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
            _terminate_process_tree(process)
            raise
        if check and process.returncode:
            raise subprocess.CalledProcessError(process.returncode, command, stdout, stderr)
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
