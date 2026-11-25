import io
import os
import stat
import subprocess
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from migration_harness.verification import roslyn_context, roslyn_syntax
from migration_harness.verification.roslyn_editor_worker import EditorSemanticWorker


class EditorSemanticTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.project = self.root / "Fixture.csproj"
        self.descriptor = ('<Project Sdk="Microsoft.NET.Sdk.Web"><PropertyGroup>'
                           '<TargetFramework>net9.0</TargetFramework></PropertyGroup></Project>')
        self.project.write_text(self.descriptor, encoding="utf-8")

    def test_uncertain_project_does_not_launch_analyzer_or_show_speculative_errors(self):
        content = "class Draft { UnresolvedType? Selected; }"
        for reason, descriptor in [
            ("project_configuration", self.descriptor.replace("</Project>",
                '<Import Project="Override.props"/></Project>')),
            ("project_configuration", self.descriptor.replace("</Project>",
                '<ItemGroup><ProjectReference Include="Shared.csproj"/></ItemGroup></Project>')),
            ("project_configuration", self.descriptor.replace("net9.0", "net8.0")),
            ("project_configuration", self.descriptor.replace("<PropertyGroup>", '<PropertyGroup Condition="true">')),
        ]:
            with self.subTest(descriptor=descriptor), patch.object(roslyn_syntax, "_prepare_tool") as prepare:
                self.project.write_text(descriptor, encoding="utf-8")
                report = roslyn_syntax.editor_semantic_diagnostics(self.root, "Fixture.csproj", "Page.cs", content, "v2")
                prepare.assert_not_called()
                self.assertEqual((report["status"], report["reason"], report["findings"], report["version"]),
                                 ("unavailable", reason, [], "v2"))

    def test_never_reads_unbounded_sibling_inputs_or_allows_unselected_project(self):
        (self.root / "Sibling.cs").write_bytes(b"class Sibling {}" * 16)
        with patch.object(roslyn_context, "_MAX_BYTES", 8):
            with self.assertRaisesRegex(roslyn_context.ProjectContextUnavailable, "project_too_large"):
                roslyn_context.editor_project_context(self.root, "Fixture.csproj", "Page.cs")
        with self.assertRaisesRegex(roslyn_context.ProjectContextUnavailable, "project_required"):
            roslyn_context.editor_project_context(self.root, "Fixture.sln", "Page.cs")
        nested = self.root / "Nested"
        nested.mkdir()
        (nested / self.project.name).write_text(self.descriptor, encoding="utf-8")
        with self.assertRaisesRegex(roslyn_context.ProjectContextUnavailable, "project_required"):
            roslyn_context.editor_project_context(self.root, "Nested/Fixture.csproj", "Elsewhere.cs")

    def test_callers_can_never_share_or_block_syntax_slots_with_background_semantic_work(self):
        with patch.object(roslyn_syntax, "editor_project_context") as project, roslyn_syntax._SEMANTIC_SLOT:
            result = roslyn_syntax.editor_semantic_diagnostics(self.root, "Fixture.csproj", "Page.cs", "", "v3")
            self.assertEqual((result["reason"], result["version"]), ("analyzer_busy", "v3"))
            project.assert_not_called()
            self.assertTrue(roslyn_syntax._EDITOR_SLOTS.acquire(blocking=False))
            roslyn_syntax._EDITOR_SLOTS.release()

    def test_metadata_probe_reuses_unchanged_sdk_description_and_does_not_read_source_text(self):
        dependency = self.root / "Dependency.cs"
        imports = self.root / "_ViewImports.cshtml"
        dependency.write_bytes(b"class Dependency {}")
        imports.write_bytes(b"@using System")
        def revision():
            return roslyn_syntax.editor_context_revision(
                self.root, "Fixture.csproj", "Pages/Index.cshtml", "v4",
            )["context_revision"]

        with patch.object(roslyn_context, "_read_file", wraps=roslyn_context._read_file) as read:
            versions = [revision()]
            self.assertEqual(revision(), versions[0])
            dependency.write_bytes(b"public class Dependency {}")
            versions.append(revision())
            imports.write_bytes(b"@using Other.Namespace")
            versions.append(revision())
            self.assertEqual([call.args[0] for call in read.call_args_list], [self.project])

            self.project.write_text(self.descriptor.replace("net9.0", "net8.0"), encoding="utf-8")
            versions.append(revision())
            dependency.unlink()
            versions.append(revision())
            self.assertEqual([call.args[0] for call in read.call_args_list], [self.project, self.project])
            self.project.write_text("<invalid>", encoding="utf-8")
            versions.append(revision())
            self.assertEqual(revision(), versions[-1])
            self.assertEqual(len(read.call_args_list), 3)
            self.project.write_text(self.descriptor, encoding="utf-8")
            versions.append(revision())
            self.assertTrue(all(versions))
            self.assertEqual(len(set(versions)), len(versions))

    def test_small_project_input_reads_only_expected_size_and_rejects_unstable_bytes(self):
        path = self.root / "Dependency.cs"
        original = b"class D {}\\r\\n"
        path.write_bytes(original)
        expected = roslyn_context._stamp(path.stat())

        class ObservedRead(io.BytesIO):
            def read(self, size):
                requested.append(size)
                return super().read(size)

        for data in (original, original + b"x", original[:-1]):
            with self.subTest(data=data):
                requested = []
                with patch.object(Path, "open", return_value=ObservedRead(data)):
                    if data == original:
                        self.assertEqual(data, roslyn_context._read_file(path, 8 * 1024 * 1024, expected))
                    else:
                        with self.assertRaisesRegex(roslyn_context.ProjectContextUnavailable, "project_changed"):
                            roslyn_context._read_file(path, 8 * 1024 * 1024, expected)
                self.assertEqual([len(original) + 1], requested)
        with patch.object(Path, "open") as open_file:
            with self.assertRaisesRegex(roslyn_context.ProjectContextUnavailable, "project_too_large"):
                roslyn_context._read_file(path, len(original) - 1, expected)
            open_file.assert_not_called()

    def test_read_rejects_a_same_length_rewrite_with_changed_stamp(self):
        path = self.root / "Dependency.cs"
        before, after = b"class A {}", b"class B {}"
        path.write_bytes(before)
        info = path.stat()
        open_path = Path.open

        class RewriteDuringRead(io.BytesIO):
            def read(self, size):
                result = super().read(size)
                with open_path(path, "wb") as destination:
                    destination.write(after)
                os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns - 1_000_000_000))
                return result

        with patch.object(Path, "open", return_value=RewriteDuringRead(before)):
            with self.assertRaisesRegex(roslyn_context.ProjectContextUnavailable, "project_changed"):
                roslyn_context._read_file(path, 1024, roslyn_context._stamp(info))
        self.assertEqual(path.read_bytes(), after)
        self.assertEqual(path.stat().st_size, info.st_size)
        self.assertNotEqual(path.stat().st_mtime_ns, info.st_mtime_ns)

    def test_dotted_csharp_reparse_point_does_not_supply_a_reference_version(self):
        name = ".Linked.cs"
        metadata = SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
        entry = SimpleNamespace(name=name, path=str(self.root / name), is_dir=lambda **kw: False,
                                stat=lambda **kw: metadata)
        with patch.object(roslyn_context.os, "scandir", return_value=nullcontext(iter([entry]))):
            reply = roslyn_syntax.editor_context_revision(self.root, "Fixture.csproj", "Page.cs", "v6")
        self.assertEqual((reply["context_revision"], reply["reason"]), (None, "project_configuration"))

    def test_semantic_response_rejects_an_input_changed_after_the_snapshot(self):
        dependency = self.root / "Dependency.cs"
        dependency.write_bytes(b"class Dependency {}")
        original = roslyn_context.editor_project_snapshot(self.root, "Fixture.csproj", "Page.cs").revision

        def changed(*args):
            dependency.write_bytes(b"public class RenamedDependency {}")
            return subprocess.CompletedProcess([], 0, b'{"findings":[],"razor":[],"skipped":[],"truncated":false}')

        with patch.object(roslyn_syntax, "_prepare_tool", return_value=[]), (
            patch.object(roslyn_syntax, "_framework_references", return_value=[])
        ), patch.object(roslyn_syntax, "_run_editor", side_effect=changed):
            report = roslyn_syntax.editor_semantic_diagnostics(
                self.root, "Fixture.csproj", "Page.cs", "class Page {}", "v5",
            )
        self.assertEqual((report["status"], report["reason"], report["context_revision"]),
                         ("unavailable", "project_changed", original))
        self.assertFalse(report["findings"])

    def test_warm_helper_does_not_reuse_foreign_drafts_or_old_project_dependencies(self):
        first, other = self.root / "first", self.root / "other"
        for directory, member in ((first, "int    Value"), (other, "string Value")):
            directory.mkdir()
            (directory / "Fixture.csproj").write_text(self.descriptor, encoding="utf-8")
            (directory / "Target.cs").write_text("class Unedited {}", encoding="utf-8")
            (directory / "Dependency.cs").write_text(f"public class D {{ public {member}; }}", encoding="utf-8")
        worker = EditorSemanticWorker()
        self.addCleanup(worker.close)
        draft = "// 日本語\r\npublic class Subject { public int Read(D dependency) => dependency.Value; }\r\n"

        def report(project, version):
            return roslyn_syntax.editor_semantic_diagnostics(
                project, "Fixture.csproj", "Target.cs", draft, version, worker=worker,
            )

        clean = report(first, "uncommitted-a")
        self.assertEqual(("clear", [], "uncommitted-a"), (clean["status"], clean["findings"], clean["version"]))
        helper = worker._helper
        wrong = report(other, "uncommitted-b")
        self.assertIs(worker._helper, helper)
        self.assertEqual(("errors", "uncommitted-b", [(2, "CS0029")]),
                         (wrong["status"], wrong["version"], [(finding["line"], finding["code"])
                                                             for finding in wrong["findings"]]))
        self.assertNotEqual(clean["context_revision"], wrong["context_revision"])

        dependency = first / "Dependency.cs"
        run = roslyn_syntax._run_editor
        def during_analysis(*args, **kwargs):
            result = run(*args, **kwargs)
            before = dependency.stat()
            dependency.write_text("public class D { public string Value; }", encoding="utf-8")
            os.utime(dependency, ns=(before.st_atime_ns, before.st_mtime_ns - 1_000_000_000))
            self.assertEqual(dependency.stat().st_size, before.st_size)
            return result

        with patch.object(roslyn_syntax, "_run_editor", side_effect=during_analysis):
            changed = report(first, "uncommitted-c")
        self.assertEqual(("project_changed", [], "uncommitted-c"),
                         (changed["reason"], changed["findings"], changed["version"]))
        next_report = report(first, "uncommitted-d")
        self.assertIs(worker._helper, helper)
        self.assertNotEqual(clean["context_revision"], next_report["context_revision"])
        self.assertEqual(["CS0029"], [finding["code"] for finding in next_report["findings"]])
        for directory in (first, other):
            self.assertEqual("class Unedited {}", (directory / "Target.cs").read_text(encoding="utf-8"))
