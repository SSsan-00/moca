"""Win32 Job Object for a launcher and its normally inherited children."""

import ctypes
import time
from ctypes import wintypes as win


class _ThreadEntry(ctypes.Structure):
    _fields_ = [
        ("size", win.DWORD), ("usage", win.DWORD), ("id", win.DWORD),
        ("owner", win.DWORD), ("base_priority", win.LONG), ("delta_priority", win.LONG),
        ("flags", win.DWORD),
    ]


class _JobAccounting(ctypes.Structure):
    _fields_ = [
        ("user_time", ctypes.c_longlong), ("kernel_time", ctypes.c_longlong),
        ("period_user_time", ctypes.c_longlong), ("period_kernel_time", ctypes.c_longlong),
        ("page_faults", win.DWORD), ("total_processes", win.DWORD),
        ("active_processes", win.DWORD), ("terminated_processes", win.DWORD),
    ]


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
        ("flags", win.DWORD), ("min_working_set", ctypes.c_size_t), ("max_working_set", ctypes.c_size_t),
        ("active_limit", win.DWORD), ("affinity", ctypes.c_size_t),
        ("priority_class", win.DWORD), ("scheduling_class", win.DWORD),
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("basic", _BasicLimits), ("io_counters", ctypes.c_ulonglong * 6),
        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
        ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t),
    ]


_HANDLE = win.HANDLE
_DWORD = win.DWORD
_BOOL = win.BOOL


CREATE_SUSPENDED = 0x00000004
_BASIC_ACCOUNTING = 1
_EXTENDED_LIMITS = 9
_KILL_ON_JOB_CLOSE = 0x00002000


class WindowsJob:
    def __init__(self, *, kill_on_close=False):
        """子プロセスを所有するWindows Job Objectを作成する。"""
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)

        def bind(name, result, *arguments):
            """Windows API関数の引数型と戻り値型を設定する。"""
            function = getattr(kernel, name)
            function.restype = result
            function.argtypes = arguments
            return function

        self._close = bind("CloseHandle", _BOOL, _HANDLE)
        self._assign = bind("AssignProcessToJobObject", _BOOL, _HANDLE, _HANDLE)
        self._terminate = bind("TerminateJobObject", _BOOL, _HANDLE, win.UINT)
        self._query = bind("QueryInformationJobObject", _BOOL, _HANDLE, ctypes.c_int,
                           ctypes.c_void_p, _DWORD, ctypes.POINTER(_DWORD))
        self._snapshot = bind("CreateToolhelp32Snapshot", _HANDLE, _DWORD, _DWORD)
        self._first = bind("Thread32First", _BOOL, _HANDLE, ctypes.POINTER(_ThreadEntry))
        self._next = bind("Thread32Next", _BOOL, _HANDLE, ctypes.POINTER(_ThreadEntry))
        self._open_thread = bind("OpenThread", _HANDLE, _DWORD, _BOOL, _DWORD)
        self._resume_thread = bind("ResumeThread", _DWORD, _HANDLE)
        create = bind("CreateJobObjectW", _HANDLE, ctypes.c_void_p, win.LPCWSTR)
        self.handle = create(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if kill_on_close:
            limits = _ExtendedLimits()
            limits.basic.flags = _KILL_ON_JOB_CLOSE
            set_info = bind("SetInformationJobObject", _BOOL, _HANDLE, ctypes.c_int, ctypes.c_void_p, _DWORD)
            if not set_info(self.handle, _EXTENDED_LIMITS, ctypes.byref(limits), ctypes.sizeof(limits)):
                error = ctypes.WinError(ctypes.get_last_error())
                self.close()
                raise error

    def start(self, process):
        """起動を保留したプロセスをJobへ登録して開始する。"""
        if not self._assign(self.handle, int(process._handle)):
            raise ctypes.WinError(ctypes.get_last_error())
        # Popen closes CreateProcess's thread handle. A suspended launcher has one primary thread.
        snapshot = self._snapshot(0x00000004, 0)  # TH32CS_SNAPTHREAD
        if snapshot == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            entry = _ThreadEntry(size=ctypes.sizeof(_ThreadEntry))
            present = self._first(snapshot, ctypes.byref(entry))
            while present:
                if entry.owner == process.pid:
                    thread = self._open_thread(0x0002, False, entry.id)  # THREAD_SUSPEND_RESUME
                    if not thread:
                        raise ctypes.WinError(ctypes.get_last_error())
                    try:
                        previous_count = self._resume_thread(thread)
                        if previous_count == 0xFFFFFFFF:
                            raise ctypes.WinError(ctypes.get_last_error())
                        if previous_count != 1:
                            raise OSError("Launcher did not have the expected suspend count")
                        return
                    finally:
                        self._close(thread)
                present = self._next(snapshot, ctypes.byref(entry))
            raise ctypes.WinError(ctypes.get_last_error())
        finally:
            self._close(snapshot)

    def active_processes(self):
        accounting = _JobAccounting()
        if not self._query(self.handle, _BASIC_ACCOUNTING, ctypes.byref(accounting), ctypes.sizeof(accounting), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return accounting.active_processes

    def stop(self, deadline):
        if self.active_processes() and not self._terminate(self.handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())
        while self.active_processes():
            if time.monotonic() >= deadline:
                raise TimeoutError("Job Object still has active processes")
            time.sleep(.025)

    def close(self):
        self._close(self.handle)
