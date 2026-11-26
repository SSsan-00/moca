"""Local SDK Roslyn/Razor diagnostics. Does not run MSBuild or write migration targets."""

import json
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from xml.etree import ElementTree

from ..execution import ConfiguredProcessIncomplete, check_cancelled, run_process
from ..files import safe_target
from .roslyn_context import (
    ProjectContextUnavailable, editor_content_proof, editor_project_context, editor_project_snapshot,
)

_SCRIPTS = [Path(__file__).with_name(name) for name in (
    "roslyn_program.cs", "roslyn_semantic.cs", "roslyn_keyword_conditions.cs", "roslyn_keyword_binding.cs",
    "roslyn_static_evidence.cs",
)]
_TOOL_LOCK = threading.Lock()
_EDITOR_SLOTS = threading.BoundedSemaphore(2)
_SEMANTIC_SLOT = threading.BoundedSemaphore(1)
_CONTEXT_SLOTS = threading.BoundedSemaphore(2)
_DOTNET_9 = re.compile(r"9\.0\.(\d+) \[(.+)\]")
_SUPPORTED = frozenset({".cs", ".cshtml"})
_tool_directory = None
_tool_command = None
_tool_dotnet_root = None


def _environment():
    return dict(os.environ, DOTNET_CLI_TELEMETRY_OPTOUT="1", DOTNET_NOLOGO="1",
                DOTNET_CLI_WORKLOAD_UPDATE_NOTIFY_DISABLE="true")


def _checked_process(command, cwd, timeout):
    return run_process(command, label="Roslyn/Razor diagnostic", cwd=cwd, check=False, capture_output=True,
                       synchronous=True, timeout=timeout, env=_environment())


def _framework_references(dotnet_root, name):
    pack = dotnet_root / "packs" / name
    versions = [(int(path.name.rsplit(".", 1)[1]), path / "ref" / "net9.0") for path in pack.glob("9.0.*")
                if re.fullmatch(r"9\.0\.\d+", path.name)]
    if not versions:
        raise OSError(".NET 9 reference pack not available")
    references = sorted(max(versions)[1].glob("*.dll"))
    if not references:
        raise OSError("empty .NET 9 reference pack")
    return references


def _prepare_tool():
    global _tool_directory, _tool_command, _tool_dotnet_root
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
        references = _framework_references(sdk.parent.parent, "Microsoft.NETCore.App.Ref")
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
            arguments = ["-nologo", "-target:exe", "-langversion:latest", "-nullable:enable", f'-out:"{dst}"']
            arguments += [f'"{path}"' for path in _SCRIPTS]
            arguments += [f'-r:"{path}"' for path in [*references, *libraries]]
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
        _tool_directory, _tool_command, _tool_dotnet_root = directory, [dotnet, "exec", str(dst)], sdk.parent.parent
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
            completed = _run_editor(command, "--stdin", payload, 4)
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


