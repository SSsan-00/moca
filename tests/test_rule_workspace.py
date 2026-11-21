import base64
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from migration_harness.dashboard import _save_settings
from migration_harness.rule_workspace import RuleWorkspace
from migration_harness.rule_chat import RuleChat


class RuleWorkspaceTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.app = self.root / "app"
        self.app.mkdir()
        subprocess.run(["git", "init", "-q", str(self.app)], check=True)
        (self.app / "original.md").write_bytes(b"first\r\nexception\r\n")
        (self.app / "legacy.php").write_text("<?php\n", encoding="utf-8")
        self.config_path = self.root / "harness.toml"
        self.config_path.write_text("""# main comment
[project]
root = 'app'
run_root = 'runs'
worktree_root = 'worktrees'

[[project.migration_rules]] # rule header comment
path = 'original.md' # rule body comment
usage = 'required'

[[units]]
id = 'sample'
source = 'legacy.php' # untouched
target = 'Page.cs'
start_line = 1
end_line = 1
""", encoding="utf-8")
        self.config = {"config_path": str(self.config_path), "project_root": str(self.app),
                       "run_root": str(self.root / "runs")}
        self.workspace = RuleWorkspace(self.config)
        self.addCleanup(self.workspace.close)

    def send(self, data, action, **fields):
        return self.workspace.action({"id": data["id"], "revision": data["revision"], "action": action, **fields},
                                     lambda content, version: _save_settings(self.config_path, content, version))

    def apply(self, draft):
        preview = self.send(draft, "preview")
        return self.send(preview, "apply", confirmed=True, preview=preview["preview"]["token"])

    def test_add_and_edit_preview_apply_and_next_draft_uses_original(self):
        original = self.config_path.read_bytes()
        draft = self.workspace.start()
        new_data = b"\xef\xbb\xbfdetail\r\nexception"
        draft = self.send(draft, "edit", source="new", name="extra.md", text="detail\nexception",
                          bytes=base64.b64encode(new_data).decode())
        draft = self.send(draft, "edit", source=draft["files"][0]["source"], text="first\nnew exception\n")
        draft = self.send(draft, "classification", source=draft["files"][1]["source"],
                          usage="reference", description="When importing", confirm_scope=True)
        ready = self.send(draft, "preview")
        self.assertIn("new exception", ready["preview"]["text_diff"])
        self.assertIn("When importing", ready["preview"]["toml_diff"])
        self.assertEqual(self.config_path.read_bytes(), original)
        saved = self.send(ready, "apply", confirmed=True, preview=ready["preview"]["token"])
        self.assertTrue(saved["applied"])
        settings = self.config_path.read_text()
        for part in ("# main comment", "# untouched", "rule header comment", "rule body comment"):
            self.assertIn(part, settings)
        self.assertEqual((self.app / "original.md").read_bytes(), b"first\r\nexception\r\n")
        next_draft = self.workspace.start()
        path = self.workspace.draft(next_draft["id"])
        self.assertEqual((path / "sources/rule-0001.md").read_bytes(), b"first\r\nnew exception\r\n")
        self.assertEqual((path / "sources/rule-0002.md").read_bytes(), new_data)

    @unittest.skipUnless(sys.platform == "win32", "Windows compares paths without case")
    def test_managed_pack_keeps_original_bytes_with_case_alias(self):
        runs = Path(self.config["run_root"])
        runs.mkdir()
        self.config["run_root"] = str(runs.with_name("RuNs"))
        original = b"# Original\r\nException\r\n"
        (self.app / "original.md").write_bytes(original)
        self.apply(self.workspace.start())
        next_draft = self.workspace.draft(self.workspace.start()["id"])
        self.assertEqual((next_draft / "sources/rule-0001.md").read_bytes(), original)

    def test_toml_change_and_external_draft_edit_invalidate_apply(self):
        draft = self.workspace.start()
        ready = self.send(draft, "preview")
        token = ready["preview"]["token"]
        path = self.workspace.draft(draft["id"])
        (path / "sources/rule-0001.md").write_text("external draft edit\n")
        with self.assertRaisesRegex(ValueError, "下書きが"):
            self.send(ready, "apply", confirmed=True, preview=token)
        ready = self.send(draft, "preview")
        self.config_path.write_text(self.config_path.read_text() + "\n# concurrent\n")
        with self.assertRaisesRegex(ValueError, "TOMLが更新"):
            self.send(ready, "apply", confirmed=True, preview=ready["preview"]["token"])
        self.assertIn("# concurrent", self.config_path.read_text())

    def test_archives_keep_previous_and_block_saved_run_references(self):
        a = self.apply(self.workspace.start())
        b = self.apply(self.workspace.start())
        self.apply(self.workspace.start())
        run = self.root / "runs" / "fixture"
        run.mkdir()
        record = run / "run.json"
        record.write_text(json.dumps({"pinned_reference": a["id"].upper()}))
        self.assertEqual(self.workspace._archives(), [])
        record.unlink()
        self.assertEqual(self.workspace._archives(), [{"id": a["id"], "archived": False}])
        with self.assertRaisesRegex(ValueError, "他の設定"):
            self.send(a, "archive")
        self.send(a, "archive", confirmed=True)
        self.assertTrue(self.workspace._archives()[0]["archived"])
        self.send(a, "restore")
        self.assertFalse(self.workspace._archives()[0]["archived"])
        self.assertTrue((self.workspace.draft(b["id"]) / "published/generated/Agent.md").is_file())

    def test_unavailable_model_has_actionable_acp_error(self):
        chat = object.__new__(RuleChat)
        chat.turn_id = 10
        with self.assertRaisesRegex(ValueError, "利用できるモデルがありません"):
            chat._receive_message({"id": 10, "error": {
                "code": -32002, "message": "No models are currently available for submission",
            }})
        self.assertIsNone(chat.turn_id)

    def test_agent_cancelled_prompt_reports_unfinished_turn_and_provider_guidance(self):
        chat = RuleChat.__new__(RuleChat)
        chat.turn_id = 10
        chat.prompt_finished = False
        with self.assertRaisesRegex(ValueError, "中断.*認証・利用資格"):
            chat._receive_message({"id": 10, "result": {"stopReason": "cancelled"}})
        self.assertIsNone(chat.turn_id)
        self.assertFalse(chat.prompt_finished)

    def test_ready_wait_uses_the_acp_connection_deadline(self):
        from threading import Event
        from unittest.mock import Mock

        from migration_harness import junie_cli

        chat = RuleChat.__new__(RuleChat)
        chat.response = []
        chat.request_info = None
        chat.session_id = None
        chat.session_ready = True
        chat.pending = None
        chat.process = Mock()
        chat.process.poll.return_value = None
        advances = 0

        def receive_connection():
            nonlocal advances
            advances += 1
            if advances == 2:
                chat.session_id = "slow-connection"
            return False

        chat.advance = receive_connection
        with patch("migration_harness.rule_chat.monotonic", side_effect=[0, 40]), patch(
            "migration_harness.rule_chat.sleep"
        ):
            self.assertEqual(chat.wait(ready=True, cancelled=Event(), publish=lambda *_: None), "")
        self.assertEqual(chat.session_id, "slow-connection")
        self.assertGreater(chat._connection_timeout(), junie_cli.ACP_TIMEOUT)

    def test_chat_proposals_are_not_written_until_accepted(self):
        class FakeChat:
            offered_skill = False

            def __init__(self, project, *_):
                self.project = project

            def begin(self, _):
                pass

            def wait(self, *, ready=False, **_):
                return "" if ready else json.dumps({"message": "案です", "unresolved": [],
                    "edit": {"source": "new", "name": "chat.md", "text": "new rule\n"}})

            def shutdown(self):
                pass
        draft = self.workspace.start()
        with patch("migration_harness.rule_workspace.RuleChat", FakeChat):
            self.send(draft, "message", text="文書なしで追加")
            worker = self.workspace.worker
            if worker:
                worker.join(3)
        reopened = RuleWorkspace(self.config).snapshot(draft["id"])
        self.assertEqual(len(reopened["files"]), 1)
        self.assertEqual(len(reopened["messages"]), 2)
        accepted = self.send(reopened, "accept")
        self.assertEqual(len(accepted["files"]), 2)
        self.assertIn("new rule", self.workspace.file_text(draft["id"], accepted["files"][1]["source"])["text"])

    def test_rule_workspace_answers_only_current_permission_and_keeps_toml_unchanged(self):
        mock = """import json,sys
def send(data): print(json.dumps(data),flush=True)
for line in sys.stdin:
    msg=json.loads(line)
    method=msg.get("method")
    if method=="initialize":
        send({"id":msg["id"],"result":{"protocolVersion":1}})
    elif method=="session/new":
        send({"id":msg["id"],"result":{"sessionId":"rule-scope"}})
    elif method=="session/prompt":
        prompt_id=msg["id"]
        send({"id":700,"method":"session/request_permission","params":{"sessionId":"rule-scope",
            "toolCall":{"toolCallId":"fixture-read","kind":"read","title":"Read synthetic fixture"},
            "options":[{"optionId":"yes","name":"Allow once","kind":"allow_once"},
                       {"optionId":"no","name":"Deny once","kind":"reject_once"}]}})
    elif msg.get("id")==700:
        choice=msg["result"]["outcome"]["optionId"]
        answer={"message":choice,"unresolved":[]}
        if choice=="yes":
            answer["edit"]={"source":"new","name":"proposal.md","text":"new fixture rule"}
        send({"method":"session/update","params":{"sessionId":"rule-scope","update":{
            "sessionUpdate":"agent_message_chunk","content":{"type":"text","text":json.dumps(answer)}}}})
        send({"id":prompt_id,"result":{"stopReason":"end_turn"}})
"""
        original = self.config_path.read_bytes()
        own = self.workspace.start()
        foreign = self.workspace.start()
        self.assertNotEqual(own["id"], foreign["id"])
        request_ids = set()
        def stop_fixture(process):
            process.terminate()
            return process.wait(timeout=5)

        with patch("migration_harness.junie_cli._close_junie", side_effect=stop_fixture), patch(
                "migration_harness.junie_cli._launch_process", side_effect=lambda *args, **kwargs: subprocess.Popen(
                    [sys.executable, "-u", "-c", mock], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL)):
            try:
                for option in ("no", "yes"):
                    with self.subTest(option=option):
                        current = self.workspace.snapshot(own["id"])
                        self.send(current, "message", text="Synthetic read permission test")
                        deadline = time.monotonic() + 8
                        while time.monotonic() < deadline:
                            with self.workspace.lock:
                                permission = (self.workspace.live or {}).get("permission")
                            if permission and permission["id"] not in request_ids:
                                break
                            time.sleep(0.02)
                        else:
                            self.fail("Permission did not reach the rule workspace")
                        current = self.workspace.snapshot(own["id"])
                        request = permission["id"]
                        with self.assertRaises(ValueError):
                            self.send(foreign, "answer", request=request, option=option)
                        for stale in {"stale-request", *request_ids}:
                            with self.assertRaises(ValueError):
                                self.send(current, "answer", request=stale, option=option)
                        with self.assertRaises(ValueError):
                            self.send(current, "answer", request=request, option="not-an-option")
                        self.send(current, "answer", request=request, option=option)
                        request_ids.add(request)
                        worker = self.workspace.worker
                        if worker:
                            worker.join(8)
                        self.assertIsNone(self.workspace.worker)
                        result = self.workspace.snapshot(own["id"])
                        self.assertEqual(result["messages"][-1]["text"], option)
                        self.assertEqual(bool(result.get("proposal")), option == "yes")
                        self.assertEqual(self.config_path.read_bytes(), original)
                        self.assertFalse((self.app / "proposal.md").exists())
            finally:
                self.workspace.close()

    def test_acp_skill_announcement_and_multiple_turns_same_session(self):
        mock = """import sys,json
for line in sys.stdin:
    req=json.loads(line)
    m=req.get('method')
    if m=='initialize': result={'protocolVersion':1}
    elif m=='session/new':
        result={'sessionId':'same'}
    elif m=='session/prompt':
        assert req['params']['sessionId']=='same'
        print(json.dumps({'method':'session/update','params':{'sessionId':'same','update':{
            'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':req['params']['prompt'][0]['text']}}}}),flush=True)
        result={'stopReason':'end_turn'}
    else: continue
    print(json.dumps({'id':req['id'],'result':result}),flush=True)
    if m=='session/new':
        print(json.dumps({'method':'session/update','params':{'sessionId':'same','update':{
            'sessionUpdate':'available_commands_update','availableCommands':[{'name':'harness-optimize-rules'}]}}}),flush=True)
"""
        with patch("migration_harness.junie_cli._launch_process", side_effect=lambda *a, **k: subprocess.Popen(
            [sys.executable, "-u", "-c", mock], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )), patch("migration_harness.junie_cli._close_junie", side_effect=lambda process: (
            process.terminate(), process.wait(timeout=3)
        )[-1]):
            chat = RuleChat(self.root, None, None)
            try:
                control = self.workspace.cancelled
                update = lambda *_: None
                chat.wait(ready=True, cancelled=control, publish=update)
                for word in ("first", "second"):
                    chat.begin(word)
                    self.assertEqual(chat.wait(cancelled=control, publish=update), word)
                self.assertTrue(chat.offered_skill)
            finally:
                chat.shutdown()
