from pathlib import Path

from fixtures import HarnessCase
from migration_harness.config import load_config
from migration_harness.web.settings_form import build_settings_draft


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

    def test_custom_style_settings_round_trip_and_require_text(self):
        draft = build_settings_draft(self.content, {"junie": {"log_tone": "custom"}})
        self.path.write_text(draft["content"], encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "カスタム指示を入力"):
            load_config(self.path)
        changes = {"log_tone": "custom", "log_tone_instruction": "関西弁で、短く"}
        draft = build_settings_draft(self.content, {"junie": changes})
        self.path.write_text(draft["content"], encoding="utf-8")
        config = load_config(self.path)
        self.assertEqual(config["junie_log_tone"], "custom")
        self.assertEqual(config["junie_log_tone_instruction"], "関西弁で、短く")

    def test_progress_style_only_changes_human_facing_prompt(self):
        from unittest.mock import patch
        from migration_harness.junie import junie_cli

        args = ["fixture-junie", "task"], self.path, "token", ()
        style = junie_cli.dashboard_progress_style("en", "custom", "Use a friendly tone.")
        self.assertIn("英語", style)
        self.assertIn("Use a friendly tone.", style)
        self.assertIn("構造化データには文体設定を適用しない", style)
        with patch.object(junie_cli, "junie_dashboard_control", return_value=object()), \
                patch.object(junie_cli, "_run_session", return_value=0) as launch:
            junie_cli.run_interactive(*args, progress_style=style)
            self.assertEqual(launch.call_args.args[0][-1], "task\n\n" + style)
            junie_cli.run_interactive(*args, progress_style=style, accept_result=lambda _: None)
            self.assertEqual(launch.call_args.args[0][-1], "task")

    def test_default_tone_records_visible_reply_but_structured_result_does_not(self):
        from io import BytesIO
        from subprocess import TimeoutExpired
        from unittest.mock import Mock, patch
        from migration_harness.junie import junie_cli
        from migration_harness.junie.junie_thoughts import FILENAME, read_junie_thoughts

        root = self.path.parent.resolve()
        for identifier, accept_result in (("default", None), ("structured", lambda _: None)):
            with self.subTest(identifier=identifier):
                run_dir = root / identifier
                run_dir.mkdir()
                dashboard = Mock(junie_thoughts_path=run_dir / FILENAME,
                                 trusted_junie_workspaces={str(run_dir)})
                process = Mock(stderr=BytesIO(), wait=Mock(side_effect=TimeoutExpired("junie", 1)))
                acp = Mock(progress_sink=None, pending=None, steering_requests={})
                events = Mock(logs=run_dir, existing_logs=set(), read_new_events=Mock(return_value=False))

                def flush():
                    if acp.progress_sink is not None:
                        acp.progress_sink("標準の回答")

                acp.flush_progress.side_effect = flush
                with (patch.object(junie_cli, "junie_dashboard_control", return_value=dashboard),
                      patch.object(junie_cli, "_launch_session_process", return_value=process),
                      patch.object(junie_cli, "_AcpSession", return_value=acp),
                      patch.object(junie_cli, "_JunieSessionEvents", return_value=events),
                      patch.object(junie_cli, "_close_junie", return_value=0)):
                    self.assertEqual(junie_cli.run_interactive(
                        ["junie", "--prompt", "task"], run_dir / "complete", "token", (),
                        accept_result=accept_result,
                    ), 0)
                rows = read_junie_thoughts(root, identifier, 0)["entries"]
                self.assertEqual([row["thought"] for row in rows], ["標準の回答"] if accept_result is None else [])

    def test_acp_visible_progress_is_recorded_without_thought_chunks(self):
        from migration_harness.junie.junie_cli import _AcpSession
        from migration_harness.junie.junie_thoughts import FILENAME, JunieThoughts, read_junie_thoughts

        run_dir = self.path.parent / "fixture"
        run_dir.mkdir()
        log = JunieThoughts.__new__(JunieThoughts)
        log.target = run_dir / FILENAME
        log.token = "completion-secret"
        acp = _AcpSession.__new__(_AcpSession)
        acp.session_id = "current"
        acp.progress_sink = log.append_progress
        acp.progress_text = ""
        acp.tool_calls = {}

        def update(session, kind, **fields):
            acp._receive_message({"method": "session/update", "params": {
                "sessionId": session, "update": {"sessionUpdate": kind, **fields},
            }})

        update("another", "agent_message_chunk", content={"type": "text", "text": "other run"})
        update("current", "agent_thought_chunk", content={"type": "text", "text": "not a progress message"})
        update("current", "agent_message_chunk", content={"type": "text", "text": "ええ感じで"})
        update("current", "agent_message_chunk", content={"type": "text", "text": "進んでます completion-secret"})
        update("current", "tool_call", toolCallId="one")
        acp.flush_progress()
        entries = read_junie_thoughts(self.path.parent, "fixture", 0)["entries"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["title"], "Junie")
        self.assertEqual(entries[0]["thought"], "ええ感じで進んでます [REDACTED]")
