"""Local SDK Roslyn/Razor diagnostics. Neither mode runs MSBuild or writes migration targets."""

import json
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic

from ..execution import ConfiguredProcessIncomplete, check_cancelled, run_process
from ..files import safe_target

_SCRIPT = Path(__file__).with_name("roslyn_program.cs")
_TOOL_LOCK = threading.Lock()
_EDITOR_SLOTS = threading.BoundedSemaphore(2)
_DOTNET_9 = re.compile(r"9\.0\.(\d+) \[(.+)\]")
_SUPPORTED = frozenset({".cs", ".cshtml"})
_tool_directory = None
_tool_command = None


def _environment():
    return dict(os.environ, DOTNET_CLI_TELEMETRY_OPTOUT="1", DOTNET_NOLOGO="1",
                DOTNET_CLI_WORKLOAD_UPDATE_NOTIFY_DISABLE="true")


def _checked_process(command, cwd, timeout):
    return run_process(command, label="Roslyn/Razor diagnostic", cwd=cwd, check=False, capture_output=True,
                       synchronous=True, timeout=timeout, env=_environment())


def _prepare_tool():
    global _tool_directory, _tool_command
    while not _TOOL_LOCK.acquire(timeout=0.1):
        check_cancelled()
    try:
        check_cancelled()
        if _tool_command is not None and Path(_tool_command[-1]).is_file():
            return _tool_command
        dotnet = shutil.which("dotnet")
        if not dotnet:
            raise OSError(".NET SDK not available")
        discovered = _checked_process([dotnet, "--list-sdks"], None, 3)
        sdks = [(int(m[1]), Path(m[2]) / f"9.0.{m[1]}") for line in discovered.stdout.decode("utf-8", "replace").splitlines()
                if (m := _DOTNET_9.fullmatch(line.strip()))]
        if discovered.returncode or not sdks:
            raise OSError(".NET 9 SDK not available")
        _, sdk = max(sdks, key=lambda item: item[0])
        roslyn = sdk / "Roslyn" / "bincore"
        razor = sdk / "Sdks" / "Microsoft.NET.Sdk.Razor" / "tools"
        pack = sdk.parent.parent / "packs" / "Microsoft.NETCore.App.Ref"
        refs = [path / "ref" / "net9.0" for path in pack.glob("9.0.*")
                if re.fullmatch(r"9\.0\.\d+", path.name)]
        if not refs:
            raise OSError(".NET 9 reference pack not available")
        reference = max(refs, key=lambda path: int(path.parents[1].name.rsplit(".", 1)[1]))
        libraries = [
            roslyn / "Microsoft.CodeAnalysis.dll", roslyn / "Microsoft.CodeAnalysis.CSharp.dll",
            razor / "Microsoft.CodeAnalysis.Razor.Compiler.dll", razor / "Microsoft.AspNetCore.Razor.Utilities.Shared.dll",
            razor / "Microsoft.Extensions.ObjectPool.dll",
        ]
        directory = TemporaryDirectory(prefix="harness-roslyn-")
        try:
            root = Path(directory.name)
            dst = root / "RoslynSyntax.dll"
            response = root / "compiler.rsp"
            arguments = ["-nologo", "-target:exe", "-langversion:latest", "-nullable:enable", f'-out:"{dst}"', f'"{_SCRIPT}"']
            arguments += [f'-r:"{path}"' for path in [*reference.glob("*.dll"), *libraries]]
            response.write_text("\n".join(arguments) + "\n", encoding="utf-8")
            for library in libraries:
                shutil.copyfile(library, root / library.name)
            (root / "RoslynSyntax.runtimeconfig.json").write_text(json.dumps({
                "runtimeOptions": {"tfm": "net9.0", "framework": {"name": "Microsoft.NETCore.App", "version": "9.0.0"}}
            }), encoding="utf-8")
            built = _checked_process([dotnet, "exec", str(roslyn / "csc.dll"), "@" + str(response)], root, 12)
            if built.returncode or not dst.is_file():
                raise OSError("Roslyn helper unavailable")
        except BaseException:
            directory.cleanup()
            raise
        if _tool_directory is not None:
            _tool_directory.cleanup()
        _tool_directory, _tool_command = directory, [dotnet, "exec", str(dst)]
        return _tool_command
    finally:
        _TOOL_LOCK.release()


