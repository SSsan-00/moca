import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from migration_harness.capsules import prepare_context
from migration_harness.context_evidence import boundary_hints, inspect_context, read_context


class ContextEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "sample.php"
        self.run = self.root / "saved"

    def prepare(self, source, limit=850, encoding="auto", start=1, end=None):
        self.source.write_bytes(source)
        unit = {"id": "demo", "source": "sample.php", "target": "Sample.cs", "inputs": [{
            "role": "php", "path": "sample.php", "start_line": start, "end_line": end,
            "encoding": encoding, "shared": False,
        }]}
        prepare_context({"project_root": str(self.root), "max_context_chars": limit}, unit, self.run)
        return json.loads((self.run / "capsule/manifest.json").read_text(encoding="utf-8"))

    def test_run_directory_alias_is_resolved_before_capsule_containment(self):
        self.prepare(b"<?php\necho 42;\n")
        alias = self.root / "nested"
        alias.mkdir()
        self.assertEqual(
            inspect_context(self.run, 1),
            inspect_context(alias / ".." / self.run.name, 1),
        )

    def test_fit_named_functions_before_preceding_capsule_runs_out(self):
        body = "".join(
            f"function transform{index}($amount) {{\r\n"
            "    if ($amount > 0) {\r\n"
            "        $amount = strlen('not a brace }'); // another {\r\n"
            "    }\r\n"
            "    return $amount;\r\n"
            "}\r\n"
            for index in range(4)
        )
        source = ("<?php\r\n" + body).encode()
        manifest = self.prepare(source, limit=555)
        self.assertEqual([f"transform{i}" for i in range(4)], [
            row["name"] for row in manifest["syntax_boundaries"]["sample.php"]["functions"]
        ])
        self.assertTrue(manifest["syntax_boundaries_analyzed"])
        self.assertTrue(all(len(fn["capsules"]) == 1 for fn in manifest["syntax_boundaries"]["sample.php"]["functions"]))
        self.assertTrue(all(part["chars"] <= 555 for part in manifest["parts"]))
        self.assertEqual(list(range(1, 26)), [
            line for part in manifest["parts"] for item in part["sources"]
            for line in range(item["start_line"], item["end_line"] + 1)
        ])
        catalog = inspect_context(self.run, 1)
        self.assertLess(len(catalog["sources"][0]["functions"]), 4)
        self.assertEqual(hashlib.sha256(source).hexdigest(), catalog["sources"][0]["sha256"])

    def test_pages_read_original_euc_jp_crlf_after_live_file_changes(self):
        source = "<?php\r\nfunction 計算() {\r\n    return '商品';\r\n}\r\n"
        self.prepare(source.encode("euc-jp"), encoding="euc-jp", start=2)
        self.source.write_bytes(b"unrelated changed worktree")
        pages = []
        offset = 0
        while offset is not None:
            result = read_context(self.run, "php-0001", 2, 4, offset=offset, max_chars=7)
            pages.append(result["text"])
            self.assertEqual(len(result["text"].encode()), result["utf8_bytes"])
            offset = result["next_offset"]
        self.assertEqual("".join(pages), source.split("\r\n", 1)[1])
        with self.assertRaisesRegex(ValueError, "outside"):
            read_context(self.run, "php-0001", 1, 4)
        with self.assertRaisesRegex(ValueError, "unknown"):
            read_context(self.run, "php-9999", 2, 4)
        (self.run / "capsule/php-sources/0001.php").write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "ハッシュ"):
            read_context(self.run, "php-0001", 2, 4)

    def test_incomplete_heredoc_markup_falls_back_without_changing_source(self):
        source = b"<?php\nfunction a() {\n  return <<<DOC\n}\nDOC;\n}\n"
        manifest = self.prepare(source, limit=450)
        self.assertFalse(manifest["syntax_boundaries_analyzed"])
        self.assertIn("heredoc", manifest["syntax_boundaries"]["sample.php"]["status"])
        self.assertEqual(source, (self.run / "capsule/php-sources/0001.php").read_bytes())

    def test_oversized_function_falls_back_and_retains_continuations(self):
        source = ("\ufeff<?php\r\nfunction large() {\r\n" +
                  ("  echo 'some ordinary length row';\r\n" * 70) + "}\r\n").encode()
        manifest = self.prepare(source, limit=550)
        function, = manifest["syntax_boundaries"]["sample.php"]["functions"]
        self.assertEqual("large", function["name"])
        self.assertGreater(len(function["capsules"]), 1)
        hint = boundary_hints(manifest, function["capsules"][0])
        self.assertIn("1 of " + str(len(function["capsules"])), hint)
        self.assertIn("not verified behavior or extra assignments", hint)
        self.assertEqual("", boundary_hints({"parts": manifest["parts"]}, 1))
        self.assertTrue(all(part["chars"] <= 550 for part in manifest["parts"]))
        with self.assertRaisesRegex(ValueError, "outside"):
            read_context(self.run, "php-0001", 1, 73, through=1)

    def test_through_scope_does_not_leak_future_characters_from_a_split_line(self):
        line = "e" * 2_000
        manifest = self.prepare(("<?php\n" + line + "\n").encode(), limit=450)
        first_slice = next(part["index"] for part in manifest["parts"] if part["sources"][0]["character_slices"])
        with self.assertRaisesRegex(ValueError, "unassigned character"):
            read_context(self.run, "php-0001", 2, 2, through=first_slice)
        self.assertEqual(line + "\n", read_context(self.run, "php-0001", 2, 2)["text"])
