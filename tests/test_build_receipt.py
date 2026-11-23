from __future__ import annotations

import hashlib
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from migration_harness.verification import build_receipt
from migration_harness import files
from migration_harness.execution import ExecutionControl, OperationCancelled, control_scope


class BuildReceiptCancellationTests(unittest.TestCase):
    def test_final_file_checks_first_chunk_final_chunk_and_eof(self):
        chunk = 64 * 1024
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            file = root / "成果物 large.bin"
            file.write_bytes(b"x" * (2 * chunk + 17))
            original_open = Path.open

            for stop_on, expected in ((1, [chunk]), (3, [chunk, chunk, 17]), (4, [chunk, chunk, 17, 0])):
                with self.subTest(stop_on=stop_on):
                    control = ExecutionControl(Mock())
                    lengths = []

                    def open_file(path, *args, **kwargs):
                        stream = original_open(path, *args, **kwargs)
                        if path != file:
                            return stream
                        fixture = Mock()
                        fixture.__enter__ = Mock(return_value=fixture)
                        fixture.__exit__ = Mock(side_effect=lambda *_args: stream.close())

                        def read(size):
                            data = stream.read(size)
                            lengths.append(len(data))
                            if len(lengths) == stop_on:
                                control.cancelled.set()
                            return data

                        fixture.read.side_effect = read
                        return fixture

                    with control_scope(control), patch.object(Path, "open", open_file):
                        with self.assertRaises(OperationCancelled):
                            build_receipt._fingerprint(root)
                        self.assertEqual(lengths, expected)
                        # The post-stop snapshot path calls the same hash helper without a check.
                        self.assertEqual(files.sha256_file(file), hashlib.sha256(b"x" * (2 * chunk + 17)).hexdigest())

    def test_cancellation_during_scan_with_only_empty_directories(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for index in range(20):
                (root / f"empty-{index:02}").mkdir()
            scandir = os.scandir
            control = ExecutionControl(Mock())
            iterations = []

            def scan(path):
                entries = scandir(path)
                fixture = Mock()
                fixture.__exit__ = Mock(side_effect=lambda *_args: entries.close())

                def iterate():
                    for entry in entries:
                        iterations.append(entry.name)
                        if len(iterations) == 4:
                            control.cancelled.set()
                        yield entry

                fixture.__enter__ = Mock(side_effect=iterate)
                return fixture

            with control_scope(control), patch.object(build_receipt.os, "scandir", side_effect=scan):
                with self.assertRaises(OperationCancelled):
                    build_receipt._fingerprint(root)
            self.assertEqual(len(iterations), 4)

    def test_fingerprint_keeps_legacy_order_exclusions_and_content(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            ordered = ("a.txt", "z.txt", "alpha/z.txt", "alpha/deep/a.txt", "beta/素材.txt")
            for name in (*reversed(ordered), ".git/skip", "alpha/skip/ignored"):
                path = root / name
                path.parent.mkdir(exist_ok=True, parents=True)
                path.write_bytes(name.encode())
            expected = hashlib.sha256()
            for name in ordered:
                expected.update(json.dumps([name, hashlib.sha256(name.encode()).hexdigest()]).encode())
            self.assertEqual(build_receipt._fingerprint(root, (".git", "alpha/skip")), expected.hexdigest())
