import base64
import json
import subprocess
import sys
import tempfile
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
