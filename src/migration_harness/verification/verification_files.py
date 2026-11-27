
from pathlib import Path

from ..command_paths import expand_command_paths
from ..config import is_command
from ..execution import check_cancelled
from ..files import safe_target, sha256_file
from ..saved_state import content_hash


_SCRIPT_SUFFIXES = frozenset({".bat", ".cmd", ".cjs", ".dll", ".exe", ".js", ".mjs", ".pl", ".ps1", ".py", ".rb", ".sh"})


def verification_scripts(config, checker):
    phases = ("before_build", "after_build")
    if checker is not None:
        phases += ("before_checker", "after_checker")
    return {phase: commands for phase in phases if (commands := config.get("scripts", {}).get(phase))}


def has_verification_evidence(verification):
    if not isinstance(verification, dict):
        return False
    commands = ("configured_checker_command", "checker_command")
    if not all(key in verification and (verification[key] is None or is_command(verification[key])) for key in commands):
        return False
    if type(verification.get("checker_from_cli")) is not bool or (
        verification["checker_from_cli"] and (
            verification.get("toml_checker_scope") != "project" or "toml_checker_command" not in verification
            or verification["toml_checker_command"] is not None and not is_command(verification["toml_checker_command"])
        )
    ):
        return False
    return (
        "build_project" in verification
        and (verification["build_project"] is None or isinstance(verification["build_project"], str))
        and isinstance(verification.get("scripts"), dict)
        and all(isinstance(values := verification.get(key), dict)
                and all(content_hash(value, missing=True) for value in values.values())
                for key in ("target_hashes", "input_files"))
    )


def configured_verification_checker(config, verification):
    configured = config["checker_command"]
    if (
        verification.get("checker_from_cli") is True
        and "configured_checker_command" in verification
        and verification.get("toml_checker_scope") == "project"
        and "toml_checker_command" in verification
        and not config.get("checker_from_cli")
        and configured == verification["toml_checker_command"]
    ):
        return verification["configured_checker_command"]
    return configured


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


def checker_provenance(config):
    if not config.get("checker_from_cli"):
        return {"checker_from_cli": False}
    return {"checker_from_cli": True, "toml_checker_command": config.get("toml_checker_command"),
            "toml_checker_scope": "project"}