def _run_editor(command, mode, payload, timeout, *, worker=None):
    if worker is not None:
        return worker.run(command, payload, timeout, environment=_environment())
    return subprocess.run([*command, mode], input=payload, cwd=Path(command[-1]).parent, capture_output=True,
                          timeout=timeout, check=False, env=_environment(),
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


_STATIC_EVIDENCE_REASONS = frozenset({
    "ok", "unsupported_file_type", "empty_target", "razor_parse_errors", "project_preprocessor_required",
    "syntax_errors", "razor_context_unverified", "fact_limit",
})
_STATIC_EVIDENCE_KINDS = frozenset({"method", "type", "property", "const", "literal"})
_SOURCE_LINE_BREAK = re.compile(r"\r\n|[\r\n\x85\u2028\u2029]")


def _decode_static_evidence(completed, targets):
    if completed.returncode or len(completed.stdout) > 4 * 1024 * 1024:
        raise ValueError("Invalid static evidence response")
    reports = json.loads(completed.stdout)
    if not isinstance(reports, list) or len(reports) != len(targets):
        raise ValueError("Invalid static evidence report count")
    result = []
    total = 0
    for index, (report, target) in enumerate(zip(reports, targets, strict=True)):
        if (not isinstance(report, dict) or set(report) != {"index", "path", "complete", "reason", "facts"}
                or type(report["index"]) is not int or report["index"] != index or report["path"] != target["Path"]
                or type(report["complete"]) is not bool or report["reason"] not in _STATIC_EVIDENCE_REASONS
                or (report["reason"] == "ok") != report["complete"] or not isinstance(report["facts"], list)):
            raise ValueError("Invalid static evidence report")
        facts = report["facts"]
        total += len(facts)
        if total > 4_000:
            raise ValueError("Too many static evidence facts")
        maximum = 1 + sum(1 for _ in _SOURCE_LINE_BREAK.finditer(target["Text"]))
        for fact in facts:
            if (not isinstance(fact, dict) or set(fact) != {"kind", "value", "start_line", "end_line"}
                    or fact["kind"] not in _STATIC_EVIDENCE_KINDS or not isinstance(fact["value"], str)
                    or len(fact["value"]) > 4_096 or (fact["kind"] != "literal" and not fact["value"])
                    or type(fact["start_line"]) is not int or type(fact["end_line"]) is not int
                    or not 1 <= fact["start_line"] <= fact["end_line"] <= maximum):
                raise ValueError("Invalid static evidence fact")
        result.append({key: report[key] for key in ("complete", "reason", "facts")})
    return result


def collect_static_evidence(targets: list[dict[str, str]]) -> list[dict]:
    """Read inline source only; complete covers explicit syntax, not runtime behavior."""
    if not isinstance(targets, list) or any(
        not isinstance(target, dict) or set(target) != {"Path", "Text"}
        or not isinstance(target["Path"], str) or not target["Path"] or "\0" in target["Path"]
        or not isinstance(target["Text"], str) for target in targets
    ):
        raise ValueError("Static evidence requires a path and inline source text for each target")
    if not targets:
        return []

    def unavailable(reason):
        return [{"complete": False, "reason": reason, "facts": []} for _ in targets]

    if len(targets) > 64 or sum(len(target["Text"]) + len(target["Path"]) for target in targets) > 8 * 1024 * 1024:
        return unavailable("input_limit")
    try:
        payload = json.dumps(targets, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except UnicodeError:
        return unavailable("invalid_unicode")
    if len(payload) > 16 * 1024 * 1024:
        return unavailable("input_limit")
    try:
        check_cancelled()
        completed = _run_editor(_prepare_tool(), "--migration-static-evidence", payload, 10)
        check_cancelled()
    except subprocess.TimeoutExpired:
        return unavailable("timeout")
    except ConfiguredProcessIncomplete:
        return unavailable("analyzer_stop_unconfirmed")
    except (OSError, ValueError):
        return unavailable("analyzer_unavailable")
    try:
        return _decode_static_evidence(completed, targets)
    except (TypeError, ValueError):
        return unavailable("analyzer_output")


def editor_context_revision(
    worktree: Path, project: str | None, target: str, version: str, *, verify_revision=None,
) -> dict:
    revision = reason = proof = None
    if not _CONTEXT_SLOTS.acquire(blocking=False):
        reason = "analyzer_busy"
    else:
        try:
            snapshot = editor_project_snapshot(worktree, project, target)
            revision = snapshot.revision
            if verify_revision == revision:
                proof = editor_content_proof(snapshot)
        except ProjectContextUnavailable as exc:
            reason = str(exc)
        except (OSError, ValueError):
            reason = "project_configuration"
        finally:
            _CONTEXT_SLOTS.release()
    result = {"context_revision": revision, "reason": reason, "version": version, "target": target}
    if proof is not None:
        result["context_proof"] = proof
    return result


def editor_semantic_diagnostics(
    worktree: Path, project: str | None, target: str, content: str, version: str, *, worker=None,
) -> dict:
    started = monotonic()
    reason = context_revision = proof = None
    if Path(target).suffix.lower() not in _SUPPORTED:
        reason = "unsupported_file_type"
    elif not _SEMANTIC_SLOT.acquire(blocking=False):
        reason = "analyzer_busy"
    else:
        try:
            snapshot = editor_project_snapshot(worktree, project, target)
            context_revision = snapshot.revision
            context, proof = editor_project_context(worktree, project, target, snapshot=snapshot, with_proof=True)
            command = _prepare_tool()
            packs = ["Microsoft.NETCore.App.Ref"] + (["Microsoft.AspNetCore.App.Ref"] if context["Web"] else [])
            context["References"] = [str(path) for pack in packs for path in _framework_references(_tool_dotnet_root, pack)]
            payload = json.dumps({"Target": {"Path": target, "Text": content}, "Project": context},
                                 ensure_ascii=False).encode("utf-8")
            completed = (_run_editor(command, "--semantic", payload, 8, worker=worker) if worker is not None
                         else _run_editor(command, "--semantic", payload, 8))
            latest = editor_project_snapshot(worktree, project, target)
            if latest.revision != context_revision or editor_content_proof(latest) != proof:
                raise ProjectContextUnavailable("project_changed")
            report = _decode_output(completed, [target], started)
            if worker is not None and report["reason"] == "analyzer_output":
                worker.discard()
            if report.get("skipped"):
                reason = report["skipped"][0]["reason"]
                if reason == "razor_unmapped_errors" and report["findings"]:
                    report.update(status="partial", reason=reason)
                    reason = None
            elif report["status"] == "errors":
                report["reason"] = "semantic_errors"
        except ProjectContextUnavailable as exc:
            reason = str(exc)
        except (ElementTree.ParseError, UnicodeError, ValueError):
            reason = "project_configuration"
        except subprocess.TimeoutExpired:
            reason = "timeout"
        except OSError:
            reason = "analyzer_unavailable"
        finally:
            _SEMANTIC_SLOT.release()
    if reason:
        report = _result(started, "unavailable", reason)
    result = {"findings": [], "razor": [], "truncated": False, **report, "version": version, "target": target,
              "context_revision": context_revision}
    if result["status"] in {"clear", "errors", "partial"}:
        result["context_proof"] = proof
    return result
