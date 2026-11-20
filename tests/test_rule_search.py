import array
import json
import unittest
from importlib.util import find_spec
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

from fixtures import HarnessCase
from migration_harness import capsules, rule_search
from migration_harness.config import load_config


class _Vectors:
    def __init__(self, _: str):
        import numpy as np
        self.np = np

    def encode(self, texts):
        for i, text in enumerate(texts):
            vector = array.array("f", [0] * rule_search.VECTOR_SIZE)
            vector[3 if "例外" in text or text.startswith("query:") else 5] = 1.0
            if self.np.little_endian is False:
                vector.byteswap()
            if text.startswith("query:"):
                yield i, self.np.frombuffer(vector.tobytes(), dtype="<f4")
            else:
                yield i, vector


class RuleSearchTests(HarnessCase):
    def setup_rules(self):
        self.config()
        (self.project / "source.php").write_text("<?php echo 42;\n", encoding="utf-8")
        for name, content in {
            "one.md": "# 返品\r\n通常ルール\r\n" + "定義\r\n" * 22 + "末尾の例外は[優先](two.md)と[旧版](stale.md)を確認。\r\n",
            "two.md": "優先順位\n",
            "stale.md": "例外（旧版）\n",
            "must.md": "常に必要な本文\n",
        }.items():
            (self.project / name).write_bytes(content.encode())
        path = self.root / "test.toml"
        with path.open("a", encoding="utf-8") as stream:
            for name in ("one", "two", "stale", "must"):
                stream.write(f'\n[[project.migration_rules]]\npath = "{name}.md"\nencoding = "utf-8"\n')
                if name != "must":
                    stream.write('usage = "reference"\ndescription = "返品"\n')
            stream.write('''
[rule_search]
model_dir = "e5"

[[rule_search.units]]
unit = "sample"
query = "How should returning orders be handled?"
references = ["one.md", "two.md"]
''')
        config = load_config(path)
        return config, config["units"][0]

    def test_segment_ranges_preserve_windows_and_physical_lines(self):
        source = "header\r\n" + "".join(f"{n:6} | line\u2028content\r\n" for n in range(1, 32))
        windows = list(rule_search._segments(source))
        self.assertEqual(windows[0][0], 2)
        self.assertEqual(windows[-1][1], 32)
        self.assertIn("line\u2028content", windows[-1][3])
        self.assertEqual(sum(high - low + 1 for low, high, *_ in windows), 31)

    def test_absent_index_preserves_required_rules(self):
        config, unit = self.setup_rules()
        with patch.object(rule_search, "report_activity") as report:
            result = self.root / "run"
            capsules.prepare_context(config, unit, result)
        manifest = json.loads((result / "capsule/manifest.json").read_bytes())
        self.assertNotIn("semantic_rule_hint", manifest)
        self.assertEqual([row["usage"] for row in manifest["rule_files"]].count("required"), 1)
        self.assertIn("未配置", report.call_args.args[0])

    @unittest.skipUnless(find_spec("numpy"), "optional rule search dependencies")
    def test_pinned_review_hint_uses_only_scoped_originals_and_keeps_rules(self):
        config, unit = self.setup_rules()
        rules = capsules.configured_search_rules(config, unit)
        index, rows = rule_search.build_rule_index(config, unit, rules, embedder_factory=_Vectors)
        self.assertGreater(rows, 2)
        run = self.root / "first"
        with patch.object(capsules, "prepare_rule_hint", side_effect=lambda *args: rule_search.prepare_rule_hint(
                *args, embedder_factory=_Vectors)):
            capsules.prepare_context(config, unit, run)
        capsule = run / "capsule"
        manifest = json.loads((capsule / "manifest.json").read_bytes())
        hint = manifest["semantic_rule_hint"]
        related = manifest["rule_files"][1]["file"]
        self.assertEqual(hint["file"], manifest["rule_files"][0]["file"])
        self.assertEqual([item["file"] for item in hint["related"]], [related])
        self.assertEqual(len(manifest["rule_files"]), 4)
        line_counts = {}
        for item in manifest["rule_files"]:
            with (capsule / "materials" / item["file"]).open(encoding="utf-8", newline="") as stream:
                lines = list(stream)
                line_counts[item["file"]] = len(lines)
            if item["file"] == hint["file"]:
                self.assertIn("末尾の例外", "".join(lines[hint["lines"][0] - 1:hint["lines"][1]]))
        index.unlink()
        self.assertIn(f"details/{related}", rule_search.review_hint(manifest, line_counts))
        hint["sha256"] = "0" * 64
        self.assertEqual(rule_search.review_hint(manifest, line_counts), "")

    @unittest.skipUnless(find_spec("numpy"), "optional rule search dependencies")
    def test_edited_markdown_falls_back_without_loading_the_model(self):
        config, unit = self.setup_rules()
        rule_search.build_rule_index(config, unit, capsules.configured_search_rules(config, unit),
                                     embedder_factory=_Vectors)
        (self.project / "one.md").write_text("new version\n", encoding="utf-8")
        with patch.object(rule_search, "report_activity") as report:
            capsules.prepare_context(config, unit, self.root / "second")
        self.assertIn("一致しません", report.call_args.args[0])

    def test_rejects_corrupt_or_excess_archive(self):
        path = self.root / "index.zip"
        with ZipFile(path, "w") as archive:
            archive.writestr("manifest.json", json.dumps({"rows": []}))
            archive.writestr("vectors", b"broken")
        with self.assertRaisesRegex(ValueError, "行情報"):
            rule_search._load_index(path, {})
