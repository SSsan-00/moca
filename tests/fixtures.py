import subprocess
import tempfile
import unittest
from pathlib import Path

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
