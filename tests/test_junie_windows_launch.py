from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from migration_harness import junie_cli
from migration_harness.errors import format_error


class JunieLaunchTests(unittest.TestCase):
    def test_preferred_task_checks_the_help_before_selecting_a_mode(self):
        command = junie_cli.build_command(Path("project"), "anchor prompt", 7, guidelines=Path("rules.md"))
        completion = Path("project/complete.txt")
        for option in ("--task=<text>", "--task-file=<path>"):
            with (
                self.subTest(option=option),
                patch.object(junie_cli, "_launch_process") as launch,
                patch.object(junie_cli, "require_interactive_cli") as require_terminal,
                patch.object(junie_cli, "_run_session", return_value=0) as run,
            ):
                launch.side_effect = lambda argv, **streams: subprocess.Popen(
                    [sys.executable, "-c", f"print({('  ' + option)!r})"], **streams,
                )
                self.assertEqual(
                    junie_cli.run_interactive(command, completion, "token", (), prefer_task=True), 0,
                )
                self.assertEqual(launch.call_args.args[0], ["junie", "--help"])
                if option == "--task=<text>":
                    require_terminal.assert_not_called()
                    expected = [
                        "junie", "--project", "project", "--ide-guidelines", "rules.md",
                        "--max-steps", "7", "--task", "anchor prompt",
                    ]
                else:
                    require_terminal.assert_called_once_with()
                    expected = command
                run.assert_called_once_with(expected, completion, "token", (), report_usage=None)

    def test_native_launch_uses_resolved_path_and_inherits_terminal(self):
        executable = os.path.abspath("Junie Tools/junie.exe")
        command = ["junie", "--project", "space & (project)", "--prompt", 'say "hello"\n%PATH% & echo no']
        with (
            patch.object(junie_cli.shutil, "which", return_value=executable) as lookup,
            patch.object(junie_cli.subprocess, "Popen") as launch,
        ):
            self.assertIs(junie_cli._launch_process(command), launch.return_value)
        lookup.assert_called_once_with("junie")
        launch.assert_called_once_with([executable, *command[1:]], cwd=os.getcwd())
        self.assertEqual(command[0], "junie")

    def test_missing_executable_reports_context_restores_terminal_and_does_not_retry(self):
        restored = Mock()
        with (
            patch.object(junie_cli, "_save_terminal_mode", return_value=restored),
            patch.object(junie_cli, "_JunieSessionEvents"),
            patch.object(junie_cli.shutil, "which", return_value=None),
            patch.object(junie_cli.subprocess, "Popen") as launch,
            patch.object(junie_cli.time, "sleep") as delay,
            self.assertRaises(junie_cli.JunieLaunchError) as failed,
        ):
            junie_cli.run_interactive(["junie", "--prompt", "private prompt"], Path("complete"), "token", ())
        report = str(failed.exception)
        self.assertIsInstance(failed.exception.__cause__, FileNotFoundError)
        for expected in ("PATH", "Command:", "CWD:", os.getcwd(), "Executable: <not resolved>", "Traceback", "_resolve_executable"):
            self.assertIn(expected, report)
        self.assertNotIn("private prompt", report)
        launch.assert_not_called()
        delay.assert_not_called()
        restored.assert_called_once_with()

    def test_launch_error_keeps_chain_and_redacts_prompts_and_authentication(self):
        command = ["junie", "--auth", "secret-token", "--prompt", 'private "prompt"\nsecond line']
        executable = os.path.abspath("Junie Tools/junie.exe")

        def fail(*args, **kwargs):
            try:
                raise PermissionError(errno.EACCES, "access denied")
            except PermissionError as cause:
                raise OSError("launch failed for secret-token: " + command[-1]) from cause

        with (
            patch.dict(os.environ, {"UNRELATED_PRIVATE_ENV": "unrelated-secret"}),
            patch.object(junie_cli.shutil, "which", return_value=executable),
            patch.object(junie_cli.subprocess, "Popen", side_effect=fail),
            self.assertRaises(junie_cli.JunieLaunchError) as failed,
        ):
            junie_cli._launch_process(command)
        for report in (str(failed.exception), format_error(failed.exception)):
            for expected in ("PermissionError", "OSError", "Traceback", "direct cause", executable, "CWD:", "--auth", "--prompt"):
                self.assertIn(expected, report)
            for private in ("secret-token", "private", "second line", "unrelated-secret", "UNRELATED_PRIVATE_ENV"):
                self.assertNotIn(private, report)

    def test_windows_batch_transports_prompt_project_and_guidelines_outside_cmd_text(self):
        prompt = '日本語 "quoted" & echo bad > x | exit\n%PATH% !NAME! ^ ()\r\nnext'
        project = 'C:\\space & ()\\%NAME% !NAME! 日本語\\'
        guidelines = project + '必須事項 & details.md'
        executable = r"C:\Junie Tools & (%)!^\Junie.BAT"
        command = junie_cli.build_command(Path(project), prompt, 2, guidelines=Path(guidelines))
        command[0] = executable
        with patch.dict(os.environ, {"SystemRoot": r"C:\Windows", "JUNIE_PROMPT": "old", "JUNIE_PROJECT": "old"}):
            before = dict(os.environ)
            line, interpreter, environment = junie_cli._windows_batch_command(command)
            self.assertEqual(dict(os.environ), before)
        self.assertEqual(interpreter, r"C:\Windows\System32\cmd.exe")
        self.assertIn(' /d /v:off /s /c ""%PHP_TO_CSHARP_JUNIE_LAUNCHER%"', line)
        self.assertTrue(line.endswith('""'))
        self.assertEqual(environment["JUNIE_PROMPT"], prompt)
        self.assertEqual(environment["JUNIE_PROJECT"], str(Path(project)))
        self.assertEqual(environment["JUNIE_IDE_GUIDELINES"], str(Path(guidelines)))
        self.assertEqual(environment["PHP_TO_CSHARP_JUNIE_LAUNCHER"], executable)
        self.assertEqual(environment["PHP_TO_CSHARP_JUNIE_ARG_1"], "--brave")
        self.assertIn('"%PHP_TO_CSHARP_JUNIE_ARG_1%"', line)
        self.assertNotIn(prompt, line)
        self.assertNotIn(project, line)
        self.assertNotIn(guidelines, line)
        self.assertNotIn(executable, line)
        self.assertNotIn("--prompt", line)

    def test_windows_batch_extensions_launch_cmd_explicitly_without_pipes(self):
        for extension in (".BAT", ".cMd"):
            executable = r"C:\Junie Tools\Junie" + extension
            with (
                self.subTest(extension=extension),
                patch.object(junie_cli.sys, "platform", "win32"),
                patch.object(junie_cli, "_resolve_executable", return_value=executable),
                patch.dict(os.environ, {"SystemRoot": r"C:\Windows"}),
                patch.object(junie_cli.subprocess, "Popen") as launch,
            ):
                junie_cli._launch_process(["junie", "--prompt", "hello"])
                self.assertIsInstance(launch.call_args.args[0], str)
                self.assertEqual(set(launch.call_args.kwargs), {"executable", "cwd", "env"})
                self.assertEqual(launch.call_args.kwargs["env"]["JUNIE_PROMPT"], "hello")

    def test_windows_equals_options_and_root_project_preserve_values(self):
        with patch.dict(os.environ, {"SystemRoot": r"C:\Windows"}):
            _, _, environment = junie_cli._windows_batch_command([
                r"C:\Junie.CMD", "--project=C:\\", '--prompt=a "b"\nc', '--ide-guidelines=C:\\資料 & rules.md',
            ])
        self.assertEqual(environment["JUNIE_PROJECT"], "C:\\")
        self.assertEqual(environment["JUNIE_PROMPT"], 'a "b"\nc')
        self.assertEqual(environment["JUNIE_IDE_GUIDELINES"], 'C:\\資料 & rules.md')

    def test_windows_batch_transports_task_text_in_environment(self):
        prompt = '対応行を提案してください。\n"quoted" & %PATH%'
        with patch.dict(os.environ, {"SystemRoot": r"C:\Windows"}):
            line, _, environment = junie_cli._windows_batch_command([
                r"C:\Junie.CMD", "--task", prompt,
            ])
        self.assertEqual(environment["JUNIE_TASK"], prompt)
        self.assertNotIn(prompt, line)
        self.assertNotIn("--task", line)

    def test_windows_batch_rejects_unsafe_or_silently_truncated_arguments(self):
        cases = (
            (["--prompt"], "requires a value"),
            (["--ide-guidelines"], "requires a value"),
            (["--prompt", "a\0b"], "NUL"),
            (["--prompt", "a" * 8192], "batch launcher limit"),
            (["--prompt", "😀" * 4096], "batch launcher limit"),
            (["--other", '" & exit'], "quotes or control"),
            (["--other", "line\nbreak"], "quotes or control"),
            (["--other", "a" * 8192], "batch command limit"),
        )
        with patch.dict(os.environ, {"SystemRoot": r"C:\Windows"}):
            for arguments, message in cases:
                with self.subTest(arguments=arguments[:1], message=message), self.assertRaisesRegex(ValueError, message):
                    junie_cli._windows_batch_command([r"C:\Junie.BAT", *arguments])

    def test_missing_cmd_is_actionable_and_prevents_launch(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(junie_cli.sys, "platform", "win32"),
            patch.object(junie_cli, "_resolve_executable", return_value=r"C:\Junie.BAT"),
            patch.object(junie_cli.subprocess, "Popen") as launch,
            self.assertRaisesRegex(junie_cli.JunieLaunchError, "SystemRoot/COMSPEC"),
        ):
            junie_cli._launch_process(["junie", "--prompt", "hello"])
        launch.assert_not_called()

    def test_session_keeps_nonzero_exit_usage_and_terminal_restore(self):
        process = Mock(returncode=7)
        process.wait.return_value = 7
        process.poll.return_value = 7
        events = Mock(usage=[0] * len(junie_cli.USAGE_FIELDS))
        events.read_new_events.return_value = False
        restored, report = Mock(), Mock()
        with (
            patch.object(junie_cli, "_save_terminal_mode", return_value=restored),
            patch.object(junie_cli, "_JunieSessionEvents", return_value=events),
            patch.object(junie_cli, "_launch_process", return_value=process),
        ):
            self.assertEqual(junie_cli._run_session(["junie"], Path("complete"), "token", (), report_usage=report), 7)
        restored.assert_called_once_with()
        report.assert_called_once_with([0] * len(junie_cli.USAGE_FIELDS))

    def test_session_interrupt_closes_child_and_restores_terminal(self):
        process = Mock(returncode=1)
        process.wait.side_effect = KeyboardInterrupt
        process.poll.side_effect = [None, 1]
        restored = Mock()
        with (
            patch.object(junie_cli, "_save_terminal_mode", return_value=restored),
            patch.object(junie_cli, "_JunieSessionEvents"),
            patch.object(junie_cli, "_launch_process", return_value=process),
            patch.object(junie_cli, "_close_junie") as close,
            self.assertRaises(KeyboardInterrupt),
        ):
            junie_cli._run_session(["junie"], Path("complete"), "token", ())
        close.assert_called_once_with(process)
        restored.assert_called_once_with()

    def test_exit_zero_before_session_captures_exact_pid_startup_error_and_full_stack(self):
        with TemporaryDirectory() as directory, patch.dict(os.environ, {
            "JUNIE_HOME": directory, "JUNIE_API_KEY": "private-api-key", "PRIVATE_TOKEN": "private-env-token",
        }):
            logs = Path(directory) / "logs"
            logs.mkdir()
            log = logs / "log-2026-11-16_010203-pid-123.log"
            process = Mock(pid=123, returncode=0)
            process.wait.return_value = 0
            process.poll.return_value = 0
            command = ["junie", "--prompt", "private-prompt"]

            def start(_):
                log.write_text(
                    "2026-11-16 01:02:03.000 [main] INFO  JunieRunner - private-info-not-for-report\n"
                    "2026-11-16 01:02:03.100 [main] ERROR JunieRunner - JLine returned dumb.\n"
                    "JunieCliException: Unable to initialize an interactive terminal: JLine returned dumb.\n"
                    "\tat example.Cli.start(Cli.kt:42)\n"
                    "Caused by: java.io.IOException: private-api-key private-env-token private-prompt\n"
                    + "\tat example.Cli.more(Cli.kt:100)\n" * 100
                    + "\tat example.Cli.last(Cli.kt:999)\n"
                    "2026-11-16 01:02:03.200 [main] INFO  JunieRunner - more-private-info\n", encoding="utf-8",
                )
                return process

            restored = Mock()
            with (
                patch.object(junie_cli, "_save_terminal_mode", return_value=restored),
                patch.object(junie_cli, "_launch_process", side_effect=start),
                patch.object(junie_cli, "_resolve_executable", return_value="/tools/junie"),
                patch.object(junie_cli.time, "sleep") as delay,
                self.assertRaises(junie_cli.JunieSessionError) as failed,
            ):
                junie_cli.run_interactive(command, Path(directory) / "complete", "token", ())
            report = format_error(failed.exception)
            for expected in ("code 0", "JLine returned dumb", "TERM", str(log), "Cli.kt:999", "Caused by:", "Command:", "Executable:"):
                self.assertIn(expected, report)
            for private in ("private-api-key", "private-env-token", "private-prompt", "private-info", "more-private-info"):
                self.assertNotIn(private, report)
            self.assertEqual(report.count("Cli.kt:100"), 100)
            restored.assert_called_once_with()
            delay.assert_not_called()

    def test_startup_logs_do_not_read_other_pids_or_old_logs(self):
        with TemporaryDirectory() as directory, patch.dict(os.environ, {"JUNIE_HOME": directory}):
            logs = Path(directory) / "logs"
            logs.mkdir()
            (logs / "log-old-pid-123.log").write_text("2026-11-16 01:00:00 [main] ERROR old private content\n")
            events = junie_cli._JunieSessionEvents("token")
            other = logs / "log-new-pid-456.log"
            other.write_text("2026-11-16 01:00:01 [main] ERROR other private content\n")
            report = events.startup_diagnostics(123, ["junie"])
            self.assertIn(str(other), report)
            self.assertNotIn("private content", report)
            self.assertNotIn("log-old", report)
            (logs / "log-new-pid-789.log").write_text("other content\n")
            self.assertIn("no unambiguous log", events.startup_diagnostics(123, ["junie"]))

    def test_wrapper_child_startup_failure_keeps_full_stack_using_launch_cwd(self):
        with TemporaryDirectory() as directory, patch.dict(os.environ, {
            "JUNIE_HOME": directory, "JUNIE_API_KEY": "private-api-key",
        }):
            logs = Path(directory) / "logs"
            logs.mkdir()
            log = logs / "log-2026-11-16_122914-pid-71220.log"
            process = Mock(pid=71200, returncode=0)
            process.wait.return_value = 0
            process.poll.return_value = 0
            command = ["junie", "--project", str(Path(directory) / "different-project"), "--prompt", "private-prompt"]
            stack = (
                "com.intellij.JunieCliException: Unable to initialize an interactive terminal: JLine returned dumb.\n"
                "\tat example.JunieCliKt.buildSystemTerminal(JunieCli.kt:252)\n"
                "\tat java.base/java.util.concurrent.ForkJoinWorkerThread.run(Unknown Source)\n"
                "Caused by: java.io.IOException: terminal failed\n"
                "\tat example.Terminal.last(Terminal.kt:999)\n"
            )

            def start(_):
                log.write_text(
                    f"2026-11-16 12:29:14.752 [main] INFO  JunieRunner - Startup: Junie 26.11.1 (4020.10) (pid=71220); log file: {log}\n"
                    f"2026-11-16 12:29:14.777 [main] INFO  JunieRunner - user.dir: {os.getcwd()}\n"
                    "2026-11-16 12:29:15.235 [main] ERROR JunieRunner - Unable to initialize an interactive terminal: JLine returned dumb.\n"
                    + stack + "private-api-key private-prompt\n"
                    "2026-11-16 12:29:15.300 [main] INFO  JunieRunner - private-info\n", encoding="utf-8",
                )
                return process

            with (
                patch.object(junie_cli, "_save_terminal_mode", return_value=Mock()),
                patch.object(junie_cli, "_launch_process", side_effect=start),
                patch.object(junie_cli, "_resolve_executable", return_value="/tools/junie"),
                self.assertRaises(junie_cli.JunieSessionError) as failed,
            ):
                junie_cli.run_interactive(command, Path(directory) / "complete", "token", ())
            report = str(failed.exception)
            self.assertIn("code 0", report)
            self.assertIn(str(log), report)
            self.assertIn(stack.rstrip(), report)
            self.assertIn("TERM", report)
            for secret in ("private-api-key", "private-prompt", "private-info"):
                self.assertNotIn(secret, report)

    def test_child_log_requires_matching_cwd_pid_filename_and_a_single_new_log(self):
        with TemporaryDirectory() as directory, patch.dict(os.environ, {"JUNIE_HOME": directory}):
            logs = Path(directory) / "logs"
            logs.mkdir()
            events = junie_cli._JunieSessionEvents("token")
            log = logs / "log-new-pid-456.log"
            for cwd, pid, declared_log in (
                (directory, 456, log),
                (os.getcwd(), 789, log),
                (os.getcwd(), 456, logs / "other.log"),
                (os.getcwd(), 456, log),
            ):
                with self.subTest(cwd=cwd, pid=pid, declared_log=declared_log):
                    log.write_text(
                        f"2026-11-16 12:29:14.752 [main] INFO  JunieRunner - Startup: Junie 26.11.1 (pid={pid}); log file: {declared_log}\n"
                        f"2026-11-16 12:29:14.777 [main] INFO  JunieRunner - user.dir: {cwd}\n"
                        "2026-11-16 12:29:15.235 [main] ERROR JunieRunner - startup details\n"
                        "\tat example.Terminal.last(Terminal.kt:999)\n", encoding="utf-8",
                    )
                    if cwd == os.getcwd() and pid == 456 and declared_log == log:
                        (logs / "log-other-pid-789.log").write_text("concurrent launch\n")
                    report = events.startup_diagnostics(123, ["junie"])
                    self.assertNotIn("startup details", report)
                    self.assertNotIn("Terminal.kt:999", report)

    def test_valid_completion_does_not_require_session_event(self):
        with TemporaryDirectory() as directory, patch.dict(os.environ, {"JUNIE_HOME": directory}):
            completion = Path(directory) / "complete"
            completion.write_text("token")
            process = Mock(returncode=0)
            process.wait.return_value = 0
            process.poll.return_value = 0
            with (
                patch.object(junie_cli, "_save_terminal_mode", return_value=Mock()),
                patch.object(junie_cli, "_launch_process", return_value=process),
            ):
                self.assertEqual(junie_cli._run_session(["junie"], completion, "token", ()), 0)


