"""Static dependency rules for MoCA's Python product modules (no product imports)."""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from collections import Counter, deque
from dataclasses import dataclass
from importlib.util import resolve_name
from pathlib import Path


PACKAGE = "migration_harness"
DOMAINS = {"context", "junie", "rules", "runs", "verification"}
APPROVED_DISTRIBUTIONS = {"tomlkit": "tomlkit"}  # distribution -> top-level import


@dataclass(frozen=True)
class Import:
    source: str
    target: str
    symbol: str | None
    scope: str
    path: Path
    line: int

    @property
    def key(self):
        return self.source, self.target, self.symbol, self.scope

    def describe(self):
        symbol = f":{self.symbol}" if self.symbol else ""
        return f"{self.path.as_posix()}:{self.line}: {self.source} -> {self.target}{symbol} [{self.scope}]"


def _product_edge(source, target, symbol, scope):
    return f"{PACKAGE}.{source}", f"{PACKAGE}.{target}", symbol, scope


RUN_WORKER = _product_edge("runs.run_manager", "web.dashboard", "execute_dashboard_action", "local")
DIRECTION_EXCEPTIONS = {
    _product_edge("config", "junie.junie_notifications", "notification_settings", "module"):
        "Configuration uses the same notification defaults and validation as the notification service.",
    _product_edge("runs.editor_view", "workflow", "can_resume_run", "module"):
        "Editor prompt timing shares the workflow's saved-run eligibility check.",
    _product_edge("junie.junie_conversation", "workflow", "plan_stage_resume", "local"):
        "A persisted editor instruction uses the same stage plan as dashboard resume.",
    _product_edge("junie.junie_conversation", "workflow", "resume_with_junie", "local"):
        "The conversation dispatcher uses the normal verification/resume entry point.",
    RUN_WORKER: "The RunManager passes a queued job to the dashboard's existing worker.",
}
LOCAL_WORKER_CYCLE = frozenset({
    RUN_WORKER,
    _product_edge("web.dashboard", "runs.run_manager", "RunManager", "local"),
})


def module_name(path, root):
    parts = path.relative_to(root).with_suffix("").parts
    return ".".join((PACKAGE, *(parts[:-1] if parts[-1] == "__init__" else parts)))


def _type_guard_aliases(tree):
    names, modules = set(), set()
    for statement in tree.body:
        if isinstance(statement, ast.ImportFrom) and statement.level == 0 and statement.module == "typing":
            names.update(alias.asname or alias.name for alias in statement.names if alias.name == "TYPE_CHECKING")
        elif isinstance(statement, ast.Import):
            modules.update(alias.asname or alias.name for alias in statement.names if alias.name == "typing")
    bindings = Counter()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            bindings.update(alias.asname or alias.name.partition(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            bindings.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bindings[node.id] += 1
        elif isinstance(node, ast.arg):
            bindings[node.arg] += 1
        elif isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef, ast.ExceptHandler)) and node.name:
            bindings[node.name] += 1
    return {name for name in names if bindings[name] == 1}, {name for name in modules if bindings[name] == 1}


def _type_guard(test, aliases):
    names, modules = aliases
    return (
        isinstance(test, ast.Name) and test.id in names
        or isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
        and isinstance(test.value, ast.Name) and test.value.id in modules
    )


def _statements(node, aliases, *, local=False, type_only=False):
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        yield node, "type" if type_only else "local" if local else "module"
        return
    if isinstance(node, ast.If) and _type_guard(node.test, aliases):
        for branch, checking in ((node.body, True), (node.orelse, type_only)):
            for child in branch:
                yield from _statements(child, aliases, local=local, type_only=checking)
        return
    for child in ast.iter_child_nodes(node):
        yield from _statements(child, aliases, local=local or isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)),
                               type_only=type_only)


def _targets(node, package, modules):
    if isinstance(node, ast.Import):
        for alias in node.names:
            yield alias.name, None
        return
    base = node.module or ""
    if node.level:
        base = resolve_name("." * node.level + base, package)
    for alias in node.names:
        submodule = f"{base}.{alias.name}"
        yield (submodule, None) if submodule in modules else (base, alias.name)


