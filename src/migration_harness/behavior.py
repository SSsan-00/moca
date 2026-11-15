"""RunのPHPとC#を隔離した作業場所で実行し、代表ケースを比較する。

両プログラムはUTF-8のJSON配列を標準入力から受け取り、同じ順番で結果を出力する。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from typing import Any, BinaryIO

from migration_harness.workflow import _has_symlink_component, _load_saved_run, worktree_source_lock

_RESULT_VERSION = 1  # 実行や照合の契約が変わるときに更新する。
_RESULT_FILE = "behavior.json"
_FRAMEWORK = "net10.0"
_PROJECT = f"""<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <OutputType>Exe</OutputType>
    <TargetFramework>{_FRAMEWORK}</TargetFramework>
  </PropertyGroup>
</Project>
"""
_NUGET_CONFIG = "<configuration><packageSources><clear /></packageSources></configuration>\n"


def _decode_json(data: bytes) -> Any:
    """曖昧な比較を避けるため、重複キーや非有限値を含むJSONを拒否する。"""
    def unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        """同名キーの後勝ちを禁止して、入力や実測値の欠落を防ぐ。"""
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    value = json.loads(data.decode("utf-8"), object_pairs_hook=unique_keys)
    json.dumps(value, allow_nan=False)
    return value


def _source_path(worktree: Path, name: str) -> Path:
    """Runのworktree内にある、リンクを経由しない実行対象を解決する。"""
    relative = Path(name)
    if relative.anchor or ".." in relative.parts:
        raise ValueError(f"behavior source must be worktree-relative: {name}")
    path = worktree / relative
    if _has_symlink_component(worktree, relative) or not path.is_file():
        raise ValueError(f"behavior source is missing or is a symlink: {name}")
    return path


def _read_inputs(
    worktree: Path, unit: dict[str, Any], fixture_path: Path,
) -> tuple[dict[str, Any], dict[str, Any], bytes, dict[str, bytes]]:
    """ケースとソースを検証し、実行するバイト列の版を記録する。"""
    if not fixture_path.is_file():
        raise ValueError(f"behavior fixture is missing: {fixture_path}")
    fixture_bytes = fixture_path.read_bytes()
    fixture = _decode_json(fixture_bytes)
    if not isinstance(fixture, dict):
        raise ValueError("behavior fixture must be a JSON object")
    php_source = fixture.get("php_source")
    if (
        not isinstance(php_source, str)
        or Path(php_source).suffix.lower() != ".php"
        or all(Path(item["path"]) != Path(php_source) for item in unit["inputs"])
    ):
        raise ValueError("php_source must name a PHP input of the saved Run's unit")
    ignored = fixture.get("ignored_fields")
    if (
        not isinstance(ignored, list)
        or any(not isinstance(key, str) or not key for key in ignored)
        or len(set(ignored)) != len(ignored)
    ):
        raise ValueError("ignored_fields must be an array of distinct top-level field names")
    cases = fixture.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("behavior fixture must contain at least one case")
    case_ids = set()
    for case in cases:
        if (
            not isinstance(case, dict)
            or not isinstance(case.get("id"), str)
            or not case["id"].strip()
            or case["id"] in case_ids
            or not isinstance(case.get("input"), dict)
            or not isinstance(case.get("expected"), dict)
            or not case["expected"]
            or set(case["expected"]).intersection(ignored)
        ):
            raise ValueError(
                "each case needs a unique id, an input object and a nonempty expected object without ignored fields"
            )
        case_ids.add(case["id"])

    targets = {}
    csharp_sources = {}
    target_names = {unit["target"], *unit.get("targets", [])}
    with worktree_source_lock:
        php_path = _source_path(worktree, php_source)
        php_bytes = php_path.read_bytes()
        for name in sorted(target_names):
            path = _source_path(worktree, name)
            relative = path.relative_to(worktree).as_posix()
            contents = path.read_bytes()
            targets[relative] = hashlib.sha256(contents).hexdigest()
            if path.suffix.lower() == ".cs":
                csharp_sources[relative] = contents
    if not csharp_sources:
        raise ValueError("behavior verification requires a declared .cs target")
    identity = {
        "fixture": {"path": str(fixture_path), "sha256": hashlib.sha256(fixture_bytes).hexdigest()},
        "php": {"path": php_path.relative_to(worktree).as_posix(), "sha256": hashlib.sha256(php_bytes).hexdigest()},
        "targets": targets,
    }
    return identity, fixture, php_bytes, csharp_sources


def _command(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    log: BinaryIO,
    steps: list[dict[str, Any]],
    timeout: int = 15,
    input_bytes: bytes = b"",
) -> bytes:
    """外部処理を制限時間付きで実行し、ログと終了理由を保存する。"""
    step: dict[str, Any] = {"argv": argv, "timeout_seconds": timeout, "exit_code": None}
    steps.append(step)
    log.write(f"\nCommand: {json.dumps(argv)}\n".encode("utf-8"))
    log.flush()
    try:
        completed = subprocess.run(
            argv, cwd=cwd, env=env, input=input_bytes, capture_output=True, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        log.write(b"stdout:\n" + (exc.stdout or b"") + b"\nstderr:\n" + (exc.stderr or b"") + b"\n")
        step["error"] = f"{argv[0]} timed out after {timeout} seconds"
        log.write((step["error"] + "\n").encode("utf-8"))
        raise RuntimeError(step["error"]) from exc
    except (OSError, KeyboardInterrupt) as exc:
        step["error"] = str(exc) if isinstance(exc, OSError) else "interrupted"
        log.write((step["error"] + "\n").encode("utf-8", errors="replace"))
        raise
    step["exit_code"] = completed.returncode
    log.write(b"stdout:\n" + completed.stdout + b"\nstderr:\n" + completed.stderr + b"\n")
    log.write(f"Exit code: {completed.returncode}\n".encode("utf-8"))
    if completed.returncode:
        raise RuntimeError(f"{argv[0]} exited with code {completed.returncode}")
    return completed.stdout


def _observations(raw: bytes, count: int) -> list[dict[str, Any]]:
    """ケース順の実測値が件数と形式の契約を満たすことを確認する。"""
    value = _decode_json(raw)
    if not isinstance(value, list) or len(value) != count or any(not isinstance(item, dict) for item in value):
        raise ValueError(f"expected {count} JSON result objects in case order")
    return value


def _matches(actual: dict[str, Any], expected: dict[str, Any], ignored: list[str]) -> bool:
    """除外した項目を除き、型と配列の順序を含めて期待値と照合する。"""
    compared = {key: value for key, value in actual.items() if key not in ignored}
    # 辞書の等価比較では true と 1 が一致するため、JSONに直して比較する。
    return json.dumps(compared, sort_keys=True) == json.dumps(expected, sort_keys=True)


def _check_result_freshness(report: dict[str, Any], worktree: Path, unit: dict[str, Any]) -> dict[str, Any]:
    """保存時から入力やソースが変わった場合は再検証を求める状態にする。"""
    try:
        fixture_path = Path(report["identity"]["fixture"]["path"])
        if not fixture_path.is_absolute():
            raise ValueError("recorded fixture path is not absolute")
        current, _, _, _ = _read_inputs(worktree, unit, fixture_path)
        if current == report["identity"]:
            return {"status": report["status"], "result": report}
        reason = "behavior source or fixture changed"
    except (OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
        reason = str(exc)
    return {"status": "stale", "result": report, "reason": reason}


def behavior_status(config: dict[str, Any], run_id: str) -> dict[str, Any]:
    """外部プロセスを起動せず、最後の結果が今の対象に対応するか照合する。"""
    run_path, worktree, record, unit = _load_saved_run(config, run_id)
    path = run_path / _RESULT_FILE
    try:
        if path.is_symlink():
            raise ValueError("behavior result is a symlink")
        if not path.is_file():
            if not path.exists():
                return {"status": "not_run", "result": None}
            raise ValueError("behavior result must be a file")
        report = _decode_json(path.read_bytes())
        if (
            not isinstance(report, dict)
            or report.get("version") != _RESULT_VERSION
            or report.get("run_id") != run_id
            or report.get("status") not in ("passed", "failed")
            or not isinstance(report.get("cases"), list)
            or not report["cases"]
        ):
            raise ValueError("invalid or unsupported behavior result")
    except (OSError, ValueError, RecursionError) as exc:
        return {"status": "stale", "result": None, "reason": str(exc)}
    if record.get("target_apply_pending"):
        return {"status": "stale", "result": report, "reason": "migration files are being applied"}
    return _check_result_freshness(report, worktree, unit)


def verify(config: dict[str, Any], run_id: str, fixture_path: Path) -> dict[str, Any]:
    """隔離した作業場所で比較を実行し、結果をRunに保存する。"""
    run_path, worktree, record, unit = _load_saved_run(config, run_id)
    if record.get("target_apply_pending") or record.get("status") in {"junie_running", "repairing", "building", "checking"}:
        raise ValueError("finish or pause the current migration operation before verifying behavior")
    fixture_path = Path(os.path.abspath(fixture_path.expanduser()))
    identity, fixture, php_source, csharp_sources = _read_inputs(worktree, unit, fixture_path)
    cases = [
        {"id": case["id"], "input": case["input"], "expected": case["expected"], "php": None, "csharp": None}
        for case in fixture["cases"]
    ]
    ignored = fixture["ignored_fields"]
    report: dict[str, Any] = {
        "version": _RESULT_VERSION,
        "run_id": run_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "identity": identity,
        "ignored_fields": ignored,
        "environment": {"target_framework": _FRAMEWORK, "timezone": "UTC"},
        "commands": [],
        "errors": {},
        "cases": cases,
    }
    input_bytes = json.dumps([case["input"] for case in cases]).encode("utf-8")

    interrupted = False
    with NamedTemporaryFile(mode="wb", dir=run_path, prefix="behavior-", suffix=".log", delete=False) as log:
        report["log_path"] = log.name
        try:
            with TemporaryDirectory(prefix="migration-behavior-") as directory:
                php_dir = Path(directory) / "php"
                dotnet_dir = Path(directory) / "dotnet"
                php_dir.mkdir()
                dotnet_dir.mkdir()
                (php_dir / "Reference.php").write_bytes(php_source)
                for index, (name, contents) in enumerate(csharp_sources.items()):
                    source_dir = dotnet_dir / str(index)
                    source_dir.mkdir()
                    source_path = source_dir / Path(name).name
                    source_path.with_suffix(".cs").write_bytes(contents)
                (dotnet_dir / "Behavior.csproj").write_text(_PROJECT, encoding="utf-8")
                (dotnet_dir / "NuGet.Config").write_text(_NUGET_CONFIG, encoding="utf-8")
                env = {
                    **os.environ, "TZ": "UTC", "DOTNET_CLI_HOME": directory,
                    "DOTNET_CLI_TELEMETRY_OPTOUT": "1", "DOTNET_NOLOGO": "1",
                }
                options = {"env": env, "log": log, "steps": report["commands"]}
                try:
                    version = _command(["php", "-n", "-v"], cwd=php_dir, **options)
                    report["environment"]["php"] = version.decode("utf-8", errors="replace").strip()
                    actual = _command(["php", "-n", "Reference.php"], cwd=php_dir, input_bytes=input_bytes, **options)
                    for case, observed in zip(cases, _observations(actual, len(cases)), strict=True):
                        case["php"] = observed
                except (OSError, RuntimeError, ValueError, RecursionError) as exc:
                    report["errors"]["php"] = str(exc)
                try:
                    info = _command(["dotnet", "--info"], cwd=dotnet_dir, **options)
                    report["environment"]["dotnet"] = info.decode("utf-8", errors="replace").strip()
                    _command(
                        [
                            "dotnet", "build", "Behavior.csproj", "-c", "Release", "--disable-build-servers",
                            "-p:ImportDirectoryBuildProps=false", "-p:ImportDirectoryBuildTargets=false",
                            "-p:ImportDirectoryPackagesProps=false",
                            "-p:RestoreConfigFile=NuGet.Config", "-p:NuGetAudit=false",
                        ],
                        cwd=dotnet_dir, timeout=90, **options,
                    )
                    actual = _command(
                        ["dotnet", f"bin/Release/{_FRAMEWORK}/Behavior.dll"],
                        cwd=dotnet_dir, input_bytes=input_bytes, **options,
                    )
                    for case, observed in zip(cases, _observations(actual, len(cases)), strict=True):
                        case["csharp"] = observed
                except (OSError, RuntimeError, ValueError, RecursionError) as exc:
                    report["errors"]["csharp"] = str(exc)
        except OSError as exc:
            report["errors"]["setup"] = str(exc)
        except KeyboardInterrupt:
            report["errors"]["interrupted"] = "behavior verification interrupted"
            interrupted = True

    for case in cases:
        case["matches"] = {
            side: case[side] is not None and _matches(case[side], case["expected"], ignored)
            for side in ("php", "csharp")
        }
    passed = not report["errors"] and all(all(case["matches"].values()) for case in cases)
    report["status"] = "passed" if passed else "failed"
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    temporary = Path(report["log_path"]).with_suffix(".json.tmp")
    try:
        temporary.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        temporary.replace(run_path / _RESULT_FILE)
    finally:
        temporary.unlink(missing_ok=True)
    if interrupted:
        raise KeyboardInterrupt
    return _check_result_freshness(report, worktree, unit)
