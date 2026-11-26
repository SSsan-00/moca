import argparse
from collections import Counter
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory

from control_flow_model import MAX_PARAMS, compare_models

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from php_control_flow import UnsupportedPhp, normalize_php

_HELPER = Path(__file__).with_name("control-flow-roslyn")
_MAX_FILE_BYTES = 2 * 1024 * 1024
_MAX_TOTAL_BYTES = 16 * 1024 * 1024
_WINDOWS_DEVICES = re.compile(r"(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³]|CONIN\$|CONOUT\$)", re.IGNORECASE)
_STATUSES = ("verified", "mismatch", "review_candidate", "unverified", "excluded")


def _raw(path, maximum):
    if not path.is_file():
        raise ValueError("not_a_regular_file")
    with path.open("rb") as file:
        data = file.read(maximum + 1)
    if len(data) > maximum:
        raise ValueError("input_size_limit")
    return data


def _scope(spec, extension):
    if not isinstance(spec, dict) or set(spec) != {"path", "name", "start_line", "end_line", "sha256"}:
        raise ValueError("invalid_paired_scope")
    name, path, digest = (spec[key] for key in ("name", "path", "sha256"))
    start, end = (spec[key] for key in ("start_line", "end_line"))
    if (not isinstance(name, str) or not 1 <= len(name) <= 160 or not isinstance(path, str) or len(path) > 260
            or any(part in {"", ".", ".."} or part.endswith((".", " ")) for part in path.split("/"))
            or any(_WINDOWS_DEVICES.fullmatch(part.split(".", 1)[0]) for part in path.split("/"))
            or any(char in '<>:"\\|?*' or ord(char) < 32 for char in path)
            or (extension is not None and PurePosixPath(path).suffix.lower() != extension)
            or not isinstance(digest, str) or not re.fullmatch("[0-9a-f]{64}", digest)
            or type(start) is not int or type(end) is not int or not 1 <= start <= end):
        raise ValueError("invalid_paired_scope")
    return spec


def _unique_keys(items):
    result = dict(items)
    if len(result) != len(items):
        raise ValueError("duplicate_plan_field")
    return result


def _plan(path):
    data = json.loads(_raw(path, 128 * 1024), object_pairs_hook=_unique_keys)
    if (not isinstance(data, dict) or set(data) != {"version", "pairs"} or type(data["version"]) is not int
            or data["version"] != 1 or not isinstance(data["pairs"], list) or not 1 <= len(data["pairs"]) <= 64):
        raise ValueError("invalid_control_flow_plan")
    identifiers = set()
    for row in data["pairs"]:
        if (not isinstance(row, dict) or not isinstance(row.get("id"), str) or not 1 <= len(row["id"]) <= 80
                or row["id"] in identifiers):
            raise ValueError("invalid_or_duplicate_pair_id")
        identifiers.add(row["id"])
        _scope(row.get("source"), None)
        if row.get("origin") == "excluded" and set(row) == {"id", "source", "origin", "reason"}:
            if not isinstance(row["reason"], str) or not 1 <= len(row["reason"].strip()) <= 300:
                raise ValueError("missing_exclusion_reason")
            continue
        if set(row) != {"id", "source", "origin", "target", "inputs"} or row["origin"] not in ("human", "ai"):
            raise ValueError("invalid_pair_origin")
        _scope(row["target"], ".cs")
        inputs = row["inputs"]
        if not isinstance(inputs, list) or len(inputs) > MAX_PARAMS or any(kind not in ("int32", "bool") for kind in inputs):
            raise ValueError("invalid_explicit_input_contract")
    return data["pairs"]


class Files:
    def __init__(self):
        self.cache = {}
        self.remaining = _MAX_TOTAL_BYTES

    def pinned_text(self, root, scope):
        path = (root / scope["path"]).resolve()
        if not path.is_relative_to(root):
            raise ValueError("path_outside_scope")
        if path not in self.cache:
            raw = _raw(path, min(self.remaining, _MAX_FILE_BYTES))
            self.remaining -= len(raw)
            self.cache[path] = (sha256(raw).hexdigest(), raw.decode("utf-8-sig"))
        digest, text = self.cache[path]
        if digest != scope["sha256"]:
            raise ValueError("pinned_version_changed")
        if scope["end_line"] > len(text.splitlines()):
            raise ValueError("pinned_range_unavailable")
        return text


