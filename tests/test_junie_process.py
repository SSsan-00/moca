"""Exercise launcher exit and child lifetime with real processes and a blocking write trigger."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from migration_harness import execution
from migration_harness.junie import junie_cli
from migration_harness.junie.junie_process import JunieProcess


_CHILD = """
import socket, sys
from pathlib import Path

with socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=10) as connection:
    connection.sendall(b"r")
    if connection.recv(1) == b"w":
        Path(sys.argv[2]).write_text("late write", encoding="utf-8")
"""

_LAUNCHER = """
import subprocess, sys, time
from pathlib import Path
from os import environ

target = Path(environ["TEST_JUNIE_TARGET"])
target.write_text("before exit", encoding="utf-8")
subprocess.Popen([sys.executable, "-c", {child!r}, environ["TEST_JUNIE_PORT"], str(target)],
                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
permit = target.with_name("parent-may-exit")
deadline = time.monotonic() + 10
while not permit.exists():
    if time.monotonic() >= deadline:
        raise TimeoutError("test did not release launcher")
    time.sleep(.01)
if environ.get("TEST_JUNIE_SKIP_TOKEN") != "1":
    target.with_name("completion").write_text("token", encoding="ascii")
if environ["TEST_JUNIE_KEEP_PARENT"] == "1":
    time.sleep(10)
""".format(child=_CHILD)


class JunieProcessTests(unittest.TestCase):
    def test_natural_launcher_exit_confirms_no_late_writes_and_preserves_an_unrelated_process(self):
        for mode in ("--task", "--prompt"):
            with self.subTest(mode=mode):
                self.check_child(mode=mode)

    def test_live_launcher_cancellation_confirms_child_exit(self):
        self.check_child(mode="--prompt", cancel="live")

    def test_cancel_when_launcher_exits_still_confirms_child_exit(self):
        self.check_child(mode="--task", cancel="after_exit")

    def test_unconfirmed_exit_blocks_result_adoption_and_retry(self):
        self.check_child(mode="--task", fail=True)

    def test_connection_retry_starts_only_after_the_previous_child_exits(self):
        self.check_child(mode="--task", retry=True)

    def test_windows_batch_launcher_owns_descendants_after_it_exits(self):
        self.check_child(mode="--task", batch=True)

    def check_child(self, *, mode, cancel=None, fail=False, batch=False, retry=False):
        root = Path(self.enterContext(TemporaryDirectory())).resolve()
        target = root / "Target.cs"
        completion = root / "completion"
        permit = root / "parent-may-exit"
        listener = self.enterContext(socket.socket())
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(5)
        self.enterContext(patch.dict(os.environ, {
            "JUNIE_HOME": str(root),
            "TEST_JUNIE_TARGET": str(target),
            "TEST_JUNIE_PORT": str(listener.getsockname()[1]),
            "TEST_JUNIE_KEEP_PARENT": "1" if cancel == "live" else "0",
        }))
        script = root / "launcher.py"
        script.write_text(_LAUNCHER, encoding="utf-8")
        command = [sys.executable, str(script), mode, "fixture"]
        if batch:
            launcher = root / "launcher.cmd"
            launcher.write_text(
                '@"%TEST_JUNIE_PYTHON%" "%TEST_JUNIE_SCRIPT%"\n@exit /b %errorlevel%\n', encoding="ascii",
            )
            self.enterContext(patch.dict(os.environ, {
                "TEST_JUNIE_PYTHON": sys.executable, "TEST_JUNIE_SCRIPT": str(script),
            }))
            command = [str(launcher), mode, "fixture"]

        sibling = subprocess.Popen([sys.executable, "-c", "input()"], stdin=subprocess.PIPE)
        control = execution.ExecutionControl(lambda _: None)
        connection = None
        parent = None
        launch = junie_cli._launch_process
        wait_for_launcher = subprocess.Popen.wait

        def start(argv, **streams):
            nonlocal connection, parent
            if connection is not None:
                wait_for_disconnect()
                connection.close()
                permit.unlink()
            with patch.dict(os.environ, {"TEST_JUNIE_SKIP_TOKEN": "1" if retry and parent is None else "0"}):
                parent = launch(argv, **streams)
            connection, _ = listener.accept()
            connection.settimeout(5)
            self.assertEqual(connection.recv(1), b"r")
            permit.touch()
            if cancel == "live":
                self.assertIsNone(parent.returncode)
                control.cancelled.set()
            elif cancel is None:
                # The launcher must be known to have exited before any token-based early stop.
                parent.wait(timeout=5)
            return parent

        def wait_then_cancel(process, timeout):
            code = wait_for_launcher(process, timeout)
            control.cancelled.set()
            return code

        def wait_for_disconnect():
            try:
                self.assertEqual(connection.recv(1), b"")
            except ConnectionResetError:
                # TerminateJobObject can reset the socket instead of sending EOF.
                pass

        accepted = Mock()
        closed = Mock()
        observer = junie_cli.session_closed_observer.set(closed)
        try:
            with (execution.control_scope(control), patch.object(junie_cli, "_launch_process", side_effect=start) as launches):
                if fail:
                    events = Mock(path=None, usage=[0] * len(junie_cli.USAGE_FIELDS), result="would be adopted")
                    events.read_new_events.return_value = True
                    with (patch.object(junie_cli, "_JunieSessionEvents", return_value=events),
                          patch.object(JunieProcess, "stop", side_effect=OSError("cannot confirm")),
                          self.assertRaisesRegex(junie_cli.JunieStopError, "終了を確認できません")):
                        junie_cli.run_interactive(command, completion, "token", (target,), accept_result=accepted)
                    launches.assert_called_once()
                    accepted.assert_not_called()
                    closed.assert_not_called()
                    self.assertTrue(junie_cli.has_completion_record(completion, "token"))
                    # Simulate operator cleanup after the unconfirmed result.
                    connection.shutdown(socket.SHUT_WR)
                elif retry:
                    events = Mock(path=None, usage=[0] * len(junie_cli.USAGE_FIELDS), result=None)
                    events.read_new_events.return_value = True
                    with patch.object(junie_cli, "_JunieSessionEvents", return_value=events):
                        self.assertEqual(junie_cli.run_interactive(command, completion, "token", (target,)), 0)
                    self.assertEqual(launches.call_count, 2)
                elif cancel:
                    if cancel == "after_exit":
                        self.enterContext(patch.object(JunieProcess, "wait", wait_then_cancel))
                    with self.assertRaises(execution.OperationCancelled):
                        junie_cli.run_interactive(command, completion, "token", (target,))
                else:
                    self.assertEqual(junie_cli.run_interactive(command, completion, "token", (target,)), 0)
                    self.assertTrue(junie_cli.has_completion_record(completion, "token"))
                    try:
                        connection.sendall(b"w")
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                wait_for_disconnect()
                self.assertEqual(closed.call_count, 0 if fail else launches.call_count)
                self.assertEqual(target.read_text(encoding="utf-8"), "before exit")
                self.assertIsNone(sibling.poll())
        finally:
            junie_cli.session_closed_observer.reset(observer)
            permit.touch()
            if connection is not None:
                connection.close()
            if parent is not None:
                if parent.poll() is None:
                    parent.kill()
                parent.wait(timeout=5)
            if sibling.poll() is None:
                try:
                    sibling.communicate(b"done\n", timeout=5)
                except subprocess.TimeoutExpired:
                    sibling.kill()
                    sibling.wait(timeout=5)
                    raise
