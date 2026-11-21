import unittest
from pathlib import Path
from unittest.mock import Mock

from migration_harness import junie_cli
from migration_harness.rule_chat import RuleChat


def make_session(kind, model="gpt-6-luna", effort="medium"):
    session = kind.__new__(kind)
    session.command = junie_cli.build_command(Path.cwd(), "synthetic prompt", None, model=model, effort=effort)
    session.request = Mock()
    session.session_id = None
    session.session_ready = False
    session.selecting_lite = False
    session.pending_effort = None
    session.steering_waiting = None
    session.turn_id = None
    session.next_turn = 10
    return session


def config(value, *, config_id="reasoning"):
    return [{"id": config_id, "category": "thought_level", "currentValue": value}]


def respond_new(session, options):
    session._receive_message({"id": 2, "result": {
        "sessionId": "session",
        "configOptions": [{"id": "model", "currentValue": "v1:11:byok:openai:gpt-6-luna"}, *options],
    }})


class JunieAcpEffortTests(unittest.TestCase):
    def test_migration_waits_for_effort_ack_before_prompt(self):
        session = make_session(junie_cli._AcpSession)
        respond_new(session, config("low"))
        request_id = junie_cli._ACP_SET_EFFORT_REQUEST
        session.request.assert_called_once_with(request_id, "session/set_config_option",
                                                sessionId="session", configId="reasoning", value="medium")
        self.assertFalse(session.session_ready)
        session._receive_message({"id": request_id, "result": {"configOptions": config("medium")}})
        self.assertTrue(session.session_ready)
        self.assertEqual(session.request.call_args.args, (3, "session/prompt"))

    def test_chat_cannot_send_until_effort_is_confirmed(self):
        session = make_session(RuleChat)
        respond_new(session, config("low"))
        with self.assertRaises(RuntimeError):
            session.begin("test")
        session._receive_message({"id": junie_cli._ACP_SET_EFFORT_REQUEST,
                                  "result": {"configOptions": config("medium")}})
        self.assertTrue(session.session_ready)
        self.assertEqual(session.request.call_count, 1)
        session.begin("test")
        self.assertEqual(session.request.call_args.args, (10, "session/prompt"))

    def test_already_matching_or_unset_does_not_change_configuration(self):
        for effort, options in (("medium", config("medium")), (None, config("low"))):
            with self.subTest(effort=effort):
                session = make_session(junie_cli._AcpSession, effort=effort)
                respond_new(session, options)
                self.assertTrue(session.session_ready)
                self.assertEqual(session.request.call_count, 1)
                self.assertEqual(session.request.call_args.args, (3, "session/prompt"))

    def test_missing_selector_stops_before_prompt_without_guessing_default(self):
        session = make_session(junie_cli._AcpSession)
        with self.assertRaises(junie_cli.JunieSessionError):
            respond_new(session, [])
        session.request.assert_not_called()
        self.assertFalse(session.session_ready)

    def test_rejected_or_unapplied_level_never_sends_a_prompt(self):
        for reply in ({"error": {"code": -32601}}, {"result": {"configOptions": config("low")}},
                      {"result": {}}, {"result": None}):
            with self.subTest(reply=reply):
                session = make_session(junie_cli._AcpSession)
                respond_new(session, config("low"))
                with self.assertRaises(junie_cli.JunieSessionError):
                    session._receive_message({"id": junie_cli._ACP_SET_EFFORT_REQUEST, **reply})
                self.assertFalse(session.session_ready)
                self.assertEqual(session.request.call_count, 1)

    def test_only_lite_none_can_run_without_a_selector(self):
        for effort, passed in (("none", True), ("low", False)):
            with self.subTest(effort=effort):
                session = make_session(junie_cli._AcpSession, model="junie-lite", effort=effort)
                if passed:
                    respond_new(session, [])
                    self.assertTrue(session.session_ready)
                    self.assertEqual(session.request.call_args.args, (3, "session/prompt"))
                else:
                    with self.assertRaises(junie_cli.JunieSessionError):
                        respond_new(session, [])
                    session.request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
