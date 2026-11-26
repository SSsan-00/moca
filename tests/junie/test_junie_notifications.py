import io
import json
import subprocess
import unittest
from contextlib import redirect_stdout
from threading import Thread
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen
from xml.etree.ElementTree import fromstring

from migration_harness.web.dashboard import Dashboard
from migration_harness.junie.junie_notifications import JunieNotifications, RequestKey, RunNotice, WindowsToast, answer_pending, notification_settings, pending_requests, run_notice, toast_xml


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
            pending_key = next(iter(self.pending()))
            live_token = "A" * 32
            server.junie_notifications.links[live_token] = pending_key
            with urlopen(f"{origin.scheme}://{origin.netloc}/junie-open/{live_token}", timeout=3) as response:
                self.assertEqual(parse_qs(urlsplit(response.geturl()).query)["run"], ["run-b"])
            self.assertIn(pending_key, server.junie_notifications.opened_requests)
            self.assertFalse(b.done())
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
            result_token = "R" * 32
            server.junie_notifications.links[result_token] = RunNotice("run-c", "build_failed", "failed")
            with urlopen(activation + result_token, timeout=3) as response:
                result_link = urlsplit(response.geturl())
            self.assertEqual(parse_qs(result_link.query), {"run": ["run-c"], "notice": ["failed"],
                                                            "status": ["build_failed"]})
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

    def test_immediate_and_three_repeats_then_remove_without_retargeting_link(self):
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
        for moment, count in [(59, 1), (60, 2), (179, 2), (180, 3), (299, 3), (300, 4), (1000, 4)]:
            self.moment = moment
            self.notifier.tick()
            self.assertEqual(self.events, [("show", tag, url)] * count)
        self.requests = {RequestKey("run-b", "operation-3", "request-3"): {}}
        self.notifier.tick()
        self.assertEqual(self.events[-2], ("remove", tag, ""))
        self.assertEqual(self.events[-1][0], "show")
        self.assertNotEqual(self.events[-1][2], url)
        self.assertEqual(parse_qs(urlsplit(self.notifier.location(token)).query), expected)

    def test_activation_stops_only_that_request_without_answering_it(self):
        other = RequestKey("run-a", "another-op", "another-request")
        self.requests[other] = {}
        self.notifier.tick()
        _, tag, url = self.events[1]
        token = url.rsplit("/", 1)[-1]
        self.assertIsNotNone(self.notifier.location(token, activate=True))
        self.assertIn(self.key, self.requests)
        for seconds in (60, 180, 300):
            self.moment = seconds
            self.notifier.tick()
        self.assertEqual([action for action, label, _ in self.events if label == tag], ["show", "remove"])
        self.assertEqual(sum(action == "show" and label != tag for action, label, _ in self.events), 4)
        # An old activation must not suppress another operation of the same Run.
        self.requests.pop(self.key)
        self.requests[RequestKey("run-b", "new-op", "new-request")] = {}
        self.notifier.location(token, activate=True)
        self.notifier.tick()
        self.assertEqual(self.events[-1][0], "show")
        self.assertNotEqual(self.events[-1][1], tag)

    def test_delayed_worker_does_not_deliver_a_burst_of_overdue_repeats(self):
        self.notifier.tick()
        self.moment = 250
        self.notifier.tick()
        self.notifier.tick()
        self.assertEqual(len(self.events), 2)
        self.moment = 300
        self.notifier.tick()
        self.assertEqual(len(self.events), 3)

    def test_custom_offsets_can_disable_repeats_and_reject_ambiguous_schedules(self):
        self.test_policy = notification_settings({"repeat_after_seconds": []})
        self.notifier.tick()
        self.moment = 600
        self.notifier.tick()
        self.assertEqual(len(self.events), 1)
        for value in ([1, 1], [3, 1], [False], [0], [86401]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                notification_settings({"repeat_after_seconds": value})
        with self.assertRaises(ValueError):
            notification_settings({"repeat_after_seconds": [1], "repeats": 1})

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
        with patch("migration_harness.junie.junie_notifications.subprocess.run") as command:
            WindowsToast()("show", "tag", url)
        args, kwargs = command.call_args
        self.assertTrue(str(args[0][0]).endswith("powershell.exe"))
        self.assertNotIn(url, args[0])
        self.assertIn(url, kwargs["input"].decode("ascii"))
        self.assertNotIn("/secret/", kwargs["input"].decode("ascii"))
        self.assertNotIn("shell", kwargs)

    def test_windows_show_retries_a_timeout_once_with_identical_tag_and_payload(self):
        expired = subprocess.TimeoutExpired("powershell.exe", 5)
        with patch("migration_harness.junie.junie_notifications.subprocess.run", side_effect=[expired, None]) as command:
            WindowsToast()("show", "stable-tag", "http://127.0.0.1:9234/junie-open/token", "failed")
        self.assertEqual(command.call_count, 2)
        self.assertEqual(command.call_args_list[0].args, command.call_args_list[1].args)
        self.assertEqual(command.call_args_list[0].kwargs["input"], command.call_args_list[1].kwargs["input"])
        self.assertEqual(json.loads(command.call_args.kwargs["input"])["tag"], "stable-tag")

    def test_windows_toast_does_not_loop_or_retry_explicit_rejection_or_removal(self):
        expired = subprocess.TimeoutExpired("powershell.exe", 5)
        rejected = subprocess.CalledProcessError(3, "powershell.exe")
        for action, error, calls in (("show", expired, 2), ("show", rejected, 1), ("remove", expired, 1)):
            with self.subTest(action=action, error=type(error).__name__):
                with patch("migration_harness.junie.junie_notifications.subprocess.run", side_effect=error) as command:
                    with self.assertRaises(type(error)):
                        WindowsToast()(action, "stable-tag", "")
                self.assertEqual(command.call_count, calls)

    def test_run_outcomes_are_distinct_and_manual_stop_or_intermediate_status_is_silent(self):
        outcomes = [
            ({"status": "build_failed"}, "failed"),
            ({"status": "checker_needs_review"}, "review"),
            ({"status": "editor_checkpoint", "operator_question": "private question"}, "review"),
            ({"status": "build_succeeded"}, "built"),
            ({"status": "checker_accepted"}, "verified"),
            ({"status": "checker_succeeded", "verification": {"inputs_changed_during_verification": True}}, "recheck"),
            ({"status": "junie_finished"}, "migrated"),
            ({"status": "editor_checkpoint", "requested_range_complete": True}, "migrated"),
            ({"status": "build_failed", "last_stop": {"saved_status": "build_failed"}}, None),
            ({"status": "building"}, None), ({"status": "repairing"}, None), ({"status": "cancelled"}, None),
        ]
        for data, kind in outcomes:
            with self.subTest(data=data):
                notice = run_notice("run-c", "run", data)
                self.assertEqual(notice.kind if notice else None, kind)
        self.assertIsNone(run_notice("run-c", "anchors", {"status": "build_succeeded"}))

    def test_run_completion_not_repeated_and_links_are_specific_and_opaque(self):
        self.requests.clear()
        for notice in (RunNotice("run-a", "build_failed", "failed"),
                       RunNotice("run-b", "checker_needs_review", "review"),
                       RunNotice("run-c", "build_succeeded", "built"),
                       RunNotice("run-c", "checker_succeeded", "verified"),
                       RunNotice("run-d", "checker_succeeded", "verified")):
            self.notifier.post(notice)
        self.notifier.tick()
        self.moment = 999
        self.notifier.tick()
        self.assertEqual([event[-1] for event in self.events], ["failed", "review", "built", "verified"])
        for (_, _, url, kind), run_id in zip(self.events, ("run-a", "run-b", "run-c", "run-d")):
            self.assertNotIn(run_id, url)
            self.assertNotIn("/secret/", url)
            location = self.notifier.location(url.rsplit("/", 1)[1])
            query = parse_qs(urlsplit(location).query)
            self.assertEqual(query["run"], [run_id])
            self.assertEqual(query["notice"], [kind])
        copy = "".join(fromstring(toast_xml("http://test", "built")).itertext())
        self.assertIn("チェッカーは未実施", copy)

    def test_milestone_is_suppressed_in_foreground_or_if_final_result_is_already_available(self):
        self.requests.clear()
        milestone = RunNotice("run-c", "building", "milestone")
        self.notifier.mark_visible()
        self.notifier.post(milestone)
        self.notifier.tick()
        self.assertEqual(self.events, [])
        self.moment = 4
        self.notifier.post(milestone)
        self.notifier.post(RunNotice("run-c", "build_failed", "failed"))
        self.notifier.tick()
        self.assertEqual([event[-1] for event in self.events], ["failed"])
        self.notifier.post(milestone)
        self.notifier.tick()
        self.notifier.post(milestone)
        self.notifier.tick()
        self.assertEqual([event[-1] for event in self.events], ["failed", "milestone"])
        self.test_policy["enabled"] = False
        self.notifier.post(RunNotice("run-d", "checker_succeeded", "verified"))
        self.notifier.tick()
        self.assertEqual(len(self.events), 2)

    def test_settings_reject_unbounded_or_unexpected_values(self):
        for value in ({"enabled": 1}, {"interval_seconds": 0}, {"interval_seconds": True}, {"repeats": -1},
                      {"repeats": 11}, {"milestone_after_seconds": True}, {"milestone_after_seconds": -1}, {"unknown": True}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                notification_settings(value)
