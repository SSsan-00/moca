"""Process lifetime of one Junie invocation, including its launcher."""

from __future__ import annotations

import subprocess
import time

from ..windows_job import CREATE_SUSPENDED, WindowsJob

_STOP_TIMEOUT = 5


class JunieProcess(subprocess.Popen):
    def __init__(self, command, *, kill_on_close=False, **kwargs):
        job = WindowsJob(kill_on_close=True) if kill_on_close else WindowsJob()
        try:
            # Assign before the launcher can spawn, including when it is cmd.exe.
            super().__init__(command, creationflags=CREATE_SUSPENDED, **kwargs)
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

    def stop(self):
        deadline = time.monotonic() + _STOP_TIMEOUT
        try:
            self._job.stop(deadline)
            return self.wait(timeout=max(0, deadline - time.monotonic()))
        finally:
            self._job.close()
