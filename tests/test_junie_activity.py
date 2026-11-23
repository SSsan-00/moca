from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import time
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from migration_harness import execution
from migration_harness.junie import junie_cli


def line(entry):
    return json.dumps(entry, ensure_ascii=False).encode("utf-8") + b"\n"


def prompt(token="this-launch"):
    return {"kind": "UserPromptEvent", "prompt": f"private prompt {token}"}


def task(task_id="task"):
    return {"kind": "TaskStartedEvent", "taskId": task_id}


def agent(kind, task_id="task", **fields):
    return {"kind": "SessionA2uxEvent", "taskId": task_id, "event": {
        "agentEvent": {"kind": kind, **fields},
    }}


def response(task_id="task"):
    return agent("LlmResponseMetadataEvent", task_id, modelUsage=[{
        "inputTokens": 10, "outputTokens": 4, "cacheInputTokens": 5, "cacheCreateTokens": 3,
    }])


@contextmanager
def count_event_bytes():
    """Measure bytes returned by the readers, independently of OS filesystem caching."""
    total = [0]
    original = Path.open

    class Reader:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def read(self, *args):
            value = self.stream.read(*args)
            total[0] += len(value)
            return value

        def readline(self, *args):
            value = self.stream.readline(*args)
            total[0] += len(value)
            return value

    def open_path(path, *args, **kwargs):
        stream = original(path, *args, **kwargs)
        return Reader(stream) if path.name == "events.jsonl" and args == ("rb",) else stream

    with patch.object(Path, "open", open_path):
        yield total


def benchmark_event_reads(polls=12, rows=4096):
    """Reproduce the former unbound scan over a late-visible historical log.

    Existing files already cost zero reads before this change; the repeated-read
    case is a candidate appearing during startup without a complete user prompt.
    """
    history = line({"kind": "SessionA2uxEvent", "padding": "x" * 980}) * rows
    with TemporaryDirectory() as directory, patch.dict(os.environ, {"JUNIE_HOME": directory}):
        root = Path(directory) / "sessions"
        old = root / "old" / "events.jsonl"
        old.parent.mkdir(parents=True)
        old.write_bytes(history)
        with count_event_bytes() as total:
            events = junie_cli._JunieSessionEvents("this-launch")
            for _ in range(polls):
                events.read_new_events()
        existing_bytes = total[0]
        candidate = root / "late-visible" / "events.jsonl"
        candidate.parent.mkdir()
        candidate.write_bytes(history)
        with count_event_bytes() as total:
            # The original reader restarted every unmatched candidate at byte 0.
            for _ in range(polls):
                with candidate.open("rb") as stream:
                    for raw in iter(stream.readline, b""):
                        if not raw.endswith(b"\n"):
                            break
                        entry = json.loads(raw)
                        if isinstance(entry, dict) and entry.get("kind") == "UserPromptEvent":
                            break
        before = total[0]
        with count_event_bytes() as total:
            for _ in range(polls):
                events.read_new_events()
        after = total[0]
        with candidate.open("ab") as stream:
            stream.write(line(prompt()) + line(task()) + line(response()))
        events.read_new_events()
        return {
            "polls": polls, "history_bytes": len(history), "preexisting_bytes": existing_bytes,
            "before_bytes": before, "after_bytes": after, "usage_after_append": events.usage,
        }


