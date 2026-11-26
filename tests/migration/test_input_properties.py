import hashlib
import json
import random
import re
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from migration_harness.config import load_config
from migration_harness.context import capsules
from migration_harness.context.php_boundaries import PhpFunction, function_boundaries
from migration_harness.junie import acp_session
from migration_harness.junie.junie_launch import JunieSessionError
from migration_harness.verification import checker_output


_CASES = 6
_NEWLINES = ("\n", "\r\n", "\r")
_CONTEXT_RECORD = re.compile(
    r" *(\d+)(?: focus chars (\d+)-(\d+) context chars (\d+)-(\d+))? \| ([^\r\n]*)(\r\n|\n|\r)"
)
_SLICE_FIELDS = ("line", "focus_start_column", "focus_end_column", "context_start_column", "context_end_column")


def samples(first_seed):
    for seed in range(first_seed, first_seed + _CASES):
        yield seed, random.Random(seed)


def phrase(rng, count):
    return "".join(rng.choice(("日本語", "翻訳", "値", "表示")) for _ in range(count))


class InputProperties(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()

    def test_php_capsules_reconstruct_source_under_a_character_budget(self):
        encodings = (("utf-8", "utf-8"), ("utf-8-sig", "auto"), ("euc-jp", "euc-jp"))
        for case, (seed, rng) in enumerate(samples(314_100)):
            encoding, requested = encodings[case % len(encodings)]
            partial = case % 2 == 1
            budget = rng.randrange(1000, 1251)
            with self.subTest(seed=seed, encoding=encoding, partial=partial, budget=budget):
                self.check_php_capsules(rng, encoding, requested, partial, budget)

    def check_php_capsules(self, rng, encoding, requested, partial, budget):
        symbol = "変換" + str(rng.randrange(10, 9999))
        text = phrase(rng, 4) + ("😀" if encoding != "euc-jp" else "")
        payload = phrase(rng, rng.randrange(720, 881))
        source_lines = [
            "<?php",
            "// Outside the selected range",
            f'function {symbol}($input) {{',
            "    if (isset($input[0])) {",
            f"        $text = '{text} {{ ]) ' . \"escaped\\\" [)\";",
            "        return '" + payload + "`" * rng.randrange(3, 8) + "';",
            '    }',
            '}',
        ]
        if partial:
            source_lines[-1] = '    // the final closing brace has not arrived'
        endings = [rng.choice(_NEWLINES) for _ in source_lines]
        if rng.randrange(2):
            endings[-1] = ""
        source_bytes = "".join(row + ending for row, ending in zip(source_lines, endings, strict=True)).encode(encoding)
        source = self.root / "Source.php"
        source.write_bytes(source_bytes)
        functions, status = function_boundaries(source, encoding)
        if partial:
            self.assertEqual(functions, ())
            self.assertEqual(status, "unbalanced PHP delimiters")
        else:
            self.assertEqual(functions, (PhpFunction(symbol, 3, 8, 3),))
            self.assertEqual(status, "named functions, lexical boundaries only")

        run = Path(self.enterContext(tempfile.TemporaryDirectory(dir=self.root)))
        unit = {"id": "sample", "target": "Target.cs", "inputs": [
            {"path": source.name, "role": "php", "encoding": requested, "start_line": 3, "end_line": 8, "shared": False},
        ]}
        capsules.prepare_context({"project_root": str(self.root), "max_context_chars": budget}, unit, run)
        capsule_path, manifest = capsules.load_capsule_manifest(run)
        saved, = manifest["php_sources"]
        self.assertEqual(saved["sha256"], hashlib.sha256(source_bytes).hexdigest())
        self.assertEqual(saved["encoding"], encoding)
        self.assertEqual(source.read_bytes(), source_bytes)
        source.write_bytes(b"<?php\n// Later edit\n")
        self.assertEqual(capsules.read_pinned_php(capsule_path, saved), source_bytes)

        row, column = 3, 1
        self.assertGreater(len(manifest["parts"]), 1)
        for part in manifest["parts"]:
            raw = Path(part["path"]).read_bytes()
            context = raw.decode("utf-8")
            self.assertEqual(part["sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(part["chars"], len(context))
            self.assertLessEqual(len(context), budget)
            segment = context[manifest["segment_offset"]:]
            slices = []
            for physical_line in re.findall(r"[^\r\n]*(?:\r\n|\n|\r)", segment):
                if not (record := _CONTEXT_RECORD.fullmatch(physical_line)):
                    continue
                number, start, end, left, right, excerpt, newline = record.groups()
                self.assertEqual(int(number), row)
                expected = source_lines[row - 1]
                if start is not None:
                    start, end, left, right = map(int, (start, end, left, right))
                    self.assertTrue(1 <= left <= start <= end <= right <= len(expected))
                    self.assertEqual(start, column)
                    self.assertEqual(excerpt, expected[left - 1:right])
                    slices.append((row, start, end, left, right))
                    column = end + 1
                    if end < len(expected):
                        self.assertEqual(newline, "\n")
                        continue
                else:
                    self.assertEqual(column, 1)
                    self.assertEqual(excerpt, expected)
                self.assertEqual(newline, endings[row - 1] or "\n")
                row, column = row + 1, 1
            self.assertEqual(slices, [
                tuple(item[field] for field in _SLICE_FIELDS)
                for group in part["sources"] for item in group["character_slices"]
            ])
        self.assertEqual((row, column), (len(source_lines) + 1, 1))

    def test_toml_reads_valid_settings_and_rejects_invalid_input_without_rewriting(self):
        project = self.root / "app"
        project.mkdir()
        (project / ".git").mkdir()
        (project / "Source.php").write_bytes(b"<?php\n")
        path = self.root / "harness.toml"
        for seed, rng in samples(314_200):
            budget, attempts, unit = rng.randrange(1000, 2001), rng.randrange(6), phrase(rng, 3)
            newline = rng.choice(("\n", "\r\n"))
            prefix = (
                '[project]\nroot = "app"\nworktree_root = "trees"\nrun_root = "runs"\n'
                f'max_context_chars = {budget}\n'
            )
            checker = f'[checker]\nmax_repair_attempts = {attempts}\n'
            unit_entry = f'[[units]]\nid = "{unit}"\nsource = "Source.php"\ntarget = "Target.cs"\n'
            for variant, text, message in (
                ("valid", prefix + checker + unit_entry, None),
                ("boolean", prefix + '[checker]\nmax_repair_attempts = true\n' + unit_entry, "non-negative integer"),
                ("duplicate-toml-key", prefix + checker + f'max_repair_attempts = {attempts}\n' + unit_entry, None),
                ("duplicate-unit", prefix + checker + unit_entry * 2, "IDs must be unique"),
            ):
                with self.subTest(seed=seed, variant=variant):
                    raw = text.replace("\n", newline).encode("utf-8")
                    path.write_bytes(raw)
                    if variant == "duplicate-toml-key":
                        with self.assertRaises(tomllib.TOMLDecodeError):
                            load_config(path)
                    elif message is not None:
                        with self.assertRaisesRegex(ValueError, message):
                            load_config(path)
                    else:
                        config = load_config(path)
                        self.assertEqual(config["max_context_chars"], budget)
                        self.assertEqual(config["max_repair_attempts"], attempts)
                        self.assertEqual([item["id"] for item in config["units"]], [unit])
                    self.assertEqual(path.read_bytes(), raw)

    def test_acp_display_updates_preserve_text_without_marking_completion(self):
        encodings = ("utf-8", "utf-8-sig", "cp932")
        for case, (seed, rng) in enumerate(samples(314_300)):
            encoding = encodings[case % len(encodings)]
            parts = ["髙" + phrase(rng, rng.randrange(1, 6)) for _ in range(3)]
            parts.append(parts[-1])
            session_id = f"synthetic-{seed}"
            for variant in ("updates", "truncated", "not-an-object"):
                with self.subTest(seed=seed, encoding=encoding, variant=variant):
                    show_request, publish = Mock(), Mock()
                    completion = self.root / "complete"
                    with patch.object(acp_session, "Thread"):
                        session = acp_session.AcpSession(
                            Mock(), ["junie"], self.root, show_request, completion, "token", None,
                        )
                    session.session_id, session.session_ready, session.progress_sink = session_id, True, publish
                    base = {"jsonrpc": "2.0", "method": "session/update"}
                    if variant == "updates":
                        for text in [None, *parts]:
                            update = {"sessionUpdate": "agent_message_chunk"}
                            if text is not None:
                                update["content"] = {"type": "text", "text": text}
                            body = {**base, "params": {"sessionId": session_id, "update": update}}
                            frame = json.dumps(body, ensure_ascii=False) + "\n"
                            session.messages.put(frame.encode(encoding if text is not None else "utf-8"))
                        self.assertIs(session.advance(), False)
                        session.flush_progress()
                        publish.assert_called_once_with("".join(parts))
                    else:
                        session.messages.put(b'{"jsonrpc": "2.0"\n' if variant == "truncated" else b'[]\n')
                        with self.assertRaisesRegex(JunieSessionError, "ACP応答を読み取れません"):
                            session.advance()
                        session.flush_progress()
                        publish.assert_not_called()
                    show_request.assert_not_called()
                    self.assertTrue(session.outgoing.empty())
                    self.assertFalse(session.prompt_finished)
                    self.assertFalse(completion.exists())

    def test_checker_success_requires_one_complete_unambiguous_response(self):
        log = self.root / "checker.log"
        encodings = ("utf-8", "utf-8-sig", "cp932")
        for case, (seed, rng) in enumerate(samples(314_400)):
            encoding = encodings[case % len(encodings)]
            newline = rng.choice(("\n", "\r\n"))
            success = checker_output.CHECKER_SUCCESS_MESSAGE
            details = "髙" + phrase(rng, rng.randrange(1, 10))
            for variant, text, expected in (
                ("success", newline + success + newline, "checker_succeeded"),
                ("incomplete", "No checks were run." + newline, "checker_incomplete"),
                ("unknown", details + newline, "checker_needs_review"),
                ("contradiction", success + newline + details, "checker_needs_review"),
                ("success-repeated", success + newline + success, "checker_needs_review"),
            ):
                with self.subTest(seed=seed, encoding=encoding, variant=variant):
                    raw = text.encode(encoding)
                    log.write_bytes(raw)
                    self.assertEqual(checker_output.checker_response_status(log), expected)
                    self.assertEqual(log.read_bytes(), raw)
        raw = checker_output.CHECKER_SUCCESS_MESSAGE.encode("utf-8")
        for size in (65_536, 65_537):
            with self.subTest(size=size):
                data = raw.ljust(size, b" ")
                log.write_bytes(data)
                self.assertEqual(
                    checker_output.checker_response_status(log),
                    "checker_succeeded" if size == 65_536 else "checker_needs_review",
                )
                self.assertEqual(log.read_bytes(), data)

    def test_checker_findings_preserve_japanese_and_require_complete_fields(self):
        log = self.root / "findings.log"
        for seed, rng in samples(314_450):
            line = rng.randrange(1, 100)
            message = "髙" + phrase(rng, 4)
            diagnostic = f"M001 Target.cs:{line}:2 warning: {message}"
            for encoding in ("utf-8", "utf-8-sig", "cp932"):
                with self.subTest(seed=seed, encoding=encoding):
                    raw = (diagnostic + "\r\n").encode(encoding)
                    log.write_bytes(raw)
                    findings, unknown = checker_output.checker_findings(log, self.root)
                    self.assertIs(unknown, False)
                    finding, = findings
                    self.assertEqual((finding.path, finding.line, finding.diagnostic), ("Target.cs", str(line), diagnostic))
                    self.assertEqual(checker_output.checker_response_status(log), "checker_needs_review")
                    self.assertEqual(log.read_bytes(), raw)

                    incomplete = f"M001 Target.cs:{line}:2 warning:"
                    raw = (incomplete + "\r\n").encode(encoding)
                    log.write_bytes(raw)
                    self.assertEqual(checker_output.checker_findings(log, self.root), ([], True))
                    self.assertEqual(checker_output.checker_response_status(log), "checker_needs_review")
                    self.assertEqual(log.read_bytes(), raw)

    def test_versionless_run_reads_valid_old_fields_and_never_rewrites_corruption(self):
        from migration_harness.runs.run_records import read_run_record

        path = self.root / "run.json"
        for seed, rng in samples(314_500):
            record = {"status": "prepared", "completed_capsules": rng.randrange(6), "notes": phrase(rng, 4)}
            raw = json.dumps(record, ensure_ascii=False).encode("utf-8")
            wrong_type = {**record, "completed_capsules": str(record["completed_capsules"])}
            for variant, data, message in (
                ("valid", raw, None),
                ("truncated", raw[:-1], "JSONが壊れています"),
                ("wrong-type", json.dumps(wrong_type).encode("utf-8"), "invalid capsule progress"),
                ("duplicate", raw[:-1] + b', "status": "prepared"}', "JSONが壊れています"),
            ):
                with self.subTest(seed=seed, variant=variant):
                    path.write_bytes(data)
                    if message:
                        with self.assertRaisesRegex(ValueError, message):
                            read_run_record(path)
                    else:
                        self.assertEqual(read_run_record(path), record)
                    self.assertEqual(path.read_bytes(), data)
