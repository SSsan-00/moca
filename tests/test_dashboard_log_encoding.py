import io
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from fixtures import SavedRunFixture
from migration_harness import workflow
from migration_harness.errors import format_error
from migration_harness.log_text import decode_log_text
from migration_harness.junie import junie_cli
from migration_harness.junie.junie_thoughts import FILENAME, JunieThoughts, read_junie_thoughts
from test_junie_thoughts import trajectory


JAPANESE = "髙額の請求を確認します"


class DashboardExternalLogTests(unittest.TestCase):
    def test_japanese_path_and_split_cp932_thought_arrive_without_replacement(self):
        with tempfile.TemporaryDirectory() as temporary, redirect_stderr(io.StringIO()):
            root = Path(temporary).resolve()
            logs = root / "日本語ログ"
            logs.mkdir()
            run_id = str(uuid.uuid4())
            target = root / run_id / FILENAME
            target.parent.mkdir()
            source = logs / "log-local-pid-123.log"
            utf8_bom = b"\xef\xbb\xbf" + trajectory(JAPANESE)
            token = "fixed-test-token"
            cp932 = trajectory(JAPANESE + " " + token).decode().encode("cp932")
            cut = cp932.index(JAPANESE[0].encode("cp932")) + 1
            source.write_bytes(utf8_bom + cp932[:cut])
            announcement = f"Junie local (pid=123): log file: {source}\r\n".encode("cp932")
            command = [sys.executable, "-c", f"import os; os.write(2,bytes.fromhex({announcement.hex()!r}))"]
            with subprocess.Popen(command, stderr=subprocess.PIPE) as child:
                collector = JunieThoughts(child.stderr, logs, set(), target, token)
                try:
                    collector.reader.join(timeout=3)
                    self.assertEqual(collector.source, source)
                    collector.poll()
                    first = read_junie_thoughts(root, run_id, 0)
                    self.assertEqual([entry["thought"] for entry in first["entries"]], [JAPANESE])
                    with source.open("ab") as stream:
                        stream.write(cp932[cut:])
                    collector.poll()
                    second = read_junie_thoughts(root, run_id, first["cursor"])
                    self.assertEqual([entry["thought"] for entry in second["entries"]], [JAPANESE + " [REDACTED]"])
                finally:
                    collector.finish()
                    self.assertEqual(child.wait(timeout=3), 0)
            self.assertEqual(source.read_bytes(), utf8_bom + cp932)

    def test_startup_diagnostics_decode_cp932_crlf_and_crop_to_launched_log(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "\u65e5\u672c\u8a9e"
            logs = home / "logs"
            logs.mkdir(parents=True)
            with patch.dict(os.environ, {"JUNIE_HOME": str(home)}, clear=True):
                events = junie_cli._JunieSessionEvents("fixed-test-token")
                log = logs / "log-local-pid-123.log"
                text = f"2026-11-17 20:56:48.002 [main] ERROR {JAPANESE}\r\n"
                log.write_bytes(text.encode("cp932"))
                (logs / "log-other-pid-120.log").write_bytes(b"do not show this")
                report = events.startup_diagnostics(123, ["junie"])
            self.assertIn(JAPANESE, report)
            self.assertNotIn("do not show this", report)
            self.assertEqual(log.read_bytes(), text.encode("cp932"))

    def test_explicit_bom_remains_utf8_and_corruption_is_replaced(self):
        self.assertEqual(decode_log_text(b"\xef\xbb\xbf" + JAPANESE.encode("utf-8")), JAPANESE)
        self.assertIn("\ufffd", decode_log_text(b"\xef\xbb\xbf" + JAPANESE.encode("cp932")))
        self.assertEqual(decode_log_text(b"bad\xff\n"), "bad\ufffd\n")

    def test_process_diagnostics_preserve_mixed_lines_and_secret_redaction(self):
        utf8 = "ビルド開始"
        raw = utf8.encode("utf-8") + b"\n" + JAPANESE.encode("cp932") + b"\n--token fixed-token\n"
        error = subprocess.CalledProcessError(1, ("test", "--token", "fixed-token"), stderr=raw)
        rendered = format_error(error)
        self.assertIn(utf8 + "\n" + JAPANESE, rendered)
        self.assertIn("--token [REDACTED]", rendered)
        self.assertNotIn("fixed-token", rendered)


class DashboardRunLogEncodingTests(SavedRunFixture):
    def check_failed_hook_tail(self, raw, expected_suffix):
        (self.worktree / "fixture-output.bin").write_bytes(raw)
        self.config["scripts"] = {"after_migration": [[
            sys.executable, "-c", "from pathlib import Path; import sys; "
            "sys.stderr.buffer.write(Path('fixture-output.bin').read_bytes()); sys.exit(1)",
        ]]}
        run_process = workflow.run_process

        def child(command, **kwargs):
            self.assertEqual(command[0], sys.executable)
            return run_process(command, **kwargs)

        with patch.object(workflow, "run_process", side_effect=child):
            with self.assertRaises(workflow.RunScriptError) as caught:
                workflow.resume_with_junie(self.config, self.run_id)
        error = str(caught.exception)
        self.assertTrue(error.endswith(expected_suffix), repr(error[-100:]))
        self.assertNotIn("OutsideExcerpt", error)
        record = self.record()
        self.assertEqual(record["error"], error)
        self.assertEqual(record["status"], "script_failed")
        self.assertTrue((self.run_path / "after_migration.log").read_bytes().endswith(raw))

    def test_long_hook_line_keeps_crlf_diagnostic_after_split_cp932_character(self):
        self.check_split_character("cp932", "\r\n")

    def test_long_hook_line_keeps_lf_diagnostic_after_split_utf8_character(self):
        self.check_split_character("utf-8", "\n")

    def test_unterminated_cp932_tail_keeps_intact_japanese_characters(self):
        for prefix, diagnostic in (
            (".", "失敗"),
            (".", "値"),
            (".", "警告"),
            ("縺", "値"),
        ):
            with self.subTest(prefix=prefix, diagnostic=diagnostic):
                initial = f"{diagnostic}: MissingProjectReference "
                encoded = initial.encode("cp932")
                fill = "." * (65_536 - len(encoded))
                raw = b"OutsideExcerpt" + b"." * 7000 + prefix.encode("cp932") + encoded + fill.encode("ascii")
                self.check_failed_hook_tail(raw, "\n" + initial + fill)

    def test_confirmed_utf8_split_precedes_cp932_interpretation_of_the_fragment(self):
        visible = "MissingProjectReference "
        character = "\ua961".encode("utf-8")
        # The single trailing byte is also a valid CP932 half-width character.
        self.assertEqual(len(character[2:].decode("cp932")), 1)
        fill = "." * (65_536 - 1 - len(visible))
        raw = b"OutsideExcerpt" + b"." * 7000 + character + (visible + fill).encode("ascii")
        self.check_failed_hook_tail(raw, "\n" + visible + fill)

    def check_split_character(self, encoding, newline):
        diagnostic = " MissingProjectReference: " + JAPANESE + newline
        last = diagnostic.encode(encoding)
        char = "語".encode(encoding)
        fill = b" " * (65_536 - (len(char) - 1) - len(last))
        raw = b"OutsideExcerpt" + b"." * 7000 + char + fill + last
        self.assertEqual(raw[-65_536:][:len(char) - 1], char[1:])
        self.check_failed_hook_tail(raw, diagnostic)

    def test_tail_starting_at_line_boundary_keeps_entire_first_diagnostic(self):
        first = "MissingProjectReference: " + JAPANESE + "\r\n"
        last = b"End of report.\n"
        first_raw = first.encode("cp932")
        fill = b"x" * (65_536 - len(first_raw) - len(last) - 1) + b"\n"
        raw = b"OutsideExcerpt\n" + first_raw + fill + last
        self.check_failed_hook_tail(raw, first + (fill + last).decode("ascii"))

    def test_long_diagnostic_is_not_lost_when_followed_only_by_whitespace(self):
        end = b"MissingProjectReference\r\n  \r\n"
        self.check_failed_hook_tail(b"OutsideExcerpt" + b"." * 70000 + end, end.decode("ascii"))

    def test_nonzero_script_exposes_cp932_diagnostic_but_keeps_original_bytes(self):
        diagnostic = JAPANESE + "\r\n"
        self.check_failed_hook_tail(diagnostic.encode("cp932"), diagnostic)
