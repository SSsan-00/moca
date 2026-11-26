from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from tests.support.build_receipt_fixtures import saved_build_receipt
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

    def test_fingerprint_rejects_a_link_even_to_the_same_bytes_inside_the_root(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            original = root / "original.dll"
            original.write_bytes(b"same bytes")
            linked = root / "linked.dll"
            try:
                linked.symlink_to(original)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"file symlink unavailable: {exc}")
            with self.assertRaisesRegex(ValueError, "通常ファイル以外"):
                build_receipt._fingerprint(root)

    def test_fingerprint_changes_after_same_size_same_mtime_replacement(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            chosen = root / "成果物.dll"
            chosen.write_bytes(b"old bytes")
            previous = build_receipt._fingerprint(root)
            timestamp = chosen.stat()
            replacement = root / "altered.dll"
            replacement.write_bytes(b"new bytes")
            os.utime(replacement, ns=(timestamp.st_atime_ns, timestamp.st_mtime_ns))
            replacement.replace(chosen)
            self.assertEqual(chosen.stat().st_size, timestamp.st_size)
            self.assertEqual(chosen.stat().st_mtime_ns, timestamp.st_mtime_ns)
            self.assertNotEqual(build_receipt._fingerprint(root), previous)


class BuildReceiptCompatibilityTests(unittest.TestCase):
    def test_versionless_receipt_preserves_historical_shape_and_requires_both_fingerprints(self):
        receipt = saved_build_receipt()
        record = {"build_exit_code": 0, "checker_build": receipt}
        config = {"build_project": receipt["inputs"]["project"]}
        with TemporaryDirectory() as folder:
            project = Path(folder).resolve()
            with patch.object(build_receipt, "build_inputs", return_value=receipt["inputs"]), patch.object(
                build_receipt, "build_artifacts", return_value=receipt["artifacts"],
            ):
                self.assertEqual(build_receipt.check_build_receipt(config, project, json.loads(json.dumps(record))),
                                 receipt)
                self.assertTrue(build_receipt.same_build_artifacts(project, receipt["inputs"], receipt))
            with patch.object(build_receipt, "build_inputs", return_value=receipt["inputs"]), patch.object(
                build_receipt, "build_artifacts", return_value=None,
            ):
                with self.assertRaisesRegex(ValueError, "成果物が変更または削除"):
                    build_receipt.check_build_receipt(config, project, record)
                self.assertFalse(build_receipt.same_build_artifacts(project, receipt["inputs"], receipt))

    def test_unusable_receipts_never_scan_the_build_project(self):
        receipt = saved_build_receipt()
        invalid = [
            (0, None), (0, "unfinished"), (False, receipt), (0.0, receipt),
            (0, {"inputs": receipt["inputs"]}), (0, {**receipt, "artifacts": None}),
            (0, {**receipt, "schema_version": 99}),
            (0, {**receipt, "inputs": {**receipt["inputs"], "source": False}}),
            (0, {**receipt, "artifacts": {**receipt["artifacts"], "obj": "unfinished"}}),
        ]
        with patch.object(build_receipt, "build_inputs") as scan, patch.object(build_receipt, "build_artifacts") as artifacts:
            for exit_code, saved in invalid:
                with self.subTest(exit_code=exit_code, saved=saved):
                    with self.assertRaisesRegex(ValueError, "ビルドを実行"):
                        build_receipt.check_build_receipt({}, Path("."), {"build_exit_code": exit_code, "checker_build": saved})
                    if exit_code is None or type(exit_code) is int:
                        self.assertFalse(build_receipt.same_build_artifacts(Path("."), receipt["inputs"], saved))
            scan.assert_not_called()
            artifacts.assert_not_called()

    def test_verified_receipt_fails_closed_after_artifact_changes_or_io_errors(self):
        with TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            worktree = root / "tree"
            worktree.mkdir()
            (worktree / "Example.csproj").write_text(
                '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup>'
                '<TargetFramework>net9.0</TargetFramework></PropertyGroup></Project>', encoding="utf-8",
            )
            dotnet = root / "fixture-sdk" / "dotnet.exe"
            packs = dotnet.parent / "packs" / "Microsoft.NETCore.App.Ref"
            packs.mkdir(parents=True)
            dotnet.write_bytes(b"fixture launcher; never executed")
            (packs / "fixture-reference.dll").write_bytes(b"fixture reference")
            assembly = worktree / "bin" / "Debug" / "net9.0" / "Example.dll"
            assembly.parent.mkdir(parents=True)
            assembly.write_bytes(b"fixture output")
            assets = worktree / "obj" / "project.assets.json"
            assets.parent.mkdir()
            assets.write_text('{"libraries": {}}', encoding="utf-8")
            config = {"build_project": "Example.csproj"}
            with patch.object(build_receipt.shutil, "which", return_value=str(dotnet)), patch.dict(
                build_receipt.os.environ, {}, clear=True,
            ):
                inputs = build_receipt.build_inputs(worktree, config["build_project"])
                self.assertIsNotNone(inputs)
                receipt = {"inputs": inputs, "artifacts": build_receipt.build_artifacts(worktree, inputs)}
                path = root / "run.json"
                path.write_text(json.dumps({"build_exit_code": 0, "checker_build": receipt}), encoding="utf-8")
                persisted = path.read_bytes()
                self.assertEqual(build_receipt.check_build_receipt(config, worktree, json.loads(persisted)), receipt)
                assembly.write_bytes(b"edited artifact")
                with self.assertRaisesRegex(ValueError, "成果物が変更または削除"):
                    build_receipt.check_build_receipt(config, worktree, json.loads(persisted))
                self.assertEqual(path.read_bytes(), persisted)
                assembly.write_bytes(b"fixture output")
                record = json.loads(persisted)
                original_scan = os.scandir
                original_hash = build_receipt.sha256_file

                @contextmanager
                def remove_after_listing(directory):
                    with original_scan(directory) as entries:
                        yield entries
                    if directory == assembly.parent:
                        assembly.unlink()

                def unavailable(file, *, check=None):
                    if file == assembly:
                        raise PermissionError("synthetic sharing/read failure")
                    return original_hash(file, check=check)

                for operation in (
                    patch.object(build_receipt.os, "scandir", side_effect=remove_after_listing),
                    patch.object(build_receipt, "sha256_file", side_effect=unavailable),
                ):
                    with operation, self.assertRaisesRegex(ValueError, "成果物が変更または削除"):
                        build_receipt.check_build_receipt(config, worktree, record)
                    self.assertEqual(path.read_bytes(), persisted)
                    self.assertEqual(record["checker_build"], receipt)
                    assembly.write_bytes(b"fixture output")
                self.assertEqual(build_receipt.check_build_receipt(config, worktree, record), receipt)
