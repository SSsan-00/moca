import subprocess
import tempfile
import time

from migration_harness.execution import print_progress
from migration_harness.windows_job import CREATE_SUSPENDED, WindowsJob


def _read(stream):
    stream.seek(0)
    return stream.read().decode("utf-8", errors="replace")


def print_browser_output(output):
    if output:
        print_progress(output.removesuffix("\n"))


def run_browser(command, *, timeout):
    started = time.monotonic()
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        job = WindowsJob(kill_on_close=True)
        child = None
        expired = False
        cleanup_error = None
        try:
            child = subprocess.Popen(command, stdout=output, stderr=errors, creationflags=CREATE_SUSPENDED)
            try:
                job.start(child)
            except BaseException:
                child.kill()
                raise
            try:
                child.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                expired = True
            finally:
                try:
                    job.stop(time.monotonic() + 5)
                except OSError as exc:
                    cleanup_error = exc
        finally:
            job.close()
            if child is not None:
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired as exc:
                    if cleanup_error is None:
                        cleanup_error = exc
        stdout, stderr = _read(output), _read(errors)
        if expired or cleanup_error:
            elapsed = time.monotonic() - started
            reason = f"Chromium regression timed out after {timeout}s" if expired else "Chromium cleanup failed"
            if cleanup_error:
                detail = "; cleanup failed: " if expired else ": "
                reason += detail + str(cleanup_error)
            raise AssertionError(f"{reason} ({elapsed:.2f}s including cleanup)"
                                 f"\nBrowser stdout:\n{stdout or '(none)'}"
                                 f"\nBrowser stderr:\n{stderr or '(none)'}")
    return subprocess.CompletedProcess(command, child.returncode, stdout, stderr)
