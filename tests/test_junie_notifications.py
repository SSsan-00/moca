import io
import json
import unittest
from contextlib import redirect_stdout
from threading import Thread
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen
from xml.etree.ElementTree import fromstring

from migration_harness.dashboard import Dashboard
from migration_harness.junie_notifications import (
    JunieNotifications, RequestKey, WindowsToast, answer_pending, notification_settings, pending_requests, toast_xml,
)


class JunieRequestTests(unittest.TestCase):
    def setUp(self):
        self.dashboard = Dashboard()
        self.views = {}
        self.manager = SimpleNamespace(views=self.views, get=lambda run_id=None: self.views.get(run_id, self.dashboard))
        for name in ("run-a", "run-b", "run-c"):
            view = self.views[name] = Dashboard()
            view.condition = self.dashboard.condition
            view.publish({"run_id": name, "unit_id": "unit-" + name, "ready": True})
            view.begin_operation("operation-" + name)
        self.options = [{"id": "yes", "label": "承認"}]

    def ask(self, name):
        return self.views[name].show_junie_request({"title": "確認", "options": self.options})

    def pending(self):
        return pending_requests(self.dashboard, self.manager)

    @patch("time.monotonic_ns", return_value=42)
    def test_queue_order_and_answer_always_tied_to_run_operation_and_request(self, _clock):
        future_b = self.ask("run-b")
        future_a = self.ask("run-a")
        self.assertEqual([key.run_id for key in self.pending()], ["run-b", "run-a"])
        key = next(iter(self.pending()))
        answer = {**vars(key), "option_id": "yes"}
        for bad in ({"operation_id": "old"}, {"run_id": "run-a"}, {"request_id": "old"}, {"option_id": "missing"}):
            self.assertFalse(answer_pending(self.dashboard, self.manager, {**answer, **bad}))
        self.assertTrue(answer_pending(self.dashboard, self.manager, answer))
        self.assertEqual(future_b.result(), "yes")
        self.assertFalse(future_a.done())
        self.assertFalse(answer_pending(self.dashboard, self.manager, answer))
        self.assertEqual([key.run_id for key in self.pending()], ["run-a"])
        self.assertEqual(self.views["run-c"].state["operation_id"], "operation-run-c")

    def test_cancel_and_new_request_do_not_inherit_stale_answers(self):
        self.ask("run-b")
        old = next(iter(self.pending()))
        self.assertTrue(self.views["run-b"].request_stop({"operation_id": old.operation_id}))
        self.assertEqual(self.pending(), {})
        self.views["run-b"].begin_operation("operation-new")
        new = self.ask("run-b")
        self.assertFalse(answer_pending(self.dashboard, self.manager, {**vars(old), "option_id": "yes"}))
        self.assertFalse(new.done())

    def test_finished_views_are_not_visible(self):
        self.ask("run-b")
        self.views["run-b"].publish({**self.views["run-b"].state, "ready": True})
        self.assertEqual(self.pending(), {})

    def test_http_queue_is_workspace_wide_and_validates_same_origin_and_exact_ids(self):
        b = self.ask("run-b")
        a = self.ask("run-a")
        self.dashboard.manager = self.manager
        log = io.StringIO()
        with redirect_stdout(log):
            server = self.dashboard.server({"junie_notifications": notification_settings({"enabled": False})}, b"", b"")
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = log.getvalue().splitlines()[0].removeprefix("Progress dashboard: ")
            endpoint = url + "junie-requests"
            origin = urlsplit(url)
            headers = {"X-Run-ID": "run-a"}
            with urlopen(Request(endpoint, headers=headers), timeout=3) as response:
                waiting = json.load(response)
            self.assertEqual([item["run_id"] for item in waiting], ["run-b", "run-a"])
            answer = {**vars(next(iter(self.pending()))), "option_id": "yes"}
            headers["Content-Type"] = "application/json"
            headers["Origin"] = "https://unrelated.test"
            with self.assertRaises(HTTPError) as denied:
                urlopen(Request(endpoint, json.dumps(answer).encode(), headers), timeout=3)
            self.assertEqual(denied.exception.code, 403)
            denied.exception.close()
            headers["Origin"] = f"{origin.scheme}://{origin.netloc}"
            with urlopen(Request(endpoint, json.dumps(answer).encode(), headers), timeout=3) as response:
                self.assertEqual(response.status, 202)
            self.assertEqual(b.result(timeout=1), "yes")
            self.assertFalse(a.done())
            with urlopen(Request(endpoint, headers={"X-Run-ID": "run-c"}), timeout=3) as response:
                self.assertEqual([item["run_id"] for item in json.load(response)], ["run-a"])
            activation = f"{origin.scheme}://{origin.netloc}/junie-open/"
            key = RequestKey("run-b", "operation-run-b", answer["request_id"])
            token = "T" * 32
            server.junie_notifications.links[token] = key
            # An opaque link issued before resolution still opens the original Run/operation/request.
            with urlopen(activation + token, timeout=3) as response:
                deep_link = urlsplit(response.geturl())
            self.assertEqual(deep_link.path, origin.path)
            self.assertEqual(parse_qs(deep_link.query), {"run": ["run-b"], "junie_operation": ["operation-run-b"],
                                                       "junie_request": [answer["request_id"]]})
            with self.assertRaises(HTTPError) as expired:
                urlopen(activation + ("X" * 32), timeout=3)
            self.assertEqual(expired.exception.code, 410)
            expired.exception.close()
        finally:
            server.shutdown()
            thread.join()
            server.server_close()


