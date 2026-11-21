from pathlib import Path

from .command_paths import expand_command_paths
from .execution import check_cancelled
from .files import safe_target, sha256_file


_SCRIPT_SUFFIXES = frozenset({".bat", ".cmd", ".cjs", ".dll", ".exe", ".js", ".mjs", ".pl", ".ps1", ".py", ".rb", ".sh"})


def _file_fingerprint(path: Path) -> str | None:
    try:
        if path.is_file():
            return sha256_file(path, check=check_cancelled)
    except OSError:
        pass
    return None


def verification_files(config, worktree, target, checker, scripts):
    expected = config.get("checker_verification_files", [])
    commands = ([checker] if checker else []) + [command for group in scripts.values() for command in group]
    paths: dict[str, Path | None] = {}
    for name in expected:
        try:
            paths["expected:" + name] = safe_target(worktree, Path(name))
        except (OSError, ValueError):
            paths["expected:" + name] = None
    for command in commands:
        argv = expand_command_paths(command, worktree, config["build_project"], target)
        for index, value in enumerate(argv):
            if not value or value.startswith("-"):
                continue
            arg = Path(value)
            explicit_executable = index == 0 and (arg.is_absolute() or len(arg.parts) > 1 or value.startswith(("./", ".\\")))
            if not explicit_executable and (arg.suffix.casefold() not in _SCRIPT_SUFFIXES or index == 0 and len(arg.parts) == 1):
                continue
            paths["command:" + arg.as_posix()] = worktree / arg
    return {name: _file_fingerprint(path) if path is not None else None for name, path in paths.items()}
