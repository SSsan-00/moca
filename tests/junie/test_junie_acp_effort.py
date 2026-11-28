import json
import unittest
from pathlib import Path
from queue import Queue
from unittest.mock import Mock, patch

from migration_harness.junie import acp_session, junie_cli
from migration_harness.rules.rule_chat import RuleChat


def make_session(kind, model="gpt-6-luna", effort="medium"):
    session = kind.__new__(kind)
    command = junie_cli.build_command(Path.cwd(), "synthetic prompt", None, model=model, effort=effort)
    with patch.object(acp_session, "Thread"):
        acp_session.AcpSession.__init__(session, Mock(), command, Path.cwd(), Mock(), Path("done"), "token", None)
    session.request = Mock()
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
    def test_alias_accepts_only_the_verified_release_and_model(self):
        for version, current, matches in (
            ("26.11.2 (4100.11)", "v1:11:byok:openai:gpt-6.1-sol", True),
            ("26.11.2 (4100.11)", "v1:11:byok:openai:gpt-6.1-terra", False),
            ("26.11.2 (4100.11)", "v1:3:byok:openai:gpt-6.1-sol", False),
            ("unknown", "gpt-6.1-sol", False),
            ("unknown", "gpt", True),
        ):
            with self.subTest(version=version, current=current):
                session = make_session(RuleChat, model="gpt")
                session._receive_message({"id": 1, "result": {"protocolVersion": 1, "agentInfo": {"version": version}}})
                new = {"id": 2, "result": {"sessionId": "session", "configOptions": [
                    {"category": "model", "id": "model", "currentValue": current}, *config("medium"),
                ]}}
                if matches:
                    session._receive_message(new)
                    self.assertTrue(session.session_ready)
                else:
                    with self.assertRaises(junie_cli.JunieSessionError):
                        session._receive_message(new)
                    self.assertFalse(session.session_ready)
                self.assertEqual(session.request.call_count, 1)

    def test_ambiguous_provider_and_unconfirmed_model_change_stop_before_prompt(self):
        values = ["v1:1:a:target", "v1:1:b:target"]
        for offered in (values, values[:1]):
            with self.subTest(offered=offered):
                session = make_session(acp_session.AcpSession, model="target", effort=None)
                selector = {"id": "model", "currentValue": "other", "options": [{"value": v} for v in offered]}
                with self.assertRaises(junie_cli.JunieSessionError):
                    session._receive_message({"id": 2, "result": {"sessionId": "session", "configOptions": [selector]}})
                    request_id, method = session.request.call_args.args
                    self.assertEqual(method, "session/set_config_option")
                    session._receive_message({"id": request_id, "result": {"configOptions": [selector]}})
                self.assertEqual(session.request.call_count, int(len(offered) == 1))
                self.assertFalse(session.session_ready)

    def test_selects_advertised_model_before_using_its_effort_selector(self):
        target = "v1:11:byok:openai:gpt-6-luna"
        selector = {"id": "provider_model", "category": "model", "currentValue": "other",
                    "options": [{"group": "byok", "options": [{"value": target, "name": "Luna"}]}]}
        for kind in (acp_session.AcpSession, RuleChat):
            with self.subTest(kind=kind):
                session = make_session(kind)
                session._receive_message({"id": 2, "result": {"sessionId": "session",
                                          "configOptions": [selector, *config("medium")]}})
                request_id, method = session.request.call_args.args
                self.assertEqual(method, "session/set_config_option")
                self.assertEqual(session.request.call_args.kwargs, {
                    "sessionId": "session", "configId": "provider_model", "value": target,
                })
                self.assertFalse(session.session_ready)
                session._receive_message({"id": request_id, "result": {"configOptions": [
                    dict(selector, currentValue=target), *config("low", config_id="new_effort"),
                ]}})
                effort_id, method = session.request.call_args.args
                self.assertEqual((method, session.request.call_args.kwargs["configId"]),
                                 ("session/set_config_option", "new_effort"))
                self.assertFalse(session.session_ready)
                session._receive_message({"id": effort_id, "result": {"configOptions": [
                    dict(selector, currentValue=target), *config("medium", config_id="new_effort"),
                ]}})
                self.assertTrue(session.session_ready)

    def test_reported_other_model_cannot_be_used_with_the_requested_effort(self):
        for metadata in (
            {"models": {"currentModelId": "other"}},
            {"configOptions": [{"id": "model", "currentValue": "v1:3:abc:custom:gpt-6-luna"}]},
        ):
            session = make_session(RuleChat, effort=None)
            with self.subTest(metadata=metadata), self.assertRaisesRegex(junie_cli.JunieSessionError, "gpt-6-luna"):
                session._receive_message({"id": 2, "result": {"sessionId": "session", **metadata}})
            self.assertFalse(session.session_ready)
            session.request.assert_not_called()

    def test_effort_confirmation_cannot_change_the_model_or_ack_another_selector(self):
        for reply in (
            [{"id": "model", "currentValue": "other"}, *config("medium")],
            config("medium", config_id="another_selector"),
        ):
            with self.subTest(reply=reply):
                session = make_session(acp_session.AcpSession)
                respond_new(session, config("low"))
                for attempt in range(2):
                    request_id = session.request.call_args.args[0]
                    try:
                        session._receive_message({"id": request_id, "result": {"configOptions": reply}})
                    except junie_cli.JunieSessionError:
                        break
                    self.assertFalse(session.session_ready)
                else:
                    self.fail("An unconfirmed selection must stop")
                self.assertFalse(session.session_ready)

    def test_ready_chat_stops_if_acp_reports_a_model_or_effort_switch(self):
        for option in ({"id": "model", "currentValue": "other"}, *config("low")):
            session = make_session(RuleChat)
            respond_new(session, config("medium"))
            update = {"method": "session/update", "params": {"sessionId": "session",
                      "update": {"sessionUpdate": "config_option_update", "configOptions": [option]}}}
            session.messages.put(json.dumps(update).encode())
            with self.subTest(option=option), self.assertRaises(junie_cli.JunieSessionError):
                session.begin("next turn")
            session.request.assert_not_called()

    def test_slow_provider_startup_has_its_own_bounded_deadline(self):
        clock = Mock(return_value=100.0)
        with patch.object(acp_session.time, "monotonic", clock), patch.object(acp_session, "Thread"):
            session = acp_session.AcpSession(
                Mock(), ["junie", "--prompt", "synthetic prompt"], Path.cwd(), Mock(),
                Path("completion.json"), "run-token", None,
            )
            session.request = Mock()
            clock.return_value = 131.0
            self.assertFalse(session.advance())
            self.assertFalse(session.session_ready)
            session.request.assert_not_called()
            clock.return_value = 159.0
            session.messages.put(b'{"id":2,"result":{"sessionId":"ready"}}')
            self.assertFalse(session.advance())
            self.assertTrue(session.session_ready)
            session.request.assert_called_once()
            self.assertEqual(session.request.call_args.args, (3, "session/prompt"))

            clock.return_value = 100.0
            stalled = acp_session.AcpSession(Mock(), ["junie"], Path.cwd(), Mock(), Path("completion.json"),
                                           "run-token", None)
            clock.return_value = 189.0
            self.assertFalse(stalled.advance())
            self.assertFalse(stalled.session_ready)
            clock.return_value = 190.0
            with self.assertRaisesRegex(junie_cli.JunieSessionError, "ACP接続がタイムアウト"):
                stalled.advance()

    def test_migration_waits_for_effort_ack_before_prompt(self):
        session = make_session(acp_session.AcpSession)
        respond_new(session, config("low"))
        request_id = acp_session.ACP_SET_EFFORT_REQUEST
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
        session._receive_message({"id": acp_session.ACP_SET_EFFORT_REQUEST,
                                  "result": {"configOptions": config("medium")}})
        self.assertTrue(session.session_ready)
        self.assertEqual(session.request.call_count, 1)
        session.begin("test")
        self.assertEqual(session.request.call_args.args, (10, "session/prompt"))

    def test_already_matching_or_unset_does_not_change_configuration(self):
        for effort, options in (("medium", config("medium")), (None, config("low"))):
            with self.subTest(effort=effort):
                session = make_session(acp_session.AcpSession, effort=effort)
                respond_new(session, options)
                self.assertTrue(session.session_ready)
                self.assertEqual(session.request.call_count, 1)
                self.assertEqual(session.request.call_args.args, (3, "session/prompt"))

    def test_missing_selector_stops_before_prompt_without_guessing_default(self):
        session = make_session(acp_session.AcpSession)
        with self.assertRaises(junie_cli.JunieSessionError):
            respond_new(session, [])
        session.request.assert_not_called()
        self.assertFalse(session.session_ready)

    def test_retry_applies_same_level_and_ignores_late_first_reply(self):
        for first in ({"result": {"configOptions": config("low")}},
                      {"error": {"code": -32603}}):
            with self.subTest(first=first):
                session = make_session(RuleChat)
                respond_new(session, config("low"))
                start = acp_session.ACP_SET_EFFORT_REQUEST
                retry = acp_session.ACP_RETRY_EFFORT_REQUEST
                session._receive_message({"id": start, **first})
                session.request.assert_called_with(retry, "session/set_config_option",
                                                   sessionId="session", configId="reasoning", value="medium")
                confirmation = {"result": {"configOptions": config("medium")}}
                session._receive_message({"id": start, **confirmation})
                self.assertFalse(session.session_ready)
                with self.assertRaises(RuntimeError):
                    session.begin("test")
                session._receive_message({"id": retry, **confirmation})
                session.begin("test")
                self.assertEqual(session.request.call_args.args, (10, "session/prompt"))
                self.assertEqual(session.request.call_count, 3)

    def test_wait_timeout_retries_once_and_keeps_migration_unsent(self):
        session = make_session(acp_session.AcpSession)
        session._check_writes = Mock()
        session.pending = None
        session.prompt_finished = False
        session.steering_new_turn = False
        session.messages = Queue()
        respond_new(session, config("low"))
        session.startup_deadline = 0
        self.assertFalse(session.advance())
        self.assertEqual(session.request.call_args.args,
                         (acp_session.ACP_RETRY_EFFORT_REQUEST, "session/set_config_option"))
        session.startup_deadline = 0
        with self.assertRaises(junie_cli.JunieSessionError):
            session.advance()
        self.assertFalse(session.session_ready)
        self.assertEqual(session.request.call_count, 2)

    def test_unsupported_request_stops_without_resending(self):
        for code in (-32601, -32602):
            with self.subTest(code=code):
                session = make_session(acp_session.AcpSession)
                respond_new(session, config("low"))
                with self.assertRaises(junie_cli.JunieSessionError):
                    session._receive_message({"id": acp_session.ACP_SET_EFFORT_REQUEST, "error": {"code": code}})
                self.assertFalse(session.session_ready)
                self.assertEqual(session.request.call_count, 1)

    def test_unapplied_retry_never_sends_a_prompt(self):
        for reply in ({"result": {"configOptions": config("low")}}, {"result": {}}, {"result": None}):
            with self.subTest(reply=reply):
                session = make_session(acp_session.AcpSession)
                respond_new(session, config("low"))
                session._receive_message({"id": acp_session.ACP_SET_EFFORT_REQUEST, **reply})
                with self.assertRaises(junie_cli.JunieSessionError):
                    session._receive_message({"id": acp_session.ACP_RETRY_EFFORT_REQUEST, **reply})
                self.assertFalse(session.session_ready)
                self.assertEqual(session.request.call_count, 2)

    def test_only_lite_none_can_run_without_a_selector(self):
        for effort, passed in (("none", True), ("low", False)):
            with self.subTest(effort=effort):
                session = make_session(acp_session.AcpSession, model="junie-lite", effort=effort)
                if passed:
                    session._receive_message({"id": 2, "result": {"sessionId": "session",
                        "configOptions": [{"id": "model", "currentValue": "junie-lite"}]}})
                    self.assertTrue(session.session_ready)
                    self.assertEqual(session.request.call_args.args, (3, "session/prompt"))
                else:
                    with self.assertRaises(junie_cli.JunieSessionError):
                        respond_new(session, [])
                    session.request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
