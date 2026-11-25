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
_WEB_CSPROJ = _CSPROJ.replace('Sdk="Microsoft.NET.Sdk"', 'Sdk="Microsoft.NET.Sdk.Web"').replace(
    "</PropertyGroup>", "<OutputType>Library</OutputType></PropertyGroup>",
)
_REFERENCE_USER = "public class Foo { public Bar Value = new Bar(); }\n"
_DOT_SOURCE = "public class Bar {}\n"
_UNSAFE_SOURCE = "public unsafe class Foo { public int* Ptr; }\n"
_RAZOR_SOURCE = "@model Sample.IndexModel\n<p>@Model.Value</p>\n"
_RAZOR_ERROR = _RAZOR_SOURCE.replace("Value", "Missing")
_RAZOR_MODEL = "namespace Sample; public class IndexModel { public int Value => 42; }\n"
_RAZOR_EXCLUDED = (
    "Features/node_modules/Index.cshtml", "jspm_packages/Index.cshtml", "bower_components/Index.cshtml",
    "Properties/PublishProfiles/Index.cshtml", "Properties/ServiceDependencies/Index.cshtml",
)


class EditorProjectDiagnosticHttpTests(HarnessCase):
    def setUp(self):
        super().setUp()
        (self.project / "source.php").write_text("<?php echo 1;", encoding="utf-8")
        targets = ["Foo.cs", ".Hidden.cs", ".excluded/Ghost.cs", "wwwroot/Bar.cs", "Properties/PublishProfiles/Blob.cs"]
        for razor in ("wwwroot/Index.cshtml", "Pages/Index.cshtml", *_RAZOR_EXCLUDED):
            targets.extend([razor, razor + ".cs"])
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

    @staticmethod
    def _razor_fixture(directory):
        (directory / "App.csproj").write_text(_WEB_CSPROJ, encoding="utf-8")
        (directory / "Models.cs").write_text(_RAZOR_MODEL, encoding="utf-8")
        for name in ("wwwroot/Index.cshtml", "Pages/Index.cshtml", *_RAZOR_EXCLUDED):
            view = directory / name
            view.parent.mkdir(parents=True, exist_ok=True)
            view.write_text(_RAZOR_ERROR if name in _RAZOR_EXCLUDED else _RAZOR_SOURCE, encoding="utf-8")

    def assert_missing_razor_member(self, response):
        self.assertEqual(response["status"], "errors", response)
        self.assertEqual([(item["code"], item["line"], item["column"]) for item in response["findings"]],
                         [("CS1061", 2, 11)])

    def test_web_razor_content_target_has_distinct_scope_from_csharp_compile(self):
        self._razor_fixture(self.worktree)
        target = "wwwroot/Index.cshtml"
        selected = self.worktree / target
        selected_bytes = selected.read_bytes()
        excluded_csharp = self.worktree / (target + ".cs")
        excluded_csharp.write_text("namespace Sample; public class IndexModel { public int Missing => 1; }", encoding="utf-8")

        result = self.request("semantic", target=target, content=_RAZOR_ERROR)
        self.assert_missing_razor_member(result)
        self.assertTrue(result["context_revision"])
        self.assertEqual(self.request("context", target=target)["context_revision"], result["context_revision"])
        self.assertEqual(self.request("semantic", target=target, content=_RAZOR_SOURCE)["status"], "clear")
        self.assert_missing_razor_member(self.request("semantic", target="Pages/Index.cshtml", content=_RAZOR_ERROR))

        unchanged = result["context_revision"]
        excluded_csharp.write_text("not valid C# and must not affect the view or revision", encoding="utf-8")
        self.assertEqual(self.request("context", target=target)["context_revision"], unchanged)
        self.assertEqual(self.request("semantic", target=target + ".cs")["reason"], "project_required")
        for excluded in _RAZOR_EXCLUDED:
            with self.subTest(excluded=excluded):
                self.assertEqual(self.request("semantic", target=excluded, content=_RAZOR_ERROR)["reason"],
                                 "project_required")

        (self.worktree / "wwwroot/_ViewImports.cshtml").write_text("@using Sample", encoding="utf-8")
        view_imports = self.request("context", target=target)["context_revision"]
        self.assertNotEqual(view_imports, unchanged)
        short_model = _RAZOR_ERROR.replace("Sample.", "")
        self.assert_missing_razor_member(self.request("semantic", target=target, content=short_model))
        (self.worktree / ".Hidden.cs").unlink()
        excluded_csharp.write_text(_DOT_SOURCE, encoding="utf-8")
        (self.worktree / "App.csproj").write_text(_CSPROJ, encoding="utf-8")
        self.assertEqual(self.request("semantic", target=target, content=_RAZOR_ERROR)["reason"],
                         "project_configuration")
        self.assertEqual(self.request("semantic")["status"], "clear")
        self.assertEqual(selected.read_bytes(), selected_bytes)

    @unittest.skipUnless(os.name == "nt" and os.environ.get("GITHUB_ACTIONS") == "true",
                         "SDK comparison is restricted to the hosted Windows workflow")
    def test_installed_web_sdk_keeps_razor_from_wwwroot(self):
        oracle = self.root / "sdk-razor-oracle"
        oracle.mkdir()
        self._razor_fixture(oracle)
        self._razor_fixture(self.worktree)
        target = "wwwroot/Index.cshtml"
        (oracle / target).write_text(_RAZOR_ERROR, encoding="utf-8")
        for tree in (oracle, self.worktree):
            (tree / (target + ".cs")).write_text("this C# must be excluded from Compile;", encoding="utf-8")
        run, build = self.sdk_build_oracle(oracle)
        broken = run(*build)
        self.assertNotEqual(broken.returncode, 0)
        errors = [line.replace("\\", "/") for line in (broken.stdout + broken.stderr).splitlines()
                  if "error " in line]
        self.assertTrue(errors, broken.stdout + broken.stderr)
        self.assertTrue(all("wwwroot/Index.cshtml(2,11): error CS1061:" in line for line in errors), errors)
        self.assert_missing_razor_member(self.request("semantic", target=target, content=_RAZOR_ERROR))

        (oracle / target).write_text(_RAZOR_SOURCE, encoding="utf-8")
        clean = run(*build)
        self.assertEqual(clean.returncode, 0, clean.stdout + clean.stderr)
        self.assertEqual(self.request("semantic", target=target, content=_RAZOR_SOURCE)["status"], "clear")
        print("Windows .NET 9 Razor oracle: wwwroot/Index.cshtml CS1061 at 2:11; tooling views and wwwroot C# excluded",
              flush=True)

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
        run, build = self.sdk_build_oracle(oracle)
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

    def sdk_build_oracle(self, oracle):
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
        return run, build

    @staticmethod
    def _web_fixture(directory):
        (directory / "App.csproj").write_text(_WEB_CSPROJ, encoding="utf-8")
        for name, text in {
            "wwwroot/Bar.cs": "public class Bar { public int Value = 1; }",
            "Properties/PublishProfiles/Blob.cs": "public class ProfileOnly {}",
            "Properties/ServiceDependencies/Ghost.cs": "public class ServiceOnly {}",
            "Features/wwwroot/Nested.cs": "public class NestedAllowed {}",
            "Features/node_modules/Types.cs": "public class NodeOnly {}",
            "jspm_packages/Types.cs": "public class JspmOnly {}",
            "bower_components/Types.cs": "public class BowerOnly {}",
        }.items():
            destination = directory / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(text + "\n", encoding="utf-8")

    def test_web_source_target_gate_and_revision_share_sdk_exclusions(self):
        (self.worktree / ".Hidden.cs").unlink()
        self._web_fixture(self.worktree)
        missing = self.request("semantic")
        self.assertEqual((missing["status"], {f["code"] for f in missing["findings"]}), ("errors", {"CS0246"}))
        self.assertEqual(self.request("semantic", target="wwwroot/Bar.cs")["reason"], "project_required")
        self.assertIsNone(self.request("context", target="Properties/PublishProfiles/Blob.cs")["context_revision"])
        with patch("migration_harness.verification.roslyn_context._MAX_ENTRIES", 27):
            dependency = self.worktree / "Features/node_modules"
            for index in range(32):
                (dependency / f"fixture-{index}.js").touch()
            self.assertEqual(self.request("context")["context_revision"], missing["context_revision"])
            self.assertEqual(self.request("semantic", content="class Main { NestedAllowed value = new(); }")["status"],
                             "clear")
        self.assertEqual(
            [item.relative_to(self.worktree).as_posix() for item, _ in
             editor_project_snapshot(self.worktree, "App.csproj", "Foo.cs").sources],
            ["Features/wwwroot/Nested.cs"],
        )
        (self.worktree / "wwwroot/Bar.cs").write_text("public class Bar { public decimal Value = 1; }", encoding="utf-8")
        self.assertEqual(self.request("context")["context_revision"], missing["context_revision"])
        models = self.worktree / "Models"
        models.mkdir()
        (models / "Bar.cs").write_text('public class Bar { public string Value = "ok"; }', encoding="utf-8")
        self.assertEqual(self.request("semantic", content="class Main { string Value(Bar b) => b.Value; }")["status"],
                         "clear")
        (models / "Bar.cs").unlink()
        (self.worktree / "App.csproj").write_text(_CSPROJ, encoding="utf-8")
        ordinary = self.request("semantic")
        self.assertEqual(ordinary["status"], "clear")
        self.assertNotEqual(ordinary["context_revision"], missing["context_revision"])
        self.assertIsNotNone(self.request("context", target="wwwroot/Bar.cs")["context_revision"])

    @unittest.skipUnless(os.name == "nt" and os.environ.get("GITHUB_ACTIONS") == "true",
                         "SDK comparison is restricted to the hosted Windows workflow")
    def test_installed_web_sdk_agrees_with_default_source_scope(self):
        oracle = self.root / "sdk-web-oracle"
        oracle.mkdir()
        (self.worktree / ".Hidden.cs").unlink()
        missing_source = "public class Foo { public Bar Value = new(); public ProfileOnly Profile = new(); }\n"
        for directory in (oracle, self.worktree):
            self._web_fixture(directory)
            (directory / "Foo.cs").write_text(missing_source, encoding="utf-8")
        run, build = self.sdk_build_oracle(oracle)
        actual = run(*build)
        self.assertNotEqual(actual.returncode, 0)
        self.assertIn("CS0246", actual.stdout + actual.stderr)
        self.assertIn("ProfileOnly", actual.stdout + actual.stderr)
        missing = self.request("semantic", content=missing_source)
        self.assertEqual(len(missing["findings"]), 2)
        self.assertEqual({item["code"] for item in missing["findings"]}, {"CS0246"})

        typed = "public class Main { public string Go(Bar b) => b.Value; }\n"
        for directory in (oracle, self.worktree):
            models = directory / "Models"
            models.mkdir()
            (models / "Bar.cs").write_text('public class Bar { public string Value = "ok"; }', encoding="utf-8")
            (directory / "Foo.cs").write_text(typed, encoding="utf-8")
            for index in range(5001):
                (directory / "Features/node_modules" / f"fixture-{index}.js").touch()
        actual = run(*build)
        self.assertEqual(actual.returncode, 0, actual.stdout + actual.stderr)
        self.assertEqual(self.request("semantic", content=typed)["status"], "clear")

        for directory in (oracle, self.worktree):
            (directory / "Models/Bar.cs").unlink()
            (directory / "App.csproj").write_text(_CSPROJ, encoding="utf-8")
            (directory / "Foo.cs").write_text(_REFERENCE_USER, encoding="utf-8")
            for asset in (directory / "Features/node_modules").glob("*.js"):
                asset.unlink()
        restored = run("restore", "App.csproj", "--source", str(self.root / "empty-feed"), "-p:NuGetAudit=false",
                       "--verbosity", "quiet")
        self.assertEqual(restored.returncode, 0, restored.stdout + restored.stderr)
        actual = run(*build)
        self.assertEqual(actual.returncode, 0, actual.stdout + actual.stderr)
        self.assertEqual(self.request("semantic")["status"], "clear")
        print("Windows .NET 9 Web oracle: excludes root assets and tooling, handles 5001 JS files, keeps plain SDK", flush=True)