def source_imports(root):
    paths = sorted(root.rglob("*.py"))
    if not (root / "__init__.py").is_file() or not paths:
        raise ValueError(f"Missing product package: {root}")
    modules = {module_name(path, root) for path in paths}
    # Subpackages without __init__.py are still importable via their module files.
    namespaces = {module.rpartition(".")[0] for module in modules}
    for module in tuple(namespaces):
        while "." in module:
            module = module.rpartition(".")[0]
            namespaces.add(module)
    modules.update(namespaces - {""})
    imports = []
    for path in paths:
        name = module_name(path, root)
        package = name if path.name == "__init__.py" else name.rpartition(".")[0]
        relative = path.relative_to(root.parent.parent)
        tree = ast.parse(path.read_bytes(), filename=relative.as_posix())
        for node, scope in _statements(tree, _type_guard_aliases(tree)):
            try:
                targets = list(_targets(node, package, modules))
            except ImportError as error:
                raise ValueError(f"{relative}:{node.lineno}: {error}") from error
            imports.extend(Import(name, target, symbol, scope, relative, node.lineno) for target, symbol in targets)
    return imports, modules


def _layer(module):
    head = module.removeprefix(PACKAGE + ".").partition(".")[0]
    return head if head in DOMAINS | {"cli", "web", "workflow"} else "common"


def forbidden_direction(source, target):
    begin, end = _layer(source), _layer(target)
    return (
        end == "cli" and begin != "cli"
        or end == "web" and begin not in {"web", "cli"}
        or end == "workflow" and begin not in {"web", "cli", "workflow"}
        or begin == "common" and end != "common"
    )


def declared_distributions(path):
    def canonical(distribution):
        return re.sub(r"[-_.]+", "-", distribution).lower()

    with path.open("rb") as stream:
        dependencies = tomllib.load(stream)["project"]["dependencies"]
    return {canonical(re.match(r"[\w.-]+", dependency)[0]) for dependency in dependencies}


def _return_path(graph, first):
    if first.source == first.target:
        return (first,)
    pending = deque([(first.target, ())])
    visited = {first.target}
    while pending:
        module, path = pending.popleft()
        for edge in graph.get(module, ()):
            if edge.target == first.source:
                if not path and frozenset({first.key, edge.key}) == LOCAL_WORKER_CYCLE:
                    continue
                return (first, *path, edge)
            if edge.target not in visited:
                visited.add(edge.target)
                pending.append((edge.target, (*path, edge)))
    return ()


def _cycles(imports):
    graph = {}
    for edge in imports:
        if edge.scope != "type":
            graph.setdefault(edge.source, []).append(edge)
    cycles = set()
    for edges in graph.values():
        for edge in edges:
            cycle = _return_path(graph, edge)
            if cycle:
                nodes = tuple(step.source for step in cycle)
                rotations = [nodes[i:] + nodes[:i] for i in range(len(nodes))]
                identifier = min(rotations)
                if identifier not in cycles:
                    cycles.add(identifier)
                    yield "Import cycle:\n  " + "\n  ".join(step.describe() for step in cycle)


def dependency_violations(imports, modules, declared, test_modules=()):
    problems = []
    approved = set(APPROVED_DISTRIBUTIONS)
    for name in sorted(declared - approved):
        problems.append(f"pyproject.toml: unapproved runtime distribution: {name}")
    for name in sorted(approved - declared):
        problems.append(f"pyproject.toml: approved runtime distribution is not declared: {name}")

    internal = []
    used_exceptions = set()
    tests = {"tests", *test_modules}
    for edge in imports:
        root = edge.target.partition(".")[0]
        if root in tests or root.startswith("test_") or edge.target.split(".")[:2] == [PACKAGE, "tests"]:
            problems.append(f"{edge.describe()}: product must not import tests")
        elif root == PACKAGE:
            if edge.target not in modules:
                problems.append(f"{edge.describe()}: unresolved product import")
                continue
            if edge.target != edge.source or edge.symbol is None:
                internal.append(edge)
            if forbidden_direction(edge.source, edge.target):
                if edge.key in DIRECTION_EXCEPTIONS:
                    used_exceptions.add(edge.key)
                else:
                    problems.append(f"{edge.describe()}: forbidden {_layer(edge.source)} -> {_layer(edge.target)} direction")
        elif root not in sys.stdlib_module_names and not any(
            module == root and distribution in declared for distribution, module in APPROVED_DISTRIBUTIONS.items()
        ):
            problems.append(f"{edge.describe()}: external import is not approved and declared")
    for source, target, symbol, scope in sorted(DIRECTION_EXCEPTIONS.keys() - used_exceptions):
        if source in modules:
            problems.append(f"{source} -> {target}:{symbol} [{scope}]: unused direction exception; remove it")
    return problems + list(_cycles(internal))
