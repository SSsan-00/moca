from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from time import monotonic

from . import junie_cli
from .capsules import _selected_lines, input_encoding
from .command_paths import build_command_for_worktree, expand_command_paths, script_command_for_phase
from .config import SCRIPT_PHASES, checker_command_for_unit, find_unit, target_paths
from .errors import safe_command
from .execution import check_cancelled, report_activity



_MAX_INPUT_BYTES = 32 * 1024 * 1024


def _probe_git(root, *args, timeout=5):
    return subprocess.run(
        ["git", "--no-pager", "-C", str(root), *args], capture_output=True, timeout=timeout,
        env={**os.environ, "GIT_NO_LAZY_FETCH": "1", "GIT_NO_REPLACE_OBJECTS": "1", "GIT_OPTIONAL_LOCKS": "0"},
    )


def _entry(root, commit, path, *, timeout):
    result = _probe_git(root, "--literal-pathspecs", "ls-tree", "-z", commit, "--", path.as_posix(), timeout=timeout)
    if result.returncode:
        raise ValueError("基準コミットのファイルを確認できません。")
    header = result.stdout.split(b"\t", 1)[0].split()
    return len(header) == 3 and header[0] in {b"100644", b"100755"}


def plan_dry_run(config, unit_id):
    check_cancelled()
    unit = find_unit(config, unit_id)
    root = Path(config["project_root"])
    planned = Path(config["worktree_root"]) / "00000000-0000-0000-0000-000000000000"
    build_project = config["build_project"]
    target = unit["target"]
    checker = checker_command_for_unit(config, unit)
    scripts = config.get("scripts", {})
    changes_before_inputs = bool(scripts.get("before_prepare"))
    build_dependent = any(scripts.get(phase) for phase in SCRIPT_PHASES[:5])
    entries = []
    start = monotonic()

    def inspect_entry(commit, path):
        check_cancelled()
        remaining = 20 - (monotonic() - start)
        if remaining <= 0:
            raise subprocess.TimeoutExpired("git", 20)
        return _entry(root, commit, path, timeout=min(5, remaining))

    def add(phase, state, detail, **fields):
        check_cancelled()
        entries.append(dict(phase=phase, state=state, detail=detail, **fields))
        report_activity(f"{phase}: {detail}", kind="phase")

    def command(phase, argv, cwd, *, may_be_generated=False):
        check_cancelled()
        check_executable(phase, argv, cwd, may_be_generated=may_be_generated)
        if len(argv) < 2:
            return
        name = Path(argv[0]).stem.lower()
        if re.fullmatch(r"python(?:[23](?:\.\d+)?)?|py", name) and not argv[1].startswith("-") and argv[1] != "-":
            script = Path(argv[1])
        elif name in {"pwsh", "powershell"}:
            option = next((i for i, arg in enumerate(argv[1:], 1) if arg.lower() == "-file"), None)
            if option is None or option + 1 >= len(argv):
                return
            script = Path(argv[option + 1])
        else:
            return
        file = (cwd / script).resolve()
        if file.is_relative_to(planned):
            try:
                present = commit is not None and inspect_entry(commit, file.relative_to(planned))
            except (ValueError, subprocess.TimeoutExpired):
                add(phase, "unchecked", "基準コミットのスクリプト照合が完了しませんでした。", path=str(script))
                return
            state = "confirmed" if present else "unchecked" if may_be_generated or not commit else "error"
        else:
            present = file.is_file()
            state = "confirmed" if present else "unchecked" if may_be_generated else "error"
        add(phase, state, "スクリプトの存在を確認しました。" if present
            else "現在の参照元にスクリプトがありません。", path=str(script))

    def check_executable(phase, argv, cwd, *, may_be_generated=False):
        details = dict(command=safe_command(argv), cwd=str(cwd))
        executable = Path(argv[0])
        if executable.is_absolute() or "/" in argv[0] or (os.name == "nt" and "\\" in argv[0]):
            if not executable.is_absolute():
                executable = cwd / executable
            if executable.is_relative_to(planned):
                if not commit:
                    add(phase, "unchecked", "基準コミット不明のため実行ファイルを確認できません。", **details)
                    return
                path = executable.relative_to(planned)
                if ".." in path.parts:
                    add(phase, "unchecked", "worktree外への相対パスは計画だけでは確定できません。", **details)
                    return
                try:
                    if inspect_entry(commit, path):
                        add(phase, "confirmed", "基準コミットに実行ファイル候補があります（未実行）。", **details)
                        return
                except (ValueError, subprocess.TimeoutExpired):
                    add(phase, "unchecked", "基準コミットの照合が完了しませんでした。", **details)
                    return
                add(phase, "unchecked" if may_be_generated else "error", "基準コミットに実行ファイルがありません。", **details)
                return
            result = shutil.which(str(executable))
        else:
            result = shutil.which(argv[0])
        add(phase, "confirmed" if result else "error",
            "実行ファイルを解決しました（未実行）。" if result else "実行ファイルを解決できません。",
            **details)

    commit = None
    try:
        rev = _probe_git(root, "rev-parse", "--verify", "--end-of-options", config["base_ref"] + "^{commit}")
        value = rev.stdout.decode("ascii", "replace").strip()
        if rev.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value):
            commit = value
            add("git", "confirmed", "基準コミットを特定しました。")
        else:
            add("git", "error", "基準リビジョンを解決できません。base_refを確認してください。")
    except subprocess.TimeoutExpired:
        add("git", "unchecked", "基準リビジョンの取得がタイムアウトしました。")
    except OSError:
        add("git", "error", "Gitを起動できません。")
    for key in ("run_root", "worktree_root"):
        parent = Path(config[key])
        while not parent.exists() and parent != parent.parent:
            parent = parent.parent
        add("git", "confirmed" if parent.is_dir() and os.access(parent, os.W_OK) else "error",
            f"{key}: 保存先の既存の親ディレクトリを確認しました。", path=config[key])

    def script_phase(phase, extra=(), *, generated=False):
        commands = [*scripts.get(phase, []), *extra]
        if not commands:
            add(phase, "skipped", "設定されたスクリプトはありません。")
            return
        cwd = root if phase == "before_prepare" else planned
        for argv in commands:
            try:
                expanded = script_command_for_phase(argv, phase, cwd, build_project, target)
                command(phase, expanded, cwd, may_be_generated=generated)
                if phase == "before_migration" and argv[0] == "@copy-migration-draft":
                    source = Path(argv[1])
                    add(phase, "confirmed" if source.is_file() else "unchecked" if generated else "error",
                        "仮C#のコピー元を検査しました（配置は未実行）。", path=str(source), destination=argv[2])
            except (ValueError, OSError) as exc:
                add(phase, "error", str(exc))

    script_phase("before_prepare")
    bytes_left = _MAX_INPUT_BYTES
    for item in unit["inputs"]:
        path = (root / item["path"]).resolve()
        report_activity(f"入力を確認中: {path}", kind="phase")
        try:
            size = path.stat().st_size
            if not path.is_file():
                raise ValueError("通常ファイルではありません。")
            if size > bytes_left:
                add("prepare", "unchecked", "読み取り予算のため文字コードと範囲を未確認。", path=str(path), bytes=size)
                continue
            bytes_left -= size
            encoding = input_encoding(path, item["encoding"])
            chars = sum(len(line) for _, line in _selected_lines(path, item["start_line"], item["end_line"], encoding))
            add("prepare", "confirmed", "現在の作業コピーで入力と範囲を読み取れました。", path=str(path),
                encoding=encoding, characters=chars, bytes=size)
        except (OSError, ValueError) as exc:
            add("prepare", "unchecked" if changes_before_inputs else "error", str(exc), path=str(path))
    if changes_before_inputs:
        add("prepare", "unchecked", "before_prepare実行後の入力は未確認。")
    add("prepare", "unchecked", "カプセル分割と周辺資料の全走査は未実行。",
        max_context_chars=config["max_context_chars"], targets=[str(path) for path in target_paths(unit)])

    script_phase("after_prepare", generated=changes_before_inputs)
    script_phase("before_migration", unit.get("draft_import_commands", ()), generated=bool(scripts))
    junie = junie_cli.build_command(
        planned / "<agent-workspace>", "<カプセルと対象ファイルの指示>", config.get("junie_max_steps"),
        guidelines=planned / "<guidelines>", model=config.get("junie_model"), effort=config.get("junie_effort"),
    )
    command("migration", junie, planned)
    add("migration", "unchecked", "Junieの認証、モデル接続、資料の判断と編集は未実行。")
    script_phase("after_migration", generated=bool(scripts))
    script_phase("before_build", generated=bool(scripts))
    command("build", build_command_for_worktree(planned, build_project, require_existing=False), planned)
    if build_project and commit:
        try:
            present = inspect_entry(commit, Path(build_project))
            add("build", "confirmed" if present else "unchecked" if build_dependent else "error",
                "基準コミット上のビルド対象を照合しました。" if present else "基準コミットにビルド対象がありません。",
                path=build_project)
        except (ValueError, subprocess.TimeoutExpired):
            add("build", "unchecked", "ビルド対象の基準コミット照合が完了しませんでした。")
    elif build_project:
        add("build", "unchecked", "基準コミット不明のためビルド対象を未確認。")
    else:
        add("build", "unchecked", "ビルド対象はdotnetの実行時の既定。")
    add("build", "unchecked", "SDK選択とビルド結果は未確認。")
    script_phase("after_build", generated=True)
    if checker is None:
        for phase in ("before_checker", "checker", "after_checker"):
            add(phase, "skipped", "チェッカー未設定のため実行されません。")
    else:
        script_phase("before_checker", generated=True)
        try:
            command("checker", expand_command_paths(checker, planned, build_project, target), planned, may_be_generated=True)
        except (OSError, ValueError) as exc:
            add("checker", "error", str(exc))
        script_phase("after_checker", generated=True)
    add("verification", "unchecked", "修正・再検証・台帳更新は未実行。",
        max_repair_attempts=config.get("max_repair_attempts"))
    return {
        "kind": "dry-run", "config_path": config["config_path"], "unit_id": unit_id,
        "base_ref": config["base_ref"], "commit": commit, "worktree_template": str(planned),
        "duration_seconds": round(monotonic() - start, 3), "checks": entries,
        "configuration_ok": commit is not None and not any(item["state"] == "error" for item in entries),
    }
