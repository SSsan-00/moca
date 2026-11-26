import subprocess
import unittest
from pathlib import Path

from tests.browser.ui_dependencies import node_executable


class RuleWorkspaceDemoTests(unittest.TestCase):
    def test_ephemeral_draft_and_confirmed_simulated_apply(self):
        root = Path(__file__).resolve().parents[2]
        subprocess.run([
            node_executable(self), str(Path(__file__).with_name("rule_workspace_demo.cjs")),
            str(root / "src/migration_harness/web/dashboard_rule_workspace_demo.js"),
        ], check=True, capture_output=True, text=True, timeout=10)
