"""Compare build receipt I/O on generated files without starting .NET or a checker."""

import argparse
from collections import Counter
import hashlib
import io
import json
import os
from pathlib import Path
import platform
from tempfile import TemporaryDirectory
from threading import Timer
from time import perf_counter
from unittest.mock import patch

from migration_harness.execution import ExecutionControl, OperationCancelled, control_scope
from migration_harness.verification import build_receipt
from migration_harness.verification.verification_files import verification_files


def write_bytes(path, pattern, size):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((pattern * (size // len(pattern) + 1))[:size])


def fixtures(root):
    tree = root / "tree"
    tree.mkdir()
    (tree / "Fixture.csproj").write_text(
        '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><TargetFramework>net9.0</TargetFramework>'
        '</PropertyGroup></Project>', encoding="utf-8",
    )
    launcher = root / "fake-sdk" / "dotnet.exe"
    packs = launcher.parent / "packs/Microsoft.NETCore.App.Ref/9.0.0/ref/net9.0"
    write_bytes(launcher, b"not executable: synthetic", len(b"not executable: synthetic"))
    for index in range(256):
        write_bytes(packs / f"synthetic-reference-{index:04d}.dll", str(index).encode(), 64 * 1024)
    for index in range(800):
        write_bytes(tree / f"source-{index // 80:03d}" / f"file-{index:05d}.cs",
                    f"// synthetic {index}\r\n".encode(), 20_000)
    write_bytes(tree / "bin/Debug/net9.0/Fixture.dll", b"compiled marker, never executed", 1024 ** 2)
    assets = tree / "obj/project.assets.json"
    assets.parent.mkdir()
    assets.write_bytes(b'{"libraries":{}}')
    for index in range(150):
        write_bytes(tree / "obj" / f"artifact-{index:04d}.bin", f"{index};".encode(), 32 * 1024)
    write_bytes(tree / "check.py", b"# no script execution\r\n", 1024 ** 2)
    return tree, launcher


def workflow(tree):
    config = {"build_project": "Fixture.csproj", "checker_verification_files": ["check.py"]}
    command = ["python", "check.py"]
    scripts = {"before_build": [command] * 12}
    inputs = build_receipt.build_inputs(tree, config["build_project"])
    assert inputs is not None
    artifacts = build_receipt.build_artifacts(tree, inputs)
    assert artifacts is not None
    receipt = {"inputs": inputs, "artifacts": artifacts}

    def run():
        times = {}
        start = perf_counter()

        def measure(name, call, *args):
            started = perf_counter()
            result = call(*args)
            times[name] = round((perf_counter() - started) * 1000, 2)
            return result

        assert measure("before_build", build_receipt.build_inputs, tree, config["build_project"]) == inputs
        assert measure("incremental_receipt", build_receipt.same_build_artifacts, tree, inputs, receipt)
        assert measure("after_build", build_receipt.build_inputs, tree, config["build_project"]) == inputs
        assert measure("record_artifacts", build_receipt.build_artifacts, tree, inputs) == artifacts
        assert measure("checker_resume", build_receipt.check_build_receipt, config, tree,
                       {"build_exit_code": 0, "checker_build": receipt}) == receipt
        checks = measure("checker_inputs", verification_files, config, tree, "Result.cs", command, scripts)
        times["total"] = round((perf_counter() - start) * 1000, 2)
        stable = [inputs["source"], inputs["framework_packs"], artifacts, checks]
        digest = hashlib.sha256(json.dumps(stable, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        return times, digest

    return run


def measure_counts(run):
    counts = Counter()
    sha256, stat, lstat = hashlib.sha256, os.stat, os.lstat

    def track_counted(call, key):
        def counted(*args, **kwargs):
            counts[key] += 1
            return call(*args, **kwargs)
        return counted

    def track_open(call):
        def recorded(file, mode="r", *args, **kwargs):
            handle = call(file, mode, *args, **kwargs)
            if isinstance(file, (str, os.PathLike)) and mode in {"r", "rb"}:
                counts["opens"] += 1
                counts["logical_bytes"] += stat(file).st_size
            return handle
        return recorded

    with (patch("builtins.open", track_open(open)), patch.object(io, "open", track_open(io.open)),
          patch.object(hashlib, "sha256", track_counted(sha256, "sha256")),
          patch.object(os, "stat", track_counted(stat, "stat")),
          patch.object(os, "lstat", track_counted(lstat, "lstat")),
          patch.object(os, "scandir", track_counted(os.scandir, "scandir"))):
        run()
    counts["sha256"] -= 1  # The measurement's summary digest is not a product call.
    return counts


def cancellation_samples(run):
    result = []
    for pause in (.03, .07, .11):
        control = ExecutionControl(lambda _: None)
        requested = []

        def cancel():
            requested.append(perf_counter())
            control.cancelled.set()

        timer = Timer(pause, cancel)
        timer.start()
        try:
            with control_scope(control):
                run()
        except OperationCancelled:
            result.append(round((perf_counter() - requested[0]) * 1000, 2))
        finally:
            timer.cancel()
            timer.join()
    assert len(result) == 3, "The timing fixture finished before a scheduled cancellation"
    return result


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    with TemporaryDirectory() as directory:
        tree, launcher = fixtures(Path(directory).resolve())
        with (patch.object(build_receipt.shutil, "which", return_value=str(launcher)),
              patch.dict(os.environ, {}, clear=True)):
            run = workflow(tree)
            run()
            times, fingerprint = run()
            counts = measure_counts(run)
            stops = cancellation_samples(run)
    print(json.dumps({"python": platform.python_version(), "platform": platform.platform(), "times_ms": times,
                      "fingerprint": fingerprint, "counts": counts, "stop_response_ms": stops}))


if __name__ == "__main__":
    main()
