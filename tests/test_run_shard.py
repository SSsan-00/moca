from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import os
import sys
import unittest
from unittest.mock import patch

from run_shard import (
    REQUIRED_ARCHITECTURE, REQUIRED_RUN_MODELS, REQUIRED_SAVED_STATE, REQUIRED_UI,
    REQUIRED_WINDOWS_RECOVERY, main, required_ui_for, windows_shards,
)


class _DiscoveredTest:
    def __init__(self, test_id):
        self.test_id = test_id

    def id(self):
        return self.test_id


class RegressionShardTests(unittest.TestCase):
    def test_each_browser_and_nonbrowser_test_has_one_shard(self):
        expected = {
            "conversation-editor": ["test_dashboard_editor.DashboardEditorTests.test_fixture"],
            "remaining": sorted(required_ui_for("remaining")),
            "chromium": ["test_settings_browser.SettingsBrowserTests.test_settings_round_trip_in_chromium"],
        }
        tests = [_DiscoveredTest(test_id) for ids in expected.values() for test_id in ids]
        self.assertEqual({name: [test.id() for test in group] for name, group in windows_shards(tests).items()}, expected)
        self.assertEqual(required_ui_for("chromium"), set(expected["chromium"]))
        self.assertEqual(required_ui_for("full"), REQUIRED_UI)
        self.assertEqual(required_ui_for("remaining") | required_ui_for("chromium"), REQUIRED_UI)
        with self.assertRaisesRegex(ValueError, "exactly once"):
            windows_shards(tests + tests[-1:])
        with self.assertRaisesRegex(ValueError, "nonempty"):
            windows_shards(tests[:-1])

    def test_architecture_is_required_only_on_shards_that_run_it(self):
        class RunnableTestCase(_DiscoveredTest, unittest.TestCase):
            def __init__(self, test_id, should_skip):
                unittest.TestCase.__init__(self)
                _DiscoveredTest.__init__(self, test_id)
                self.should_skip = should_skip

            def runTest(self):
                if self.should_skip:
                    self.skipTest("architecture fixture skipped")

        architecture_id = min(REQUIRED_ARCHITECTURE)
        all_ids = (
            REQUIRED_UI | REQUIRED_ARCHITECTURE | REQUIRED_RUN_MODELS | REQUIRED_SAVED_STATE | REQUIRED_WINDOWS_RECOVERY
            | {"test_dashboard_editor.DashboardEditorTests.test_fixture"}
        )
        for shard, skips_architecture, expected in (
            ("chromium", False, 0), ("remaining", False, 0), ("remaining", True, 1), ("full", True, 1),
        ):
            with self.subTest(shard=shard, skips_architecture=skips_architecture):
                suite = unittest.TestSuite(
                    RunnableTestCase(test_id, skips_architecture and test_id == architecture_id)
                    for test_id in sorted(all_ids)
                )
                output = StringIO()
                with (
                    patch.object(sys, "argv", ["run_shard.py", shard, "--require-ui"]), patch.dict(os.environ),
                    patch.object(unittest.TestLoader, "discover", return_value=suite),
                    redirect_stdout(output), redirect_stderr(output),
                ):
                    self.assertEqual(main(), expected, output.getvalue())
                if skips_architecture:
                    self.assertIn("Required architecture checks did not pass: " + architecture_id, output.getvalue())