class JunieNotificationTests(unittest.TestCase):
    def setUp(self):
        self.moment = 0
        self.test_policy = notification_settings({})
        self.key = RequestKey("run-b", "operation-2", "request-2")
        self.requests = {self.key: {"detail": "private file contents"}}
        self.events = []
        self.notifier = JunieNotifications(
            lambda: self.requests.copy(), lambda: self.test_policy,
            "http://127.0.0.1:9234", "/secret/", lambda *args: self.events.append(args), now=lambda: self.moment,
        )

    def test_immediate_and_two_repeats_then_remove_without_retargeting_link(self):
        self.notifier.tick()
        self.assertEqual(len(self.events), 1)
        tag, url = self.events[0][1:]
        self.assertEqual(len(tag), 16)
        self.assertNotIn("secret", url)
        self.assertNotIn("run-b", url)
        self.assertNotIn("request-2", url)
        self.assertNotIn("private file", toast_xml(url))
        self.assertNotIn(url, "".join(fromstring(toast_xml(url)).itertext()))
        token = url.rsplit("/", 1)[1]
        expected = {"run": ["run-b"], "junie_operation": ["operation-2"], "junie_request": ["request-2"]}
        self.assertEqual(parse_qs(urlsplit(self.notifier.location(token)).query), expected)
        for moment in (14, 299, 300, 301, 599, 600, 601, 1000):
            self.moment = moment
            self.notifier.tick()
        self.assertEqual(self.events, [("show", tag, url)] * 3)
        self.requests = {RequestKey("run-b", "operation-3", "request-3"): {}}
        self.notifier.tick()
        self.assertEqual(self.events[-2], ("remove", tag, ""))
        self.assertEqual(self.events[-1][0], "show")
        self.assertNotEqual(self.events[-1][2], url)
        self.assertEqual(parse_qs(urlsplit(self.notifier.location(token)).query), expected)

    def test_configurable_repeat_and_disable(self):
        self.test_policy = notification_settings({"enabled": False, "interval_seconds": 45, "repeats": 1})
        self.notifier.tick()
        self.assertEqual(self.events, [])
        self.test_policy["enabled"] = True
        self.notifier.tick()
        self.moment = 44
        self.notifier.tick()
        self.assertEqual(len(self.events), 1)
        self.moment = 45
        self.notifier.tick()
        self.assertEqual(len(self.events), 2)
        self.moment = 90
        self.notifier.tick()
        self.assertEqual(len(self.events), 2)
        self.test_policy["enabled"] = False
        self.notifier.tick()
        self.assertEqual(self.events[-1][0], "remove")

    def test_pending_rechecked_immediately_before_dispatch(self):
        with patch.object(self.notifier, "pending", side_effect=[self.requests, {}]):
            self.notifier.tick()
        self.assertEqual(self.events, [])

    def test_windows_process_receives_only_opaque_link_not_dashboard_secret_or_run_details(self):
        url = "http://127.0.0.1:9234/junie-open/" + "T" * 32
        with patch("migration_harness.junie_notifications.subprocess.run") as command:
            WindowsToast()("show", "tag", url)
        args, kwargs = command.call_args
        self.assertTrue(str(args[0][0]).endswith("powershell.exe"))
        self.assertNotIn(url, args[0])
        self.assertIn(url, kwargs["input"].decode("ascii"))
        self.assertNotIn("/secret/", kwargs["input"].decode("ascii"))
        self.assertNotIn("shell", kwargs)

    def test_settings_reject_unbounded_or_unexpected_values(self):
        for value in ({"enabled": 1}, {"interval_seconds": 0}, {"interval_seconds": True}, {"repeats": -1},
                      {"repeats": 11}, {"unknown": True}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                notification_settings(value)
