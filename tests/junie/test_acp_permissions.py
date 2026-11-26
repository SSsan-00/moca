import hashlib
import json
import sys
import tempfile
import unittest
from collections import deque
from queue import Queue
from pathlib import Path
from unittest.mock import Mock, patch

from tests.support.fixtures import save_capsule_fixture

from migration_harness.junie import acp_session, junie_cli, migration_session
from migration_harness.junie.acp_permissions import PinnedReferenceReads, ReadRefused, metrics_kind
from migration_harness.config import _execution_settings
from migration_harness.web.dashboard import Dashboard
from migration_harness.execution import ExecutionControl, OperationCancelled, control_scope


class AcpPermissionTests(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.run_path = Path(root.name).resolve()
        self.workspace = self.run_path / "agent-workspace"
        self.materials = self.workspace / ".migration-materials"
        self.materials.mkdir(parents=True)
        self.document = self.materials / "reference-0001.txt"
        self.document.write_bytes("一行\r\n二行\r\n三行".encode())
        self.hashes = {self.document.name: hashlib.sha256(self.document.read_bytes()).hexdigest()}
        self.reads = PinnedReferenceReads(self.workspace, self.hashes, set())

    def test_only_exact_pinned_path_and_range_are_returned(self):
        self.assertEqual(self.reads.read({"path": str(self.document), "line": 2, "limit": 1}), "二行\r\n")
        self.assertEqual(self.reads.read({"path": str(self.document), "line": 4}), "")
        self.assertEqual(self.reads.read({"path": str(self.document)}), "一行\r\n二行\r\n三行")
        for request in (
            {"path": str(self.document), "paths": [str(self.document)]},
            {"path": str(self.document), "command": "read"},
            {"path": str(self.document), "line": True},
            {"path": str(self.document), "limit": 0},
            {"path": self.document.name},
            {"path": str(self.workspace / "unused" / ".." / ".migration-materials" / self.document.name)},
            {"path": str(self.run_path / "other-run" / self.document.name)},
        ):
            with self.subTest(request=request), self.assertRaises(ReadRefused):
                self.reads.read(request)

    def test_changed_excluded_symlink_or_cancelled_reference_never_returns_content(self):
        excluded = self.materials / "rule-0002.txt"
        excluded.write_bytes(b"not active")
        hashes = {**self.hashes, excluded.name: hashlib.sha256(excluded.read_bytes()).hexdigest()}
        with self.assertRaisesRegex(ReadRefused, "target_not_pinned"):
            PinnedReferenceReads(self.workspace, hashes, set()).read({"path": str(excluded)})
        with self.assertRaisesRegex(ReadRefused, "reference_changed"):
            self.document.write_bytes(b"new body not registered")
            self.reads.read({"path": str(self.document)})
        self.document.unlink()
        try:
            self.document.symlink_to(excluded)
        except (OSError, NotImplementedError):
            pass
        else:
            with self.assertRaisesRegex(ReadRefused, "unavailable_reference"):
                self.reads.read({"path": str(self.document)})
        control = ExecutionControl(lambda entry: None)
        control.cancelled.set()
        with control_scope(control), self.assertRaises(OperationCancelled):
            self.reads.read({"path": str(self.document)})

    def test_oversized_result_is_not_silently_truncated(self):
        self.document.write_bytes(b"a\n" * 20_000)
        reads = PinnedReferenceReads(self.workspace, {
            self.document.name: hashlib.sha256(self.document.read_bytes()).hexdigest(),
        }, set())
        with self.assertRaisesRegex(ReadRefused, "bounded_read_required"):
            reads.read({"path": str(self.document)})
        self.assertEqual(reads.read({"path": str(self.document), "line": 20_000, "limit": 1}), "a\n")

    def test_protocol_separates_direct_read_from_agent_owned_permission(self):
        program = r'''
import json, sys
def read(): return json.loads(sys.stdin.readline())
def send(**m): print(json.dumps({"jsonrpc":"2.0", **m}), flush=True)

assert read()["params"]["clientCapabilities"] == {"fs":{"readTextFile":True}}
send(id=1,result={"protocolVersion":1})
assert read()["method"] == "session/new"
send(id=2,result={"sessionId":"bound"})
assert read()["method"] == "session/prompt"
args = {"sessionId":"bound", "path":sys.argv[1], "line":2, "limit":1}
send(id=20,method="fs/read_text_file",params=args)
assert read() == {"jsonrpc":"2.0", "id":20, "result":{"content":"二行\r\n"}}
send(id=21,method="fs/read_text_file",params={**args, "sessionId":"other"})
assert read()["error"]["code"] == -32601
send(id=22,method="fs/read_text_file",params={**args, "path":sys.argv[1]+".bad"})
assert read()["error"]["code"] == -32602
send(id=23,method="session/request_permission",params={"sessionId":"bound","toolCall":{
   "toolCallId":"tool","kind":"read","title":"looks harmless",
   "rawInput":{"path":sys.argv[1]},"locations":[{"path":sys.argv[1]}]},
   "options":[{"optionId":"ask","name":"Allow once","kind":"allow_once"}]})
assert read()["result"]["outcome"] == {"outcome":"cancelled"}
send(id=3,result={"stopReason":"end_turn"})
'''
        requests, metrics = self._exchange(program, self.reads)
        self.assertEqual(len(requests), 1)
        self.assertEqual(metrics, [("read", "pinned_reference_read"), ("read", "target_not_pinned"),
                                   ("read", "agent_owned_unverified")])
        self.assertEqual(metrics_kind("untrusted tool command"), "unknown")

    def test_rejected_requests_are_counted_without_opening_a_dialog_or_counting_other_sessions(self):
        program = r'''
import json, sys
def read(): return json.loads(sys.stdin.readline())
def send(**m): print(json.dumps({"jsonrpc":"2.0", **m}), flush=True)

assert read()["params"]["clientCapabilities"] == {}
send(id=1,result={"protocolVersion":1})
assert read()["method"] == "session/new"
send(id=2,result={"sessionId":"bound"})
assert read()["method"] == "session/prompt"
for id, session in ((20, "bound"), (21, "other")):
    send(id=id,method="fs/read_text_file",params={"path":sys.argv[1], "sessionId":session})
    assert read() == {"jsonrpc":"2.0", "id":id, "error":{"code":-32601,"message":"Method not supported"}}
send(id=22,method="session/request_permission",params={"sessionId":"bound",
    "toolCall":{"toolCallId":"sample","kind":"private-kind","rawInput":{"path":sys.argv[1]}}, "options":[]})
assert read() == {"jsonrpc":"2.0", "id":22, "error":{"code":-32602,"message":"No permission options"}}
send(id=3,result={"stopReason":"end_turn"})
'''
        for reads in (None, PinnedReferenceReads(self.workspace, {}, set())):
            with self.subTest(reads_enabled=reads is not None):
                requests, metrics = self._exchange(program, reads)
                self.assertEqual(requests, [])
                self.assertEqual(metrics, [("read", "client_read_unavailable"), ("unknown", "no_permission_options")])

    def test_permission_dialog_requires_the_current_initialized_session(self):
        show_request = Mock(return_value=None)
        with patch.object(acp_session, "Thread"):
            session = acp_session.AcpSession(Mock(), ["junie"], self.workspace, show_request,
                                           self.workspace / "complete", "token", None)
        session.send = Mock()
        params = {
            "toolCall": {"toolCallId": "sample", "kind": "execute"},
            "options": [{"optionId": "ask", "name": "Allow once", "kind": "allow_once"}],
        }

        for session_id, request_session in ((None, {}), (None, {"sessionId": None}),
                                            ("bound", {}), ("bound", {"sessionId": "other"})):
            with self.subTest(session_id=session_id, request_session=request_session):
                show_request.reset_mock()
                session.send.reset_mock()
                session.session_id = session_id
                session._receive_message({"id": 20, "method": "session/request_permission",
                                          "params": {**params, **request_session}})
                show_request.assert_not_called()
                session.send.assert_called_once_with(id=20, error={"code": -32601, "message": "Method not supported"})

        show_request.reset_mock()
        session.send.reset_mock()
        session.session_id = "bound"
        session._receive_message({"id": 21, "method": "session/request_permission",
                                  "params": {**params, "sessionId": "bound"}})
        show_request.assert_called_once()
        session.send.assert_called_once_with(id=21, result={"outcome": {"outcome": "cancelled"}})

    def _exchange(self, program, reads):
        launch = junie_cli._launch_process
        process = launch([sys.executable, "-u", "-c", program, str(self.document)], owned=True,
                         stdin=junie_cli.subprocess.PIPE, stdout=junie_cli.subprocess.PIPE)
        requests, metrics = [], []
        pin_token = junie_cli.reference_read_scope.set(reads)
        counter_token = junie_cli.permission_observer.set(lambda *args: metrics.append(args))
        session = acp_session.AcpSession(process, ["junie", "prompt"], self.workspace, requests.append,
                                       self.workspace / "complete", "token", None)
        try:
            session.initialize()
            import time
            deadline = time.monotonic() + 10
            while not session.advance():
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.005)
            process.wait(timeout=5)
        finally:
            try:
                junie_cli._close_junie(process)
            finally:
                session.close()
                junie_cli.reference_read_scope.reset(pin_token)
                junie_cli.permission_observer.reset(counter_token)
        return requests, metrics

    def test_pin_host_support_is_explicit_and_diagnostics_do_not_require_it(self):
        self.assertFalse(_execution_settings({}, self.run_path)["junie_pinned_reference_reads"])
        with self.assertRaisesRegex(ValueError, "pinned_reference_reads must be a boolean"):
            _execution_settings({"junie": {"pinned_reference_reads": "true"}}, self.run_path)
        application = Dashboard()
        application.begin_operation()
        record = {"run_id": self.run_path.name, "status": "ready"}
        def exercise():
            self.assertIsNone(junie_cli.reference_read_scope.get())
            self.assertIsNotNone(junie_cli.permission_observer.get())
        with control_scope(application.control), patch.object(junie_cli, "run_interactive", side_effect=exercise):
            migration_session.run_budgeted_junie({}, self.run_path / "run.json", record, "migration", None, self.workspace, [])

    def test_budgeted_run_counts_only_flushed_pages_and_keeps_last_receipt_without_content(self):
        manifest = self.run_path / "capsule" / "manifest.json"
        context = manifest.parent / "part-0001" / "context.md"
        context.parent.mkdir(parents=True)
        context.write_text("context", encoding="utf-8")
        save_capsule_fixture(manifest.parent, "fixture", [(context, [])], materials=self.hashes)
        application = Dashboard()
        application.begin_operation()
        record = {"run_id": self.run_path.name, "status": "ready", "current_capsule": 2}
        def exercise():
            session = acp_session.AcpSession.__new__(acp_session.AcpSession)
            session.reference_reads = junie_cli.reference_read_scope.get()
            session.observe_permission = junie_cli.permission_observer.get()
            session.writes, session.outgoing = deque(), Queue()
            session.command = ["junie"]
            for line, limit, success in [(1, 1, False), (4, 1, True), (2, 99, True)]:
                session.read_reference(7, {"path": str(self.document), "line": line, "limit": limit})
                _, written = session.outgoing.get_nowait()
                if success:
                    written.set_result(None)
                    session._check_writes()
                else:
                    written.set_exception(BrokenPipeError())
                    with self.assertRaises(junie_cli.JunieSessionError):
                        session._check_writes()
                    self.assertIsNone(session.reference_reads.last_delivery)
                if line == 4:
                    empty = session.reference_reads.last_delivery
                    self.assertEqual((empty["utf8_bytes"], empty["start_line"], empty["end_line"]), (0, None, None))
        with control_scope(application.control), patch.object(junie_cli, "run_interactive", side_effect=exercise):
            for phase in ("migration", "checker_repair"):
                migration_session.run_budgeted_junie({"junie_pinned_reference_reads": True}, self.run_path / "run.json",
                                             record, phase, None, self.workspace, [])
        data = json.loads((self.run_path / "run.json").read_text("utf-8"))
        self.assertEqual(data["junie_permission_counts"], {"read": {"pinned_reference_read": 4}})
        count = len("二行\r\n三行".encode())
        self.assertEqual(data["junie_reference_delivery"], {
            "utf8_bytes": count * 2, "last_response": {
                "material_id": self.document.name, "sha256": self.hashes[self.document.name], "utf8_bytes": count,
                "start_line": 2, "end_line": 3, "phase": "checker_repair", "capsule": 2,
            },
        })
        self.assertNotIn(str(self.workspace), str(data["junie_reference_delivery"]))
        self.assertNotIn("一行", str(data))
        self.assertIsNone(junie_cli.reference_read_scope.get())
