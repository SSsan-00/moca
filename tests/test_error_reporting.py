from __future__ import annotations

import io
import json
import os
import shlex
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen

from migration_harness import cli
from migration_harness.web import dashboard
from migration_harness.errors import format_error, safe_command
from migration_harness.workflow import RunInterrupted, RunScriptError


class ErrorFormattingTests(unittest.TestCase):
    def test_frames_and_explicit_chain_without_source_locals_or_environment(self):
        def fail():
            private_local = "local-value-must-not-appear"
            raise OSError("missing executable")  # source-comment-must-not-appear

        try:
            try:
                fail()
            except OSError as cause:
                raise RuntimeError("launch failed") from cause
        except RuntimeError as error:
            with (
                patch.dict(os.environ, {"PRIVATE_TEST_SECRET": "environment-value-must-not-appear"}),
                patch("linecache.getline", side_effect=AssertionError("source read")),
            ):
                details = format_error(error)
        for expected in ("OSError: missing executable", "RuntimeError: launch failed", "direct cause", __file__, "in fail", "line "):
            self.assertIn(expected, details)
        for secret in ("local-value-must-not-appear", "environment-value-must-not-appear", "source-comment-must-not-appear", "raise OSError"):
            self.assertNotIn(secret, details)

    def test_implicit_and_suppressed_context_are_retained(self):
        try:
            try:
                raise ValueError("original failure")
            except ValueError:
                raise RuntimeError("wrapper") from None
        except RuntimeError as error:
            details = format_error(error)
        self.assertIn("ValueError: original failure", details)
        self.assertIn("During handling", details)
        self.assertIn("RuntimeError: wrapper", details)

    def test_cycles_and_exception_groups_keep_each_failure(self):
        inner = ValueError("first cause")
        outer = RuntimeError("second cause")
        outer.__cause__ = inner
        inner.__context__ = outer
        details = format_error(ExceptionGroup("parallel failures", [outer, OSError("third cause")]))
        for message in ("first cause", "second cause", "third cause"):
            self.assertEqual(details.count(message), 1)

    def test_process_diagnostics_include_code_both_streams_and_context(self):
        command = ["git", "--token", "token-private", "--prompt=prompt-private", "status"]
        error = subprocess.CalledProcessError(
            17, command, output=b"stdout token-private \xff", stderr=b"stderr prompt-private",
        )
        cwd = Path("/work/project")
        details = format_error(error, cwd=cwd, executable="/bin/git")
        for expected in ("CalledProcessError", "Return code: 17", "stdout:", "stderr:", "Command: git", "status", str(cwd), "/bin/git", "\ufffd"):
            self.assertIn(expected, details)
        self.assertNotIn("token-private", details)
        self.assertNotIn("prompt-private", details)

    def test_explicit_command_redacts_echoes_in_every_chain_message_and_output(self):
        prompt = "private source with 'quotes'\nand another line"
        command = ["junie", "-a", "auth-private", "--task", prompt]
        cause = subprocess.CalledProcessError(1, ["wrapper"], output=repr(prompt), stderr="auth-private")
        error = OSError(f"launch failed: {prompt}; {json.dumps(prompt)}; auth-private")
        error.__cause__ = cause
        details = format_error(error, command=command, cwd="/work")
        for secret in (prompt, repr(prompt)[1:-1], json.dumps(prompt)[1:-1], "auth-private"):
            self.assertNotIn(secret, details)
        self.assertIn("wrapper", details)
        self.assertIn("Return code: 1", details)
        self.assertEqual(command[-1], prompt)

    def test_launcher_attributes_redact_cause_and_include_zero_return_code(self):
        error = OSError("interactive terminal unavailable")
        error.command = ["junie", "--api-key", "launcher-private"]
        error.cwd = "/work/project"
        error.executable = "/bin/junie"
        error.returncode = 0
        error.__cause__ = ValueError("launcher-private rejected")
        details = format_error(error)
        for expected in ("Return code: 0", "Command: junie", "Working directory: /work/project", "Executable: /bin/junie"):
            self.assertIn(expected, details)
        self.assertNotIn("launcher-private", details)

    def test_preformatted_cause_is_not_duplicated(self):
        try:
            raise OSError("missing launcher")
        except OSError as cause:
            error = RuntimeError("launch diagnostics\n" + format_error(cause))
            error.__cause__ = cause
        details = format_error(error)
        self.assertEqual(details.count("OSError: missing launcher"), 1)
        self.assertIn("launch diagnostics", details)

    def test_safe_command_redacts_aliases_and_keeps_paths_and_input(self):
        for flag in ("--token", "--api-key", "--api_key", "--password", "--client-secret", "--access-token", "--authorization", "-a", "--task", "--system-prompt"):
            for equals in (False, True):
                with self.subTest(flag=flag, equals=equals):
                    arguments = [flag + "=private-value"] if equals else [flag, "private-value"]
                    command = ["junie", *arguments, "--project", r"C:\work tree\project"]
                    original = command.copy()
                    rendered = safe_command(command)
                    self.assertNotIn("private-value", rendered)
                    self.assertIn("[REDACTED]", rendered)
                    self.assertIn(r"C:\work tree\project", rendered)
                    self.assertEqual(command, original)

    def test_command_strings_and_echoed_argv_are_redacted(self):
        for command in (
            'junie --token=private-token --prompt "private prompt" --project "C:\\work tree"',
            "junie -a 'private-token' --task='private prompt' --project 'C:\\work tree'",
            repr(["junie", "--token", "private-token", "--prompt", "private prompt", "--project", "project"]),
        ):
            with self.subTest(command=command):
                rendered = safe_command(command)
                self.assertNotIn("private-token", rendered)
                self.assertNotIn("private prompt", rendered)
                self.assertIn("--project", rendered)

    def test_shell_quoted_prompt_with_embedded_quotes_is_fully_hidden(self):
        command = shlex.join(["junie", "--prompt", "first'private-rest", "--project", "project"])
        self.assertNotIn("private-rest", safe_command(command))

    def test_prompt_echo_with_nested_sensitive_flag_is_redacted_as_a_whole(self):
        prompt = "private-prefix --token nested-secret private-suffix"
        error = subprocess.CalledProcessError(2, ["junie", "--prompt", prompt], output=prompt, stderr=repr(prompt))
        details = format_error(error)
        for secret in ("private-prefix", "nested-secret", "private-suffix"):
            self.assertNotIn(secret, details)

    def test_command_strings_redact_punctuation_and_unclosed_quotes(self):
        for value in ("private,tail", "private]tail", "private)tail", '"private unclosed tail'):
            with self.subTest(value=value):
                self.assertNotIn("tail", safe_command("junie --prompt " + value))