@unittest.skipUnless(sys.platform == "win32", "requires the real Windows cmd.exe")
class WindowsBatchIntegrationTests(unittest.TestCase):
    def test_path_lookup_and_batch_forwarding_preserve_metacharacters_and_exit_code(self):
        for extension in ("BAT", "CMD"):
            with self.subTest(extension=extension), TemporaryDirectory() as temporary:
                root = Path(temporary) / "Junie Tools & (%JUNIE_TEST_EXPAND%) !JUNIE_TEST_EXPAND! ^ 日本語"
                root.mkdir()
                launcher = root / ("Junie." + extension)
                capture = root / "capture.py"
                result = root / "result.json"
                launcher.write_bytes(
                    b'@echo off\r\n"%PHP_TO_CSHARP_TEST_PYTHON%" "%PHP_TO_CSHARP_TEST_CAPTURE%" %*\r\n'
                    b'@exit /b %errorlevel%\r\n'
                )
                capture.write_text(
                    "import json, os, pathlib, sys\n"
                    "pathlib.Path(os.environ['PHP_TO_CSHARP_TEST_RESULT']).write_text(json.dumps({\n"
                    "'argv': sys.argv[1:], 'prompt': os.environ.get('JUNIE_PROMPT'),\n"
                    "'project': os.environ.get('JUNIE_PROJECT'), 'cwd': os.getcwd(),\n"
                    "'guidelines': os.environ.get('JUNIE_IDE_GUIDELINES')\n"
                    "}), encoding='utf-8')\n"
                    "sys.exit(7)\n", encoding="utf-8",
                )
                prompt = '日本語 "quotes" & echo BAD > injected.txt | exit\n%JUNIE_TEST_EXPAND% !JUNIE_TEST_EXPAND! ^ ()\n' + "x" * 7000
                project = str(root) + "\\"
                guidelines = str(root / "必須事項 & details.md")
                literal = "space &|<>^() %JUNIE_TEST_EXPAND% !JUNIE_TEST_EXPAND! tail\\"
                command = ["junie", "--project", project, "--ide-guidelines", guidelines, "--max-steps", "2", "--literal", literal, "--prompt", prompt]
                with patch.dict(os.environ, {
                    "PATH": str(root), "PATHEXT": ".COM;.EXE;.BAT;.CMD", "JUNIE_TEST_EXPAND": "SHOULD_NOT_EXPAND",
                    "PHP_TO_CSHARP_TEST_PYTHON": sys.executable,
                    "PHP_TO_CSHARP_TEST_CAPTURE": str(capture), "PHP_TO_CSHARP_TEST_RESULT": str(result),
                }), patch.object(junie_cli.os, "getcwd", return_value=str(root)):
                    process = junie_cli._launch_process(command)
                    try:
                        self.assertEqual(process.wait(timeout=20), 7)
                    finally:
                        if process.poll() is None:
                            junie_cli._close_junie(process)
                data = json.loads(result.read_text(encoding="utf-8"))
                self.assertEqual(data["prompt"], prompt)
                self.assertEqual(data["project"], project)
                self.assertEqual(data["guidelines"], guidelines)
                self.assertEqual(data["argv"], ["--max-steps", "2", "--literal", literal])
                self.assertEqual(data["cwd"], str(root))
                self.assertFalse((root / "injected.txt").exists())


if __name__ == "__main__":
    unittest.main()
