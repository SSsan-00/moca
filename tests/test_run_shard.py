import unittest

from run_shard import REQUIRED_UI, required_ui_for, windows_shards


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
