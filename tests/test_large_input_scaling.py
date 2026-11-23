import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from migration_harness.context import capsules
from migration_harness import workflow
from migration_harness.context.php_boundaries import function_boundaries


class LargeInputScalingTests(unittest.TestCase):
    def test_selected_prefix_keeps_whole_functions_in_large_php(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / "source.php"
            functions = [
                f'function f{i}() {{\r\n  $x = {i};\r\n  if ($x > 5) {{\r\n'
                '    return trim("本文");\r\n  }\r\n  return "例外";\r\n}\r\n'
                for i in range(100)
            ]
            prefix = ("<?php\r\n" + "".join(functions)).encode("utf-8-sig")
            results = []
            for name, data in [
                ("compact", prefix),
                ("large", prefix + b"/*" + b"x" * (4 * 1024 * 1024) + b"*/\r\n"),
            ]:
                source.write_bytes(data)
                run = root / name
                result = capsules.prepare_context(
                    {"project_root": root, "max_context_chars": 1600},
                    {"id": "scale", "target": "T.cs", "inputs": [
                        {"path": source.name, "role": "php", "start_line": 1, "end_line": 701,
                         "encoding": "utf-8-sig"},
                    ]},
                    run,
                )
                folder = run / "capsule"
                manifest = json.loads((folder / "manifest.json").read_bytes())
                self.assertEqual((folder / manifest["php_sources"][0]["snapshot"]).read_bytes(), data)
                self.assertTrue(all(chars <= 1600 for chars in result["context_part_chars"]))
                bounds = manifest["syntax_boundaries"][source.name]
                self.assertEqual(len(bounds["functions"]), 100)
                self.assertTrue(all(len(fn["capsules"]) == 1 for fn in bounds["functions"]))
                results.append([Path(part["path"]).read_bytes() for part in manifest["parts"]])
            self.assertEqual(*results)
            self.assertEqual(function_boundaries(source, "utf-8-sig")[1], "static scan byte limit")

    def test_bounded_scan_uses_the_file_prefix_or_refuses_unknown_lexical_state(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.php"
            source.write_bytes(b"<?php\nclass A {\n"
                               b"  function done() {\n    return 1;\n  }\n"
                               b"  function later() {\n/*" + b"x" * (4 * 1024 * 1024) + b"*/\n}\n}\n")
            found, status = function_boundaries(source, "utf-8", through_line=6)
            self.assertEqual([(fn.name, fn.start_line, fn.end_line) for fn in found], [("done", 3, 5)])
            self.assertIn("bounded", status)
            self.assertEqual(function_boundaries(source, "utf-8", through_line=8),
                             ((), "static scan byte limit before requested lines"))
            source.write_bytes(b"<?php\n/*\nfunction hidden() {\n}\n*/\n" + b"x" * (4 * 1024 * 1024))
            self.assertEqual(function_boundaries(source, "utf-8", through_line=4),
                             ((), "unterminated PHP block comment"))

    def test_function_append_reuses_the_row_list_only_after_the_fit_check(self):
        item = {"path": "source.php", "role": "php"}
        initial = capsules._new_group(0, item, 1, "old\r\n")
        next_group = capsules._new_group(0, item, 2, "new with ````` fence\r\n")
        records = initial["records"]
        groups = [initial]
        self.assertFalse(capsules._append_segment_group(groups, next_group, 0, 20))
        self.assertEqual(groups, [initial])
        self.assertEqual(records, ["old\r\n"])
        self.assertTrue(capsules._append_segment_group(groups, next_group, 0, 200))
        self.assertIs(groups[0]["records"], records)
        self.assertEqual(records, ["old\r\n", "new with ````` fence\r\n"])
        self.assertEqual(capsules._group_size(groups[0]), len(capsules._render_groups(groups)))

    def test_preflight_reuses_only_verified_excerpt_hash_postflight_still_rehashes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            capsule = root / "capsule"
            capsule.mkdir()
            agent = root / "agent"
            agent.mkdir()
            original = capsule / "original.md"
            data = b"unchanged\n"
            original.write_bytes(data)
            digest = hashlib.sha256(data).hexdigest()
            manifest = {"parts": [{"path": str(original), "index": 1, "sha256": digest, "segment_sha256": digest}]}
            verified = workflow._stage_source_excerpts(capsule, manifest, agent)
            relative = Path(".migration-materials/source-0001.md")
            (agent / "guidelines.md").write_text("unchanged", encoding="utf-8")
            with patch.object(workflow, "sha256_file", wraps=workflow.sha256_file) as sha:
                before = workflow._unlisted_workspace_files(agent, set(), verified)
                self.assertEqual([call.args[0].name for call in sha.call_args_list], ["guidelines.md"])
            self.assertEqual(before, workflow._unlisted_workspace_files(agent, set()))
            (agent / relative).write_bytes(b"changed\n")
            self.assertNotEqual(before, workflow._unlisted_workspace_files(agent, set()))
            (agent / relative).unlink()
            with self.assertRaisesRegex(ValueError, "staged verification source changed"):
                workflow._unlisted_workspace_files(agent, set(), verified)
