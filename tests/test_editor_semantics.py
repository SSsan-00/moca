import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from migration_harness.verification import roslyn_context, roslyn_syntax


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
