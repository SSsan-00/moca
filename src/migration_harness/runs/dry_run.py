from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from time import monotonic

from ..junie import junie_cli
from ..context.capsules import _selected_lines, input_encoding
from ..command_paths import build_command_for_worktree, expand_command_paths, script_command_for_phase
from ..config import SCRIPT_PHASES, find_unit, target_paths
from ..errors import safe_command
from ..execution import check_cancelled, report_activity


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


def _python_script(argv, launcher):
    args = iter(argv[1:])
    first = next(args, None)
    if launcher == "py" and first and re.fullmatch(
        r"(?:-[23](?:\.\d+)?(?:-32|-64)?|-V:[A-Za-z0-9._/-]+)", first
    ):
        first = next(args, None)
    switches = {"-b", "-bb", "-B", "-d", "-E", "-I", "-O", "-OO", "-P", "-q", "-s", "-S", "-u", "-v", "-vv", "-x"}
    arg = first
    while arg is not None:
        if arg == "--":
            arg = next(args, None)
            return Path(arg) if arg and arg != "-" else None
        if arg == "-":
            return None
        if arg in {"-W", "-X"}:
            if not next(args, None):
                return None
        elif arg == "--check-hash-based-pycs":
            if next(args, None) not in {"default", "always", "never"}:
                return None
        elif arg in switches or arg.startswith(("-W", "-X")):
            pass
        elif arg.startswith("-"):
            return None
        else:
            return Path(arg) if arg else None
        arg = next(args, None)
    return None


def _launcher_script(argv):
    if len(argv) < 2:
        return None
    name = Path(argv[0]).stem.lower()
    if re.fullmatch(r"python(?:[23](?:\.\d+)?)?|py", name):
        return _python_script(argv, name)
    if name in {"pwsh", "powershell"}:
        for option, argument in zip(argv[1:], argv[2:]):
            if option.lower() == "-file":
                return Path(argument)
    return None


def _presence_state(present, uncertain):
    if present:
        return "confirmed"
    return "unchecked" if uncertain else "error"