class JunieEventTests(unittest.TestCase):
    def test_approval_wait_reports_fixed_instruction_without_request_content(self):
        entry = agent("ViewFilesBlockUpdatedEvent", approvalRequest={"path": "PRIVATE"})
        entry["event"]["state"] = "INPUT_REQUIRED"
        self.assertEqual(junie_cli._event_activity(entry),
                         "Junieが操作の確認を待っています。起動した端末で確認してください。")

    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.enterContext(patch.dict(os.environ, {"JUNIE_HOME": directory.name}))
        self.messages = []
        self.control = execution.ExecutionControl(self.messages.append)
        self.enterContext(execution.control_scope(self.control))

    def write(self, name, *entries, raw=b""):
        path = self.root / "sessions" / name / "events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"".join(map(line, entries)) + raw)
        return path

    def append(self, path, *entries, raw=b""):
        with path.open("ab") as stream:
            stream.write(b"".join(map(line, entries)) + raw)

    def test_headless_events_match_new_session_by_project_and_count_untagged_usage(self):
        project = self.root / "anchor workspace"
        usage = response()
        del usage["taskId"]
        old = self.write("old", task(), usage)
        old.with_name("summary.json").write_bytes(line({"projectDir": str(project)}))
        events = junie_cli._JunieSessionEvents("this-launch", headless_project=project)
        self.append(old, usage)
        unrelated = self.write("unrelated", task(), usage)
        unrelated.with_name("summary.json").write_bytes(line({"projectDir": str(self.root / "elsewhere")}))
        ours = self.write("ours", task(), usage)

        self.assertFalse(events.read_new_events())
        self.assertIsNone(events.path)
        ours.with_name("summary.json").write_bytes(line({"projectDir": str(project)}))
        self.assertFalse(events.read_new_events())
        self.assertEqual(events.path, ours)
        self.assertEqual(events.usage, [18, 4, 5, 3, 1])

    def test_only_fixed_metadata_is_reported_and_repeated_status_is_coalesced(self):
        events = junie_cli._JunieSessionEvents("this-launch")
        private = "PRIVATE thoughts prompt command arguments /private/path"
        self.write("ours", prompt(), task(),
                   agent("AgentThoughtBlockUpdatedEvent", text=private),
                   agent("AgentCurrentStatusUpdatedEvent", text=private),
                   agent("ToolBlockUpdatedEvent", toolType=" READ_file ", text=private, details=private),
                   agent("ToolBlockUpdatedEvent", toolType=private, toolName=private),
                   agent("ToolBlockUpdatedEvent", toolType={"name": private}),
                   agent("TerminalBlockUpdatedEvent", command=private, output=private),
                   agent("ViewFilesBlockUpdatedEvent", files=[private]),
                   agent("FileChangesBlockUpdatedEvent", changes=[private]),
                   response(), response())
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.assertFalse(events.read_new_events())
            self.assertFalse(events.read_new_events())
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual([entry["message"] for entry in self.messages], [
            "Junieのセッションに更新がありました。", "Junieからファイルの読み取りの報告が届きました。",
            "Junieからツールの実行の報告が届きました。", "Junieからコマンドの実行の報告が届きました。",
            "Junieからファイルの変更の報告が届きました。", "Junieのモデル応答を受信しました。",
        ])
        self.assertTrue(all(entry["kind"] == "activity" for entry in self.messages))
        self.assertNotIn("PRIVATE", json.dumps(self.messages))
        self.assertEqual(events.usage, [36, 8, 10, 6, 2])

    def test_other_sessions_tasks_and_non_string_prompts_cannot_match(self):
        events = junie_cli._JunieSessionEvents("this-launch")
        self.write("other", prompt("different-launch"), task(), response())
        self.write("invalid", {"kind": "UserPromptEvent", "prompt": {"this-launch": True}}, task(), response())
        self.assertFalse(events.read_new_events())
        self.assertIsNone(events.path)
        self.assertIsNone(events.last_event_at)
        self.assertEqual(self.messages, [])
        current = self.write("ours", prompt(), task(), response("other-task"))
        events.read_new_events()
        self.assertEqual(events.path, current)
        self.assertEqual(events.usage, [0] * 5)
        self.assertEqual(len(self.messages), 1)

    def test_result_comes_only_from_the_matching_completed_main_agent(self):
        def result(text, *, task_id="task", state="COMPLETED", cancelled=False, agent_kind="MainAgent"):
            event = agent("ResultBlockUpdatedEvent", task_id, result=text, cancelled=cancelled,
                          agent={"kind": agent_kind})
            event["event"]["state"] = state
            return event

        events = junie_cli._JunieSessionEvents("this-launch")
        self.write("other", prompt("other-launch"), task(), result("other session"))
        current = self.write("ours", prompt(), task(), result("other task", task_id="other"),
                             result("partial", state="IN_PROGRESS"), result("cancelled", cancelled=True),
                             result("worker", agent_kind="SubAgent"))
        events.read_new_events()
        self.assertIsNone(events.result)
        self.append(current, result('{"decision": true}'))
        events.read_new_events()
        self.assertEqual(events.result, '{"decision": true}')
        self.assertNotIn("decision", json.dumps(self.messages))

    def test_existing_history_is_skipped_but_matching_append_is_counted(self):
        current = self.write("existing", prompt(), task(), response())
        with count_event_bytes() as total:
            events = junie_cli._JunieSessionEvents("this-launch")
            events.read_new_events()
        self.assertEqual(total[0], 0)
        self.append(current, prompt(), task(), response())
        events.read_new_events()
        events.read_new_events()
        self.assertEqual(events.usage, [18, 4, 5, 3, 1])

    def test_partial_history_at_launch_is_not_mistaken_for_current_event(self):
        current = self.write("existing", raw=line(prompt())[:-2])
        events = junie_cli._JunieSessionEvents("this-launch")
        self.append(current, raw=b"}\n" + line(task()) + line(response()))
        events.read_new_events()
        self.assertIsNone(events.path)
        self.assertEqual(events.usage, [0] * 5)
        self.append(current, prompt(), task(), response())
        events.read_new_events()
        self.assertEqual(events.usage, [18, 4, 5, 3, 1])

    def test_partial_lines_invalid_records_and_appends_are_read_once(self):
        events = junie_cli._JunieSessionEvents("this-launch")
        current = self.write("ours", prompt(), task(), raw=b"not json\n[]\n\xff\n")
        raw = line({**response(), "extra": "日本語"})
        split = raw.index("日".encode("utf-8")) + 1
        with count_event_bytes() as total:
            events.read_new_events()
            before = total[0]
            self.append(current, raw=raw[:split])
            for _ in range(4):
                events.read_new_events()
            self.assertEqual(total[0] - before, split)
            self.assertEqual(events.usage, [0] * 5)
            self.append(current, raw=raw[split:-1])
            events.read_new_events()
            self.assertEqual(events.usage, [0] * 5)
            self.append(current, raw=b"\n")
            events.read_new_events()
            events.read_new_events()
            self.assertEqual(total[0] - before, len(raw))
        self.assertEqual(events.usage, [18, 4, 5, 3, 1])

    def test_later_matching_prompt_and_follow_up_tasks_keep_session_usage(self):
        events = junie_cli._JunieSessionEvents("this-launch")
        current = self.write("reused", prompt("different-launch"), task(), response())
        events.read_new_events()
        self.append(current, prompt(), task(), response())
        events.read_new_events()
        self.append(current, prompt("different-launch"), task(), response())
        events.read_new_events()
        self.assertEqual(events.usage, [36, 8, 10, 6, 2])
        self.assertEqual(events.path, current)
        self.write("another-session", prompt("different-launch"), task(), response())
        self.append(current, prompt(), task("next"), response("next"))
        events.read_new_events()
        self.assertEqual(events.usage, [54, 12, 15, 9, 3])

    def test_replaced_or_truncated_logs_require_fresh_token_match(self):
        for replace in (False, True):
            with self.subTest(replace=replace):
                events = junie_cli._JunieSessionEvents("this-launch")
                current = self.write(f"current-{replace}", prompt(), task(), response(),
                                     agent("AgentThoughtBlockUpdatedEvent", text="x" * 1024))
                events.read_new_events()
                if replace:
                    replacement = current.with_suffix(".new")
                    replacement.write_bytes(line(task()) + line(response()))
                    replacement.replace(current)
                else:
                    current.write_bytes(line(task()) + line(response()))
                events.read_new_events()
                self.assertIsNone(events.path)
                self.append(current, prompt(), task(), response())
                events.read_new_events()
                self.assertEqual(events.usage, [36, 8, 10, 6, 2])

    def test_usage_validation_and_connection_failure_keep_their_contract(self):
        events = junie_cli._JunieSessionEvents("this-launch")
        failure = agent("AgentFailureEvent", errorCode="ConnectionFailed", agent={"kind": "MainAgent"})
        failure["event"]["state"] = "FAILED"
        current = self.write("ours", prompt(), task(),
                             agent("LlmResponseMetadataEvent", modelUsage=[
                                 {"inputTokens": True, "outputTokens": 2},
                                 {"inputTokens": 1, "outputTokens": -1},
                                 {"inputTokens": 1.5, "outputTokens": 2},
                                 {"inputTokens": 1, "outputTokens": 2, "cacheInputTokens": None},
                             ]), response(), failure, response())
        self.assertTrue(events.read_new_events())
        self.assertEqual(events.usage, [18, 4, 5, 3, 1])
        self.assertFalse(events.read_new_events())
        self.assertEqual(events.usage, [36, 8, 10, 6, 2])
        self.assertEqual(events.path, current)

    def test_cancel_mid_scan_retains_unread_usage_for_final_flush(self):
        events = junie_cli._JunieSessionEvents("this-launch")
        current = self.write("ours", prompt(), task(), response(), response())
        original = json.loads

        def parse(raw):
            entry = original(raw)
            if entry.get("kind") == "TaskStartedEvent":
                self.control.cancelled.set()
            return entry

        with patch.object(junie_cli.json, "loads", side_effect=parse), self.assertRaises(execution.OperationCancelled):
            events.read_new_events()
        self.assertLess(events.cursors[current].offset, current.stat().st_size)
        self.assertEqual(events.usage, [0] * 5)
        events.read_new_events(cancellable=False)
        self.assertEqual(events.usage, [36, 8, 10, 6, 2])

    def test_malformed_and_other_task_records_do_not_reset_activity_clock(self):
        events = junie_cli._JunieSessionEvents("this-launch")
        current = self.write("ours", prompt(), task())
        with patch.object(junie_cli.time, "monotonic", return_value=1):
            events.read_new_events()
        self.messages.clear()
        self.append(current, {}, {"kind": []}, {"kind": ""}, response("other-task"))
        with patch.object(junie_cli.time, "monotonic", return_value=10):
            events.read_new_events()
        self.assertEqual(events.last_event_at, 1)
        self.assertEqual(self.messages, [])

    def test_large_unmatched_candidate_is_not_reparsed_each_poll(self):
        result = benchmark_event_reads()
        self.assertEqual(result["preexisting_bytes"], 0)
        self.assertEqual(result["before_bytes"], result["history_bytes"] * result["polls"])
        self.assertEqual(result["after_bytes"], result["history_bytes"])
        self.assertEqual(result["usage_after_append"], [18, 4, 5, 3, 1])


class JunieControlTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.enterContext(patch.dict(os.environ, {"JUNIE_HOME": directory.name}))
        self.messages = []
        self.control = execution.ExecutionControl(self.messages.append)
        self.enterContext(execution.control_scope(self.control))
        self.restored = Mock()
        self.enterContext(patch.object(junie_cli, "_save_terminal_mode", return_value=self.restored))

    def run_session(self, **kwargs):
        return junie_cli._run_session(["junie", "--prompt", "PRIVATE"], self.root / "done", "this-launch", (), **kwargs)

    def test_cancel_before_launch_never_starts_process(self):
        self.control.cancelled.set()
        with patch.object(junie_cli.subprocess, "Popen") as launch:
            with self.assertRaises(execution.OperationCancelled):
                self.run_session()
            with self.assertRaises(execution.OperationCancelled):
                junie_cli._launch_process(["junie"])
        launch.assert_not_called()

    def test_validated_final_result_closes_session_and_invalid_result_also_stops_child(self):
        for reject in (False, True):
            with self.subTest(reject=reject):
                self.restored.reset_mock()
                completion = self.root / "done"
                completion.unlink(missing_ok=True)
                path = self.root / "sessions" / str(reject) / "events.jsonl"
                process = Mock(returncode=None)
                process.poll.side_effect = lambda: process.returncode
                result = agent("ResultBlockUpdatedEvent", result='{"decision": true}', cancelled=False,
                               agent={"kind": "MainAgent"})
                result["event"]["state"] = "COMPLETED"

                def wait(timeout):
                    if not path.exists():
                        path.parent.mkdir(parents=True)
                        path.write_bytes(line(prompt()) + line(task()) + line(response()) + line(result))
                    raise subprocess.TimeoutExpired("junie", timeout)

                def accept(answer):
                    self.assertEqual(answer, '{"decision": true}')
                    if reject:
                        raise ValueError("invalid decision")
                    completion.write_text("this-launch")

                def close(child):
                    self.assertIs(child, process)
                    process.returncode = 0
                    return 0

                process.wait.side_effect = wait
                callback, usage = Mock(side_effect=accept), []
                with (patch.object(junie_cli, "_launch_process", return_value=process),
                      patch.object(junie_cli, "_close_junie", side_effect=close) as closer):
                    if reject:
                        with self.assertRaisesRegex(ValueError, "invalid decision"):
                            self.run_session(accept_result=callback, report_usage=usage.append)
                    else:
                        self.assertEqual(self.run_session(accept_result=callback, report_usage=usage.append), 0)
                callback.assert_called_once()
                closer.assert_called_once_with(process)
                self.restored.assert_called_once_with()
                self.assertEqual(usage, [[0] * 5, [18, 4, 5, 3, 1]])
                self.assertEqual(completion.exists(), not reject)

    def test_cancel_during_poll_closes_child_flushes_usage_and_restores_terminal(self):
        process = Mock(returncode=None)
        process.poll.side_effect = lambda: process.returncode
        usage = []

        def wait(timeout):
            path = self.root / "sessions" / "ours" / "events.jsonl"
            path.parent.mkdir(parents=True)
            path.write_bytes(line(prompt()) + line(task()) + line(response()))
            self.control.cancelled.set()
            raise subprocess.TimeoutExpired("junie", timeout)

        def close(child):
            self.assertIs(child, process)
            process.returncode = -15
            return -15

        process.wait.side_effect = wait
        with (patch.object(junie_cli, "_launch_process", return_value=process),
              patch.object(junie_cli, "_close_junie", side_effect=close) as closer,
              self.assertRaises(execution.OperationCancelled) as stopped):
            self.run_session(report_usage=usage.append)
        self.assertIsInstance(stopped.exception, KeyboardInterrupt)
        closer.assert_called_once_with(process)
        self.restored.assert_called_once_with()
        self.assertEqual(usage, [[0] * 5, [18, 4, 5, 3, 1]])

    def test_cleanup_failure_is_reported_without_claiming_a_successful_stop(self):
        process = Mock()
        process.poll.return_value = None
        cancelled = execution.OperationCancelled("stop")
        process.wait.side_effect = cancelled
        with (patch.object(junie_cli, "_launch_process", return_value=process),
              patch.object(junie_cli, "_close_junie", side_effect=OSError("close failed")),
              self.assertRaises(junie_cli.JunieStopError) as stopped):
            self.run_session()
        self.assertIn("終了を確認できませんでした", str(stopped.exception))
        self.assertIsInstance(stopped.exception.__cause__, OSError)
        self.restored.assert_not_called()

    def test_cancellation_during_retry_wait_prevents_relaunch(self):
        def sleep(seconds):
            self.assertEqual(seconds, 0.25)
            self.control.cancelled.set()

        with (patch.object(junie_cli, "_run_session", side_effect=junie_cli.JunieConnectionError("offline")) as launch,
              patch.object(junie_cli.time, "sleep", side_effect=sleep),
              redirect_stdout(io.StringIO()), self.assertRaises(execution.OperationCancelled)):
            junie_cli.run_interactive(["junie", "--prompt", "PRIVATE"], self.root / "done", "this-launch", ())
        launch.assert_called_once()
        self.assertEqual(self.messages[-1]["message"], "Junieの通信に失敗しました。再試行を待っています。")

    def test_wrapped_unconfirmed_stop_prevents_connection_retry(self):
        error = junie_cli.JunieConnectionError("cleanup connection error")
        error.__context__ = junie_cli.JunieStopError("termination unconfirmed")
        with (patch.object(junie_cli, "_run_session", side_effect=[error, 0]) as launch,
              patch.object(junie_cli.time, "sleep") as sleep,
              self.assertRaisesRegex(junie_cli.JunieConnectionError, "cleanup connection error")):
            junie_cli.run_interactive(["junie", "--prompt", "PRIVATE"], self.root / "done", "this-launch", ())
        launch.assert_called_once()
        sleep.assert_not_called()

    def test_controlled_retries_keep_original_total_delay_and_prompt(self):
        command = junie_cli.build_command(self.root, "PRIVATE", 30, model="sonnet", effort="high")
        with (patch.object(junie_cli, "_run_session", side_effect=[
                  junie_cli.JunieConnectionError("offline"), junie_cli.JunieConnectionError("offline"), 0,
              ]) as launch, patch.object(junie_cli.time, "sleep") as sleep, redirect_stdout(io.StringIO())):
            self.assertEqual(junie_cli.run_interactive(command, self.root / "done", "this-launch", ()), 0)
        self.assertEqual(launch.call_count, 3)
        self.assertEqual(sum(call.args[0] for call in sleep.call_args_list), 4)
        self.assertTrue(all(call.args[0] == 0.25 for call in sleep.call_args_list))
        self.assertEqual(command[-1], "PRIVATE")
        for call in launch.call_args_list:
            self.assertEqual(call.args[0][:-1], command[:-1])

    def test_heartbeat_reports_liveness_and_time_since_actual_event_without_printing(self):
        clock = [0.0]
        process = Mock(returncode=None)
        process.poll.side_effect = lambda: process.returncode
        ticks = iter([2, 3, 4, 6, 9, 10])

        def wait(timeout):
            clock[0] = next(ticks)
            if clock[0] == 4:
                path = self.root / "sessions" / "ours" / "events.jsonl"
                path.parent.mkdir(parents=True)
                path.write_bytes(line(prompt()) + line(task()) + line(response()))
            if clock[0] == 10:
                process.returncode = 0
                return 0
            raise subprocess.TimeoutExpired("junie", timeout)

        process.wait.side_effect = wait
        stdout = io.StringIO()
        with (patch.object(junie_cli, "_launch_process", return_value=process),
              patch.object(junie_cli.time, "monotonic", side_effect=lambda: clock[0]), redirect_stdout(stdout)):
            self.assertEqual(self.run_session(), 0)
        self.assertEqual(stdout.getvalue(), "")
        heartbeats = [entry["message"] for entry in self.messages if entry["kind"] == "heartbeat"]
        self.assertEqual(heartbeats, [
            "Junieのプロセスは起動中です（起動から3秒、セッションのイベントはまだ確認できていません）。",
            "Junieのプロセスは起動中です（最後のイベントから2秒）。",
            "Junieのプロセスは起動中です（最後のイベントから5秒）。",
        ])
        self.assertNotIn("PRIVATE", json.dumps(self.messages))
        self.restored.assert_called_once_with()

    def test_windows_cleanup_still_terminates_whole_launcher_tree(self):
        process = Mock(pid=123)
        process.poll.return_value = None
        process.wait.return_value = 1
        # Construct Path before mocking os.name (Path chooses its platform at construction).
        system_root = Path(self.root)
        with (patch.object(junie_cli.os, "name", "nt"), patch.dict(os.environ, {"SystemRoot": str(system_root)}),
              patch.object(junie_cli, "Path", return_value=system_root),
              patch.object(junie_cli.subprocess, "run") as kill):
            self.assertEqual(junie_cli._close_junie(process), 1)
        self.assertEqual(kill.call_args.args[0][1:], ["/PID", "123", "/T", "/F"])
        self.assertTrue(kill.call_args.kwargs["check"])

    @unittest.skipIf(os.name == "nt", "POSIX descendant cleanup")
    def test_posix_cleanup_kills_descendants_but_not_siblings(self):
        process = Mock(pid=100)
        process.poll.return_value = None
        process.wait.return_value = -15
        listing = "100 50\n200 100\n300 200\n400 50\n"
        with (patch.object(junie_cli.subprocess, "run", return_value=Mock(stdout=listing)),
              patch.object(junie_cli.os, "kill") as kill):
            self.assertEqual(junie_cli._close_junie(process), -15)
        self.assertEqual([call.args for call in kill.call_args_list], [
            (300, signal.SIGKILL), (200, signal.SIGKILL),
        ])
        process.terminate.assert_called_once_with()

    @unittest.skipIf(os.name == "nt", "POSIX descendant cleanup")
    def test_real_posix_launcher_and_child_stop_on_dashboard_cancellation(self):
        marker = self.root / "child.pid"
        child_script = (
            "import os, signal, time; from pathlib import Path; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            f"Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(30)"
        )
        launcher_script = (
            "import subprocess, sys; "
            f"child = subprocess.Popen([sys.executable, '-c', {child_script!r}]); child.wait()"
        )
        child_pid = None
        process = subprocess.Popen([sys.executable, "-c", launcher_script])
        try:
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(marker.exists(), "test child did not start")
            child_pid = int(marker.read_text())

            def launch(_, **streams):
                self.control.cancelled.set()
                return process

            with patch.object(junie_cli, "_launch_process", side_effect=launch), self.assertRaises(execution.OperationCancelled):
                self.run_session()
            self.assertIsNotNone(process.poll())
            # A killed descendant can briefly remain as a zombie until the launcher/OS reaps it.
            result = subprocess.run(["ps", "-p", str(child_pid), "-o", "stat="], capture_output=True, text=True)
            self.assertTrue(not result.stdout.strip() or result.stdout.strip().startswith("Z"), result.stdout)
            self.restored.assert_called_once_with()
        finally:
            if child_pid is not None:
                try:
                    os.kill(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)


if __name__ == "__main__":
    if sys.argv[1:] == ["--benchmark"]:
        print(json.dumps(benchmark_event_reads(), indent=2))
    else:
        unittest.main()
