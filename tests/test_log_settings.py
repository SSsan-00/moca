from pathlib import Path

from fixtures import HarnessCase
from migration_harness.config import load_config
from migration_harness.settings_form import build_settings_draft


class DetailLogSettingsTests(HarnessCase):
    def setUp(self):
        super().setUp()
        self.path = Path(self.config()["config_path"])
        self.content = self.path.read_text(encoding="utf-8")

    def test_older_toml_retains_default_presentation(self):
        draft = build_settings_draft(self.content)
        self.assertEqual(draft["form"]["junie"]["log_language"], "ja")
        self.assertEqual(draft["form"]["junie"]["log_tone"], "neutral")
        self.assertEqual(draft["content"], self.content)
        self.assertEqual(load_config(self.path)["junie_log_language"], "ja")
        self.assertEqual(load_config(self.path)["junie_log_tone"], "neutral")

    def test_form_presentation_round_trip(self):
        presentation = {"log_language": "en", "log_tone": "polite"}
        draft = build_settings_draft(self.content, {"junie": presentation})
        for key, value in presentation.items():
            self.assertEqual(draft["form"]["junie"][key], value)
        self.path.write_text(draft["content"], encoding="utf-8")
        config = load_config(self.path)
        self.assertEqual(config["junie_log_language"], "en")
        self.assertEqual(config["junie_log_tone"], "polite")

    def test_unknown_enum_cannot_be_loaded(self):
        for key in ("log_language", "log_tone"):
            with self.subTest(key=key):
                draft = build_settings_draft(self.content, {"junie": {key: "unknown"}})
                self.path.write_text(draft["content"], encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "詳細ログ"):
                    load_config(self.path)
