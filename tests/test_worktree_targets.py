import codecs
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from migration_harness import editor_queue, worktree_targets


class TargetFileTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.relative = Path("Page.cs")
        self.path = self.root / self.relative

    def save(self, content, version):
        worktree_targets.save_target_edit(self.root, (self.relative,), {
            "target": self.relative.as_posix(), "version": version, "content": content,
        })

    def test_readers_share_bytes_but_only_editor_normalizes_newlines(self):
        self.assertEqual(worktree_targets.read_target(self.root, self.relative), (None, ""))
        for encoding in ("utf-8", "utf-8-sig"):
            for text in ("", "// 日本語😀\r\nclass Page {}\r// end\n"):
                with self.subTest(encoding=encoding, text=text):
                    data = text.encode(encoding)
                    self.path.write_bytes(data)
                    self.assertEqual(worktree_targets.read_target(self.root, self.relative), (data, text))
                    self.assertEqual(editor_queue.editor_target(self.root, self.relative),
                                     (data, text.replace("\r\n", "\n").replace("\r", "\n")))

    def test_readers_reject_non_utf8_binary_and_paths_outside_workspace(self):
        for reader in (worktree_targets.read_target, editor_queue.editor_target):
            for data in ("日本語".encode("cp932"), b"class Page {}\0"):
                with self.subTest(reader=reader.__name__, data=data):
                    self.path.write_bytes(data)
                    with self.assertRaises(ValueError):
                        reader(self.root, self.relative)
                    self.assertEqual(self.path.read_bytes(), data)
            with self.assertRaisesRegex(ValueError, "escapes"):
                reader(self.root, Path("../Page.cs"))

    def test_save_preserves_newlines_adds_one_bom_and_rejects_stale_edits(self):
        for newline in ("\n", "\r\n", "\r"):
            for encoding in ("utf-8", "utf-8-sig"):
                with self.subTest(newline=newline, encoding=encoding):
                    original = ("// 元の内容" + newline).encode(encoding)
                    self.path.write_bytes(original)
                    version = worktree_targets.target_hash(self.path)
                    self.save("// 日本語😀\nclass Page {}\n", version)
                    expected = ("// 日本語😀" + newline + "class Page {}" + newline).encode("utf-8-sig")
                    self.assertEqual(self.path.read_bytes(), expected)
                    self.save("// 日本語😀\nclass Page {}\n", worktree_targets.target_hash(self.path))
                    self.assertEqual(self.path.read_bytes(), expected)
                    with self.assertRaisesRegex(ValueError, "更新"):
                        self.save("old edit", version)
                    self.assertEqual(self.path.read_bytes(), expected)

    def test_saved_size_limit_includes_bom_and_failed_save_keeps_existing_file(self):
        with patch.object(worktree_targets, "EDIT_BYTES", 8):
            self.save("abcde", None)
            expected = codecs.BOM_UTF8 + b"abcde"
            self.assertEqual(worktree_targets.read_target(self.root, self.relative), (expected, "abcde"))
            with self.assertRaisesRegex(ValueError, "8 MiB"):
                self.save("abcdef", worktree_targets.target_hash(self.path))
            self.assertEqual(self.path.read_bytes(), expected)
            self.assertEqual(list(self.root.iterdir()), [self.path])
            self.path.write_bytes(expected + b"f")
            for reader in (worktree_targets.read_target, editor_queue.editor_target):
                with self.assertRaisesRegex(ValueError, "8 MiB"):
                    reader(self.root, self.relative)