class CliErrorReportingTests(unittest.TestCase):
    def test_removed_command_is_not_available(self):
        for command in ("prepare", "run", "resume", "view", "review", "anchors", "dry-run", "preview-context",
                        "ledger-review", "script-recovery", "repair-draft"):
            with (
                self.subTest(command=command),
                patch.object(sys, "argv", ["harness", command, "--config", "local.toml"]),
                redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as exited,
            ):
                cli.main()
            self.assertEqual(exited.exception.code, 2)

    def test_first_dashboard_setup_does_not_treat_missing_config_as_a_failure(self):
        with (
            TemporaryDirectory() as directory,
            patch.object(sys, "argv", ["harness", "dashboard", "--config", str(Path(directory) / "new.toml"), "--open-browser"]),
            patch.object(dashboard, "run_dashboard") as open_dashboard,
        ):
            cli.main()
        self.assertIsNone(open_dashboard.call_args.kwargs["config_error"])
        self.assertTrue(open_dashboard.call_args.kwargs["open_browser"])

    def test_existing_config_opens_the_browser_when_requested(self):
        with (
            patch.object(sys, "argv", ["harness", "dashboard", "--config", "local.toml", "--open-browser"]),
            patch.object(cli, "load_config", return_value={"config_path": "local.toml"}),
            patch.object(dashboard, "run_dashboard") as open_dashboard,
        ):
            cli.main()
        open_dashboard.assert_called_once_with({"config_path": "local.toml"}, open_browser=True)

    def test_exit_codes_and_full_diagnostics(self):
        cause = subprocess.CalledProcessError(7, ["git", "status"], output="out-detail", stderr="err-detail")
        for error in (RuntimeError("review failed"), cause):
            if error is not cause:
                error.__cause__ = cause
            with (
                self.subTest(error=error),
                patch.object(sys, "argv", ["harness", "rule-review", "--config", "local.toml", "--run-id", "saved-run"]),
                patch.object(cli, "load_config", return_value={}),
                patch("migration_harness.rules.rule_review.review_rules", side_effect=error),
                redirect_stderr(io.StringIO()) as stderr,
                self.assertRaises(SystemExit) as exited,
            ):
                cli.main()
            self.assertEqual(exited.exception.code, 2)
            for expected in ("CalledProcessError", "Return code: 7", "out-detail", "err-detail", "cli.py"):
                self.assertIn(expected, stderr.getvalue())

    def test_config_failures_open_recoverable_dashboard(self):
        with (
            patch.object(sys, "argv", ["harness", "dashboard", "--config", "local.toml"]),
            patch.object(cli, "load_config", side_effect=ValueError("invalid config field")),
            patch.object(dashboard, "run_dashboard") as open_dashboard,
        ):
            cli.main()
        self.assertIn("invalid config field", open_dashboard.call_args.kwargs["config_error"])
        self.assertIn("config_path", open_dashboard.call_args.args[0])

    def test_keyboard_interrupt_exit_code_is_preserved(self):
        with (
            patch.object(sys, "argv", ["harness", "dashboard", "--config", "local.toml"]),
            patch.object(cli, "load_config", side_effect=KeyboardInterrupt),
            redirect_stderr(io.StringIO()) as stderr,
            self.assertRaises(SystemExit) as exited,
        ):
            cli.main()
        self.assertEqual(exited.exception.code, 130)
        self.assertIn("Interrupted", stderr.getvalue())


