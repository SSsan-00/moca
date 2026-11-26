"""チェッカー単独再開用に、照合可能な .NET ビルドの版を記録する。"""
from __future__ import annotations


import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from time import monotonic
from xml.etree import ElementTree

from ..execution import HEARTBEAT_SECONDS, check_cancelled, report_activity
from ..files import safe_target, sha256_file
from ..saved_state import content_hash, nonempty_text


def _fingerprint(root: Path, excluded=(), *, label: str | None = None) -> str:
    """ディレクトリを安定した順で読み、停止可能なビルド版のハッシュを返す。"""
    digest = hashlib.sha256()
    next_update = monotonic() + HEARTBEAT_SECONDS

    def check():
        """列挙・読み取りの合間に停止と進捗通知の時刻を確認する。"""
        nonlocal next_update
        check_cancelled()
        now = monotonic()
        if now >= next_update:
            report_activity(f"ビルド版を照合しています（{label or root.name}）。")
            next_update = now + HEARTBEAT_SECONDS

    # os.walk と同じ順番で、各ディレクトリのファイルを先に記録する。
    pending = [root]
    while pending:
        check()
        parent = pending.pop()
        relative = parent.relative_to(root)
        directories, files = [], []
        with os.scandir(parent) as entries:
            for entry in entries:
                check()
                if (relative / entry.name).as_posix() in excluded:
                    continue
                (directories if entry.is_dir() else files).append(entry.name)
        for name in sorted(directories, reverse=True):
            check()
            path = parent / name
            if path.is_symlink() or path.resolve() != path:
                raise ValueError("ビルド入力にリンクがあります。")
            pending.append(path)
        for name in sorted(files):
            check()
            path = parent / name
            if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
                raise ValueError("ビルド入力に通常ファイル以外があります。")
            row = [path.relative_to(root).as_posix(), sha256_file(path, check=check)]
            digest.update(json.dumps(row).encode("utf-8"))
    check_cancelled()
    return digest.hexdigest()


def build_inputs(worktree: Path, project_name: str | None) -> dict | None:
    """依存範囲を限定できる単一の SDK スタイルプロジェクトを照合する。"""
    if not project_name or not project_name.lower().endswith(".csproj"):
        return None
    relative = Path(project_name)
    if relative.is_absolute() or ".." in relative.parts:
        return None
    check_cancelled()
    try:
        project = safe_target(worktree, relative)
        root = ElementTree.fromstring(project.read_bytes())
        if root.tag != "Project" or root.attrib != {"Sdk": "Microsoft.NET.Sdk"}:
            return None
        settings = {}
        allowed = {
            "TargetFramework": r"net[5-9]\.[0-9]+|net[1-9][0-9]+\.[0-9]+",
            "Nullable": r"enable|disable|annotations|warnings",
            "ImplicitUsings": r"enable|disable|true|false",
            "OutputType": r"Exe|Library|WinExe",
            "LangVersion": r"(?:[1-9][0-9]*(?:\.[0-9]+)?|default|latest|latestMajor|preview)",
            "TreatWarningsAsErrors": r"true|false",
        }
        for group in root:
            if group.tag != "PropertyGroup" or group.attrib:
                return None
            for item in group:
                value = (item.text or "").strip()
                if item.attrib or len(item) or item.tag in settings or not re.fullmatch(allowed.get(item.tag, r"(?!)"), value):
                    return None
                settings[item.tag] = value
        framework = settings.get("TargetFramework", "")
        if not re.fullmatch(r"net[5-9]\.[0-9]+|net[1-9][0-9]+\.[0-9]+", framework):
            return None
        overrides = {
            "directory.build.props", "directory.build.targets", "directory.build.rsp",
            "directory.packages.props", "global.json", "msbuild.rsp",
        }
        for parent in project.parents:
            check_cancelled()
            for entry in parent.iterdir():
                check_cancelled()
                if entry.name.casefold() in overrides:
                    return None
        environment = {key: value for key, value in os.environ.items() if key not in {"_", "SHLVL"}}
        if any(key.casefold().startswith(("msbuild", "custombefore", "customafter", "directorybuild",
                                          "roslyntargets", "csc", "referenceassembly", "frameworkpathoverride"))
               for key in environment):
            return None
        dotnet = shutil.which("dotnet")
        if not dotnet:
            return None
        executable = Path(dotnet).resolve()
        packs = executable.parent / "packs" / "Microsoft.NETCore.App.Ref"
        if packs.is_symlink() or not packs.is_dir():
            return None
        excluded = (".git", *((relative.parent / path).as_posix() for path in ("bin", "obj")))
        return {
            "project": project_name, "framework": framework,
            "source": _fingerprint(worktree, excluded, label="作業ツリー"),
            "environment": hashlib.sha256(json.dumps(environment, sort_keys=True).encode("utf-8")).hexdigest(),
            "dotnet": str(executable), "launcher": sha256_file(executable, check=check_cancelled),
            "framework_packs": _fingerprint(packs.resolve(), label=".NET SDK 参照パック"),
        }
    except (OSError, ValueError, ElementTree.ParseError):
        return None


