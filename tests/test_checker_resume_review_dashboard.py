from __future__ import annotations

import io
import json
import subprocess
import sys
import unittest
import uuid
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from migration_harness import cli, dashboard, workflow


class SavedRunFixture(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        self.run_id = str(uuid.uuid4())
        self.run_path = root / "runs" / self.run_id
        self.worktree = root / "worktrees" / self.run_id
        self.worktree.mkdir(parents=True)
        self.target = self.worktree / "Example.cs"
        self.target.write_text("class Original {}\n", encoding="utf-8")
        project = root / "project"
        project.mkdir()
        (project / "example.php").write_text("<?php echo 'example';\n", encoding="utf-8")
        (project / "viewer.html").write_text(
            '<!doctype html><body><input id="workspace-import-file"></body>', encoding="utf-8",
        )
        self.config = {
            "run_root": str(self.run_path.parent),
            "worktree_root": str(self.worktree.parent),
            "project_root": str(project),
            "units": [{"id": "example", "target": self.target.name}],
            "build_project": None,
            "checker_command": ["fixture-checker"],
            "max_repair_attempts": 0,
            "junie_max_steps": None,
            "diff_viewer_html": "viewer.html",
            "diff_viewer_anchor_reference": root / "reference.json",
        }
        capsule = self.run_path / "capsule"
        capsule.mkdir(parents=True)
        context = capsule / "context.txt"
        context.write_text("example", encoding="utf-8")
        (capsule / "manifest.json").write_text(json.dumps({
            "parts": [{
                "index": 1, "path": str(context), "chars": 7,
                "sources": [{"path": "example.php", "start_line": 1, "end_line": 1, "encoding": "utf-8"}],
            }],
        }), encoding="utf-8")
        self.record_path = self.run_path / "run.json"
        self.build_log = self.run_path / "build.log"
        self.checker_log = self.run_path / "checker.log"
        self.build_log.write_bytes(b"old successful build\n")
        self.checker_log.write_bytes(b"PREVIOUS Example.cs:1 warning: previous finding\n")
        self.save_record({
            "run_id": self.run_id, "unit_id": "example", "target": self.target.name,
            "project_root": str(project), "worktree_path": str(self.worktree),
            "status": "checker_needs_review", "completed_capsules": 1, "context_capsules": 1,
            "max_context_chars": 1000, "source_boundary_review_required": False,
            "build_exit_code": 0, "build_log_path": str(self.build_log),
            "checker_exit_code": 1, "checker_log_path": str(self.checker_log),
        })

    def save_record(self, record):
        self.record_path.write_text(json.dumps(record), encoding="utf-8")

    def record(self):
        return json.loads(self.record_path.read_text(encoding="utf-8"))

    def mock_commands(self, *responses):
        expected = iter(responses)
        calls = []

        def run(command, *, cwd, stdout, stderr, check):
            calls.append(command)
            name, returncode, log = next(expected)
            self.assertEqual(command, ["dotnet", "build"] if name == "dotnet" else [name])
            self.assertEqual(Path(cwd), self.worktree)
            self.assertEqual(stderr, subprocess.STDOUT)
            self.assertFalse(check)
            stdout.write(log)
            return subprocess.CompletedProcess(command, returncode)

        return patch.object(workflow.subprocess, "run", side_effect=run), calls

    def resume(self, *responses):
        mock, calls = self.mock_commands(*responses)
        with mock, redirect_stdout(io.StringIO()):
            result = workflow.resume_with_junie(self.config, self.run_id)
        self.assertEqual(len(calls), len(responses))
        return result

    def review(self, finding_id=None, reason=None):
        return workflow.review_checker(self.config, self.run_id, finding_id, reason)


class CheckerResumeAndReviewTests(SavedRunFixture):
    build = ("dotnet", 0, b"rebuilt edited target\n")
    diagnostic = b"MIG101 Example.cs:1 warning: use SharedUtility\n"

    def test_manual_edit_rebuilds_before_rechecking_without_history_or_approvals(self):
        self.target.write_text("class ManuallyEdited {}\n", encoding="utf-8")
        result = self.resume(self.build, ("fixture-checker", 0, b"no findings\n"))
        self.assertEqual(result["status"], "checker_succeeded")
        saved = self.record()
        self.assertEqual(saved["status"], "checker_succeeded")
        self.assertEqual(saved["build_exit_code"], 0)
        self.assertEqual(saved["build_log_path"], str(self.build_log))
        self.assertEqual(self.build_log.read_bytes(), self.build[2])
        self.assertEqual(self.checker_log.read_bytes(), b"no findings\n")

    def test_failed_rebuild_stops_before_checker_and_records_current_failure(self):
        self.target.write_text("broken C#", encoding="utf-8")
        old_checker_log = self.checker_log.read_bytes()
        result = self.resume(("dotnet", 99, b"edited target does not build\n"))
        self.assertEqual(result["status"], "build_failed")
        saved = self.record()
        self.assertEqual(saved["status"], "build_failed")
        self.assertEqual(saved["build_exit_code"], 99)
        self.assertNotIn("checker_exit_code", saved)
        self.assertEqual(self.build_log.read_bytes(), b"edited target does not build\n")
        self.assertEqual(self.checker_log.read_bytes(), old_checker_log)

    def test_mixed_and_invalid_checker_output_stop_before_automatic_repair(self):
        self.config["max_repair_attempts"] = 2
        malformed = (
            (b"Traceback: checker database is offline\n", "unrecognized"),
            (b"MIG102 ../outside.cs:5 warning: outside the worktree\n", "unrecognized"),
            (b"MIG102 Bad\0Path.cs:5 warning: invalid path\n", "unrecognized"),
            (b"\xff\n", "utf-8"),
        )
        for extra, error in malformed:
            with self.subTest(extra=extra):
                log = self.diagnostic + extra
                with (
                    patch.object(workflow, "_repair_target") as repair,
                    patch.object(workflow, "_validate_junie_execution") as validate,
                ):
                    result = self.resume(self.build, ("fixture-checker", 2, log))
                repair.assert_not_called()
                validate.assert_not_called()
                self.assertEqual(result["status"], "checker_needs_review")
                self.assertIn(error, result["error"])
                saved = self.record()
                self.assertEqual(saved["error"], result["error"])
                self.assertEqual(saved.get("repair_history", []), [])
                self.assertEqual(self.checker_log.read_bytes(), log)
                self.assertFalse((self.run_path / "checker-before-repair-1.log").exists())
                if error == "utf-8":
                    with self.assertRaises(UnicodeDecodeError):
                        self.review()
                else:
                    report = self.review()
                    self.assertTrue(report["unrecognized"])
                    self.assertEqual(len(report["findings"]), 1)

    def test_supported_diagnostics_still_allow_automatic_repair(self):
        self.config["max_repair_attempts"] = 1
        with (
            patch.object(workflow, "_validate_junie_execution"),
            patch.object(workflow, "_repair_target") as repair,
        ):
            result = self.resume(
                self.build, ("fixture-checker", 1, self.diagnostic),
                self.build, ("fixture-checker", 0, b"fixed\n"),
            )
        self.assertEqual(result["status"], "checker_succeeded")
        repair.assert_called_once()
        self.assertEqual(repair.call_args.args[4], self.diagnostic.decode().strip())
        self.assertEqual(len(self.record()["repair_history"]), 1)
        self.assertEqual((self.run_path / "checker-before-repair-1.log").read_bytes(), self.diagnostic)

    def test_character_and_byte_limits_only_block_automatic_repair(self):
        self.config["max_repair_attempts"] = 1
        for count, size, error in ((70, 110, "too much text"), (350, 180, "too large")):
            with self.subTest(count=count):
                log = b"".join(
                    f"MIG101 Example.cs:{line} warning: {line} {'x' * size}\n".encode()
                    for line in range(1, count + 1)
                )
                self.assertGreater(len(log), 8_000)
                self.assertEqual(len(log) > 65_536, count == 350)
                with patch.object(workflow, "_repair_target") as repair:
                    result = self.resume(self.build, ("fixture-checker", 1, log))
                repair.assert_not_called()
                self.assertEqual(result["status"], "checker_needs_review")
                self.assertIn(error, result["error"])
                report = self.review()
                self.assertEqual(len(report["findings"]), count)
                self.assertEqual(report["locations"][-1]["line"], str(count))
                last_id = report["findings"][-1][0]
                self.review(last_id, "Reviewed the last finding against the migration.")
                self.assertTrue(self.review()["findings"][-1][2])
                self.assertEqual(len(self.record()["repair_history"]), 0)

    def test_large_approved_log_can_complete_on_resume(self):
        self.config["max_repair_attempts"] = 1
        log = b"MIG101 Example.cs:1 warning: " + b"x" * 65_536 + b"\n"
        self.resume(self.build, ("fixture-checker", 1, log))
        finding_id = self.review()["findings"][0][0]
        self.review(finding_id, "The reported code uses the intended behavior.")
        result = self.resume(self.build, ("fixture-checker", 1, log))
        self.assertEqual(result["status"], "checker_accepted")
        self.assertEqual(self.record()["checker_exit_code"], 1)

    def test_unknown_output_prevents_previously_approved_findings_from_completing_run(self):
        self.resume(self.build, ("fixture-checker", 1, self.diagnostic))
        self.review(self.review()["findings"][0][0], "The warning is expected.")
        result = self.resume(self.build, ("fixture-checker", 1, self.diagnostic + b"checker offline\n"))
        self.assertEqual(result["status"], "checker_needs_review")
        self.assertIn("unrecognized", result["error"])

    def test_duplicate_id_remains_ambiguous(self):
        log = self.diagnostic + self.diagnostic.replace(b":1", b":2")
        self.resume(self.build, ("fixture-checker", 1, log))
        first, second = self.review()["findings"]
        self.assertEqual(first[0], second[0])
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            self.review(first[0], "Cannot distinguish these findings.")
        self.assertFalse(self.record().get("checker_reviews"))

    def test_approval_expires_when_target_changes(self):
        self.resume(self.build, ("fixture-checker", 1, self.diagnostic))
        finding_id = self.review()["findings"][0][0]
        self.review(finding_id, "The original content was reviewed.")
        self.target.write_text("class Changed {}\n", encoding="utf-8")
        self.assertFalse(self.review()["findings"][0][2])
        with self.assertRaisesRegex(ValueError, "changed or was missing"):
            self.review(finding_id, "Cannot approve a stale finding.")
        result = self.resume(self.build, ("fixture-checker", 1, self.diagnostic))
        self.assertEqual(result["status"], "checker_needs_review")
        self.assertEqual(self.review()["findings"][0], (finding_id, self.diagnostic.decode().strip(), False))
        self.assertEqual(len(self.record()["checker_reviews"]), 1)


class SavedDashboardTests(SavedRunFixture):
    @staticmethod
    def get_ready_event(url):
        with urlopen(url + "events", timeout=3) as response:
            for line in response:
                if line.startswith(b"id: "):
                    revision = int(line.split(b": ", 1)[1])
                elif line.startswith(b"data: "):
                    state = json.loads(line.split(b": ", 1)[1])
                    if state.get("ready"):
                        return revision, state
        raise AssertionError("no ready dashboard event")

    @staticmethod
    def post(url, endpoint, payload):
        origin = urlsplit(url)
        headers = {"Origin": f"{origin.scheme}://{origin.netloc}", "Content-Type": "application/json"}
        return urlopen(Request(url + endpoint, json.dumps(payload).encode(), headers), timeout=3)

    def open_dashboard(self, inspect, *, resume=False):
        terminal = io.StringIO()
        view = dashboard.Dashboard()
        dequeue = view.actions.get
        inspected = False

        def interact(*, timeout):
            nonlocal inspected
            if inspected:
                raise KeyboardInterrupt
            inspected = True
            first_line = terminal.getvalue().splitlines()[0]
            inspect(first_line.removeprefix("Progress dashboard: "))
            if resume:
                return dequeue(timeout=timeout)
            raise KeyboardInterrupt

        with (
            redirect_stdout(terminal),
            patch.object(dashboard, "Dashboard", return_value=view),
            patch.object(view.actions, "get", side_effect=interact),
        ):
            return dashboard.run_dashboard(self.config, run_id=self.run_id)

    def test_complete_runs_show_saved_results_ledger_and_diff_without_running_commands(self):
        original = self.record()
        for status, checker_exit in (("checker_succeeded", 0), ("checker_accepted", 1), ("build_succeeded", None)):
            with self.subTest(status=status):
                saved = {**original, "status": status}
                if checker_exit is None:
                    self.config["checker_command"] = None
                    saved.pop("checker_exit_code")
                    saved.pop("checker_log_path")
                else:
                    saved["checker_exit_code"] = checker_exit
                self.save_record(saved)
                before = self.record_path.read_bytes()

                def inspect(url):
                    revision, state = self.get_ready_event(url)
                    self.assertEqual(state["status"], status)
                    self.assertEqual(state["build_exit_code"], 0)
                    self.assertEqual(state.get("checker_exit_code"), checker_exit)
                    self.assertFalse(state["resumable"])
                    with urlopen(url, timeout=3) as response:
                        response.readline()
                        root_tag = response.readline()
                    self.assertIn(b"data-saved", root_tag)
                    self.assertNotIn(b"data-anchor-reference", root_tag)
                    with urlopen(url + "migration-ledger", timeout=3) as response:
                        self.assertEqual(json.load(response)["capsules"][0]["sources"][0]["path"], "example.php")
                    with self.post(url, "comparison", {}) as response:
                        snapshot_id = json.load(response)["id"]
                    with urlopen(f"{url}review/{snapshot_id}.json", timeout=3) as response:
                        self.assertIn("class Original", json.load(response)["workspace"]["rightText"])
                    for action in (
                        {"action": "resume"},
                        {"action": "accept", "finding_id": "123456789abc", "reason": "Done"},
                        {"action": "anchors"},
                    ):
                        with self.assertRaises(HTTPError) as rejected:
                            self.post(url, "control", {**action, "revision": revision})
                        with rejected.exception as response:
                            self.assertEqual(response.code, 409)

                with (
                    patch.object(workflow.subprocess, "run") as commands,
                    patch.object(dashboard, "run_with_junie") as junie,
                    patch.object(dashboard, "resume_with_junie") as resume,
                    patch.object(dashboard, "generate_anchors") as anchors,
                ):
                    result = self.open_dashboard(inspect)
                commands.assert_not_called()
                junie.assert_not_called()
                resume.assert_not_called()
                anchors.assert_not_called()
                self.assertEqual(result["status"], status)
                self.assertEqual(self.record_path.read_bytes(), before)

    def test_saved_resumable_run_waits_for_explicit_resume(self):
        mock, calls = self.mock_commands(("dotnet", 0, b"rebuilt\n"), ("fixture-checker", 0, b"clean\n"))
        before = self.record_path.read_bytes()

        def inspect(url):
            revision, state = self.get_ready_event(url)
            self.assertTrue(state["resumable"])
            self.assertEqual(state["status"], "checker_needs_review")
            self.assertEqual(calls, [])
            self.assertEqual(self.record_path.read_bytes(), before)
            with self.post(url, "control", {"action": "resume", "revision": revision}) as response:
                self.assertEqual(response.status, 202)

        with mock:
            result = self.open_dashboard(inspect, resume=True)
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["status"], "checker_succeeded")

    def test_cli_view_and_resume_dashboard_open_saved_run(self):
        for args in (["view"], ["resume", "--dashboard"]):
            with (
                self.subTest(args=args),
                patch.object(sys, "argv", ["harness", *args, "--config", "fixture.toml", "--run-id", self.run_id]),
                patch.object(cli, "load_config", return_value=self.config),
                patch.object(dashboard, "run_dashboard", return_value=self.record()) as open_view,
                patch.object(cli, "resume_with_junie") as resume,
            ):
                cli.main()
                open_view.assert_called_once_with(self.config, run_id=self.run_id)
                resume.assert_not_called()
