import base64
import ctypes
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from itertools import product
from pathlib import Path
from threading import Event
from unittest.mock import patch

from migration_harness.config import load_config
from migration_harness.files import sha256_file
from migration_harness.junie.reply_text import unwrap_json_fence
from migration_harness.web.dashboard import _save_settings
from migration_harness.rules.rule_workspace import RuleWorkspace
from migration_harness.rules.rule_chat import RuleChat, decode_reply


class RuleReplyTests(unittest.TestCase):
    def test_unwrap_only_removes_the_fence_lines(self):
        body = ' \t{\r\n  "message": "one\u2028two\u2029three\u0085four"\n}\r\n '
        self.assertEqual(unwrap_json_fence(body), body)
        for end in ("\n", "\r\n", "\r"):
            with self.subTest(end=repr(end)):
                self.assertEqual(unwrap_json_fence(end.join(("```json", body, "```"))), body)

    def test_reply_preserves_unicode_separators_inside_the_json(self):
        for separator in ("\u2028", "\u2029", "\u0085"):
            value = f"前{separator}後"
            payload = {
                "message": value, "unresolved": [value],
                "edit": {"source": "new", "name": "new.md", "text": value + "\r\n"},
                "plan": {"format_version": 2, "documents": [{"title": value}]},
            }
            payload_text = json.dumps(payload, ensure_ascii=False, indent=2)
            for newline in ("\n", "\r\n", "\r"):
                body = payload_text.replace("\n", newline)
                for prefix, fence in product(("", "\ufeff"), (None, "```", "```json")):
                    text = body if fence is None else newline.join((fence, body, "```"))
                    with self.subTest(separator=ascii(separator), newline=ascii(newline), bom=bool(prefix), fence=fence):
                        self.assertEqual(decode_reply(prefix + text), payload)

    def test_plain_or_invalid_replies_are_returned_verbatim(self):
        for text in (
            "  説明です。\u2028続き\r\n", "\ufeff", "[]", '{"message":1}',
            "\ufeff```json\r\n{invalid}\r\n```",
        ):
            with self.subTest(text=text):
                self.assertEqual(decode_reply(text), {"message": text})


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
                                     lambda content, version, verify_origins: _save_settings(
                                         self.config_path, content, version, verify_origins=verify_origins))

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

    def test_changed_registered_source_blocks_preview_and_preserves_edited_draft(self):
        draft = self.workspace.start()
        draft = self.send(draft, "edit", source="rule-0001.md", text="approved draft change\n")
        original_toml = self.config_path.read_bytes()
        original = self.app / "original.md"
        original.write_bytes(b"external change\r\n")
        with self.assertRaisesRegex(ValueError, "登録元"):
            self.send(draft, "preview")
        self.assertEqual(self.config_path.read_bytes(), original_toml)
        self.assertEqual(original.read_bytes(), b"external change\r\n")
        self.assertEqual(self.workspace.file_text(draft["id"], "rule-0001.md")["text"], "approved draft change\r\n")

    def test_changed_source_during_settings_validation_blocks_commit(self):
        draft = self.send(self.workspace.start(), "preview")
        before = self.config_path.read_bytes()
        original = self.app / "original.md"

        def change_original(candidate):
            normalized = load_config(candidate)
            original.write_bytes(b"new obligation")
            return normalized

        with patch("migration_harness.web.dashboard.load_config", side_effect=change_original):
            with self.assertRaisesRegex(ValueError, "登録元"):
                self.send(draft, "apply", confirmed=True, preview=draft["preview"]["token"])
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertEqual(original.read_bytes(), b"new obligation")
        self.assertFalse(self.workspace.snapshot(draft["id"]).get("applied"))

    def test_deleted_or_unreadable_source_blocks_apply_but_touch_is_allowed(self):
        ready = self.send(self.workspace.start(), "preview")
        original = self.app / "original.md"
        data, before = original.read_bytes(), self.config_path.read_bytes()
        token = ready["preview"]["token"]
        original.unlink()
        with self.assertRaisesRegex(ValueError, "登録元"):
            self.send(ready, "apply", confirmed=True, preview=token)
        self.assertEqual(self.config_path.read_bytes(), before)
        original.write_bytes(data)
        with patch("migration_harness.rules.rule_workspace.sha256_file", side_effect=PermissionError):
            with self.assertRaisesRegex(ValueError, "登録元"):
                self.send(ready, "apply", confirmed=True, preview=token)
        self.assertEqual(self.config_path.read_bytes(), before)
        stamp = original.stat().st_mtime_ns + 2_000_000_000
        os.utime(original, ns=(stamp, stamp))
        self.assertTrue(self.send(ready, "apply", confirmed=True, preview=token)["applied"])

    def test_toml_change_during_origin_verification_is_not_overwritten(self):
        ready = self.send(self.workspace.start(), "preview")
        edited = self.config_path.read_bytes() + b"\n# external settings edit\n"

        def edit_toml(original):
            self.config_path.write_bytes(edited)
            return sha256_file(original)

        with patch("migration_harness.rules.rule_workspace.sha256_file", side_effect=edit_toml):
            with self.assertRaisesRegex(ValueError, "設定ファイルが更新"):
                self.send(ready, "apply", confirmed=True, preview=ready["preview"]["token"])
        self.assertEqual(self.config_path.read_bytes(), edited)
        self.assertEqual(self.workspace.snapshot(ready["id"])["preview"], ready["preview"])

    def test_managed_pack_checks_the_currently_registered_markdown(self):
        first = self.apply(self.workspace.start())
        second = self.workspace.start()
        before = self.config_path.read_bytes()
        registered = self.workspace.draft(first["id"]) / "published/generated/Agent.md"
        registered.write_bytes(registered.read_bytes() + b"new published obligation\n")
        with self.assertRaisesRegex(ValueError, "登録元"):
            self.send(second, "preview")
        with self.assertRaisesRegex(ValueError, "管理フォルダ外.*設定"):
            self.workspace.start()
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertTrue(registered.read_bytes().endswith(b"new published obligation\n"))
        self.assertEqual(self.workspace.snapshot(second["id"])["files"], second["files"])
        recovered = self.app / "recovered.md"
        recovered.write_bytes(registered.read_bytes())
        content = before.decode().replace(registered.as_posix(), recovered.as_posix())
        _save_settings(self.config_path, content, second["base"])
        fresh = self.workspace.start()
        self.assertIn("new published obligation", self.workspace.file_text(fresh["id"], "rule-0001.md")["text"])

    def test_apply_rejects_different_normalized_root_while_toml_bytes_match(self):
        ready = self.send(self.workspace.start(), "preview")
        before = self.config_path.read_bytes()

        def shifted_root(candidate):
            return {**load_config(candidate), "project_root": str(self.root / "another-worktree")}

        with patch("migration_harness.web.dashboard.load_config", side_effect=shifted_root):
            with self.assertRaisesRegex(ValueError, "設定画面で"):
                self.send(ready, "apply", confirmed=True, preview=ready["preview"]["token"])
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertEqual(self.workspace.snapshot(ready["id"])["preview"], ready["preview"])

    def test_absolute_registered_original_uses_same_project_snapshot(self):
        external = self.root / "external.md"
        external.write_bytes(b"ABSOLUTE_RULE\n")
        content = self.config_path.read_text(encoding="utf-8").replace(
            "'original.md'", json.dumps(external.as_posix()),
        )
        self.config_path.write_text(content, encoding="utf-8")
        draft = self.workspace.start()
        self.assertEqual(self.workspace.file_text(draft["id"], "rule-0001.md")["text"], "ABSOLUTE_RULE\n")
        self.assertTrue(self.apply(draft)["applied"])
        self.assertEqual(external.read_bytes(), b"ABSOLUTE_RULE\n")

    def test_project_root_case_alias_still_uses_the_live_project(self):
        content = self.config_path.read_text(encoding="utf-8").replace("root = 'app'", "root = 'APP'")
        self.config_path.write_text(content, encoding="utf-8")
        self.assertTrue(self.apply(self.workspace.start())["applied"])

    def test_project_root_win32_alias_uses_the_same_directory(self):
        root = self.app.resolve()
        buffer = ctypes.create_unicode_buffer(32768)
        length = ctypes.windll.kernel32.GetShortPathNameW(str(root), buffer, len(buffer))
        # 8.3 名を作らないボリュームでは、同じWin32パスの拡張形式で検証する。
        alias = Path(buffer.value) if 0 < length < len(buffer) else root
        if alias == root:
            alias = Path("//?/" + root.as_posix())
        self.assertNotEqual(alias, root)
        self.assertTrue(alias.samefile(root))
        self.config["project_root"] = str(alias)
        self.assertTrue(self.apply(self.workspace.start())["applied"])

    def test_missing_live_project_root_conflicts_without_a_new_draft(self):
        self.config["project_root"] = str(self.root / "missing")
        with self.assertRaisesRegex(ValueError, "設定画面で"):
            self.workspace.start()
        self.assertFalse(Path(self.config["run_root"]).exists())

    def test_old_draft_without_origin_versions_is_preserved_not_silently_rebased(self):
        draft = self.workspace.start()
        metadata = self.workspace.draft(draft["id"]) / "state.json"
        state = json.loads(metadata.read_bytes())
        del state["origins"]
        metadata.write_text(json.dumps(state), encoding="utf-8")
        before = self.config_path.read_bytes()
        with self.assertRaisesRegex(ValueError, "新しい下書き"):
            self.send(draft, "preview")
        self.assertTrue(metadata.exists())
        self.assertEqual(self.config_path.read_bytes(), before)

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

        from migration_harness.junie import junie_cli

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
        with patch("migration_harness.rules.rule_chat.monotonic", side_effect=[0, 40]), patch(
            "migration_harness.rules.rule_chat.sleep"
        ):
            self.assertEqual(chat.wait(ready=True, cancelled=Event(), publish=lambda *_: None), "")
        self.assertEqual(chat.session_id, "slow-connection")
        self.assertGreater(chat._connection_timeout(), junie_cli.ACP_TIMEOUT)

    def test_chat_proposals_are_not_written_until_accepted(self):
        proposed = "前\u2028中\u2029後\u0085末\r\n"
        live = []
        workspace = self.workspace

        class FakeChat:
            offered_skill = False

            def __init__(self, project, *_):
                self.project = project

            def begin(self, _):
                pass

            def wait(self, *, ready=False, publish, cancelled):
                if ready:
                    return ""
                text = "\ufeff```json\r\n" + json.dumps({"message": "案です", "unresolved": [],
                    "edit": {"source": "new", "name": "chat.md", "text": proposed}}, ensure_ascii=False) + "\r\n```"
                for chunk in (text[:1], text[:20], text):
                    publish(None, chunk)
                    live.append(workspace.live["text"])
                return text

            def shutdown(self):
                pass
        draft = self.workspace.start()
        with patch("migration_harness.rules.rule_workspace.RuleChat", FakeChat):
            self.send(draft, "message", text="文書なしで追加")
            worker = self.workspace.worker
            if worker:
                worker.join(3)
        reopened = RuleWorkspace(self.config).snapshot(draft["id"])
        self.assertEqual(len(reopened["files"]), 1)
        self.assertEqual(reopened["messages"][-1]["text"], "案です")
        self.assertEqual(reopened["unresolved"], [])
        self.assertEqual(reopened["proposal"]["text"], proposed)
        self.assertFalse(any("\\u" in part or "edit" in part or "{" in part for part in live))
        accepted = self.send(reopened, "accept")
        self.assertEqual(len(accepted["files"]), 2)
        self.assertEqual(self.workspace.file_text(draft["id"], accepted["files"][1]["source"])["text"], proposed)
        self.assertTrue(self.send(accepted, "preview")["preview"]["token"])

    def test_stop_between_reply_and_commit_does_not_save_proposal(self):
        entered, release = Event(), Event()
        class FakeChat:
            offered_skill = False

            def __init__(self, project, *_):
                self.project = project

            def begin(self, _):
                pass

            def wait(self, *, ready=False, **_):
                return "" if ready else json.dumps({
                    "message": "Do not save after stop", "unresolved": [],
                    "edit": {"source": "new", "name": "late.md", "text": "rule"},
                })

            def shutdown(self):
                pass

        def paused_decode(text):
            entered.set()
            release.wait(5)
            return decode_reply(text)

        original = self.config_path.read_bytes()
        draft = self.workspace.start()
        with patch("migration_harness.rules.rule_workspace.RuleChat", FakeChat), patch(
            "migration_harness.rules.rule_workspace.decode_reply", paused_decode
        ):
            self.send(draft, "message", text="Stop this turn")
            worker = self.workspace.worker
            try:
                self.assertTrue(entered.wait(3))
                self.send(draft, "stop")
            finally:
                release.set()
                if worker:
                    worker.join(5)
        saved = self.workspace.snapshot(draft["id"])
        self.assertEqual([item["role"] for item in saved["messages"]], ["operator"])
        self.assertNotIn("proposal", saved)
        self.assertIsNone(self.workspace.chat)
        self.assertIsNone(self.workspace.worker)
        self.assertEqual(self.config_path.read_bytes(), original)

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

        with patch("migration_harness.junie.junie_cli._close_junie", side_effect=stop_fixture), patch(
                "migration_harness.junie.junie_cli._launch_process", side_effect=lambda *args, **kwargs: subprocess.Popen(
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
        text=req['params']['prompt'][0]['text']
        frame={'jsonrpc':'2.0','method':'session/update','params':{'sessionId':'same','update':{
            'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':text}}}}
        codec=text.partition(':')[0]
        sys.stdout.buffer.write((json.dumps(frame,ensure_ascii=False)+'\\n').encode(codec))
        sys.stdout.buffer.flush()
        result={'stopReason':'end_turn'}
    else: continue
    print(json.dumps({'id':req['id'],'result':result}),flush=True)
    if m=='session/new':
        print(json.dumps({'method':'session/update','params':{'sessionId':'same','update':{
            'sessionUpdate':'available_commands_update','availableCommands':[{'name':'harness-optimize-rules'}]}}}),flush=True)
"""
        with patch("migration_harness.junie.junie_cli._launch_process", side_effect=lambda *a, **k: subprocess.Popen(
            [sys.executable, "-u", "-c", mock], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )), patch("migration_harness.junie.junie_cli._close_junie", side_effect=lambda process: (
            process.terminate(), process.wait(timeout=3)
        )[-1]):
            chat = RuleChat(self.root, None, None)
            try:
                control = self.workspace.cancelled
                update = lambda *_: None
                chat.wait(ready=True, cancelled=control, publish=update)
                for word in ("utf-8:日本語🍣", "cp932:伝票を表示", "utf-8-sig:再確認"):
                    chat.begin(word)
                    self.assertEqual(chat.wait(cancelled=control, publish=update), word)
                self.assertTrue(chat.offered_skill)
            finally:
                chat.shutdown()
