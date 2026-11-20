import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from migration_harness.target_evidence import (
    diagnostic_references, read_target_reference, related_target_references, target_references,
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
