from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from migration_harness.behavior import behavior_status, verify


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "behavior-search"


class BehaviorVerificationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name).resolve()
        self.run_id = str(uuid.uuid4())
        self.worktree = root / "worktrees" / self.run_id
        self.run_path = root / "runs" / self.run_id
        self.worktree.mkdir(parents=True)
        self.run_path.mkdir(parents=True)
        self.fixture = root / "cases.json"
        shutil.copyfile(EXAMPLE / "cases.json", self.fixture)
        self.target = self.worktree / "Search.cs"
        self.php = self.worktree / "search.php"
        shutil.copyfile(EXAMPLE / self.target.name, self.target)
        shutil.copyfile(EXAMPLE / self.php.name, self.php)
        self.unit = {"id": "search", "target": self.target.name, "inputs": [{"path": self.php.name}]}
        self.config = {
            "project_root": str(root / "project"),
            "worktree_root": str(self.worktree.parent),
            "run_root": str(self.run_path.parent),
            "units": [self.unit],
        }
        self.run_record = {
            "run_id": self.run_id,
            "unit_id": self.unit["id"],
            "project_root": self.config["project_root"],
            "worktree_path": str(self.worktree),
            "target": self.unit["target"],
            "status": "checker_succeeded",
        }
        (self.run_path / "run.json").write_text(json.dumps(self.run_record), encoding="utf-8")
        self.cases = json.loads(self.fixture.read_bytes())["cases"]

    def stub_process(self, argv, *, cwd, input, **kwargs):
        self.assertNotEqual(cwd, self.worktree)
        self.assertFalse(cwd.is_relative_to(self.worktree))
        if argv[-1] in ("Reference.php", "bin/Release/net10.0/Behavior.dll"):
            self.assertEqual(json.loads(input), [case["input"] for case in self.cases])
            observations = [dict(case["expected"], generated_at=argv[0]) for case in self.cases]
            return subprocess.CompletedProcess(argv, 0, json.dumps(observations).encode(), b"")
        return subprocess.CompletedProcess(argv, 0, b"fake toolchain", b"diagnostic")

    def test_legacy_run_without_result_is_not_run(self):
        with patch("migration_harness.behavior.subprocess.run") as execute:
            self.assertEqual(behavior_status(self.config, self.run_id), {"status": "not_run", "result": None})
        execute.assert_not_called()

    def test_fixture_php_and_target_bytes_each_invalidate_pass(self):
        with patch("migration_harness.behavior.subprocess.run", side_effect=self.stub_process):
            result = verify(self.config, self.run_id, self.fixture)
        self.assertEqual(result["status"], "passed")
        identity = result["result"]["identity"]
        self.assertEqual(identity["targets"], {self.target.name: hashlib.sha256(self.target.read_bytes()).hexdigest()})
        self.assertEqual(identity["php"]["sha256"], hashlib.sha256(self.php.read_bytes()).hexdigest())
        self.assertEqual(identity["fixture"]["sha256"], hashlib.sha256(self.fixture.read_bytes()).hexdigest())
        with patch("migration_harness.behavior.subprocess.run") as execute:
            for path in (self.fixture, self.php, self.target):
                with self.subTest(path=path.name):
                    original = path.read_bytes()
                    path.write_bytes(original + b"\n")
                    stale = behavior_status(self.config, self.run_id)
                    self.assertEqual(stale["status"], "stale")
                    self.assertEqual(stale["result"]["status"], "passed")
                    path.write_bytes(original)
                    self.assertEqual(behavior_status(self.config, self.run_id)["status"], "passed")
            self.target.unlink()
            self.assertEqual(behavior_status(self.config, self.run_id)["status"], "stale")
        execute.assert_not_called()

    def test_multi_target_identity_covers_primary_and_noncompiled_target(self):
        view = self.worktree / "Search.cshtml"
        view.write_text("<h1>Search</h1>", encoding="utf-8")
        self.unit["targets"] = [self.target.name, view.name]
        self.run_record["targets"] = self.unit["targets"]
        (self.run_path / "run.json").write_text(json.dumps(self.run_record), encoding="utf-8")
        with patch("migration_harness.behavior.subprocess.run", side_effect=self.stub_process):
            result = verify(self.config, self.run_id, self.fixture)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(set(result["result"]["identity"]["targets"]), {self.target.name, view.name})
        view.write_text("<h1>Results</h1>", encoding="utf-8")
        self.assertEqual(behavior_status(self.config, self.run_id)["status"], "stale")

    def test_verifies_copied_bytes_and_marks_concurrent_edit_stale(self):
        original = self.target.read_bytes()

        def edit_and_run(argv, **kwargs):
            if argv[-1] == "Reference.php":
                self.target.write_bytes(original + b"\n// edited\n")
            if argv[:2] == ["dotnet", "build"]:
                copied = list(kwargs["cwd"].rglob(self.target.name))
                self.assertEqual([path.read_bytes() for path in copied], [original])
            return self.stub_process(argv, **kwargs)

        with patch("migration_harness.behavior.subprocess.run", side_effect=edit_and_run):
            result = verify(self.config, self.run_id, self.fixture)
        self.assertEqual(result["status"], "stale")
        self.assertEqual(result["result"]["status"], "passed")
        self.assertEqual(behavior_status(self.config, self.run_id), result)

    def test_runtime_timeout_saves_expected_and_other_observations(self):
        def time_out_php(argv, **kwargs):
            if argv[-1] == "Reference.php":
                raise subprocess.TimeoutExpired(argv, kwargs["timeout"], output=b"partial output")
            return self.stub_process(argv, **kwargs)

        with patch("migration_harness.behavior.subprocess.run", side_effect=time_out_php):
            result = verify(self.config, self.run_id, self.fixture)
        self.assertEqual(result["status"], "failed")
        report = result["result"]
        self.assertIn("timed out", report["errors"]["php"])
        self.assertIsNone(report["commands"][1]["exit_code"])
        self.assertIn(b"partial output", Path(report["log_path"]).read_bytes())
        self.assertEqual(report["cases"][0]["expected"], self.cases[0]["expected"])
        self.assertIsNone(report["cases"][0]["php"])
        self.assertEqual(report["cases"][0]["matches"], {"php": False, "csharp": True})
        self.assertEqual(behavior_status(self.config, self.run_id), result)

    def test_invalid_fixture_never_executes(self):
        fixture = json.loads(self.fixture.read_bytes())
        fixture["ignored_fields"] = ["status"]
        self.fixture.write_text(json.dumps(fixture), encoding="utf-8")
        with patch("migration_harness.behavior.subprocess.run") as execute:
            with self.assertRaisesRegex(ValueError, "without ignored fields"):
                verify(self.config, self.run_id, self.fixture)
        execute.assert_not_called()

    def test_symlinked_target_never_executes(self):
        self.target.unlink()
        try:
            self.target.symlink_to(EXAMPLE / self.target.name)
        except (NotImplementedError, OSError) as exc:
            self.skipTest(f"cannot create a symlink: {exc}")
        with patch("migration_harness.behavior.subprocess.run") as execute:
            with self.assertRaisesRegex(ValueError, "symlink"):
                verify(self.config, self.run_id, self.fixture)
        execute.assert_not_called()

    def test_malformed_or_copied_result_is_never_current(self):
        result_path = self.run_path / "behavior.json"
        result_path.write_text("{", encoding="utf-8")
        self.assertEqual(behavior_status(self.config, self.run_id)["status"], "stale")
        result_path.write_text(json.dumps({"version": 1, "run_id": str(uuid.uuid4()), "status": "passed"}))
        self.assertEqual(behavior_status(self.config, self.run_id)["status"], "stale")

    def test_real_php_and_dotnet_detect_ordering_regression_in_companion(self):
        if not shutil.which("php") or not shutil.which("dotnet"):
            self.skipTest("requires PHP 8+ CLI and the .NET 10 SDK/runtime")
        php = subprocess.check_output(["php", "-n", "-r", "echo PHP_MAJOR_VERSION;"], text=True, timeout=15)
        if int(php) < 8:
            self.skipTest("requires PHP 8+ CLI")
        sdks = subprocess.check_output(["dotnet", "--list-sdks"], text=True, timeout=15)
        runtimes = subprocess.check_output(["dotnet", "--list-runtimes"], text=True, timeout=15)
        if not any(line.startswith("10.") for line in sdks.splitlines()) or "Microsoft.NETCore.App 10." not in runtimes:
            self.skipTest("requires the .NET 10 SDK/runtime")

        companion = self.worktree / "SearchOrdering.cs"
        companion.write_text("static class Ordering { public static int Key(int id) => id; }", encoding="utf-8")
        self.unit["targets"] = [self.target.name, companion.name]
        self.run_record["targets"] = self.unit["targets"]
        (self.run_path / "run.json").write_text(json.dumps(self.run_record), encoding="utf-8")
        source = self.target.read_text(encoding="utf-8")
        self.target.write_text(source.replace("OrderBy(row => row.id)", "OrderBy(row => Ordering.Key(row.id))"), encoding="utf-8")

        first = verify(self.config, self.run_id, self.fixture)
        self.assertEqual(first["status"], "passed", first["result"]["errors"])
        self.assertEqual(behavior_status(self.config, self.run_id), first)
        self.assertEqual(len(first["result"]["commands"]), 5)
        self.assertTrue(first["result"]["environment"]["php"].startswith("PHP "))
        self.assertIn("10.", first["result"]["environment"]["dotnet"])
        self.assertTrue(all("generated_at" in case["csharp"] for case in first["result"]["cases"]))

        companion.write_text("static class Ordering { public static int Key(int id) => -id; }", encoding="utf-8")
        self.assertEqual(behavior_status(self.config, self.run_id)["status"], "stale")
        second = verify(self.config, self.run_id, self.fixture)
        self.assertEqual(second["status"], "failed")
        case = next(case for case in second["result"]["cases"] if case["id"] == "zero-query")
        self.assertEqual(case["expected"]["rows"][0]["id"], 1)
        self.assertEqual(case["csharp"]["rows"][0]["id"], 2)
        self.assertEqual(case["matches"], {"php": True, "csharp": False})
        self.assertEqual(behavior_status(self.config, self.run_id), second)
        self.assertNotEqual(first["result"]["log_path"], second["result"]["log_path"])
        self.assertTrue(Path(first["result"]["log_path"]).is_file())
        self.assertEqual(json.loads((self.run_path / "run.json").read_bytes()), self.run_record)
