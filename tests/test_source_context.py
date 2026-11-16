import json
import os
import shutil
import stat
import tempfile
import unittest
from itertools import count
from pathlib import Path
from unittest.mock import patch

from migration_harness import source_context
from migration_harness.execution import ExecutionControl, OperationCancelled, control_scope
from migration_harness.files import sha256_file


class TargetContextTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.worktree = self.root / "worktree"
        self.capsule = self.root / "run/capsule"
        self.agent = self.root / "run/agent-workspace"
        self.targets = (Path("Pages/Orders/Index.cshtml"), Path("Pages/Orders/Index.cshtml.cs"))
        self.worktree.mkdir()
        self.agent.mkdir(parents=True)
        for target in self.targets:
            self.put(target, b"original target")

    def put(self, relative, content=b"reference", *, root=None):
        path = (root or self.worktree) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.chmod(0o600)
        path.write_bytes(content)
        return path

    def stage(self):
        return source_context.stage_target_context(self.worktree, self.targets, self.capsule, self.agent)

    def manifest(self):
        return json.loads((self.capsule / "target-context/manifest.json").read_text(encoding="utf-8"))

    def symlink(self, path, destination, *, directory=False):
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.symlink_to(destination, target_is_directory=directory)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")

    def test_parent_and_sibling_inc_keep_paths_nested_binary_and_empty_directories(self):
        expected = {
            "Pages/Orders/_ViewImports.cshtml": b"@using Example\r\n",
            "Pages/Orders/nested/画像.bin": bytes(range(256)),
            "Pages/inc/helpers/nested.cs": b"shared\r\n\x00\xff",
            "Pages/Orders/.hidden": b"hidden",
            "Pages/Orders/bin/asset.dat": b"do not silently omit ordinary directories",
        }
        for name, content in expected.items():
            self.put(name, content)
        (self.worktree / "Pages/inc/empty").mkdir()
        for name in ("Pages/Unrelated/private.cs", "Pages/_ViewImports.cshtml", "legacy/orders/list.php"):
            self.put(name)
        instructions = self.stage()
        self.assertIn("read-only", instructions)
        self.assertIn("relative paths", instructions)
        manifest = self.manifest()
        self.assertEqual({entry["path"] for entry in manifest["files"]}, set(expected))
        self.assertEqual({path.relative_to(self.agent).as_posix() for path in self.agent.rglob("*") if path.is_file()}, set(expected))
        self.assertTrue((self.agent / "Pages/inc/empty").is_dir())
        for entry in manifest["files"]:
            with self.subTest(entry=entry["path"]):
                path = self.agent / entry["path"]
                self.assertEqual(path.read_bytes(), expected[entry["path"]])
                self.assertEqual(entry["sha256"], sha256_file(path))
                self.assertEqual(entry["source"], str(self.worktree / entry["path"]))
                self.assertFalse(path.stat().st_mode & stat.S_IWUSR)
                self.assertTrue((self.worktree / entry["path"]).stat().st_mode & stat.S_IWUSR)

    def test_libs_and_extensions_are_pinned_one_level_above_inc(self):
        self.targets = (Path("src/Web/Pages/Orders/Index.cshtml.cs"),)
        expected = {
            "src/Web/Libs/nested/Shared.cs": b"library",
            "src/Web/Extensions/Format.cs": b"extension",
            "src/Web/Pages/inc/Helper.cs": b"include",
        }
        for name, content in expected.items():
            self.put(name, content)
        for name in ("src/Web/Pages/Libs/WrongLevel.cs", "src/Web/Pages/Extensions/WrongLevel.cs",
                     "Libs/WrongRoot.cs", "Extensions/WrongRoot.cs"):
            self.put(name)

        instructions = self.stage()
        self.assertIn("Libs and Extensions", instructions)
        self.assertEqual({entry["path"] for entry in self.manifest()["files"]}, set(expected))
        for name, content in expected.items():
            with self.subTest(name=name):
                path = self.agent / name
                self.assertEqual(path.read_bytes(), content)
                self.assertFalse(path.stat().st_mode & stat.S_IWUSR)
                self.put(name, b"later worktree contents")
                self.put(name, b"agent edit", root=self.agent)

        self.stage()
        self.assertEqual({name: (self.agent / name).read_bytes() for name in expected}, expected)

    def test_targets_and_interrupted_drafts_are_never_pinned_or_overwritten(self):
        self.put("Pages/Orders/helper.cs")
        for target in self.targets:
            self.put(target, b"interrupted draft", root=self.agent)
        self.stage()
        for target in self.targets:
            self.put(target, b"later worktree target")
        self.stage()
        self.assertEqual([entry["path"] for entry in self.manifest()["files"]], ["Pages/Orders/helper.cs"])
        for target in self.targets:
            self.assertEqual((self.agent / target).read_bytes(), b"interrupted draft")
            self.assertTrue((self.agent / target).stat().st_mode & stat.S_IWUSR)
            self.assertFalse((self.capsule / "target-context/files" / target).exists())

    def test_resume_and_repair_restore_pins_after_original_changes_or_disappears(self):
        relative = Path("Pages/inc/reference.bin")
        original = self.put(relative, b"after preparation scripts\x00\xff")
        self.stage()
        manifest_bytes = (self.capsule / "target-context/manifest.json").read_bytes()
        original.unlink()
        self.put("Pages/inc/new.cs", b"must not enter pinned context")
        self.put("Libs/new.cs", b"must not enter pinned context")
        for tampering in (b"agent edit", None):
            with self.subTest(tampering=tampering):
                self.put(relative, b"agent edit", root=self.agent)
                if tampering is None:
                    (self.agent / relative).unlink()
                self.stage()
                self.assertEqual((self.agent / relative).read_bytes(), b"after preparation scripts\x00\xff")
                self.assertFalse((self.agent / "Pages/inc/new.cs").exists())
                self.assertFalse((self.agent / "Libs/new.cs").exists())
                self.assertEqual((self.capsule / "target-context/manifest.json").read_bytes(), manifest_bytes)

    def test_same_parent_and_overlapping_inc_are_deduplicated(self):
        self.targets += (Path("Pages/inc/Shared.cs"), Path("Pages/Orders/nested/Other.cs"), self.targets[0])
        for target in self.targets:
            self.put(target)
        self.put("Pages/Orders/nested/reference.cs")
        self.put("Pages/inc/reference.cs")
        self.stage()
        manifest = self.manifest()
        self.assertEqual(len(manifest["files"]), 2)
        self.assertEqual(len(manifest["directories"]), len(set(manifest["directories"])))
        self.assertEqual(set(manifest["targets"]), {path.as_posix() for path in self.targets})

    def test_root_target_excludes_metadata_and_top_level_harness_files_only(self):
        self.targets = (Path("Target.cs"), Path("inc/Target.cs"))
        for target in self.targets:
            self.put(target)
        excluded = (".git", ".hg/store/file", ".svn/file", "nested/.git/config",
                    ".migration-materials/shared.md", ".migration-context.md", ".migration-other/file",
                    "migration-complete-0001.txt", "checker-findings-1.md", "migration-read-rules.py",
                    "migration-rule-reads-0001.json", "migration-rule-reads-cache/file")
        for name in excluded:
            self.put(name)
        allowed = {"inc/reference.cs", "nested/.migration-context.md", "README.md",
                   "nested/migration-read-rules.py", "nested/migration-rule-reads-0001.json"}
        for name in allowed:
            self.put(name)
        self.stage()
        files = {entry["path"] for entry in self.manifest()["files"]}
        self.assertTrue(allowed <= files)
        self.assertFalse(files.intersection(excluded))
        self.assertNotIn("Target.cs", files)
        self.assertNotIn("inc/Target.cs", files)
        self.assertEqual(len(files), len(self.manifest()["files"]))

    def test_new_target_directory_can_still_use_sibling_inc(self):
        self.targets = (Path("New/Orders/Index.cshtml"),)
        self.put("New/inc/helper.cs")
        self.stage()
        self.assertEqual((self.agent / "New/inc/helper.cs").read_bytes(), b"reference")
        self.assertFalse((self.agent / self.targets[0]).exists())

    def test_legacy_first_call_pins_even_empty_context(self):
        self.assertEqual(self.stage(), "")
        self.assertEqual(self.manifest()["files"], [])
        self.put("Pages/Orders/late.cs")
        self.assertEqual(self.stage(), "")
        self.assertFalse((self.agent / "Pages/Orders/late.cs").exists())

    def test_symlinks_in_context_are_rejected_including_dangling_and_directories(self):
        outside = self.put("outside.txt", b"private", root=self.root)
        for name, destination, directory in (
            ("Pages/Orders/link", outside, False),
            ("Pages/Orders/link", self.root / "missing", False),
            ("Pages/Orders/link", self.root, True),
            ("Pages/inc", self.root, True),
        ):
            with self.subTest(name=name, destination=destination):
                link = self.worktree / name
                self.symlink(link, destination, directory=directory)
                try:
                    with self.assertRaisesRegex(ValueError, "symlink.*replace"):
                        self.stage()
                    self.assertFalse((self.capsule / "target-context").exists())
                finally:
                    link.unlink()

    def test_symlink_in_target_parent_is_rejected(self):
        self.targets = (Path("Alias/Target.cs"),)
        self.symlink(self.worktree / "Alias", self.root, directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.stage()

    def test_agent_symlink_cannot_overwrite_outside_file(self):
        relative = "Pages/Orders/helper.cs"
        self.put(relative)
        outside = self.put("outside.txt", b"private", root=self.root)
        self.symlink(self.agent / relative, outside)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.stage()
        self.assertEqual(outside.read_bytes(), b"private")

    def test_agent_parent_symlink_is_rejected(self):
        self.put("Pages/Orders/helper.cs")
        self.symlink(self.agent / "Pages", self.root, directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.stage()
        self.assertFalse((self.root / "Orders").exists())

    def test_missing_or_corrupted_snapshot_never_repins_live_references(self):
        relative = "Pages/Orders/helper.cs"
        self.put(relative, b"pinned")
        self.stage()
        pin = self.capsule / "target-context/files" / relative
        self.put(relative, b"new live bytes")
        self.put(relative, b"corrupted", root=self.capsule / "target-context/files")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            self.stage()
        self.assertEqual((self.agent / relative).read_bytes(), b"pinned")
        pin.unlink()
        with self.assertRaisesRegex(ValueError, "missing"):
            self.stage()
        (self.capsule / "target-context/manifest.json").unlink()
        with self.assertRaisesRegex(ValueError, "manifest is missing"):
            self.stage()

    def test_saved_snapshot_symlink_is_rejected_even_with_identical_contents(self):
        relative = "Pages/Orders/helper.cs"
        original = self.put(relative)
        self.stage()
        pin = self.capsule / "target-context/files" / relative
        pin.chmod(0o600)
        pin.unlink()
        self.symlink(pin, original)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.stage()

    def test_file_change_during_copy_fails_without_publishing_snapshot(self):
        original = self.put("Pages/Orders/helper.cs")
        copyfile = shutil.copyfile

        def mutate(source, destination, **kwargs):
            result = copyfile(source, destination, **kwargs)
            if source == original:
                original.write_bytes(b"changed during copy")
            return result

        with patch.object(source_context.shutil, "copyfile", side_effect=mutate):
            with self.assertRaisesRegex(ValueError, "changed while copying"):
                self.stage()
        self.assertFalse((self.capsule / "target-context").exists())
        self.assertFalse(list(self.capsule.glob(".target-context-*")))

    def test_directory_change_during_traversal_fails_without_publishing_snapshot(self):
        original = self.put("Pages/Orders/helper.cs")
        copyfile = shutil.copyfile

        def mutate(source, destination, **kwargs):
            result = copyfile(source, destination, **kwargs)
            if source == original:
                self.put("Pages/Orders/late.cs")
            return result

        with patch.object(source_context.shutil, "copyfile", side_effect=mutate):
            with self.assertRaisesRegex(ValueError, "changed while copying"):
                self.stage()
        self.assertFalse((self.capsule / "target-context").exists())

    def test_traversal_reports_activity_and_can_be_cancelled_then_retried(self):
        self.put("Pages/Orders/nested/reference.cs")
        events = []

        def cancel(event):
            events.append(event)
            if "nested" in event["message"]:
                control.cancelled.set()

        control = ExecutionControl(cancel)
        with patch.object(source_context.time, "monotonic", side_effect=count(0, 2)), control_scope(control), self.assertRaises(OperationCancelled):
            self.stage()
        self.assertTrue(events)
        self.assertFalse((self.capsule / "target-context").exists())
        self.stage()
        manifest_bytes = (self.capsule / "target-context/manifest.json").read_bytes()
        control.cancelled.clear()
        with patch.object(source_context.time, "monotonic", side_effect=count(0, 2)), control_scope(control), self.assertRaises(OperationCancelled):
            self.stage()
        self.assertEqual((self.capsule / "target-context/manifest.json").read_bytes(), manifest_bytes)

    def test_activity_is_throttled_independently_of_per_entry_cancellation(self):
        for index in range(260):
            self.put(f"Pages/Orders/{index}.cs")
        events = []
        with patch.object(source_context.time, "monotonic", return_value=0), control_scope(ExecutionControl(events.append)):
            with patch.object(source_context, "check_cancelled", wraps=source_context.check_cancelled) as checked:
                self.stage()
        self.assertEqual(len(events), 2)
        self.assertGreater(checked.call_count, 520)
        self.assertIn("260", events[-1]["message"])

    def test_reserved_and_escaping_targets_are_rejected(self):
        for name in (".migration-context.md", ".migration-materials/Target.cs", "../Target.cs",
                     "/Target.cs", ".git/config", "migration-complete-1", "checker-findings-1", ".",
                     "migration-read-rules.py", "migration-rule-reads-0001.json"):
            with self.subTest(name=name):
                self.targets = (Path(name),)
                with self.assertRaisesRegex(ValueError, "reserved or invalid"):
                    self.stage()

    def test_manifest_cannot_restore_targets_or_escape_context(self):
        self.put("Pages/Orders/helper.cs")
        self.put(self.targets[0], b"draft", root=self.agent)
        self.stage()
        manifest = self.manifest()
        manifest_path = self.capsule / "target-context/manifest.json"
        for name in (self.targets[0].as_posix(), "../../outside", "/outside", "Pages/Other/private.cs",
                     ".migration-context.md"):
            with self.subTest(name=name):
                manifest["files"][0]["path"] = name
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                with self.assertRaises(ValueError):
                    self.stage()
                self.assertEqual((self.agent / self.targets[0]).read_bytes(), b"draft")

    def test_changed_target_set_cannot_reuse_snapshot(self):
        self.put("Pages/Orders/helper.cs")
        self.stage()
        self.targets += (Path("Pages/Orders/helper.cs"),)
        with self.assertRaisesRegex(ValueError, "changed targets"):
            self.stage()

    def test_invalid_manifest_entry_types_and_target_descendants_are_rejected(self):
        self.put("Pages/Orders/helper.cs")
        self.stage()
        original = self.manifest()
        manifest_path = self.capsule / "target-context/manifest.json"
        cases = (
            ("directories", [{"path": "Pages/Orders"}]),
            ("files", ["Pages/Orders/helper.cs"]),
            ("files", [{"path": "Pages/Orders/helper.cs", "sha256": "invalid"}]),
            ("directories", [str(self.targets[0] / "nested")]),
        )
        for key, value in cases:
            with self.subTest(key=key, value=value):
                manifest_path.write_text(json.dumps({**original, key: value}), encoding="utf-8")
                with self.assertRaises(ValueError):
                    self.stage()
                self.assertFalse((self.agent / self.targets[0]).exists())

    def test_capsule_cannot_overlap_agent_workspace(self):
        self.capsule = self.agent / "capsule"
        with self.assertRaisesRegex(ValueError, "separate directories"):
            self.stage()

    def test_output_inside_worktree_is_rejected_before_traversal(self):
        for output in ("capsule", "agent"):
            with self.subTest(output=output):
                original = getattr(self, output)
                setattr(self, output, self.worktree / output)
                try:
                    with self.assertRaisesRegex(ValueError, "outputs must be outside"):
                        self.stage()
                finally:
                    setattr(self, output, original)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO test requires POSIX")
    def test_special_files_fail_instead_of_being_skipped_or_blocking(self):
        os.mkfifo(self.worktree / "Pages/Orders/pipe")
        with self.assertRaisesRegex(ValueError, "regular file"):
            self.stage()


if __name__ == "__main__":
    unittest.main()
