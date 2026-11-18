"""ローカルの任意依存と CI の必須依存を区別する。"""

import os
import shutil


def require_ui_dependency(test, value, reason):
    """利用できる依存を返し、欠けていれば実行環境に応じて報告する。"""
    if value:
        return value
    if os.environ.get("HARNESS_REQUIRE_UI") == "1":
        test.fail(reason)
    test.skipTest(reason)


def node_executable(test):
    """配布する JavaScript の実行に使う Node.js を探す。"""
    return require_ui_dependency(test, shutil.which("node"), "Node.js is required for the UI tests")
