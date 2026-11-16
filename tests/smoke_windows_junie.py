"""Windows CIで公式JunieのBATを、本番と同じ起動経路で確認する。"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from migration_harness import junie_cli


def main() -> None:
    if sys.platform != "win32":
        raise SystemExit("Run this smoke check on Windows with Junie installed")
    installed = Path(junie_cli._resolve_executable("junie"))
    if installed.suffix.lower() != ".bat":
        raise AssertionError(f"Expected the official BAT launcher, got {installed}")
    print(f"Official Junie launcher: {installed}", flush=True)
    with TemporaryDirectory() as temporary:
        root = Path(temporary) / "Junie 日本語 & spaces"
        root.mkdir()
        for extension in ("BAT", "CMD"):
            launcher = root / f"Junie.{extension}"
            shutil.copyfile(installed, launcher)
            with patch.dict(os.environ, {
                "PATH": str(root) + os.pathsep + os.environ["PATH"],
                "JUNIE_SHARE_ANONYMOUS_STATISTICS": "false",
            }):
                resolved = Path(junie_cli._resolve_executable("junie"))
                assert resolved == launcher, (resolved, launcher)
                for flag in ("--version", "--help"):
                    command = junie_cli.build_command(
                        root, '起動確認 "quotes" & | %PATH% !NAME!\nsecond line', 2,
                    )
                    command.insert(1, flag)
                    print(f"Launching {extension}: {flag}", flush=True)
                    process = junie_cli._launch_process(command)
                    try:
                        code = process.wait(timeout=90)
                        assert code == 0, f"{launcher} {flag}: exit {code}"
                    finally:
                        if process.poll() is None:
                            junie_cli._close_junie(process)
            launcher.unlink()
    print("Official Junie BAT/CMD startup checks passed (no model requests).", flush=True)


if __name__ == "__main__":
    main()
