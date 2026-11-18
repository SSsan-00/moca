"""チェッカー単独再開用に、照合可能な .NET ビルドの版を記録する。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from xml.etree import ElementTree

from .execution import check_cancelled
from .files import safe_target, sha256_file


def _fingerprint(root: Path, excluded=()) -> str:
    digest = hashlib.sha256()
    for current, directories, filenames in os.walk(root, onerror=_raise_scan_error):
        parent = Path(current)
        if parent == root:
            directories[:] = [name for name in directories if name not in excluded]
            filenames = [name for name in filenames if name not in excluded]
        directories.sort()
        for name in directories:
            path = parent / name
            if path.is_symlink() or path.resolve() != path:
                raise ValueError("ビルド入力にリンクがあります。")
        for name in sorted(filenames):
            check_cancelled()
            path = parent / name
            if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
                raise ValueError("ビルド入力に通常ファイル以外があります。")
            row = [path.relative_to(root).as_posix(), sha256_file(path)]
            digest.update(json.dumps(row).encode("utf-8"))
    return digest.hexdigest()


def _raise_scan_error(error: OSError) -> None:
    raise error


def build_inputs(worktree: Path, project_name: str | None) -> dict | None:
    """依存範囲を限定できる単一の SDK スタイルプロジェクトを照合する。"""
    if not project_name or Path(project_name).name != project_name or not project_name.endswith(".csproj"):
        return None
    project = worktree / project_name
    if project.is_symlink():
        return None
    try:
        root = ElementTree.fromstring(project.read_bytes())
        if root.tag != "Project" or root.attrib != {"Sdk": "Microsoft.NET.Sdk"}:
            return None
        children = list(root)
        if len(children) != 1 or children[0].tag != "PropertyGroup" or children[0].attrib:
            return None
        properties = list(children[0])
        if (len(properties) != 1 or properties[0].tag != "TargetFramework" or properties[0].attrib
                or len(properties[0])):
            return None
        framework = (properties[0].text or "").strip()
        if not re.fullmatch(r"net[5-9]\.[0-9]+|net[1-9][0-9]+\.[0-9]+", framework):
            return None
        overrides = {
            "directory.build.props", "directory.build.targets", "directory.build.rsp",
            "directory.packages.props", "global.json", "msbuild.rsp",
        }
        if any(entry.name.casefold() in overrides for parent in (worktree, *worktree.parents)
               for entry in parent.iterdir()):
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
        return {
            "project": project_name, "framework": framework,
            "source": _fingerprint(worktree, (".git", "bin", "obj")),
            "environment": hashlib.sha256(json.dumps(environment, sort_keys=True).encode("utf-8")).hexdigest(),
            "dotnet": str(executable), "launcher": sha256_file(executable),
            "framework_packs": _fingerprint(packs.resolve()),
        }
    except (OSError, ValueError, ElementTree.ParseError):
        return None


def build_artifacts(worktree: Path, inputs: dict) -> dict | None:
    """生成物と復元メタデータを照合する。外部パッケージがある場合は再ビルドが必要。"""
    assembly = Path(inputs["project"]).stem + ".dll"
    bin_root, obj_root = worktree / "bin", worktree / "obj"
    if any(root.is_symlink() or not root.is_dir() or not root.resolve().is_relative_to(worktree)
           for root in (bin_root, obj_root)):
        return None
    try:
        dll = safe_target(worktree, Path("bin/Debug") / inputs["framework"] / assembly)
        assets = safe_target(worktree, Path("obj/project.assets.json"))
        if not dll.is_file() or json.loads(assets.read_bytes()).get("libraries") != {}:
            return None
        return {"bin": _fingerprint(bin_root), "obj": _fingerprint(obj_root)}
    except (OSError, ValueError, AttributeError):
        return None
