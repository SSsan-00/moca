"""Process lifetime of one Junie invocation, including its launcher."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

_STOP_TIMEOUT = 5


class JunieProcess(subprocess.Popen):
    def __init__(self, command, *, terminal=False, **kwargs):
        self._terminal = None
        self._job = None
        if os.name == "nt":
            from .windows_job import WindowsJob

            job = WindowsJob()
            try:
                # Assign before the launcher can spawn, including when it is cmd.exe.
                super().__init__(command, creationflags=0x00000004, **kwargs)  # CREATE_SUSPENDED
                try:
                    job.start(self)
                except BaseException:
                    self.kill()
                    self.wait(timeout=_STOP_TIMEOUT)
                    for stream in (self.stdin, self.stdout, self.stderr):
                        if stream is not None:
                            stream.close()
                    raise
            except BaseException:
                job.close()
                raise
            self._job = job
        elif terminal and sys.stdin.isatty():
            fd = sys.stdin.fileno()
            if os.tcgetpgrp(fd) != os.getpgrp():
                raise OSError("Junie requires the terminal foreground")
            # Take the terminal before launching Junie and let Ctrl+C end the whole invocation,
            # even when the foreground command handles or ignores SIGINT.
            foreground_launcher = (
                "import os, signal, subprocess, sys; "
                "signal.signal(signal.SIGINT, lambda *_: os._exit(130)); "
                "signal.signal(signal.SIGTTOU, signal.SIG_IGN); "
                "os.tcsetpgrp(0, os.getpgrp()); "
                "signal.signal(signal.SIGTTOU, signal.SIG_DFL); "
                "code = subprocess.call(sys.argv[1:]); "
                "sys.exit(code if code >= 0 else 128 - code)"
            )
            super().__init__([sys.executable, "-I", "-c", foreground_launcher, *command], process_group=0, **kwargs)
            self._terminal = fd
        else:
            super().__init__(command, start_new_session=True, **kwargs)

    def terminal_interrupted(self):
        return self._terminal is not None and self.returncode == 128 + signal.SIGINT

    def wait_for_launcher(self, timeout):
        if self._job is not None:
            self.wait(timeout=timeout)
            return
        # Keep the leader unreaped so its group ID cannot be recycled during cleanup.
        deadline = time.monotonic() + timeout
        while not os.waitid(os.P_PID, self.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired("Junie", timeout)
            time.sleep(min(.025, remaining))

    def stop(self):
        deadline = time.monotonic() + _STOP_TIMEOUT
        if self._job is not None:
            try:
                self._job.stop(deadline)
                return self.wait(timeout=max(0, deadline - time.monotonic()))
            finally:
                self._job.close()

        if self.returncode is not None:
            raise OSError("Junie process group leader was already reaped")
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired("Junie process group", _STOP_TIMEOUT)
                listing = subprocess.run(
                    ["/bin/ps", "-ax", "-o", "pgid=", "-o", "stat="],
                    check=True, capture_output=True, text=True, timeout=remaining,
                )
                members = (row.split() for row in listing.stdout.splitlines())
                if not any(int(group) == self.pid and not state.startswith("Z") for group, state in members):
                    break
                try:
                    os.killpg(self.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    # Darwin can report EPERM for a group that just became all zombies.
                    # Recheck membership within the same deadline before claiming termination.
                    pass
                time.sleep(.025)
        finally:
            if self._terminal is not None:
                old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTTOU})
                try:
                    if os.tcgetpgrp(self._terminal) == self.pid:
                        os.tcsetpgrp(self._terminal, os.getpgrp())
                finally:
                    signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
        return self.wait(timeout=max(0, deadline - time.monotonic()))
