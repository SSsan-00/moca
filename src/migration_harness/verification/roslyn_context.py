import hashlib
import json
import os
import stat
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from time import monotonic
from xml.etree import ElementTree

from ..files import safe_target

_MAX_ENTRIES = 5000
_MAX_SOURCES = 400
_MAX_BYTES = 8 * 1024 * 1024
_MAX_DESCRIPTOR = 64 * 1024
_WEB_DEPENDENCY_DIRECTORIES = frozenset({"node_modules", "jspm_packages", "bower_components"})
_OVERRIDES = frozenset({
    "directory.build.props", "directory.build.targets", "directory.build.rsp",
    "directory.packages.props", "global.json", "msbuild.rsp",
})
_COMPILATION_ENVIRONMENT = frozenset({
    "allowunsafeblocks", "appdesignerfolder", "assemblysearchpaths", "checkforoverflowunderflow",
    "defaultlanguagesourceextension", "defaultwebcontentitemexcludes",
    "features", "nocompilerstandardlib", "nostandardlib", "platformtarget", "runtimeidentifier", "runtimeidentifiers",
    "targetframeworkidentifier", "targetframeworkrootpath", "targetframeworkversion", "targetingpackroot",
    "treatwarningsaserrors", "usewindowsforms", "usewpf", "warninglevel", "warningsaserrors", "warningsnotaserrors",
})
_PROPERTIES = {
    "TargetFramework": {"net9.0"},
    "Nullable": {"enable", "disable", "annotations", "warnings"},
    "ImplicitUsings": {"enable", "disable", "true", "false"},
    "OutputType": {"Exe", "Library", "WinExe"},
    "LangVersion": {"13", "13.0", "default"},
    "AssemblyName": None,
    "RootNamespace": None,
}


class ProjectContextUnavailable(ValueError):
    pass


