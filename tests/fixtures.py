import io
import json
import subprocess
import tempfile
import unittest
import uuid
from contextlib import redirect_stdout
from unittest.mock import patch
from pathlib import Path

from migration_harness import workflow
from migration_harness.config import load_config


class HarnessCase(unittest.TestCase):
    """実行データを隔離できるGitフィクスチャを各テストに提供する。"""

    def setUp(self):
        """ユーザーの署名や改行設定に依存しないGitリポジトリを用意する。"""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.project = self.root / "app"
        self.project.mkdir()
        self.git("init", "-q")
        self.git("config", "core.autocrlf", "false")
        self.git("config", "user.name", "Harness test")
        self.git("config", "user.email", "harness@example.invalid")
        self.git("config", "commit.gpgsign", "false")

    def git(self, *args):
        """すべてのGit操作をサンプルリポジトリに限定する。"""
        return subprocess.run(
            ["git", "-C", str(self.project), *args], check=True, capture_output=True,
        )

    def commit_project(self):
        """worktreeを作れる基準リビジョンにサンプルを固定する。"""
        self.git("add", ".")
        self.git("commit", "-qm", "Create fixture")

    def config(self, target='target = "Page.cs"'):
        """サンプルに閉じた最小の動作設定を、実際の設定パーサーに通す。"""
        path = self.root / "test.toml"
        path.write_text(
            f"""[project]
root = "app"
worktree_root = "trees"
run_root = "runs"
max_context_chars = 1000

[[units]]
id = "sample"
{target}

[[units.inputs]]
role = "php"
path = "source.php"
encoding = "utf-8"
start_line = 1
end_line = 1
""",
            encoding="utf-8",
        )
        return load_config(path)


class SavedRunFixture(unittest.TestCase):
    """外部ツールを起動せずに再開やHTTPの操作を検証できるRunを用意する。"""

    def setUp(self):
        """チェッカーが判断待ちになった直後のRunと移行先を作る。"""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        self.run_id = str(uuid.uuid4())
        self.run_path = root / "runs" / self.run_id
        self.worktree = root / "worktrees" / self.run_id
        self.worktree.mkdir(parents=True)
        self.target = self.worktree / "Example.cs"
        self.target.write_text("class Original {}\n", encoding="utf-8")
        project = root / "project"
        project.mkdir()
        (project / "example.php").write_text("<?php echo 'example';\n", encoding="utf-8")
        (project / "viewer.html").write_text(
            '<!doctype html><body><input id="workspace-import-file"></body>', encoding="utf-8",
        )
        self.config = {
            "run_root": str(self.run_path.parent),
            "worktree_root": str(self.worktree.parent),
            "project_root": str(project),
            "units": [{"id": "example", "target": self.target.name}],
            "build_project": None,
            "checker_command": ["fixture-checker"],
            "max_repair_attempts": 0,
            "junie_max_steps": None,
            "diff_viewer_html": "viewer.html",
            "diff_viewer_anchor_reference": [root / "reference.json"],
        }
        capsule = self.run_path / "capsule"
        capsule.mkdir(parents=True)
        context = capsule / "context.txt"
        context.write_text("example", encoding="utf-8")
        (capsule / "manifest.json").write_text(json.dumps({
            "parts": [{
                "index": 1, "path": str(context), "chars": 7,
                "sources": [{"path": "example.php", "start_line": 1, "end_line": 1, "encoding": "utf-8"}],
            }],
        }), encoding="utf-8")
        self.record_path = self.run_path / "run.json"
        self.build_log = self.run_path / "build.log"
        self.checker_log = self.run_path / "checker.log"
        self.build_log.write_bytes(b"old successful build\n")
        self.checker_log.write_bytes(b"PREVIOUS Example.cs:1 warning: previous finding\n")
        self.save_record({
            "run_id": self.run_id, "unit_id": "example", "target": self.target.name,
            "project_root": str(project), "worktree_path": str(self.worktree),
            "status": "checker_needs_review", "completed_capsules": 1, "context_capsules": 1,
            "max_context_chars": 1000, "source_boundary_review_required": False,
            "build_exit_code": 0, "build_log_path": str(self.build_log),
            "checker_exit_code": 1, "checker_log_path": str(self.checker_log),
        })

    def save_record(self, record):
        """シナリオの開始状態をRun記録に書き込む。"""
        self.record_path.write_text(json.dumps(record), encoding="utf-8")

    def record(self):
        """見かけの戻り値とは別に、実際に保存されたRun記録を取得する。"""
        return json.loads(self.record_path.read_text(encoding="utf-8"))

    def mock_commands(self, *responses):
        """外部コマンドの順序とログを制御する。"""
        expected = iter(responses)
        calls = []
        run_process = subprocess.run

        def run(command, *, label, cancellable=True, **kwargs):
            """指定した順序と実行場所で呼ばれたことを確認し、結果を返す。"""
            if command[0] == "git":
                return run_process(command, **kwargs)
            calls.append(command)
            name, returncode, log = next(expected)
            self.assertEqual(command, ["dotnet", "build"] if name == "dotnet" else self.config["checker_command"])
            self.assertEqual(Path(kwargs["cwd"]), self.worktree)
            self.assertEqual(kwargs["stderr"], subprocess.STDOUT)
            self.assertFalse(kwargs["check"])
            kwargs["stdout"].write(log)
            return subprocess.CompletedProcess(command, returncode)

        return patch.object(workflow, "run_process", side_effect=run), calls

    def resume(self, *responses):
        """外部ツールを差し替えて通常の再開経路を実行する。"""
        mock, calls = self.mock_commands(*responses)
        with mock, redirect_stdout(io.StringIO()):
            result = workflow.resume_with_junie(self.config, self.run_id)
        self.assertEqual(len(calls), len(responses))
        return result

    def review(self, finding_id=None, reason=None):
        """同じRunの一覧取得と指摘への承認を呼び分ける。"""
        return workflow.review_checker(self.config, self.run_id, finding_id, reason)