def _result(started, status, reason, **details):
    return {"status": status, "reason": reason, "seconds": monotonic() - started, **details}


def _decode_output(completed, paths, started):
    if completed.returncode or len(completed.stdout) > 128 * 1024:
        return _result(started, "unavailable", "analyzer_exit")
    try:
        result = json.loads(completed.stdout)
        findings, razor = result["findings"], result.get("razor", [])
        skipped, truncated = result["skipped"], result["truncated"]
        if not all(isinstance(items, list) for items in (findings, razor, skipped)) or type(truncated) is not bool:
            raise ValueError("invalid result")
        for item in (*findings, *razor):
            if (item["path"] not in paths or not isinstance(item["code"], str) or not isinstance(item["message"], str)
                    or any(type(item[key]) is not int or item[key] < minimum
                           for key, minimum in (("line", 1), ("column", 1), ("start", 0), ("length", 0)))):
                raise ValueError("invalid diagnostic")
        if any(item["path"] not in paths or not isinstance(item["reason"], str) for item in skipped):
            raise ValueError("invalid skipped file")
    except (KeyError, TypeError, ValueError):
        return _result(started, "unavailable", "analyzer_output")
    status = "errors" if findings else "unavailable" if skipped else "clear"
    reason = "syntax_errors" if findings else "incomplete" if skipped else "razor_advisory" if razor else "ok"
    return _result(started, status, reason, findings=findings, razor=razor, skipped=skipped, truncated=truncated)


def syntax_diagnostics(worktree, run_path, targets):
    paths = [path.as_posix() for path in targets if path.suffix.lower() in _SUPPORTED]
    started = monotonic()
    if not paths:
        return _result(started, "unavailable", "no_supported_targets")
    try:
        for path in paths:
            safe_target(worktree, Path(path))
        command = _prepare_tool()
        with TemporaryDirectory(prefix="roslyn-", dir=run_path) as directory:
            manifest = Path(directory) / "targets.json"
            manifest.write_text(json.dumps(paths), encoding="utf-8")
            completed = _checked_process([*command, str(manifest)], worktree, 4)
    except subprocess.TimeoutExpired:
        return _result(started, "unavailable", "timeout")
    except ConfiguredProcessIncomplete:
        return _result(started, "blocked", "analyzer_stop_unconfirmed")
    except (OSError, ValueError):
        return _result(started, "unavailable", "analyzer_unavailable")
    return _decode_output(completed, paths, started)


def editor_diagnostics(target: str, content: str, version: str) -> dict:
    started = monotonic()
    report = None
    if Path(target).suffix.lower() not in _SUPPORTED:
        report = _result(started, "unavailable", "unsupported_file_type")
    elif not _EDITOR_SLOTS.acquire(blocking=False):
        report = _result(started, "unavailable", "analyzer_busy")
    else:
        try:
            command = _prepare_tool()
            payload = json.dumps([{"Path": target, "Text": content}]).encode("utf-8")
            completed = subprocess.run([*command, "--stdin"], input=payload, cwd=Path(command[-1]).parent,
                                       capture_output=True, timeout=4, check=False, env=_environment(),
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            report = _decode_output(completed, [target], started)
            if report.get("razor") and not report.get("findings"):
                report.update(status="errors", reason="razor_syntax")
        except subprocess.TimeoutExpired:
            report = _result(started, "unavailable", "timeout")
        except (OSError, ValueError):
            report = _result(started, "unavailable", "analyzer_unavailable")
        finally:
            _EDITOR_SLOTS.release()
    return {"findings": [], "razor": [], "skipped": [], "truncated": False, **report, "version": version, "target": target}
