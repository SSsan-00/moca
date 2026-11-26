import io
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from migration_harness import cli


class MoCALauncherTests(unittest.TestCase):
    def test_launcher_shows_banner_before_reading_config_and_still_opens_browser(self):
        events = Mock()
        events.load.return_value = {"fixture": True}
        with (
            patch.dict(os.environ, {"MOCA_LAUNCHER_BANNER": "1"}),
            patch.object(sys, "argv", ["harness", "dashboard", "--config", "settings.toml", "--open-browser"]),
            patch.object(cli, "_show_moca_banner", events.banner),
            patch.object(cli, "load_config", events.load),
            patch("migration_harness.web.dashboard.run_dashboard", events.dashboard),
        ):
            cli.main()
            self.assertNotIn("MOCA_LAUNCHER_BANNER", os.environ)
        self.assertEqual([item[0] for item in events.mock_calls], ["banner", "load", "dashboard"])
        events.load.assert_called_once_with(Path("settings.toml"))
        events.dashboard.assert_called_once_with({"fixture": True}, open_browser=True)

    def test_direct_dashboard_cli_keeps_its_output_unchanged(self):
        with (
            patch.dict(os.environ),
            patch.object(sys, "argv", ["harness", "dashboard", "--config", "settings.toml", "--open-browser"]),
            patch.object(cli, "_show_moca_banner") as banner,
            patch.object(cli, "load_config", return_value={}),
            patch("migration_harness.web.dashboard.run_dashboard") as dashboard,
        ):
            os.environ.pop("MOCA_LAUNCHER_BANNER", None)
            cli.main()
        banner.assert_not_called()
        dashboard.assert_called_once_with({}, open_browser=True)

    def test_banner_is_monochrome_and_falls_back_to_ascii(self):
        for encoding, height, center, arrow in (("utf-8", 6, 2, "████▄"), ("cp1252", 3, 1, "|>")):
            with self.subTest(encoding=encoding):
                raw = io.BytesIO()
                with io.TextIOWrapper(raw, encoding=encoding) as terminal:
                    with patch.object(sys, "stderr", terminal), patch.object(cli, "_is_console", return_value=True):
                        cli._show_moca_banner()
                    lines = raw.getvalue().decode(encoding).splitlines()
                self.assertEqual(len(lines), height)
                self.assertTrue(lines[center].endswith(f"{arrow}  MoCA"))
                self.assertEqual(sum("MoCA" in line for line in lines), 1)
                self.assertNotIn("\033", "".join(lines))

    def test_redirection_of_any_stream_suppresses_the_banner(self):
        for stream in ("stdin", "stdout", "stderr"):
            with self.subTest(stream=stream):
                sink, redirected = io.StringIO(), io.StringIO()
                with (
                    patch.object(sys, "stderr", sink),
                    patch.object(sys, stream, redirected),
                    patch.object(cli, "_is_console", side_effect=lambda channel: channel is not redirected),
                ):
                    cli._show_moca_banner()
                self.assertEqual(sink.getvalue(), "")
                self.assertEqual(redirected.getvalue(), "")

    def test_nul_is_not_a_console(self):
        with open(os.devnull, "w") as sink:
            self.assertFalse(cli._is_console(sink))
