"""Win32 Job Object for a Junie launcher and its normally inherited children."""

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


_HANDLE = win.HANDLE
_DWORD = win.DWORD
_BOOL = win.BOOL


class WindowsJob:
    def __init__(self):
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)

        def bind(name, result, *arguments):
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

    def start(self, process):
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
                            raise OSError("Junie launcher did not have the expected suspend count")
                        return
                    finally:
                        self._close(thread)
                present = self._next(snapshot, ctypes.byref(entry))
            raise ctypes.WinError(ctypes.get_last_error())
        finally:
            self._close(snapshot)

    def stop(self, deadline):
        accounting = _JobAccounting()

        def active():
            if not self._query(self.handle, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None):
                raise ctypes.WinError(ctypes.get_last_error())
            return accounting.active_processes

        if active() and not self._terminate(self.handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())
        while active():
            if time.monotonic() >= deadline:
                raise TimeoutError("Junie Job Object still has active processes")
            time.sleep(.025)

    def close(self):
        self._close(self.handle)