class _DryRun:
    def __init__(self, config, unit):
        self.config = config
        self.unit = unit
        self.root = Path(config["project_root"])
        self.worktree = (Path(config["worktree_root"]) / "00000000-0000-0000-0000-000000000000").resolve()
        self.scripts = config.get("scripts", {})
        self.entries = []
        self.started = monotonic()
        self.commit = None

    def add(self, phase, state, detail, **fields):
        check_cancelled()
        self.entries.append(dict(phase=phase, state=state, detail=detail, **fields))
        report_activity(f"{phase}: {detail}", kind="phase")

    def has_entry(self, path):
        check_cancelled()
        remaining = 20 - (monotonic() - self.started)
        if remaining <= 0:
            raise subprocess.TimeoutExpired("git", 20)
        return _entry(self.root, self.commit, path, timeout=min(5, remaining))

    def check_repository(self):
        try:
            rev = _probe_git(self.root, "rev-parse", "--verify", "--end-of-options", self.config["base_ref"] + "^{commit}")
            value = rev.stdout.decode("ascii", "replace").strip()
            if rev.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value):
                self.commit = value
                self.add("git", "confirmed", "基準コミットを特定しました。")
            else:
                self.add("git", "error", "基準リビジョンを解決できません。base_refを確認してください。")
        except subprocess.TimeoutExpired:
            self.add("git", "unchecked", "基準リビジョンの取得がタイムアウトしました。")
        except OSError:
            self.add("git", "error", "Gitを起動できません。")
        for key in ("run_root", "worktree_root"):
            parent = Path(self.config[key])
            while not parent.exists() and parent != parent.parent:
                parent = parent.parent
            self.add("git", "confirmed" if parent.is_dir() and os.access(parent, os.W_OK) else "error",
                f"{key}: 保存先の既存の親ディレクトリを確認しました。", path=self.config[key])

    def check_command(self, phase, argv, cwd, *, may_be_generated=False):
        check_cancelled()
        self.check_executable(phase, argv, cwd, may_be_generated=may_be_generated)
        script = _launcher_script(argv)
        if script is None:
            return
        file = (cwd / script).resolve()
        if file.is_relative_to(self.worktree):
            try:
                present = self.commit is not None and self.has_entry(file.relative_to(self.worktree))
            except (ValueError, subprocess.TimeoutExpired):
                self.add(phase, "unchecked", "基準コミットのスクリプト照合が完了しませんでした。", path=str(script))
                return
            state = _presence_state(present, may_be_generated or not self.commit)
        else:
            present = file.is_file()
            state = _presence_state(present, may_be_generated)
        self.add(phase, state, "スクリプトの存在を確認しました。" if present
            else "現在の参照元にスクリプトがありません。", path=str(script))

    def check_executable(self, phase, argv, cwd, *, may_be_generated):
        details = dict(command=safe_command(argv), cwd=str(cwd))
        executable = Path(argv[0])
        if executable.is_absolute() or "/" in argv[0] or "\\" in argv[0]:
            if not executable.is_absolute():
                executable = cwd / executable
            if executable.is_relative_to(self.worktree):
                self.check_worktree_executable(phase, executable.relative_to(self.worktree), may_be_generated, details)
                return
            result = shutil.which(str(executable))
        else:
            result = shutil.which(argv[0])
        self.add(phase, "confirmed" if result else "error",
            "実行ファイルを解決しました（未実行）。" if result else "実行ファイルを解決できません。",
            **details)

    def check_worktree_executable(self, phase, path, may_be_generated, details):
        if not self.commit:
            self.add(phase, "unchecked", "基準コミット不明のため実行ファイルを確認できません。", **details)
            return
        if ".." in path.parts:
            self.add(phase, "unchecked", "worktree外への相対パスは計画だけでは確定できません。", **details)
            return
        try:
            if self.has_entry(path):
                self.add(phase, "confirmed", "基準コミットに実行ファイル候補があります（未実行）。", **details)
                return
        except (ValueError, subprocess.TimeoutExpired):
            self.add(phase, "unchecked", "基準コミットの照合が完了しませんでした。", **details)
            return
        self.add(phase, "unchecked" if may_be_generated else "error", "基準コミットに実行ファイルがありません。", **details)

    def check_scripts(self, phase, extra=(), *, generated=False):
        commands = [*self.scripts.get(phase, []), *extra]
        if not commands:
            self.add(phase, "skipped", "設定されたスクリプトはありません。")
            return
        cwd = self.root if phase == "before_prepare" else self.worktree
        for argv in commands:
            try:
                expanded = script_command_for_phase(argv, phase, cwd, self.config["build_project"], self.unit["target"])
                self.check_command(phase, expanded, cwd, may_be_generated=generated)
                if phase == "before_migration" and argv[0] == "@copy-migration-draft":
                    source = Path(argv[1])
                    self.add(phase, _presence_state(source.is_file(), generated),
                        "仮C#のコピー元を検査しました（配置は未実行）。", path=str(source), destination=argv[2])
            except (ValueError, OSError) as exc:
                self.add(phase, "error", str(exc))

    def check_inputs(self, changes_before_inputs):
        bytes_left = _MAX_INPUT_BYTES
        for item in self.unit["inputs"]:
            path = (self.root / item["path"]).resolve()
            report_activity(f"入力を確認中: {path}", kind="phase")
            try:
                size = path.stat().st_size
                if not path.is_file():
                    raise ValueError("通常ファイルではありません。")
                if size > bytes_left:
                    self.add("prepare", "unchecked", "読み取り予算のため文字コードと範囲を未確認。", path=str(path), bytes=size)
                    continue
                bytes_left -= size
                encoding = input_encoding(path, item["encoding"])
                chars = sum(len(line) for _, line in _selected_lines(path, item["start_line"], item["end_line"], encoding))
                self.add("prepare", "confirmed", "現在の作業コピーで入力と範囲を読み取れました。", path=str(path),
                    encoding=encoding, characters=chars, bytes=size)
            except (OSError, ValueError) as exc:
                self.add("prepare", "unchecked" if changes_before_inputs else "error", str(exc), path=str(path))
        if changes_before_inputs:
            self.add("prepare", "unchecked", "before_prepare実行後の入力は未確認。")
        self.add("prepare", "unchecked", "カプセル分割と周辺資料の全走査は未実行。",
            max_context_chars=self.config["max_context_chars"], targets=[str(path) for path in target_paths(self.unit)])

    def check_migration(self):
        junie = junie_cli.build_command(
            self.worktree / "<agent-workspace>", "<カプセルと対象ファイルの指示>", self.config.get("junie_max_steps"),
            guidelines=self.worktree / "<guidelines>", model=self.config.get("junie_model"), effort=self.config.get("junie_effort"),
        )
        self.check_command("migration", junie, self.worktree)
        self.add("migration", "unchecked", "Junieの認証、モデル接続、資料の判断と編集は未実行。")

    def check_build(self):
        build_project = self.config["build_project"]
        command = build_command_for_worktree(self.worktree, build_project, require_existing=False)
        self.check_command("build", command, self.worktree)
        if build_project and self.commit:
            try:
                present = self.has_entry(Path(build_project))
                may_be_generated = any(self.scripts.get(phase) for phase in SCRIPT_PHASES[:5])
                self.add("build", _presence_state(present, may_be_generated),
                    "基準コミット上のビルド対象を照合しました。" if present else "基準コミットにビルド対象がありません。",
                    path=build_project)
            except (ValueError, subprocess.TimeoutExpired):
                self.add("build", "unchecked", "ビルド対象の基準コミット照合が完了しませんでした。")
        elif build_project:
            self.add("build", "unchecked", "基準コミット不明のためビルド対象を未確認。")
        else:
            self.add("build", "unchecked", "ビルド対象はdotnetの実行時の既定。")
        self.add("build", "unchecked", "SDK選択とビルド結果は未確認。")

    def check_checker(self):
        checker = self.config["checker_command"]
        if checker is None:
            for phase in ("before_checker", "checker", "after_checker"):
                self.add(phase, "skipped", "チェッカー未設定のため実行されません。")
        else:
            self.check_scripts("before_checker", generated=True)
            try:
                expanded = expand_command_paths(checker, self.worktree, self.config["build_project"], self.unit["target"])
                self.check_command("checker", expanded, self.worktree, may_be_generated=True)
            except (OSError, ValueError) as exc:
                self.add("checker", "error", str(exc))
            self.check_scripts("after_checker", generated=True)