def _stamp(info):
    return (info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _read_file(path, budget, expected):
    before = path.stat()
    if _stamp(before) != expected:
        raise ProjectContextUnavailable("project_changed")
    if before.st_size > budget:
        raise ProjectContextUnavailable("project_too_large")
    with path.open("rb") as stream:
        data = stream.read(before.st_size + 1)
    if len(data) > budget:
        raise ProjectContextUnavailable("project_too_large")
    if len(data) != before.st_size:
        raise ProjectContextUnavailable("project_changed")
    after = path.stat()
    if _stamp(after) != expected or after.st_ino != before.st_ino:
        raise ProjectContextUnavailable("project_changed")
    return data


def _project_file(worktree, configured):
    if configured:
        relative = Path(configured)
    else:
        candidates = []
        for index, item in enumerate(worktree.iterdir()):
            if index >= _MAX_ENTRIES:
                raise ProjectContextUnavailable("project_too_large")
            if item.suffix.lower() in {".csproj", ".sln", ".slnx", ".vbproj", ".fsproj", ".proj"}:
                candidates.append(item.relative_to(worktree))
        if len(candidates) != 1:
            raise ProjectContextUnavailable("project_required")
        relative = candidates[0]
    if relative.suffix.lower() != ".csproj" or relative.is_absolute() or ".." in relative.parts:
        raise ProjectContextUnavailable("project_required")
    project = safe_target(worktree, relative)
    if not project.is_file():
        raise ProjectContextUnavailable("project_required")
    return project


def _check_project_overrides(project):
    properties = {"configuration", "platform", "defineconstants", "baseoutputpath", "baseintermediateoutputpath",
                  *(name.casefold() for name in _PROPERTIES)}
    prefixes = ("msbuild", "custombefore", "customafter", "directorybuild", "roslyntargets", "csc",
                "referenceassembly", "frameworkpathoverride", "enabledefault", "defaultitemexclude", "razor",
                "compilertool", "disableimplicit", "disableframework")
    overridden = properties | _COMPILATION_ENVIRONMENT
    if any(key in overridden or key.startswith(prefixes) for key in map(str.casefold, os.environ)):
        raise ProjectContextUnavailable("project_configuration")
    for parent in project.parents:
        if any((parent / name).exists() for name in _OVERRIDES):
            raise ProjectContextUnavailable("project_configuration")


@lru_cache(maxsize=32)
def _project_description(project, stamp):
    try:
        root = ElementTree.fromstring(_read_file(project, _MAX_DESCRIPTOR, stamp))
    except ElementTree.ParseError:
        return None, None
    sdk = root.get("Sdk")
    if (root.tag != "Project" or set(root.attrib) != {"Sdk"}
            or sdk not in {"Microsoft.NET.Sdk", "Microsoft.NET.Sdk.Web"}):
        return None, None
    return root, sdk == "Microsoft.NET.Sdk.Web"


def _project_settings(project, stamp):
    root, _ = _project_description(project, stamp)
    if root is None:
        raise ProjectContextUnavailable("project_configuration")
    settings = {}
    for group in root:
        if group.tag != "PropertyGroup" or group.attrib:
            raise ProjectContextUnavailable("project_configuration")
        for item in group:
            value = (item.text or "").strip()
            allowed = _PROPERTIES.get(item.tag)
            if (item.attrib or len(item) or item.tag in settings or item.tag not in _PROPERTIES
                    or allowed is not None and value not in allowed
                    or allowed is None and not all(part.isidentifier() for part in value.split("."))):
                raise ProjectContextUnavailable("project_configuration")
            settings[item.tag] = value
    if "TargetFramework" not in settings:
        raise ProjectContextUnavailable("project_configuration")
    return settings


def _excluded_directory(parts, web, *, razor_content=False):
    parts = tuple(part.casefold() for part in parts)
    if not parts:
        return False
    if parts[0] in {"bin", "obj"}:
        return True
    return bool(web) and (
        (not razor_content and parts[0] == "wwwroot")
        or parts[:2] in {("properties", "publishprofiles"), ("properties", "servicedependencies")}
        or any(part in _WEB_DEPENDENCY_DIRECTORIES for part in parts)
    )


def _project_sources(root, project, target, deadline, web):
    entries = 0
    files = []
    directories = [root]
    while directories:
        parent = directories.pop()
        prefix = parent.relative_to(root).parts
        with os.scandir(parent) as children:
            for item in children:
                entries += 1
                if entries > _MAX_ENTRIES or monotonic() > deadline:
                    raise ProjectContextUnavailable("project_too_large")
                if _excluded_directory((*prefix, item.name), web):
                    continue
                if item.name.startswith(".") and (
                        not item.name.lower().endswith(".cs") or item.is_dir(follow_symlinks=False)):
                    continue
                metadata = item.stat(follow_symlinks=False)
                if (stat.S_ISLNK(metadata.st_mode)
                        or getattr(metadata, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                    raise ProjectContextUnavailable("project_configuration")
                path = Path(item.path)
                if stat.S_ISDIR(metadata.st_mode):
                    directories.append(path)
                elif path.suffix.lower() in {".csproj", ".fsproj", ".vbproj", ".razor"} and path != project:
                    raise ProjectContextUnavailable("project_configuration")
                elif path.suffix.lower() == ".cs" and path != target:
                    if len(files) == _MAX_SOURCES:
                        raise ProjectContextUnavailable("project_too_large")
                    if not stat.S_ISREG(metadata.st_mode):
                        raise ProjectContextUnavailable("project_configuration")
                    files.append((path, _stamp(metadata)))
    return sorted(files)


@dataclass(frozen=True)
class ProjectSnapshot:
    worktree: Path
    project: Path
    descriptor: tuple[int, int, int]
    target: Path
    web: bool | None
    sources: list[tuple[Path, tuple[int, int, int]]]
    imports: list[tuple[Path, tuple[int, int, int]]]
    revision: str
    deadline: float


def editor_project_snapshot(worktree, configured_project, target):
    worktree = worktree.resolve(strict=True)
    project = _project_file(worktree, configured_project)
    context_root = project.parent
    path = safe_target(worktree, Path(target))
    if not path.is_relative_to(context_root):
        raise ProjectContextUnavailable("project_required")
    target_path = path.relative_to(context_root)
    _check_project_overrides(project)
    deadline = monotonic() + 2
    descriptor = _stamp(project.stat())
    _, web = _project_description(project, descriptor)
    parent_parts = target_path.parts[:-1]
    razor_content = path.suffix.lower() == ".cshtml"
    if (any(part.startswith(".") for part in parent_parts)
            or _excluded_directory(parent_parts, web, razor_content=razor_content)):
        raise ProjectContextUnavailable("project_required")
    sources = _project_sources(context_root, project, path, deadline, web) if web is not None else []
    imports = []
    if web is not None and razor_content:
        for directory in reversed([p for p in path.parents if p.is_relative_to(context_root)]):
            relative = (directory / "_ViewImports.cshtml").relative_to(worktree)
            item = safe_target(worktree, relative)
            if item.is_file():
                imports.append((item, _stamp(item.stat())))
    revision = hashlib.sha256(json.dumps([
        (item.relative_to(context_root).as_posix(), metadata)
        for item, metadata in [(project, descriptor), *sources, *imports]
    ]).encode("utf-8")).hexdigest()
    return ProjectSnapshot(worktree, project, descriptor, path, web, sources, imports, revision, deadline)


def editor_project_context(worktree, configured_project, target, *, snapshot=None):
    snapshot = snapshot or editor_project_snapshot(worktree, configured_project, target)
    project = snapshot.project
    context_root = project.parent
    path = snapshot.target
    settings = _project_settings(project, snapshot.descriptor)
    if path.suffix.lower() == ".cshtml" and (
            not snapshot.web or path.name.casefold() in {"_viewimports.cshtml", "_viewstart.cshtml"}):
        raise ProjectContextUnavailable("project_configuration")
    remaining = _MAX_BYTES

    def collect(paths, max_file_bytes):
        nonlocal remaining
        documents = []
        for source, stamp in paths:
            if monotonic() > snapshot.deadline:
                raise ProjectContextUnavailable("project_too_large")
            resolved = safe_target(snapshot.worktree, source.relative_to(snapshot.worktree))
            raw = _read_file(resolved, min(remaining, max_file_bytes), stamp)
            remaining -= len(raw)
            documents.append({"Path": source.relative_to(context_root).as_posix(), "Text": raw.decode("utf-8-sig")})
        return documents

    return {
        "Sources": collect(snapshot.sources, _MAX_BYTES), "Imports": collect(snapshot.imports, _MAX_DESCRIPTOR),
        "ProjectPath": path.relative_to(context_root).as_posix(),
        "Web": snapshot.web, "AssemblyName": settings.get("AssemblyName", project.stem),
        "RootNamespace": settings.get("RootNamespace", project.stem.replace("-", "_")),
        "ImplicitUsings": settings.get("ImplicitUsings") in {"enable", "true"},
        "OutputType": settings.get("OutputType", "Exe" if snapshot.web else "Library"),
        "Nullable": settings.get("Nullable", "disable"),
    }
