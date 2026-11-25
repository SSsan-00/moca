import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import tomlkit

from fixtures import HarnessCase, prepare_fixture
from migration_harness.context import capsules
from migration_harness.junie import junie_cli
from migration_harness.web import review_anchors
from migration_harness import workflow
from migration_harness.config import load_config
from migration_harness.web.diff_viewer import capture_workspace, review_html
from migration_harness.web.settings_form import build_settings_draft


class ConfigFilePathTests(HarnessCase):
    """設定の表記を変えても、原文の固定・比較・分離した編集先が変わらないことを確認する。"""

    def setUp(self):
        super().setUp()
        self.source = self.project / "注文.inc"
        self.source.write_bytes(b"<?php\nfunction total() { return 42; }\n")
        (self.project / "Page.cs").write_text("class Page { int Total() => 42; }\n", encoding="utf-8")
        (self.project / "App.csproj").write_text('<Project Sdk="Microsoft.NET.Sdk" />', encoding="utf-8")
        self.commit_project()
        self.reference = self.root / "対応 例.json"
        self.reference.write_text(json.dumps({
            "kind": "diff-viewer-workspace", "version": 1,
            "workspace": {
                "leftText": "function example() {}", "rightText": "void Example() {}",
                "anchors": {"manualAnchors": [{"leftLineNo": 0, "rightLineNo": 0}]},
            },
        }), encoding="utf-8")
        self.viewer = self.root / "viewer.html"
        self.viewer.write_text('<body><input id="workspace-import-file"></body>', encoding="utf-8")
        self.raw = {
            "project": {"root": "app", "worktree_root": "trees", "run_root": "runs"},
            "diff_viewer": {"html": "../viewer.html", "anchor_reference": self.reference.name},
            "build": {"project": "App.csproj"},
            "units": [{"id": "orders", "source": self.source.name, "target": "Page.cs"}],
        }

    def load(self):
        path = self.root / "harness.toml"
        path.write_text(tomlkit.dumps(self.raw), encoding="utf-8")
        return load_config(path)

    def test_absolute_and_relative_paths_have_the_same_worktree_destinations(self):
        relative = self.load()
        self.assertEqual(relative["max_context_chars"], 250_000)
        self.raw["project"].update(root=str(self.project), worktree_root=str(self.root / "trees"), run_root=str(self.root / "runs"))
        self.raw["units"][0].update(source=str(self.source), target=str(self.project / "Page.cs"))
        self.raw["build"]["project"] = str(self.project / "App.csproj")
        self.raw["diff_viewer"].update(html=str(self.viewer), anchor_reference=str(self.reference))
        absolute = self.load()
        self.assertEqual(relative, absolute)
        prepared = prepare_fixture(absolute, "orders")
        record = json.loads((Path(prepared["run_path"]) / "run.json").read_bytes())
        with patch.object(workflow.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as build:
            workflow._build_worktree(absolute, prepared, record)
        self.assertEqual(build.call_args.args[0][-1], str(Path(prepared["worktree_path"]) / "App.csproj"))
        self.assertNotEqual(build.call_args.kwargs["cwd"], self.project)

    def test_outside_edit_destinations_are_rejected(self):
        for section, key in (("units", "target"), ("build", "project")):
            with self.subTest(section=section):
                entry = self.raw[section][0] if section == "units" else self.raw[section]
                before = entry[key]
                entry[key] = str(self.root / "outside.csproj")
                with self.assertRaisesRegex(ValueError, "inside the application repository"):
                    self.load()
                entry[key] = before

    def test_source_accepts_no_extension_and_arbitrary_extensions(self):
        for name in ("legacy", "legacy.inc", "legacy.tpl", "legacy.html", "legacy.PHP", "legacy.custom"):
            with self.subTest(name=name):
                self.raw["units"][0]["source"] = name
                source = self.load()["units"][0]["inputs"][0]
                self.assertEqual(source["path"], name)
                self.assertTrue(capsules.is_php_input(source))
        self.assertTrue(capsules.is_php_input({"role": "legacy_custom_role", "path": "legacy.php"}))

    def test_external_inputs_are_pinned_and_non_php_extension_reaches_review(self):
        external = self.root / "legacy template"
        original = self.source.read_bytes()
        external.write_bytes(original)
        self.raw["units"][0]["source"] = str(external)
        rule = self.root / "rules.md"
        rule.write_text("Preserve the value 42.\n", encoding="utf-8")
        reference_php = self.root / "reference.inc"
        reference_php.write_text("<?php function example() {}\n", encoding="utf-8")
        reference_cs = self.root / "Reference.cs"
        reference_cs.write_text("class Example {}\n", encoding="utf-8")
        self.raw["project"].update(
            migration_rules=[{"path": "../rules.md"}],
            references=[{"role": "reference_php", "path": str(reference_php)},
                        {"role": "reference_csharp", "path": "../Reference.cs"}],
        )
        config = self.load()
        prepared = prepare_fixture(config, "orders")
        capsule = Path(prepared["capsule_path"])
        manifest = json.loads((capsule / "manifest.json").read_bytes())
        self.assertEqual({source["path"] for source in manifest["php_sources"]}, {external.as_posix(), reference_php.as_posix()})
        rules = "".join((capsule / "materials" / chunk["file"]).read_text(encoding="utf-8") for chunk in manifest["rule_files"])
        self.assertIn("Preserve the value 42.", rules)
        external.write_text("changed", encoding="utf-8")
        _, payload = capture_workspace(config, prepared["run_id"])
        self.assertIn(original.decode(), payload["workspace"]["leftText"])
        self.assertNotIn("changed", payload["workspace"]["leftText"])
        html = review_html(config, json.dumps(payload).encode())
        self.assertIn(b"harness-review-workspace", html)
        ledger = workflow.migration_ledger(config, prepared["run_id"])
        source = ledger["capsules"][0]["sources"][0]
        self.assertEqual(source["path"], external.as_posix())

    def test_configured_reference_is_passed_to_junie_and_errors_are_not_ignored(self):
        second_reference = self.root / "another.json"
        second_reference.write_text(self.reference.read_text(encoding="utf-8").replace("example", "other").replace("Example", "Other"), encoding="utf-8")
        self.raw["diff_viewer"]["anchor_reference"] = [self.reference.name, str(second_reference)]
        self.raw["junie"] = {"model": "sonnet", "effort": "high"}
        config = self.load()
        prepared = prepare_fixture(config, "orders")
        config.update(junie_model="opus", junie_effort="low")

        def junie(command, completion, token, targets, **kwargs):
            self.assertEqual(command[command.index("--model") + 1], "sonnet")
            self.assertEqual(command[command.index("--effort") + 1], "high")
            self.assertTrue(kwargs["prefer_task"])
            guide = json.loads((completion.parent / "guide.json").read_bytes())
            self.assertEqual(guide["examples"], [
                {"left": "function example() {}", "right": "void Example() {}"},
                {"left": "function other() {}", "right": "void Other() {}"},
            ])
            targets[0].write_text('[{"leftLineNo":2,"rightLineNo":1}]', encoding="utf-8")
            completion.write_text(token, encoding="ascii")
            return 0

        with patch.object(junie_cli, "require_interactive_cli"), patch.object(junie_cli, "run_interactive", side_effect=junie):
            output, count = review_anchors.generate_anchors(config, prepared["run_id"])
        self.assertEqual(count, 1)
        self.assertEqual(json.loads(output.read_bytes())["workspace"]["anchors"]["manualAnchors"], [{"leftLineNo": 1, "rightLineNo": 0}])
        second_reference.write_text("invalid", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, second_reference.name):
            review_anchors.generate_anchors(config, prepared["run_id"])

    def test_reference_form_round_trip_and_clear(self):
        content = tomlkit.dumps(self.raw)
        self.assertEqual(self.load()["diff_viewer_anchor_reference"], [self.reference])
        paths = [str(self.reference), "another.json"]
        changed = build_settings_draft(content, {"diff_viewer": {"anchor_reference": paths}})
        self.assertEqual(changed["form"]["diff_viewer"]["anchor_reference"], paths)
        self.raw = tomlkit.parse(changed["content"]).unwrap()
        self.assertEqual(self.load()["diff_viewer_anchor_reference"], [self.reference, self.root / "another.json"])
        cleared = build_settings_draft(changed["content"], {"diff_viewer": {"anchor_reference": []}})
        self.raw = tomlkit.parse(cleared["content"]).unwrap()
        self.assertEqual(self.load()["diff_viewer_anchor_reference"], [])

    def test_relative_draft_copy_source_and_absolute_destination(self):
        draft = self.root / "Draft.cs"
        draft.write_text("class Draft {}\n", encoding="utf-8")
        self.raw["scripts"] = {"before_migration": [["@copy-migration-draft", "../Draft.cs", str(self.project / "Page.cs")]]}
        config = self.load()
        self.assertEqual(config["scripts"]["before_migration"], [["@copy-migration-draft", str(draft), "Page.cs"]])
        prepared = prepare_fixture(config, "orders")
        worktree = Path(prepared["worktree_path"])
        with patch.dict(os.environ, {"PYTHONIOENCODING": "cp1252"}):
            workflow._run_script_phase(config, "before_migration", worktree, Path(prepared["run_path"]))
        log = Path(prepared["run_path"]) / "before_migration.log"
        self.assertIn("配置しました: Page.cs", log.read_text(encoding="utf-8"))
        self.assertEqual((worktree / "Page.cs").read_bytes(), draft.read_bytes())
        self.assertNotEqual((self.project / "Page.cs").read_bytes(), draft.read_bytes())

    def test_unit_draft_imports_are_matched_and_staged_only_for_the_selected_unit(self):
        razor = self.root / "Draft.CSHTML"
        code = self.root / "Draft.cshtml.cs"
        razor_bytes = b"<p>Draft</p>\r\n"
        razor.write_bytes(razor_bytes)
        orders = self.raw["units"][0]
        del orders["target"]
        orders["targets"] = ["Pages/Orders.cshtml.cs", "Pages/Orders.cshtml"]
        self.raw["units"].append({**orders, "id": "other"})
        sources = ["../Draft.CSHTML", str(code)]
        edited = build_settings_draft(tomlkit.dumps(self.raw), {"units": {"0": {"draft_imports": sources}}})
        self.assertEqual(edited["form"]["units"][0]["draft_imports"], sources)
        self.assertEqual(edited["form"]["units"][1]["draft_imports"], [])
        cleared = build_settings_draft(edited["content"], {"units": {"0": {"draft_imports": []}}})
        self.assertNotIn("draft_imports", cleared["content"])
        self.raw = tomlkit.parse(edited["content"]).unwrap()
        config = self.load()

        def junie(command, completion, token, targets, **kwargs):
            if completion.parent.parent.name == imported_id:
                self.assertEqual([path.read_bytes() for path in targets], [code.read_bytes(), razor_bytes])
            else:
                self.assertTrue(all(not path.exists() for path in targets))
            for path in targets:
                path.write_text("migrated\n", encoding="utf-8")
            completion.write_text(token, encoding="ascii")
            return 0

        with (
            patch.object(junie_cli, "require_interactive_cli"),
            patch.object(junie_cli, "run_interactive", side_effect=junie),
            patch.object(workflow, "_verify_worktree"),
        ):
            imported = prepare_fixture(config, "orders")
            imported_id = imported["run_id"]
            with self.assertRaises(workflow.RunScriptError):
                workflow.resume_with_junie(config, imported_id)
            self.assertEqual((Path(imported["worktree_path"]) / orders["targets"][1]).read_bytes(), razor_bytes)
            razor.write_bytes(b"changed after successful copy")
            code.write_bytes(b"class Draft {}\r\n")
            workflow.resume_with_junie(config, imported_id)
            workflow.run_with_junie(config, "other")
        self.assertFalse((self.project / "Pages").exists())

    def test_unit_draft_imports_reject_missing_ambiguous_or_duplicate_matches(self):
        unit = self.raw["units"][0]
        unit["draft_imports"] = ["Draft.txt"]
        self.assertEqual(self.load()["units"][0]["draft_import_commands"][0][-1], "Page.cs")
        del unit["target"]
        unit["targets"] = ["Page.cshtml", "Page.cshtml.cs"]
        with self.assertRaisesRegex(ValueError, "対応する移行先がありません"):
            self.load()
        unit["draft_imports"] = ["Draft.cs"]
        self.assertEqual(self.load()["units"][0]["draft_import_commands"][0][-1], "Page.cshtml.cs")
        unit["draft_imports"].append("Other.cshtml.cs")
        with self.assertRaisesRegex(ValueError, "対応するコピー元が複数"):
            self.load()
        unit.update(targets=["Page.cs", "Other.cs"], draft_imports=["Draft.cs"])
        with self.assertRaisesRegex(ValueError, "対応する移行先が複数"):
            self.load()
