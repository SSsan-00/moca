import io
import json
import os
import re
import subprocess
import unittest
import uuid
from contextlib import redirect_stdout
from pathlib import Path
from threading import Thread
from unittest.mock import patch
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from fixtures import HarnessCase
from migration_harness.web import dashboard
from migration_harness.verification.roslyn_context import editor_project_snapshot


_CSPROJ = """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup><TargetFramework>net9.0</TargetFramework></PropertyGroup>
</Project>
"""
_REFERENCE_USER = "public class Foo { public Bar Value = new Bar(); }\n"
_DOT_SOURCE = "public class Bar {}\n"
_UNSAFE_SOURCE = "public unsafe class Foo { public int* Ptr; }\n"


class EditorProjectDiagnosticHttpTests(HarnessCase):
    def setUp(self):
        super().setUp()
        (self.project / "source.php").write_text("<?php echo 1;", encoding="utf-8")
        targets = ["Foo.cs", ".Hidden.cs", ".excluded/Ghost.cs"]
        self.config_values = self.config(target="targets = " + json.dumps(targets))
        self.config_values["build_project"] = "App.csproj"
        self.run_id = str(uuid.uuid4())
        self.worktree = Path(self.config_values["worktree_root"]) / self.run_id
        self.worktree.mkdir(parents=True)
        (self.worktree / "App.csproj").write_text(_CSPROJ, encoding="utf-8")
        (self.worktree / "Foo.cs").write_text(_REFERENCE_USER, encoding="utf-8")
        (self.worktree / ".Hidden.cs").write_text(_DOT_SOURCE, encoding="utf-8")
        for folder in (".excluded", ".directory.cs", "bin", "obj"):
            excluded = self.worktree / folder
            excluded.mkdir()
            (excluded / "Ghost.cs").write_text("not valid C# source;", encoding="utf-8")
        run = Path(self.config_values["run_root"]) / self.run_id
        run.mkdir(parents=True)
        record = {
            "run_id": self.run_id, "unit_id": "sample", "project_root": self.config_values["project_root"],
            "worktree_path": str(self.worktree), "target": targets[0], "targets": targets,
            "status": "build_succeeded", "completed_capsules": 1,
        }
        self.record = run / "run.json"
        self.record.write_text(json.dumps(record), encoding="utf-8")
        view = dashboard.Dashboard()
        view.observe(record)
        view.ready(self.config_values, record)
        output = io.StringIO()
        with redirect_stdout(output):
            server = view.server(self.config_values, b"", b"")
        self.url = output.getvalue().splitlines()[0].removeprefix("Progress dashboard: ")
        address = urlsplit(self.url)
        self.origin = f"{address.scheme}://{address.netloc}"
        server_thread = Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server_thread.join)
        self.addCleanup(server.shutdown)

    def request(self, mode, *, target="Foo.cs", content=_REFERENCE_USER):
        payload = {"run_id": self.run_id, "target": target, "version": "fixture-draft", "mode": mode}
        if mode != "context":
            payload["content"] = content
        request = Request(self.url + "roslyn-diagnostics", data=json.dumps(payload).encode(), headers={
            "Origin": self.origin, "Content-Type": "application/json", "X-Run-ID": self.run_id,
        })
        with urlopen(request, timeout=25) as response:
            self.assertEqual(response.status, 200)
            reply = json.load(response)
        self.assertEqual((reply["target"], reply["version"]), (target, "fixture-draft"))
        return reply

    def test_unsupported_inherited_compiler_settings_invalidate_the_diagnosis(self):
        source_before, run_before = (self.worktree / "Foo.cs").read_bytes(), self.record.read_bytes()
        ordinary = self.request("semantic", content=_UNSAFE_SOURCE)
        self.assertEqual((ordinary["status"], {finding["code"] for finding in ordinary["findings"]}),
                         ("errors", {"CS0227"}))

        with patch.dict(os.environ, {"aLlOwUnSaFeBlOcKs": "true"}):
            rejected = self.request("semantic", content=_UNSAFE_SOURCE)
            version = self.request("context")
            self.assertEqual((rejected["status"], rejected["reason"], rejected["findings"], rejected["context_revision"]),
                             ("unavailable", "project_configuration", [], None))
            self.assertEqual((version["context_revision"], version["reason"]), (None, "project_configuration"))

        self.assertEqual(self.request("context")["context_revision"], ordinary["context_revision"])
        self.assertEqual((self.worktree / "Foo.cs").read_bytes(), source_before)
        self.assertEqual(self.record.read_bytes(), run_before)

    def test_dotted_files_participate_in_references_targets_and_dependency_versions(self):
        original = self.record.read_bytes()
        before = self.request("semantic")
        self.assertEqual((before["status"], before["findings"]), ("clear", []))
        self.assertEqual([path.name for path, _ in editor_project_snapshot(self.worktree, "App.csproj", "Foo.cs").sources],
                         [".Hidden.cs"])
        changed_target = self.request("semantic", target=".Hidden.cs", content="class Bar { string Message => 1; }")
        self.assertEqual((changed_target["status"], [f["code"] for f in changed_target["findings"]]),
                         ("errors", ["CS0029"]))
        self.assertEqual(self.request("semantic", target=".excluded/Ghost.cs")["reason"], "project_required")

        dot = self.worktree / ".Hidden.cs"
        (self.worktree / ".excluded/Ghost.cs").write_text("still not valid C#;", encoding="utf-8")
        self.assertEqual(self.request("context")["context_revision"], before["context_revision"])
        dot.write_text("public class Baz {}\n", encoding="utf-8")
        self.assertNotEqual(self.request("context")["context_revision"], before["context_revision"])
        self.assertEqual({item["code"] for item in self.request("semantic")["findings"]}, {"CS0246"})

        dot.unlink()
        removed = self.request("context")["context_revision"]
        self.assertNotEqual(removed, before["context_revision"])
        dot.write_text(_DOT_SOURCE, encoding="utf-8")
        self.assertNotEqual(self.request("context")["context_revision"], removed)
        self.assertEqual(self.request("semantic")["status"], "clear")
        self.assertEqual(dot.read_text(encoding="utf-8"), _DOT_SOURCE)
        self.assertEqual(self.record.read_bytes(), original)
        for folder in ("obj", "bin"):
            self.assertEqual([path.name for path in (self.worktree / folder).iterdir()], ["Ghost.cs"])

    @unittest.skipUnless(os.name == "nt" and os.environ.get("GITHUB_ACTIONS") == "true",
                         "SDK comparison is restricted to the hosted Windows workflow")
    def test_installed_sdk_agrees_with_dotted_source_membership_on_windows_ci(self):
        oracle = self.root / "sdk-oracle"
        oracle.mkdir()
        (oracle / "App.csproj").write_text(_CSPROJ, encoding="utf-8")
        (oracle / "Foo.cs").write_text(_REFERENCE_USER, encoding="utf-8")
        (oracle / ".Hidden.cs").write_text(_DOT_SOURCE, encoding="utf-8")
        (oracle / ".excluded").mkdir()
        (oracle / ".excluded/Ghost.cs").write_text("this invalid C# file must be excluded;", encoding="utf-8")
        feed = self.root / "empty-feed"
        feed.mkdir()
        environment = dict(os.environ, DOTNET_CLI_HOME=str(self.root / "dotnet-home"),
                           DOTNET_CLI_TELEMETRY_OPTOUT="1", DOTNET_NOLOGO="1",
                           DOTNET_CLI_WORKLOAD_UPDATE_NOTIFY_DISABLE="true", DOTNET_GENERATE_ASPNET_CERTIFICATE="false")

        def run(*args):
            return subprocess.run(["dotnet", *args], cwd=oracle, env=environment, capture_output=True,
                                  timeout=40, check=False, text=True, encoding="utf-8", errors="replace")

        listed = run("--list-sdks")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        versions = [int(patch) for patch in re.findall(r"(?m)^9\.0\.(\d+) \[", listed.stdout)]
        self.assertTrue(versions, listed.stdout)
        (oracle / "global.json").write_text(json.dumps({
            "sdk": {"version": f"9.0.{max(versions)}", "rollForward": "disable"},
        }), encoding="utf-8")
        restored = run("restore", "App.csproj", "--source", str(feed), "-p:NuGetAudit=false", "--verbosity", "quiet")
        self.assertEqual(restored.returncode, 0, restored.stdout + restored.stderr)
        build = ("build", "App.csproj", "--no-restore", "--disable-build-servers", "-p:UseSharedCompilation=false",
                 "--verbosity", "quiet")
        actual = run(*build)
        self.assertEqual(actual.returncode, 0, actual.stdout + actual.stderr)
        self.assertEqual(self.request("semantic")["status"], "clear")

        (oracle / ".Hidden.cs").unlink()
        (self.worktree / ".Hidden.cs").unlink()
        actual = run(*build)
        self.assertNotEqual(actual.returncode, 0)
        self.assertIn("CS0246", actual.stdout + actual.stderr)
        self.assertEqual({item["code"] for item in self.request("semantic")["findings"]}, {"CS0246"})
        print("Windows .NET 9 oracle: dotted source included, dotted directory excluded, removal gives CS0246", flush=True)
