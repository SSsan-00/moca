import os
from pathlib import Path
from time import monotonic
from xml.etree import ElementTree

from ..files import safe_target

_MAX_ENTRIES = 5000
_MAX_SOURCES = 400
_MAX_BYTES = 8 * 1024 * 1024
_MAX_DESCRIPTOR = 64 * 1024
_OVERRIDES = frozenset({
    "directory.build.props", "directory.build.targets", "directory.build.rsp",
    "directory.packages.props", "global.json", "msbuild.rsp",
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


def _read_file(path, budget):
    stat = path.stat()
    if stat.st_size > budget:
        raise ProjectContextUnavailable("project_too_large")
    with path.open("rb") as stream:
        data = stream.read(budget + 1)
    if len(data) > budget:
        raise ProjectContextUnavailable("project_too_large")
    after = path.stat()
    if (stat.st_size, stat.st_mtime_ns, stat.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
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


def _project_settings(project):
    root = ElementTree.fromstring(_read_file(project, _MAX_DESCRIPTOR))
    sdk = root.get("Sdk")
    if (root.tag != "Project" or set(root.attrib) != {"Sdk"}
            or sdk not in {"Microsoft.NET.Sdk", "Microsoft.NET.Sdk.Web"}):
        raise ProjectContextUnavailable("project_configuration")
    properties = {"configuration", "platform", "defineconstants", "baseoutputpath", "baseintermediateoutputpath",
                  *(name.casefold() for name in _PROPERTIES)}
    prefixes = ("msbuild", "custombefore", "customafter", "directorybuild", "roslyntargets", "csc",
                "referenceassembly", "frameworkpathoverride", "enabledefault", "defaultitemexclude", "razor",
                "compilertool", "disableimplicit", "disableframework")
    if any(key.casefold() in properties or key.casefold().startswith(prefixes) for key in os.environ):
        raise ProjectContextUnavailable("project_configuration")
    for parent in project.parents:
        if any((parent / name).exists() for name in _OVERRIDES):
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
    return sdk == "Microsoft.NET.Sdk.Web", settings


def _project_sources(root, project, target, deadline):
    entries = 0
    files = []
    directories = [root]
    while directories:
        parent = directories.pop()
        with os.scandir(parent) as children:
            for item in children:
                entries += 1
                if entries > _MAX_ENTRIES or monotonic() > deadline:
                    raise ProjectContextUnavailable("project_too_large")
                if item.name.startswith(".") or parent == root and item.name.lower() in {"obj", "bin"}:
                    continue
                path = Path(item.path)
                if item.is_symlink() or path.resolve() != path:
                    raise ProjectContextUnavailable("project_configuration")
                if item.is_dir():
                    directories.append(path)
                elif path.suffix.lower() in {".csproj", ".fsproj", ".vbproj", ".razor"} and path != project:
                    raise ProjectContextUnavailable("project_configuration")
                elif path.suffix.lower() == ".cs" and path != target:
                    if len(files) == _MAX_SOURCES:
                        raise ProjectContextUnavailable("project_too_large")
                    files.append(path)
    return sorted(files)


def editor_project_context(worktree, configured_project, target):
    worktree = worktree.resolve(strict=True)
    project = _project_file(worktree, configured_project)
    context_root = project.parent
    path = safe_target(worktree, Path(target))
    if not path.is_relative_to(context_root):
        raise ProjectContextUnavailable("project_required")
    target_path = path.relative_to(context_root)
    if (any(part.startswith(".") for part in target_path.parts)
            or target_path.parts[0].lower() in {"bin", "obj"}):
        raise ProjectContextUnavailable("project_required")
    web, settings = _project_settings(project)
    razor = path.suffix.lower() == ".cshtml"
    if razor and (not web or path.name.casefold() in {"_viewimports.cshtml", "_viewstart.cshtml"}):
        raise ProjectContextUnavailable("project_configuration")
    deadline = monotonic() + 2
    sources = _project_sources(context_root, project, path, deadline)
    imports = []
    if razor:
        for directory in reversed([p for p in path.parents if p.is_relative_to(context_root)]):
            relative = (directory / "_ViewImports.cshtml").relative_to(worktree)
            item = safe_target(worktree, relative)
            if item.is_file():
                imports.append(item)
    remaining = _MAX_BYTES

    def collect(paths):
        nonlocal remaining
        documents = []
        for source in paths:
            if monotonic() > deadline:
                raise ProjectContextUnavailable("project_too_large")
            resolved = safe_target(worktree, source.relative_to(worktree))
            raw = _read_file(resolved, min(remaining, _MAX_DESCRIPTOR) if source in imports else remaining)
            remaining -= len(raw)
            documents.append({"Path": source.relative_to(context_root).as_posix(), "Text": raw.decode("utf-8-sig")})
        return documents

    return {
        "Sources": collect(sources), "Imports": collect(imports), "ProjectPath": target_path.as_posix(),
        "Web": web, "AssemblyName": settings.get("AssemblyName", project.stem),
        "RootNamespace": settings.get("RootNamespace", project.stem.replace("-", "_")),
        "ImplicitUsings": settings.get("ImplicitUsings") in {"enable", "true"},
        "OutputType": settings.get("OutputType", "Exe" if web else "Library"),
        "Nullable": settings.get("Nullable", "disable"),
    }
