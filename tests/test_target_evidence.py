import codecs
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from migration_harness.context.target_evidence import diagnostic_references, read_target_reference, related_target_references, target_references
from migration_harness.rules import detail_review_evidence, rule_materials


WIDE_ENCODINGS = (
    ("utf-16-le", codecs.BOM_UTF16_LE), ("utf-16-be", codecs.BOM_UTF16_BE),
    ("utf-32-le", codecs.BOM_UTF32_LE), ("utf-32-be", codecs.BOM_UTF32_BE),
)


class TargetEvidenceTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.capsule = Path(temporary.name)
        self.root = self.capsule / "target-context"
        self.root.mkdir()
        self.rows = []

    def pin(self, name, data):
        path = self.root / "files" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        self.rows.append({"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        (self.root / "manifest.json").write_text(json.dumps({"files": self.rows}), encoding="utf-8")
        return path

    def test_companion_is_verified_and_exact(self):
        self.pin("Pages/Checkout.cshtml", b"@page\r\n")
        companion = self.pin("Pages/Checkout.cshtml.cs", b"class Checkout {}\r\n")
        self.pin("Other/Checkout.cshtml.cs", b"class Checkout {}\r\n")
        related, = related_target_references(self.capsule, "target:Pages/Checkout.cshtml")["references"]
        self.assertEqual("Pages/Checkout.cshtml.cs", related["path"])
        companion.write_bytes(b"altered")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            related_target_references(self.capsule, "target:Pages/Checkout.cshtml")

    def test_utf16_crlf_paging_keeps_exact_text_and_rejects_bad_ranges(self):
        text = "@page\r\n<p>日本語</p>\r\n"
        self.pin("Pages/Page.cshtml", text.encode("utf-16"))
        offset = 0
        parts = []
        while offset is not None:
            page = read_target_reference(self.capsule, "target:Pages/Page.cshtml", 1, 2, offset=offset, max_chars=3)
            parts.append(page["text"])
            offset = page["next_offset"]
        self.assertEqual(text, "".join(parts))
        with self.assertRaisesRegex(ValueError, "fixed source"):
            read_target_reference(self.capsule, "target:Pages/Page.cshtml", 1, 3)
        with self.assertRaisesRegex(ValueError, "unknown"):
            read_target_reference(self.capsule, "target:Pages/../Page.cshtml", 1, 1)

    def test_review_citation_ranges_match_the_wide_encoded_target_reader(self):
        inputs = {}
        for encoding, bom in WIDE_ENCODINGS:
            for index, end in enumerate(("\r\n", "\r", "\n")):
                for trailing in (False, True):
                    name = f"{encoding}-{index}-{trailing}.cs"
                    content = end.join(("class Example {", '    string Name = "上不\u2028";', "}"))
                    if trailing:
                        content += end
                    raw = bom + content.encode(encoding)
                    self.pin(name, raw)
                    inputs[name] = raw
        review = self.capsule / "review"
        inventory = detail_review_evidence.stage_review_evidence(
            review, self.capsule, {"rule_files": []}, self.root / "files",
        )
        rule = "rule-0001.txt"
        for name, raw in inputs.items():
            with self.subTest(name=name):
                key = f"evidence/target/{name}"
                self.assertEqual((review / key).read_bytes(), raw)
                self.assertEqual(inventory[key], {
                    "path": name, "sha256": hashlib.sha256(raw).hexdigest(), "file_lines": 3,
                })
                for line in (3, 4, 8):
                    citation = {"file": key, "lines": [line, line]}
                    decisions = {rule: {"applies": False, "reason": "Fixture evidence", "context_evidence": citation}}
                    if line == 3:
                        self.assertTrue(read_target_reference(self.capsule, f"target:{name}", line, line)["text"].startswith("}"))
                        self.assertEqual(rule_materials.selected_details([{"file": rule}], decisions,
                                                                         evidence_inventory=inventory), set())
                    else:
                        with self.assertRaisesRegex(ValueError, "fixed source"):
                            read_target_reference(self.capsule, f"target:{name}", line, line)
                        with self.assertRaises(rule_materials.InvalidDetailContextEvidence):
                            rule_materials.selected_details([{"file": rule}], decisions, evidence_inventory=inventory)

    def test_review_evidence_streams_wide_code_units_and_crlf_across_chunks(self):
        for encoding, bom in WIDE_ENCODINGS:
            first = "x" * ((64 * 1024 - len(bom)) // len(bom) - 1)
            content = first + "\r\n上\r\n" + first + "😀"
            self.pin(encoding + ".cs", bom + content.encode(encoding))
        for size in (64 * 1024, 64 * 1024 - 1):
            with self.subTest(size=size), patch.object(detail_review_evidence, "_COPY_CHUNK_BYTES", size):
                inventory = detail_review_evidence.stage_review_evidence(
                    self.capsule / f"review-{size}", self.capsule, {"rule_files": []}, self.root / "files",
                )
                self.assertEqual(len(inventory), len(WIDE_ENCODINGS))
                self.assertEqual({entry["file_lines"] for entry in inventory.values()}, {3})

    def test_undecodable_reference_is_kept_but_cannot_supply_a_line_citation(self):
        sources = {"opaque.bin": b"\xff\r\nraw", "Incomplete.cs": codecs.BOM_UTF16_LE + b"x\0\n\0y"}
        for name, raw in sources.items():
            self.pin(name, raw)
        review = self.capsule / "review"
        inventory = detail_review_evidence.stage_review_evidence(
            review, self.capsule, {"rule_files": []}, self.root / "files",
        )
        for name, raw in sources.items():
            key = f"evidence/target/{name}"
            self.assertEqual((review / key).read_bytes(), raw)
            self.assertEqual(inventory[key]["sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(inventory[key]["file_lines"], 0)
        with self.assertRaises(UnicodeDecodeError):
            read_target_reference(self.capsule, "target:Incomplete.cs", 1, 1)

    def test_diagnostic_respects_path_ambiguity_and_line_validity(self):
        pinned = self.pin("Shared/Lookup.cshtml.cs", b"class Lookup\r\n{ }\r\n")
        self.pin("Other/Lookup.cshtml.cs", b"class Lookup {}\r\n")
        self.assertEqual([], diagnostic_references(self.capsule, "Lookup.cshtml.cs(1,2): error"))
        findings = r"C:\repo\Shared\Lookup.cshtml.cs(2,1): error"
        matched, = diagnostic_references(self.capsule, findings)
        self.assertEqual(("Shared/Lookup.cshtml.cs", 2), (matched["path"], matched["line"]))
        self.assertEqual([], diagnostic_references(self.capsule, findings.replace("(2,1)", "(3,1)")))
        pinned.write_bytes(b"untrusted")
        self.assertEqual([], diagnostic_references(self.capsule, findings))

    def test_msbuild_path_with_spaces_never_falls_back_to_a_different_basename(self):
        self.pin("Pages/My Page.cshtml.cs", b"class MyPage {}\n" * 12)
        self.pin("Other/My Page.cshtml.cs", b"class Alternate {}\n")
        self.pin("Page.cshtml.cs", b"class Decoy {}\n" * 12)
        findings = r"2>  C:\work tree\Pages\My Page.cshtml.cs(12,3): error CS1002: ; expected"
        match, = diagnostic_references(self.capsule, findings)
        self.assertEqual(("Pages/My Page.cshtml.cs", 12), (match["path"], match["line"]))
        self.assertEqual([], diagnostic_references(self.capsule, findings.replace("Pages", "Missing")))
        self.assertEqual([], diagnostic_references(self.capsule, findings.replace("(12,3)", "(13,3)")))

    def test_navigation_budget_does_not_allow_unbounded_reference_read(self):
        self.pin("TooBig.cs", b"x" * (2 * 1024 * 1024 + 1))
        self.assertIn("TooBig.cs", target_references(self.capsule))
        with self.assertRaisesRegex(ValueError, "budget"):
            read_target_reference(self.capsule, "target:TooBig.cs", 1, 1)
        self.assertEqual([], diagnostic_references(self.capsule, "TooBig.cs(1,1): error"))
