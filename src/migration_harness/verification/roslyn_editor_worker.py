import subprocess
import threading
from concurrent.futures import Future
from contextlib import suppress
from pathlib import Path
from queue import SimpleQueue


_IDLE_SECONDS = 12
_MAX_REQUESTS = 32
_RESPONSE_LIMIT = 128 * 1024


class _Process:
    def __init__(self, command, environment):
        self.command = tuple(command)
        self.process = subprocess.Popen(
            [*command, "--semantic-stream"], cwd=Path(command[-1]).parent, env=environment,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.requests = SimpleQueue()
        self.count = 0
        self.thread = threading.Thread(target=self._exchange, daemon=True)
        try:
            self.thread.start()
        except BaseException:
            self.process.kill()
            self.process.wait(timeout=2)
            self.process.stdin.close()
            self.process.stdout.close()
            raise

    def _exchange(self):
        stdin, stdout = self.process.stdin, self.process.stdout
        while (item := self.requests.get()) is not None:
            payload, answer = item
            try:
                stdin.write(payload)
                stdin.write(b"\n")
                stdin.flush()
                line = stdout.readline(_RESPONSE_LIMIT + 1)
            except OSError:
                line = b""
            valid = bool(line.endswith(b"\n") and len(line) <= _RESPONSE_LIMIT)
            answer.set_result(subprocess.CompletedProcess(self.command, int(not valid), line))
            if not valid:
                break
            del item, payload, answer, line

    def submit(self, payload):
        answer = Future()
        self.requests.put((payload, answer))
        return answer

    def close(self):
        self.requests.put(None)
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=2)
        self.thread.join(timeout=2)
        if not self.thread.is_alive():
            for pipe in (self.process.stdin, self.process.stdout):
                with suppress(OSError):
                    pipe.close()


class EditorSemanticWorker:
    def __init__(self):
        self._lock = threading.Lock()
        self._helper = self._idle = None
        self._active = self._closed = False

    def _retire_locked(self):
        if self._idle is not None:
            self._idle.cancel()
            self._idle = None
        if self._helper is not None:
            helper, self._helper = self._helper, None
            helper.close()

    def _expire(self, timer):
        with self._lock:
            if self._idle is timer:
                self._retire_locked()

    def discard(self):
        with self._lock:
            self._retire_locked()

    def close(self):
        with self._lock:
            self._closed = True
            self._retire_locked()

    def run(self, command, payload, timeout, *, environment):
        with self._lock:
            if self._closed or self._active:
                raise OSError("semantic helper is in use or closed")
            if self._idle is not None:
                self._idle.cancel()
                self._idle = None
            if self._helper is not None and (
                    self._helper.command != tuple(command) or self._helper.process.poll() is not None):
                self._retire_locked()
            if self._helper is None:
                self._helper = _Process(command, environment)
            helper = self._helper
            pending = helper.submit(payload)
            self._active = True
        keep = False
        try:
            reply = pending.result(timeout=timeout)
            helper.count += 1
            keep = reply.returncode == 0 and helper.count < _MAX_REQUESTS
            return reply
        except TimeoutError as exc:
            raise subprocess.TimeoutExpired(command, timeout) from exc
        finally:
            with self._lock:
                self._active = False
                if self._helper is helper:
                    if not keep:
                        self._retire_locked()
                    else:
                        timer = threading.Timer(_IDLE_SECONDS, lambda: self._expire(timer))
                        timer.daemon = True
                        self._idle = timer
                        timer.start()
