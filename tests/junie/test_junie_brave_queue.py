import io
import json
import uuid
from contextlib import redirect_stdout
from threading import Thread
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from tests.support.fixtures import SavedRunFixture
from migration_harness import workflow
from migration_harness.runs.run_manager import RunManager
from migration_harness.web import dashboard


class JunieBraveQueueTests(SavedRunFixture):
    def setUp(self):
        super().setUp()
        original = self.config["units"][0]
        self.config["units"].extend({**original, "id": unit} for unit in ("one", "two", "three", "four"))
        self.save_record({**self.record(), "status": "interrupted", "completed_capsules": 0, "current_capsule": 1})
        (self.run_path / "agent-workspace").mkdir()
        self.view = dashboard.Dashboard()
        self.view.publish({"status": "idle", "ready": True})
        self.manager = RunManager(self.config, self.view)
        self.view.manager = self.manager
        self.manager.limit = 0
        self.addCleanup(self.manager.close)

    def open_http_dashboard(self):
        output = io.StringIO()
        with redirect_stdout(output):
            server = self.view.server(self.config, b"", b"")
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)
        address = output.getvalue().splitlines()[0].removeprefix("Progress dashboard: ")
        parsed = urlsplit(address)
        origin = f"{parsed.scheme}://{parsed.netloc}"

        def request(path, action=None):
            data = None if action is None else json.dumps({"revision": 0, **action}).encode("utf-8")
            headers = {} if action is None else {"Content-Type": "application/json", "Origin": origin}
            with urlopen(Request(address + path, data, headers), timeout=5) as response:
                return response.status, json.load(response)

        return request

    def saved_queue(self):
        return json.loads(self.manager.queue_path.read_bytes())

    def test_removed_queued_actions_never_start_on_restart(self):
        normal = self.manager.submit({"action": "run", "unit_id": "two"})["run_id"]
        queue = self.saved_queue()
        waiting = str(uuid.uuid4())
        queue["jobs"].extend([
            {"action": "prepare", "run_id": waiting, "unit_id": "one"},
            {"action": "prepare", "run_id": self.run_id, "unit_id": "example", "running": True},
        ])
        original_record = self.record_path.read_bytes()
        self.manager.close()
        self.manager.queue_path.write_text(json.dumps(queue), encoding="utf-8")

        with patch.object(dashboard, "execute_dashboard_action", return_value={"status": "fixture_completed"}) as execute:
            restored = RunManager(self.config, dashboard.Dashboard())
            self.addCleanup(restored.close)
            self.assertEqual(list(restored.pending), [normal])
            self.assertEqual(restored.get(waiting).state["status"], "cancelled")
            self.assertIn("取り消しました", restored.get(waiting).state["message"])
            self.assertIn(self.run_id, restored.blocked_conversations)
            self.assertEqual(self.record_path.read_bytes(), original_record)
            with self.assertRaisesRegex(ValueError, "利用できません"):
                restored.submit({"action": "prepare", "unit_id": "one"})
            restored.limit = 1
            restored.start_pending()
            with restored.condition:
                self.assertTrue(restored.condition.wait_for(lambda: not restored.busy, timeout=5))
            self.assertEqual([call.args[2]["run_id"] for call in execute.call_args_list], [normal])

    def test_script_retry_waiting_at_restart_requires_confirmation_again(self):
        view = self.manager.get(self.run_id)
        self.manager.submit({"action": "recover-script", "run_id": self.run_id,
                             "revision": view.revision, "retry_version": "a" * 64})
        original = self.record_path.read_bytes()
        self.manager.close()
        with patch.object(dashboard, "execute_dashboard_action") as execute:
            restored = RunManager(self.config, dashboard.Dashboard())
            self.addCleanup(restored.close)
            self.assertFalse(restored.pending)
            self.assertIn("やり直してください", restored.get(self.run_id).state["message"])
            self.assertEqual(restored.get(self.run_id).state["status"], "interrupted")
            self.assertEqual(self.record_path.read_bytes(), original)
            execute.assert_not_called()

    def test_http_brave_is_single_run_or_explicit_current_resume_plan(self):
        request = self.open_http_dashboard()
        resume = {"action": "resume-run", "run_id": self.run_id, "junie_brave": True}
        plan_query = urlencode({"run_id": self.run_id, "start_stage": "migration"})
        status, plan = request("resume-plan?" + plan_query)
        self.assertEqual(status, 200)
        for rejected, code in (
            ({"action": "run", "unit_id": "one", "junie_brave": "true"}, 400),
            ({"action": "run", "unit_id": "one", "junie_brave": 1}, 400),
            ({"action": "run", "unit_ids": ["one", "two"], "junie_brave": True}, 409),
            ({"action": "prepare", "unit_id": "one"}, 400),
            ({"action": "prepare", "unit_id": "one", "junie_brave": True}, 400),
            ({**resume, "action": "resume"}, 400),
            (resume, 400),
            ({**resume, "start_stage": "migration", "plan_version": "0" * 64}, 409),
            ({**resume, "start_stage": "build", "plan_version": plan["version"]}, 400),
            ({**resume, "start_stage": "checker", "plan_version": plan["version"]}, 400),
            ({**resume, "verification_request": plan}, 400),
        ):
            with self.assertRaises(HTTPError, msg=f"Expected rejection for {rejected}") as error:
                request("control", rejected)
            with error.exception as response:
                self.assertEqual(response.code, code)
            self.assertFalse(self.manager.jobs)
            self.assertEqual(self.saved_queue()["jobs"], [])

        status, response = request("control", {"action": "run", "unit_id": "one", "junie_brave": True})
        self.assertEqual(status, 202)
        brave = response["run_id"]
        status, _ = request("control", {**resume, "start_stage": "migration", "plan_version": plan["version"]})
        self.assertEqual(status, 202)
        status, response = request("control", {"action": "run", "unit_id": "two", "junie_brave": False})
        self.assertEqual(status, 202)
        ordinary = response["run_id"]
        self.assertIs(self.manager.jobs[brave]["junie_brave"], True)
        actual = self.manager.jobs[self.run_id]
        self.assertIs(actual["junie_brave"], True)
        self.assertEqual(actual["verification_request"]["version"], plan["version"])
        persisted = {job["run_id"]: job for job in self.saved_queue()["jobs"]}
        for run_id in (brave, self.run_id):
            self.assertIs(persisted[run_id]["junie_brave_reconfirm"], True)
            self.assertNotIn("junie_brave", persisted[run_id])
        self.assertNotIn("junie_brave", persisted[ordinary])
        self.assertNotIn("junie_brave_reconfirm", persisted[ordinary])

    def test_restart_never_launches_saved_brave_permission_and_keeps_ordinary_queue(self):
        submit = self.manager.submit
        brave = submit({"action": "run", "unit_id": "one", "junie_brave": True})["run_id"]
        normal = submit({"action": "run", "unit_id": "two", "junie_brave": False})["run_id"]
        other = submit({"action": "run", "unit_id": "four"})["run_id"]
        plan = workflow.plan_stage_resume(self.config, self.run_id, "migration")
        submit({"action": "resume-run", "run_id": self.run_id, "junie_brave": True,
                "verification_request": {**plan, "id": uuid.uuid4().hex}})
        original_record = self.record_path.read_bytes()
        queue = self.saved_queue()
        ordinary = [job for job in queue["jobs"] if job["run_id"] in (normal, other)]
        tampered = str(uuid.uuid4())
        queue["jobs"].append({"action": "run", "run_id": tampered, "unit_id": "three",
                              "operation_id": uuid.uuid4().hex, "junie_brave": True})
        self.manager.close()
        self.manager.queue_path.write_text(json.dumps(queue), encoding="utf-8")

        with patch.object(dashboard, "execute_dashboard_action", return_value={"status": "fixture_completed"}) as execute:
            restored = RunManager(self.config, dashboard.Dashboard())
            self.addCleanup(restored.close)
            self.assertEqual(list(restored.pending), [normal, other])
            self.assertEqual(self.saved_queue()["jobs"], ordinary)
            for run_id in (brave, self.run_id, tampered):
                row = restored.row(restored.get(run_id))
                self.assertTrue(row["ready"])
                self.assertFalse(row.get("queued"))
                self.assertIn("Brave", row["message"])
                self.assertIn("再起動", row["message"])
            self.assertEqual(self.record_path.read_bytes(), original_record)
            restored.limit = 1
            restored.start_pending()
            with restored.condition:
                self.assertTrue(restored.condition.wait_for(lambda: not restored.busy, timeout=5))
            jobs = [call.args[2] for call in execute.call_args_list]
            self.assertEqual([job["run_id"] for job in jobs], [normal, other])
            self.assertEqual([job["operation_id"] for job in jobs], [job["operation_id"] for job in ordinary])
            self.assertTrue(all(not job.get("junie_brave") for job in jobs))
            self.assertEqual(self.record_path.read_bytes(), original_record)
            restored.limit = 0
            new_id = restored.submit({"action": "run", "unit_id": "one", "junie_brave": True})["run_id"]
            self.assertNotEqual(new_id, brave)
            self.assertIs(restored.jobs[new_id]["junie_brave"], True)
            fresh_plan = workflow.plan_stage_resume(self.config, self.run_id, "migration")
            restored.submit({"action": "resume-run", "run_id": self.run_id, "junie_brave": True,
                             "verification_request": {**fresh_plan, "id": uuid.uuid4().hex}})
            self.assertIs(restored.jobs[self.run_id]["junie_brave"], True)