def build_artifacts(worktree: Path, inputs: dict) -> dict | None:
    """生成物と復元メタデータを照合する。外部パッケージがある場合は再ビルドが必要。"""
    check_cancelled()
    relative_project = Path(inputs["project"])
    assembly = relative_project.stem + ".dll"
    relative_dir = relative_project.parent
    bin_root, obj_root = worktree / relative_dir / "bin", worktree / relative_dir / "obj"
    if any(root.is_symlink() or not root.is_dir() or not root.resolve().is_relative_to(worktree)
           for root in (bin_root, obj_root)):
        return None
    try:
        dll = safe_target(worktree, relative_dir / "bin/Debug" / inputs["framework"] / assembly)
        assets = safe_target(worktree, relative_dir / "obj/project.assets.json")
        if not dll.is_file() or json.loads(assets.read_bytes()).get("libraries") != {}:
            return None
        return {"bin": _fingerprint(bin_root), "obj": _fingerprint(obj_root)}
    except (OSError, ValueError, AttributeError):
        return None


_INPUT_NAMES = frozenset({"project", "framework", "dotnet"})
_INPUT_HASHES = frozenset({"source", "environment", "launcher", "framework_packs"})
_ARTIFACT_HASHES = frozenset({"bin", "obj"})


def _valid_build_receipt(receipt):
    if not isinstance(receipt, dict) or receipt.keys() != {"inputs", "artifacts"}:
        return False
    inputs, artifacts = receipt["inputs"], receipt["artifacts"]
    return (
        isinstance(inputs, dict) and inputs.keys() == _INPUT_NAMES | _INPUT_HASHES
        and all(nonempty_text(inputs[name]) for name in _INPUT_NAMES)
        and all(content_hash(inputs[name]) for name in _INPUT_HASHES)
        and isinstance(artifacts, dict) and artifacts.keys() == _ARTIFACT_HASHES
        and all(content_hash(value) for value in artifacts.values())
    )


def same_build_artifacts(worktree, inputs, receipt):
    return (
        _valid_build_receipt(receipt)
        and receipt["inputs"] == inputs
        and (artifacts := build_artifacts(worktree, inputs)) is not None
        and artifacts == receipt["artifacts"]
    )


def check_build_receipt(config, worktree, record):
    receipt = record.get("checker_build")
    exit_code = record.get("build_exit_code")
    if type(exit_code) is not int or exit_code != 0 or not _valid_build_receipt(receipt):
        raise ValueError("照合できるビルド成果物がありません。ビルドを実行してください。")
    inputs = build_inputs(worktree, config["build_project"])
    if not inputs or receipt["inputs"] != inputs:
        raise ValueError("ビルド入力、依存関係、または実行環境が変わりました。再ビルドしてください。")
    if build_artifacts(worktree, inputs) != receipt["artifacts"]:
        raise ValueError("ビルド成果物が変更または削除されました。再ビルドしてください。")
    return receipt