class Roslyn:
    def __init__(self, workspace):
        self.workspace = workspace
        self.command = None
        self.preparation_failed = False

    def _execute(self, command, *, payload=None, timeout):
        env = os.environ | {"DOTNET_CLI_TELEMETRY_OPTOUT": "1", "DOTNET_CLI_WORKLOAD_UPDATE_NOTIFY_DISABLE": "1"}
        try:
            result = subprocess.run(command, cwd=self.workspace, input=payload, capture_output=True,
                                    timeout=timeout, env=env)
        except (OSError, subprocess.SubprocessError):
            raise ValueError("roslyn_unavailable") from None
        if result.returncode:
            raise ValueError("roslyn_unavailable")
        return result.stdout

    def __call__(self, text, scope):
        if self.preparation_failed:
            raise ValueError("roslyn_unavailable")
        if self.command is None:
            try:
                for name in ("Program.cs", "MocaControlFlow.csproj", "global.json", "NuGet.Config"):
                    shutil.copyfile(_HELPER / name, self.workspace / name)
                self._execute(["dotnet", "build", "MocaControlFlow.csproj", "-c", "Release",
                               "-o", "out", "--disable-build-servers", "--nologo", "-v", "quiet"], timeout=75)
            except (OSError, ValueError):
                self.preparation_failed = True
                raise ValueError("roslyn_unavailable") from None
            self.command = ["dotnet", "out/MocaControlFlow.dll"]
        payload = json.dumps({"path": scope["path"], "text": text, "method": scope["name"],
                              "start_line": scope["start_line"], "end_line": scope["end_line"]})
        raw = self._execute(self.command, payload=payload.encode("utf-8"), timeout=8)
        try:
            if len(raw) > 512 * 1024:
                raise ValueError("Roslyn report limit")
            report = json.loads(raw)
            fields = {"status", "reason", "params", "return_type", "body"}
            if (not isinstance(report, dict) or set(report) not in (fields, fields | {"diagnostic_line"})
                    or ("diagnostic_line" in report and
                        (type(report["diagnostic_line"]) is not int or report["diagnostic_line"] < 1))
                    or report["status"] not in ("supported", "unverified")
                    or not isinstance(report["reason"], str) or not re.fullmatch("[a-z_]{1,64}", report["reason"])):
                raise ValueError("invalid Roslyn report")
        except (TypeError, ValueError, RecursionError):
            raise ValueError("roslyn_invalid_report") from None
        return report


def _assess(row, roots, files, roslyn):
    source, target = row["source"], row.get("target")
    result = {key: row[key] for key in ("id", "source", "origin")}
    if target is not None:
        result["target"] = target
    try:
        original = files.pinned_text(roots[0], source)
        if row["origin"] == "excluded":
            return {**result, "status": "excluded", "reason": row["reason"]}
        migrated = files.pinned_text(roots[1], target)
    except UnicodeError:
        return {**result, "status": "unverified", "reason": "unsupported_text_encoding"}
    except OSError:
        return {**result, "status": "unverified", "reason": "pinned_file_unreadable"}
    except ValueError as exc:
        return {**result, "status": "unverified", "reason": str(exc)}
    if row["origin"] == "ai":
        return {**result, "status": "review_candidate", "reason": "pairing_not_confirmed"}
    try:
        left = normalize_php(original, name=source["name"], start_line=source["start_line"], end_line=source["end_line"])
    except (UnsupportedPhp, RecursionError) as exc:
        return {**result, "status": "unverified", "reason": "php_unsupported", "detail": str(exc),
                **({"diagnostic_line": exc.line} if isinstance(exc, UnsupportedPhp) and exc.line else {})}
    try:
        right = roslyn(migrated, target)
    except ValueError as exc:
        return {**result, "status": "unverified", "reason": str(exc)}
    if right["status"] != "supported":
        return {**result, "status": "unverified", "reason": "csharp_" + right["reason"],
                **({"diagnostic_line": right["diagnostic_line"]} if "diagnostic_line" in right else {})}
    contract = ["int" if kind == "int32" else kind for kind in row["inputs"]]
    try:
        return result | compare_models(left, right, contract, source, target)
    except (KeyError, TypeError, ValueError, RecursionError):
        return {**result, "status": "unverified", "reason": "unsupported_normalized_model"}


def check(plan, source_root, target_root, roslyn):
    roots = (source_root.resolve(strict=True), target_root.resolve(strict=True))
    if not all(root.is_dir() for root in roots):
        raise ValueError("invalid_root")
    rows = _plan(plan)
    files = Files()
    results = [_assess(row, roots, files, roslyn) for row in rows]
    counts = dict.fromkeys(_STATUSES, 0) | Counter(result["status"] for result in results)
    status = ("mismatch" if counts["mismatch"] else "unverified"
              if counts["unverified"] or counts["review_candidate"] or not counts["verified"] else "verified")
    return {"status": status, "summary": counts, "pairs": results}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Explicit, static PHP to C# decision checks; neither source runs.")
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--target-root", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        with TemporaryDirectory(prefix="moca-control-flow-") as folder:
            report = check(args.plan, args.source_root, args.target_root, Roslyn(Path(folder)))
    except (OSError, ValueError, RecursionError):
        report = {"status": "unverified", "reason": "invalid_or_unreadable_plan_or_roots", "pairs": []}
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return {"verified": 0, "mismatch": 1, "unverified": 2}[report["status"]]


if __name__ == "__main__":
    sys.exit(main())
