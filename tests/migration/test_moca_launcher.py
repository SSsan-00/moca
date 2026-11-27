import io
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from migration_harness import cli, terminal_brand


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

    def test_banner_fits_or_uses_a_readable_fallback_without_escape_codes(self):
        for encoding, columns, height, literal in (
            ("utf-8", 80, 6, None), ("cp1252", 80, 3, "MoCA"), ("utf-8", 36, 3, "MoCA"),
            ("utf-8", 8, 1, "MoCA"), ("utf-8", 4, 0, None),
        ):
            with self.subTest(encoding=encoding, columns=columns):
                raw = io.BytesIO()
                with io.TextIOWrapper(raw, encoding=encoding) as terminal:
                    with (
                        patch.object(sys, "stderr", terminal),
                        patch.object(terminal, "fileno", return_value=2),
                        patch.object(cli, "_is_console", return_value=True),
                        patch.object(terminal_brand, "get_terminal_size", return_value=os.terminal_size((columns, 24))),
                        patch.dict(os.environ, {"NO_COLOR": "1"}),
                        patch.object(terminal_brand, "_console_state") as state,
                    ):
                        cli._show_moca_banner()
                    state.assert_not_called()
                    lines = raw.getvalue().decode(encoding).splitlines()
                self.assertEqual(len(lines), height)
                self.assertTrue(all(len(line) < columns for line in lines))
                self.assertNotIn("\033", "".join(lines))
                if literal:
                    self.assertEqual("".join(lines).count(literal), 1)
                elif height == 6:
                    self.assertIn("████", lines[3])
                    self.assertGreater(len(lines[3]), 65)

    def test_palette_matches_console_background_and_keeps_contrast(self):
        def color(r, g, b):
            return r | (g << 8) | (b << 16)
        palette = [0] * 16
        palette[7] = color(255, 255, 255)
        palette[6] = color(135, 77, 49)
        palette[14] = color(226, 164, 124)
        self.assertEqual(terminal_brand._accent_index(palette, 0x07), 14)
        self.assertEqual(terminal_brand._accent_index(palette, 0x70), 6)
        self.assertIsNone(terminal_brand._accent_index(palette, 0x4007))
        self.assertIsNone(terminal_brand._accent_index([0] * 16, 7))
        classic = [0] * 16
        classic[4] = color(197, 15, 31)
        classic[6] = color(193, 156, 0)
        classic[7] = color(204, 204, 204)
        classic[14] = color(249, 241, 165)
        classic[15] = color(242, 242, 242)
        self.assertEqual(terminal_brand._accent_index(classic, 0x07), 14)
        self.assertEqual(terminal_brand._accent_index(classic, 0xF0), 4)

    @unittest.skipUnless(os.name == "nt", "Windows console modes")
    def test_image_output_restores_console_mode_even_if_the_stream_disconnects(self):
        from ctypes import POINTER, cast
        from ctypes.wintypes import DWORD
        from migration_harness import terminal_image

        def get_mode(handle, result):
            cast(result, POINTER(DWORD))[0] = 3
            return True

        kernel = Mock(GetConsoleMode=get_mode)
        screen = Mock(cursor=(0, 0), attributes=7)
        args = ((kernel, 11, screen), (204, 204, 204), (12, 12, 12), (226, 164, 124))
        with patch.object(terminal_image, "_supports_sixel", return_value=True):
            output = io.StringIO()
            self.assertTrue(terminal_image.write_terminal_image(output, *args))
            self.assertTrue(output.getvalue().startswith('\x1bP0;1q"1;1;520;84'))
            self.assertTrue(output.getvalue().endswith("-\x1b\\\n"))
            broken = Mock()
            broken.write.side_effect = OSError("console disconnected")
            with self.assertRaises(OSError):
                terminal_image.write_terminal_image(broken, *args)
        self.assertEqual([call.args for call in kernel.SetConsoleMode.call_args_list],
                         [(11, 7), (11, 3), (11, 7), (11, 3)])

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