def plan_dry_run(config, unit_id):
    check_cancelled()
    unit = find_unit(config, unit_id)
    plan = _DryRun(config, unit)
    changes_before_inputs = bool(plan.scripts.get("before_prepare"))
    has_scripts = bool(plan.scripts)
    plan.check_repository()
    plan.check_scripts("before_prepare")
    plan.check_inputs(changes_before_inputs)
    plan.check_scripts("after_prepare", generated=changes_before_inputs)
    plan.check_scripts("before_migration", unit.get("draft_import_commands", ()), generated=has_scripts)
    plan.check_migration()
    plan.check_scripts("after_migration", generated=has_scripts)
    plan.check_scripts("before_build", generated=has_scripts)
    plan.check_build()
    plan.check_scripts("after_build", generated=True)
    plan.check_checker()
    plan.add("verification", "unchecked", "修正・再検証・台帳更新は未実行。",
        max_repair_attempts=config.get("max_repair_attempts"))
    return {
        "kind": "dry-run", "config_path": config["config_path"], "unit_id": unit_id,
        "base_ref": config["base_ref"], "commit": plan.commit, "worktree_template": str(plan.worktree),
        "duration_seconds": round(monotonic() - plan.started, 3), "checks": plan.entries,
        "configuration_ok": plan.commit is not None and not any(item["state"] == "error" for item in plan.entries),
    }
