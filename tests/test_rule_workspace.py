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
from migration_harness.rules.rule_workspace import RuleWorkspace, _diff
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
    def test_diff_keeps_lines_separate_when_final_newline_is_missing(self):
        diff = _diff("old", "new", "rule.md")
        self.assertIn("-old\n\\ No newline at end of file\n+new\n\\ No newline at end of file\n", diff)

    def test_model_selection_is_saved_and_restarts_the_next_chat(self):
        sessions = []

        class FakeChat:
            offered_skill = False

            def __init__(self, project, model, effort):
                self.project = project
                self.closed = False
                sessions.append((model, effort, self))

            def begin(self, _):
                pass

            def wait(self, *, ready=False, **_):
                return "" if ready else json.dumps({"message": "確認しました", "unresolved": []})

            def shutdown(self):
                self.closed = True

        draft = self.workspace.start()
        with patch("migration_harness.rules.rule_workspace.RuleChat", FakeChat):
            draft = self.send(draft, "model", model="gpt", effort="medium")
            draft = self.send(draft, "message", text="確認")
            if self.workspace.worker:
                self.workspace.worker.join(3)
            draft = self.workspace.snapshot(draft["id"])
            self.assertEqual((draft["junie_model"], draft["junie_effort"]), ("gpt", "medium"))
            draft = self.send(draft, "model", model="sonnet", effort="high")
            self.assertTrue(sessions[0][2].closed)
            self.send(draft, "message", text="続けて確認")
            if self.workspace.worker:
                self.workspace.worker.join(3)
        self.assertEqual([(model, effort) for model, effort, _ in sessions], [("gpt", "medium"), ("sonnet", "high")])

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

    def test_failed_final_state_write_never_exposes_new_markdown_at_the_old_revision(self):
        original_toml = self.config_path.read_bytes()
        original_rules = (self.app / "original.md").read_bytes()
        replace = Path.replace
        changed = "修正したルール\n例外条件\n"
        for kind in ("edit", "accept", "add"):
            with self.subTest(kind=kind):
                draft = self.workspace.start()
                root = self.workspace.draft(draft["id"])
                state_file = root / "state.json"
                registered = draft["files"][0]["source"]
                source = "rule-0002.md" if kind == "add" else registered
                plan = {"format_version": 2, "documents": [{"path": "Agent.md", "title": "Fixture",
                    "parts": [{"source": registered, "start": 1, "end": 2}]}]}
                stored = json.loads(state_file.read_bytes())
                stored.update(plan=plan, proposal={"source": source, "text": changed})
                state_file.write_text(json.dumps(stored), encoding="utf-8")
                stale = self.send(draft, "preview")
                previous = stale["preview"]["token"]
                final_bytes = changed.replace("\n", "\r\n").encode("utf-8") if kind != "add" else changed.encode("utf-8")

                def fail_state(candidate, destination):
                    if destination == state_file:
                        raise PermissionError("fixture: final state replacement")
                    return replace(candidate, destination)

                write_kind = "edit" if kind == "add" else kind
                fields = {"source": "new", "name": "more.md", "text": changed} if kind == "add" else (
                    {"source": registered, "text": changed} if kind == "edit" else {}
                )
                reopened = RuleWorkspace(self.config)
                self.addCleanup(reopened.close)

                def after_restart(action, **kwargs):
                    return reopened.action({"id": stale["id"], "revision": stale["revision"],
                        "action": action, **kwargs}, lambda *_: self.fail("stale configuration could be applied"))

                with patch.object(Path, "replace", fail_state):
                    with self.assertRaisesRegex(PermissionError, "final state replacement"):
                        self.send(stale, write_kind, **fields)
                    self.assertEqual((root / "sources" / source).read_bytes(), final_bytes)
                    with self.assertRaisesRegex(PermissionError, "final state replacement"):
                        reopened.file_text(draft["id"], source)
                    for action in ("edit", "preview", "apply"):
                        with self.assertRaisesRegex(PermissionError, "final state replacement"):
                            after_restart(action, source=registered, text="別画面の旧版", confirmed=True, preview=previous)
                    self.assertEqual((root / "sources" / source).read_bytes(), final_bytes)

                for action in ("edit", "accept", "preview", "apply"):
                    with self.assertRaisesRegex(ValueError, "更新済み"):
                        after_restart(action, source=registered, text="別画面の旧版", confirmed=True, preview=previous)
                recovered = reopened.snapshot(draft["id"])
                self.assertEqual(recovered["revision"], stale["revision"] + 1)
                self.assertEqual(reopened.file_text(draft["id"], source)["text"], final_bytes.decode("utf-8"))
                self.assertEqual(len(recovered["files"]), 2 if kind == "add" else 1)
                for key in ("plan", "preview", "proposal"):
                    self.assertNotIn(key, recovered)
                self.assertTrue(self.send(recovered, "preview")["preview"]["token"])
                self.assertEqual(self.config_path.read_bytes(), original_toml)
                self.assertEqual((self.app / "original.md").read_bytes(), original_rules)

    def test_body_replace_failure_recovers_the_original_revision(self):
        draft = self.workspace.start()
        name = draft["files"][0]["source"]
        file = self.workspace.draft(draft["id"]) / "sources" / name
        original = file.read_bytes()
        replace = Path.replace

        def fail_body(candidate, destination):
            if destination == file:
                raise PermissionError("fixture: body replacement")
            return replace(candidate, destination)

        with patch.object(Path, "replace", fail_body):
            with self.assertRaisesRegex(PermissionError, "body replacement"):
                self.send(draft, "edit", source=name, text="未反映の本文")
        reopened = RuleWorkspace(self.config)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.file_text(draft["id"], name), {"text": original.decode(), "revision": draft["revision"]})
        self.assertEqual(self.send(draft, "edit", source=name, text="次の正常な本文")["revision"], draft["revision"] + 1)

    def test_interruption_after_state_commit_does_not_increment_twice(self):
        draft = self.workspace.start()
        root = self.workspace.draft(draft["id"])
        source = draft["files"][0]["source"]
        replace = Path.replace

        def interrupt_after_state(candidate, destination):
            result = replace(candidate, destination)
            if destination == root / "state.json":
                raise PermissionError("fixture: state already replaced")
            return result

        with patch.object(Path, "replace", interrupt_after_state):
            with self.assertRaisesRegex(PermissionError, "state already replaced"):
                self.send(draft, "edit", source=source, text="確定済みの本文")
        reopened = RuleWorkspace(self.config)
        self.addCleanup(reopened.close)
        expected = {"text": "確定済みの本文", "revision": draft["revision"] + 1}
        for _ in range(2):
            self.assertEqual(reopened.file_text(draft["id"], source), expected)
        self.assertFalse((root / "edit-pending.json").exists())

    def test_unexpected_body_during_recovery_is_not_overwritten_or_published(self):
        draft = self.workspace.start()
        root = self.workspace.draft(draft["id"])
        state_file = root / "state.json"
        source = draft["files"][0]["source"]
        body = root / "sources" / source
        replace = Path.replace

        def fail_state(candidate, destination):
            if destination == state_file:
                raise PermissionError("fixture: final state replacement")
            return replace(candidate, destination)

        with patch.object(Path, "replace", fail_state):
            with self.assertRaisesRegex(PermissionError, "final state replacement"):
                self.send(draft, "edit", source=source, text="保存した本文")
        unexpected = b"unexpected fixture content"
        body.write_bytes(unexpected)
        before = state_file.read_bytes()
        reopened = RuleWorkspace(self.config)
        self.addCleanup(reopened.close)
        with self.assertRaisesRegex(ValueError, "本文と保存記録が一致しません"):
            reopened.file_text(draft["id"], source)
        with self.assertRaisesRegex(ValueError, "本文と保存記録が一致しません"):
            self.send(draft, "preview")
        self.assertEqual(body.read_bytes(), unexpected)
        self.assertEqual(state_file.read_bytes(), before)

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

    def test_duplicate_chat_keys_hold_preview_and_apply_until_valid_review(self):
        original = "タイムアウトは30秒\r\nタイムアウトは60秒\r\n".encode("utf-8")
        source = self.app / "original.md"
        source.write_bytes(original)
        toml = self.config_path.read_bytes()
        outstanding = "タイムアウトの優先順位が未確定"
        duplicate_unresolved = (
            '{"message":"ambiguous","unresolved":["' + outstanding + '"],"unresolved":[],'
            '"edit":{"source":"rule-0001.md","text":"Drop the original rules."}}'
        )
        replies = iter((
            duplicate_unresolved,
            json.dumps({"message": outstanding, "unresolved": [outstanding]}, ensure_ascii=False),
            duplicate_unresolved,
            '```json\r\n{"message":"ambiguous","unresolved":[],"edit":'
            '{"source":"rule-0001.md","text":"first","te\\u0078t":"second"}}\r\n```',
            '{"message":"ambiguous","unresolved":[],"plan":{"format_version":2,'
            '"documents":[{"path":"Agent.md","path":"details/wrong.md"}]}}',
            '```json\r\n' + json.dumps(
                {"message": "確認\u2028完了\u2029です\u0085。", "unresolved": []}, ensure_ascii=False,
            ) + '\r\n```',
        ))

        class FakeChat:
            def __init__(self, project, *_):
                self.project = project

            def begin(self, _):
                pass

            def wait(self, *, ready=False, **_):
                return "" if ready else next(replies)

            def shutdown(self):
                pass

        def ask(draft):
            draft = self.send(draft, "message", text="優先順位を確認")
            worker = self.workspace.worker
            if worker:
                worker.join(5)
            self.assertIsNone(self.workspace.worker)
            return RuleWorkspace(self.config).snapshot(draft["id"])

        def assert_held(state):
            for key in ("preview", "proposal", "plan", "applied"):
                self.assertNotIn(key, state)
            with self.assertRaisesRegex(ValueError, "未解決"):
                self.send(state, "preview")
            with self.assertRaisesRegex(ValueError, "確認して"):
                self.send(state, "apply", confirmed=True, preview=old_token)
            self.assertEqual(self.config_path.read_bytes(), toml)
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(draft_file.read_bytes(), original)

        draft = self.workspace.start()
        draft_file = self.workspace.draft(draft["id"]) / "sources" / draft["files"][0]["source"]
        old_token = self.send(draft, "preview")["preview"]["token"]
        with patch("migration_harness.rules.rule_workspace.RuleChat", FakeChat):
            state = ask(draft)
            self.assertTrue(any("重複" in item for item in state["unresolved"]))
            assert_held(state)
            with self.assertRaises(ValueError):
                self.send(state, "accept")
            state = ask(state)
            self.assertEqual(state["unresolved"], [outstanding])
            assert_held(state)
            for duplicate in ("unresolved", "edit", "plan"):
                with self.subTest(duplicate=duplicate):
                    state = ask(state)
                    self.assertIn(outstanding, state["unresolved"])
                    assert_held(state)
                    self.assertTrue(any("重複" in item for item in state["unresolved"]))
                    self.assertFalse(any(item["text"] == "ambiguous" for item in state["messages"]))
            state = ask(state)

        self.assertEqual(state["unresolved"], [])
        self.assertEqual(state["messages"][-1]["text"], "確認\u2028完了\u2029です\u0085。")
        with self.assertRaisesRegex(ValueError, "再確認"):
            self.send(state, "apply", confirmed=True, preview=old_token)
        self.assertEqual(self.config_path.read_bytes(), toml)
        self.assertTrue(self.apply(state)["applied"])
        self.assertNotEqual(self.config_path.read_bytes(), toml)
        self.assertEqual(source.read_bytes(), original)

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