class DashboardErrorReportingTests(unittest.TestCase):
    def open_dashboard(self, config, interactions, **kwargs):
        view = dashboard.Dashboard()
        terminal = io.StringIO()
        steps = iter(interactions)

        def poll(*, timeout):
            step = next(steps, None)
            if step is None:
                raise KeyboardInterrupt
            url = terminal.getvalue().splitlines()[0].removeprefix("Progress dashboard: ")
            return step(view, url)

        with (
            patch.object(dashboard, "Dashboard", return_value=view),
            patch.object(view.actions, "get", side_effect=poll),
            redirect_stdout(terminal),
        ):
            result = dashboard.run_dashboard(config, **kwargs)
        return view, result

    def assert_ready_error(self, view, expected):
        self.assertTrue(view.state["ready"])
        self.assertIn(expected, view.state["message"])
        self.assertIn(expected, view.state["error_details"])

    def test_browser_opens_ready_dashboard_and_failure_leaves_it_accessible(self):
        for outcome in (True, False, OSError("browser unavailable")):
            with self.subTest(outcome=outcome):
                opened_urls = []

                def open_browser(url, *, new):
                    opened_urls.append(url)
                    self.assertEqual(new, 2)
                    with urlopen(url, timeout=3) as response:
                        self.assertIn(b"data-workspace", response.read())
                    if isinstance(outcome, OSError):
                        raise outcome
                    return outcome

                def inspect(view, url):
                    self.assertEqual(opened_urls, [url])
                    with urlopen(url, timeout=3) as response:
                        self.assertIn(b"data-workspace", response.read())
                    raise KeyboardInterrupt

                with patch.object(dashboard.webbrowser, "open", side_effect=open_browser):
                    self.open_dashboard({}, [inspect], open_browser=True)

    def test_initial_failure_stays_open_and_can_start_another_run(self):
        failure = subprocess.CalledProcessError(9, ["git", "worktree", "add"], stderr="worktree detail")
        view = dashboard.Dashboard()
        result = {"status": "prepared", "run_id": "saved-run"}
        action = {"action": "run", "unit_id": "example", "run_id": "saved-run"}
        with patch.object(dashboard, "run_with_junie", side_effect=[failure, result]):
            view.begin_operation()
            dashboard.execute_dashboard_action(view, {}, action)
            self.assert_ready_error(view, "worktree detail")
            self.assertIn("Return code: 9", view.state["message"])
            view.begin_operation()
            dashboard.execute_dashboard_action(view, {}, action)
        self.assertTrue(view.state["ready"])
        self.assertEqual(view.state["message"], "")
        self.assertEqual(view.state["error_details"], "")

    def test_saved_run_load_error_and_config_error_stay_visible(self):
        def inspect(view, url):
            self.assert_ready_error(view, "invalid saved record")
            self.assertFalse(view.state["resumable"])
            with urlopen(url, timeout=3) as response:
                self.assertIn(b"data-workspace", response.read())
            raise KeyboardInterrupt

        with patch.object(dashboard, "load_saved_run", side_effect=ValueError("invalid saved record")):
            self.open_dashboard({}, [inspect], run_id="broken")

        def config_inspect(view, url):
            self.assert_ready_error(view, "invalid project root")
            raise KeyboardInterrupt

        with patch.object(dashboard, "run_with_junie") as run:
            self.open_dashboard({}, [config_inspect], config_error="ValueError: invalid project root")
        run.assert_not_called()

    def test_script_failure_with_unreadable_record_stays_recoverable(self):
        view = dashboard.Dashboard()
        view.begin_operation()
        with (
            patch.object(dashboard, "run_with_junie", side_effect=RunScriptError("script failed", "saved-run")),
            patch.object(dashboard, "load_saved_run", side_effect=OSError("record unreadable")),
        ):
            dashboard.execute_dashboard_action(view, {}, {"action": "run", "run_id": "saved-run", "unit_id": "example"})
        self.assert_ready_error(view, "script failed")
        self.assertIn("record unreadable", view.state["message"])

    def test_initial_interruption_loads_saved_state_and_allows_resume(self):
        saved = {"status": "script_failed", "run_id": "saved-run"}
        view = dashboard.Dashboard()
        view.begin_operation()
        with (
            patch.object(dashboard, "run_with_junie", side_effect=RunInterrupted("saved-run")),
            patch.object(dashboard, "load_saved_run", return_value=(None, None, saved, None)),
        ):
            result = dashboard.execute_dashboard_action(view, {}, {"action": "run", "run_id": "saved-run", "unit_id": "example"})
        self.assertTrue(view.state["ready"])
        self.assertTrue(view.state["resumable"])
        self.assertEqual(view.state["run_id"], "saved-run")
        self.assertEqual(result, saved)

    def test_malformed_saved_state_does_not_close_dashboard(self):
        saved = {"status": "prepared", "run_id": "saved-run", "token_usage": [None]}

        def inspect(view, url):
            self.assertTrue(view.state["ready"])
            self.assertIn("Error", view.state["error_details"])
            raise KeyboardInterrupt

        with patch.object(dashboard, "load_saved_run", return_value=(None, None, saved, None)):
            self.open_dashboard({}, [inspect], run_id="saved-run")

    def test_settings_and_workspace_read_errors_keep_http_contract_and_diagnostics(self):
        def inspect(view, url):
            for endpoint, target, error in (
                ("settings", "_read_settings", PermissionError("cannot read settings.toml")),
                ("settings", "build_settings_draft", TypeError("invalid settings table")),
                ("workspace", "load_saved_run", ValueError("unused")),
            ):
                with self.subTest(endpoint=endpoint, target=target):
                    with patch.object(dashboard, target, side_effect=error):
                        with self.assertRaises(HTTPError) as raised:
                            urlopen(url + endpoint, timeout=3)
                    with raised.exception as response:
                        self.assertEqual(response.code, 409)
                        self.assertEqual(response.headers.get_content_type(), "text/plain")
                        details = response.read().decode()
                        self.assertIn("dashboard.py", details)
                        self.assertIn(str(error) if endpoint == "settings" else "TypeError", details)
            self.assertTrue(view.state["ready"])
            raise KeyboardInterrupt

        self.open_dashboard({"config_path": "missing.toml", "units": None}, [inspect])

    def test_review_loading_error_has_chain_and_is_cleared_after_recovery(self):
        view = dashboard.Dashboard()
        cause = OSError("cannot read checker.log")
        error = ValueError("review failed")
        error.__cause__ = cause
        result = {"status": "checker_needs_review", "run_id": "saved-run", "build_exit_code": 0}
        with patch.object(dashboard, "review_checker", side_effect=error):
            view.ready({"checker_command": ["checker"]}, result)
        self.assertIn("cannot read checker.log", view.state["review_error"])
        self.assertIn("review failed", view.state["review_error"])
        view.ready({}, {"status": "prepared"})
        self.assertNotIn("review_error", view.state)

    def test_failed_preserves_full_record_resume_decision(self):
        view = dashboard.Dashboard()
        saved = {
            "run_id": "saved-run", "status": "build_succeeded",
            "verification": {"checker_command": ["checker"]},
        }
        view.observe(saved)
        view.ready({"checker_command": None}, saved)
        self.assertFalse(view.state["resumable"])
        view.failed(ValueError("operation failed"))
        self.assertFalse(view.state["resumable"])
        self.assert_ready_error(view, "operation failed")


if __name__ == "__main__":
    unittest.main()
