import hashlib
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

from tests.support.fixtures import HarnessCase, stub_syntax_diagnostics, prepare_fixture
from migration_harness.junie import acp_session, junie_cli
from migration_harness.runs import run_records
from migration_harness.runs.editor_queue import add_prompt, editor_queue
from migration_harness import workflow
from migration_harness import execution
from migration_harness.context import migration_materials
from migration_harness.junie import migration_session
from migration_harness.verification import build_cycle, repairs
from migration_harness.web.diff_viewer import capture_workspace


class RazorTargetConfigTest(HarnessCase):
    def test_requires_counterpart_in_same_directory(self):
        for config, missing in [
            ('target = "Pages/Menu.cshtml"', "Pages/Menu.cshtml.cs"),
            ('target = "Pages/Menu.cshtml.cs"', "Pages/Menu.cshtml"),
            ('targets = ["Pages/Menu.cshtml", "Pages/Other.cshtml.cs"]', "Pages/Menu.cshtml.cs"),
            ('targets = ["Pages/Menu.cshtml", "Other/Menu.cshtml.cs"]', "Pages/Menu.cshtml.cs"),
        ]:
            with self.subTest(config=config), self.assertRaises(ValueError) as invalid:
                self.config(config)
            self.assertIn(missing, str(invalid.exception))
        self.assertEqual(self.config('target = "Common.cs"')["units"][0]["target"], "Common.cs")
        self.assertEqual(
            self.config('targets = ["Pages/Menu.cshtml", "Pages/Menu.cshtml.cs"]')["units"][0]["targets"],
            ["Pages/Menu.cshtml", "Pages/Menu.cshtml.cs"],
        )


class MultipleTargetsTest(HarnessCase):
    """複数のRazorファイルの反映、中断、承認を一つのRunで検証する。"""

    targets = ("Page.cshtml.cs", "Page.cshtml")

    def setUp(self):
        super().setUp()
        self.enterContext(stub_syntax_diagnostics())
        (self.project / "source.php").write_text("<?php echo 1;\n", encoding="utf-8")
        for name in self.targets:
            (self.project / name).write_text(f"original {name}", encoding="utf-8")
        self.commit_project()
        self.settings = self.config('targets = ["Page.cshtml.cs", "Page.cshtml"]')
        self.prepared = prepare_fixture(self.settings, "sample")
        self.run_path = Path(self.prepared["run_path"])
        self.worktree = Path(self.prepared["worktree_path"])
        self.record_path = self.run_path / "run.json"

    def record(self):
        """移行先の復旧状況を、ディスク上のRun記録から確認する。"""
        return json.loads(self.record_path.read_bytes())

    def edit_with_junie(self, *, complete=True, outside=False, scratch=False):
        def run(args, completion, token, targets, *, report_usage=None, prompts=None):
            self.assertEqual(tuple(path.name for path in targets), self.targets)
            self.assertIn("extract the exact ordered data mechanically from the assigned PHP lines", args[-1])
            self.assertIn("including both values and order", args[-1])
            self.assertIn("Never add an explicit route to a bare @page", args[-1])
            self.assertIn("fully qualified type in @model", args[-1])
            self.assertIn("Razor encodes that expression once", args[-1])
            self.assertIn(migration_materials.CSHARP_IDENTIFIERS, args[-1])
            self.assertIn("The harness validates UTF-8 and adds the BOM", args[-1])
            self.assertIn("do not run encoding or BOM checks or create a hex dump", args[-1])
            self.assertNotIn("Put temporary extraction scripts", args[-1])
            for path in targets:
                path.write_text(f"migrated {path.name}", encoding="utf-8")
            if outside:
                (completion.parent / "unlisted.cs").write_text("unlisted", encoding="utf-8")
            if scratch:
                (completion.parent / ".migration-scratch/values.json").write_text("[1, 2]")
            if complete:
                completion.write_text(token, encoding="ascii")
            return 0
        return run

    def resume(self, run):
        """対話端末だけを差し替えて通常の再開経路を呼ぶ。"""
        with patch.object(junie_cli, "require_interactive_cli"), patch.object(junie_cli, "run_interactive", run):
            return workflow.resume_with_junie(self.settings, self.prepared["run_id"])

    def queue_prompt(self, text):
        with editor_queue(self.run_path) as queue:
            add_prompt(queue, {"text": text})

    def assert_prompts(self, *expected):
        with editor_queue(self.run_path) as queue:
            self.assertEqual([(item["text"], item["status"]) for item in queue["prompts"]], list(expected))

    def assert_supplied(self, command, *instructions):
        self.assertIn("Operator follow-up for this migration", command[-1])
        guidelines = Path(command[command.index("--ide-guidelines") + 1]).read_text(encoding="utf-8")
        for instruction in instructions:
            self.assertIn(instruction, guidelines)

    def test_outputs_keep_utf8_bom_newlines_and_recovery_hashes_on_repeated_application(self):
        contents = {
            self.targets[0]: '// 日本語😀\r\nclass Page {}\r\n'.encode("utf-8"),
            self.targets[1]: '@page\n<p>日本語😀</p>\r'.encode("utf-8-sig"),
        }

        def junie(args, completion, token, targets, **kwargs):
            for path in targets:
                path.write_bytes(contents[path.name])
            return 0

        self.resume(junie)
        expected = {name: data.decode("utf-8-sig").encode("utf-8-sig") for name, data in contents.items()}
        for _ in range(2):
            record = self.record()
            for name, data in expected.items():
                self.assertEqual((self.worktree / name).read_bytes(), data)
                self.assertEqual((self.run_path / "agent-workspace" / name).read_bytes(), data)
                self.assertEqual(record["target_hashes"][name], hashlib.sha256(data).hexdigest())
            self.resume(lambda *args, **kwargs: 0)

    def test_non_utf8_output_is_rejected_before_any_worktree_file_is_replaced(self):
        def junie(args, completion, token, targets, **kwargs):
            targets[0].write_bytes(b"class Page {}\r\n")
            targets[1].write_bytes("日本語".encode("cp932"))
            completion.write_text(token)
            return 0

        with self.assertRaisesRegex(ValueError, "UTF-8"):
            self.resume(junie)
        for name in self.targets:
            self.assertEqual((self.worktree / name).read_bytes(), f"original {name}".encode("utf-8"))
        self.assertNotIn("target_apply_pending", self.record())

    def test_retries_staged_and_live_prompts_after_rejecting_non_utf8_output(self):
        staged = "UNIQUE_FOLLOWUP_RETAIN_AUDIT_ID"
        live = "UNIQUE_LIVE_FOLLOWUP_RETAIN_DISPLAY_NAME"
        self.queue_prompt(staged)

        def invalid_output(args, completion, token, targets, *, prompts, **kwargs):
            self.assert_supplied(args, staged)
            self.queue_prompt(live)
            identifiers, instruction = prompts.take()
            self.assertEqual(instruction, live)
            prompts.finish(identifiers, True)
            self.assert_prompts((staged, "sending"), (live, "applying"))
            targets[0].write_bytes(b"class Page {}\r\n")
            targets[1].write_bytes("日本語".encode("cp932"))
            completion.write_text(token, encoding="ascii")
            return 0

        with self.assertRaisesRegex(ValueError, "UTF-8"):
            self.resume(invalid_output)
        self.assert_prompts((staged, "queued"), (live, "queued"))
        self.assertEqual(self.record()["completed_capsules"], 0)
        for name in self.targets:
            self.assertEqual((self.worktree / name).read_bytes(), f"original {name}".encode("utf-8"))

        def accept_output(args, *args_tail, **kwargs):
            self.assert_supplied(args, staged, live)
            self.assert_prompts((staged, "sending"), (live, "sending"))
            return self.edit_with_junie()(args, *args_tail, **kwargs)

        with patch.object(workflow, "_verify_worktree", side_effect=lambda _config, _unit, result, _record: result):
            self.assertEqual(self.resume(accept_output)["completed_capsules"], 1)
        self.assert_prompts((staged, "sent"), (live, "sent"))
        for name in self.targets:
            self.assertEqual((self.worktree / name).read_bytes(), f"migrated {name}".encode("utf-8-sig"))

    def test_bomless_utf16_output_is_rejected_before_publishing(self):
        def junie(args, completion, token, targets, **kwargs):
            targets[0].write_bytes(b"class Page {}")
            targets[1].write_bytes("<p>Page</p>".encode("utf-16-le"))
            completion.write_text(token)
            return 0

        with self.assertRaisesRegex(ValueError, "not UTF-8 source text .*NUL byte"):
            self.resume(junie)
        for name in self.targets:
            self.assertEqual((self.worktree / name).read_bytes(), f"original {name}".encode("utf-8"))
        self.assertNotIn("target_apply_pending", self.record())

    # 中断時の編集が残り、再開後に両方のファイルを比較できることを検証する。
    def test_partial_edits_are_kept_and_both_targets_are_compared(self):
        from migration_harness.junie import acp_session, junie_cli

        def cancelled_while_editing(args, *rest, **kwargs):
            self.assertIn("reread the target and make a fresh, smaller patch", args[-1])
            session = acp_session.AcpSession.__new__(acp_session.AcpSession)
            session.steering_waiting = None
            session.steering_new_turn = False
            self.assertTrue(session._advance_prompt(3, {"result": {"stopReason": "cancelled"}}))
            return self.edit_with_junie(complete=False)(args, *rest, **kwargs)

        paused = self.resume(cancelled_while_editing)
        self.assertEqual(paused["status"], "interrupted")
        self.assertEqual(paused["completed_capsules"], 0)
        self.assertEqual(self.record()["junie_exit_code"], 0)
        self.assertIn("Junie ACP stopped: cancelled.", self.record()["error"])
        with patch.object(workflow, "_verify_worktree", wraps=lambda _config, _unit, result, _record: result) as verify:
            done = self.resume(self.edit_with_junie())
        verify.assert_called_once()
        self.assertEqual(done["completed_capsules"], 1)
        self.assertEqual(set(self.record()["target_hashes"]), set(self.targets))
        _, snapshot = capture_workspace(self.settings, self.prepared["run_id"])
        self.assertEqual([segment["fileName"] for segment in snapshot["workspace"]["rightSegments"]], list(self.targets))

    # 主移行先以外の外部編集も検出して上書きを防ぐことを検証する。
    def test_rejects_worktree_edits_to_second_file_before_resuming(self):
        self.resume(self.edit_with_junie(complete=False))
        (self.worktree / self.targets[1]).write_text("someone else's edits", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "worktree targets changed"):
            self.resume(self.edit_with_junie())

    # 許可外の変更があれば、許可済みファイルも反映する前に停止することを検証する。
    def test_rejects_changes_to_unlisted_files_before_publishing_any_target(self):
        with self.assertRaisesRegex(ValueError, "outside the configured targets: 'unlisted[.]cs'"):
            self.resume(self.edit_with_junie(outside=True))
        self.assertIn("unlisted.cs", self.record()["error"])
        for name in self.targets:
            self.assertEqual((self.worktree / name).read_text(encoding="utf-8"), f"original {name}")
        self.assertEqual(self.record()["completed_capsules"], 0)

    def test_scratch_files_are_allowed_but_never_published(self):
        with patch.object(workflow, "_verify_worktree", wraps=lambda _config, _unit, result, _record: result):
            result = self.resume(self.edit_with_junie(scratch=True))
        self.assertEqual(result["completed_capsules"], 1)
        self.assertTrue((self.run_path / "agent-workspace/.migration-scratch/values.json").is_file())
        self.assertFalse((self.worktree / ".migration-scratch").exists())
        for name in self.targets:
            self.assertEqual((self.worktree / name).read_bytes(), f"migrated {name}".encode("utf-8-sig"))

    def test_scratch_does_not_allow_symlinks_or_hide_changes_to_materials(self):
        agent = self.run_path / "agent-workspace"
        (agent / ".migration-scratch").mkdir(parents=True)
        material = agent / ".migration-materials/rule.txt"
        material.parent.mkdir()
        material.write_text("original")
        before = migration_session.unlisted_workspace_files(agent, set())
        material.write_text("changed")
        self.assertNotEqual(migration_session.unlisted_workspace_files(agent, set()), before)
        link = agent / ".migration-scratch/link"
        try:
            link.symlink_to(material)
        except OSError:
            self.skipTest("symlinks unavailable on this host")
        with self.assertRaisesRegex(ValueError, "must not be symbolic links"):
            migration_session.unlisted_workspace_files(agent, set())

    # 途中の反映を復旧したうえで、カプセルの完了を改めて判定することを検証する。
    def test_recovers_partial_application_without_counting_the_capsule(self):
        instruction = "UNIQUE_FOLLOWUP_RETAIN_VALIDATION"
        self.queue_prompt(instruction)
        real_replace = Path.replace

        def fail_second_file(source, target):
            """二つ目の移行先を置き換える箇所でだけ障害を起こす。"""
            if Path(target) == self.worktree / self.targets[1]:
                raise OSError("fixture failure applying second target")
            return real_replace(source, target)

        with patch.object(Path, "replace", fail_second_file), self.assertRaisesRegex(OSError, "fixture failure"):
            self.resume(self.edit_with_junie())
        self.assertIn("target_apply_pending", self.record())
        self.assertEqual(self.record()["completed_capsules"], 0)
        self.assert_prompts((instruction, "queued"))
        self.assertEqual((self.worktree / self.targets[0]).read_bytes(), f"migrated {self.targets[0]}".encode("utf-8-sig"))
        self.assertEqual((self.worktree / self.targets[1]).read_text(encoding="utf-8"), f"original {self.targets[1]}")

        def resume_junie(args, completion, token, targets, *, report_usage=None, prompts=None):
            """Junieの再開前に二つ目まで復旧していることを確認する。"""
            self.assertEqual((self.worktree / self.targets[1]).read_bytes(), f"migrated {self.targets[1]}".encode("utf-8-sig"))
            self.assertNotIn("target_apply_pending", self.record())
            self.assert_supplied(args, instruction)
            self.assert_prompts((instruction, "sending"))
            return self.edit_with_junie()(args, completion, token, targets)

        with patch.object(workflow, "_verify_worktree", wraps=lambda _config, _unit, result, _record: result):
            self.resume(resume_junie)
        self.assertNotIn("target_apply_pending", self.record())
        self.assertEqual(self.record()["completed_capsules"], 1)
        self.assert_prompts((instruction, "sent"))

    # 反映失敗後の外部編集を、復旧記録で上書きしないことを検証する。
    def test_failed_application_does_not_overwrite_an_external_edit(self):
        real_replace = Path.replace

        def fail_second_file(source, target):
            """二つ目の移行先を置き換える箇所でだけ障害を起こす。"""
            if Path(target) == self.worktree / self.targets[1]:
                raise OSError("fixture failure applying second target")
            return real_replace(source, target)

        with patch.object(Path, "replace", fail_second_file), self.assertRaises(OSError):
            self.resume(self.edit_with_junie())
        other = self.worktree / self.targets[1]
        other.write_text("external edit", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "external change"):
            self.resume(self.edit_with_junie())
        self.assertEqual(other.read_text(encoding="utf-8"), "external edit")

    # 片方を編集しても、もう一方の指摘の承認は有効なことを検証する。
    def test_acceptance_is_linked_to_each_reviewer_file(self):
        self.resume(self.edit_with_junie(complete=False))
        record = self.record()
        hashes = record["target_hashes"]
        record.update(status="checker_needs_review", checker_exit_code=1, review_file_hashes=hashes)
        run_records.write_run_record(self.record_path, record)
        (self.run_path / "checker.log").write_text(
            "".join(f"RZ1 {name}:1 warning: sample\n" for name in self.targets), encoding="utf-8",
        )
        run_id = self.prepared["run_id"]
        report = workflow.review_checker(self.settings, run_id, None, None)
        self.assertEqual(len(report["findings"]), 2)
        for finding_id, _, _ in report["findings"]:
            workflow.review_checker(self.settings, run_id, finding_id, "Fixture decision")
        self.assertTrue(all(accepted for _, _, accepted in workflow.review_checker(self.settings, run_id, None, None)["findings"]))
        (self.worktree / self.targets[1]).write_text("external edit", encoding="utf-8")
        report = workflow.review_checker(self.settings, run_id, None, None)
        self.assertEqual(sum(accepted for _, _, accepted in report["findings"]), 1)

    def test_razor_model_error_repairs_only_the_matching_source_page(self):
        model, page = map(Path, self.targets)
        project = self.worktree / "App.csproj"
        generator = self.worktree / "obj" / "Debug" / build_cycle._RAZOR_GENERATOR
        diagnostic = f"(2,8): error CS0246: Missing PageModel [{project}]\n"
        actual = f"{self.worktree / page}{diagnostic}"
        duplicate = f"{generator / 'Page_cshtml.g.cs'}{diagnostic}"
        unrelated = f"{generator / 'Other_cshtml.g.cs'}{diagnostic}"
        record = {"build_exit_code": 1, "repair_history": []}
        with patch.object(junie_cli, "require_interactive_cli"), patch.object(repairs, "attempt_repair") as repair:
            log = self.run_path / "build.log"
            for contents in (duplicate, unrelated + actual):
                log.write_text(contents, encoding="utf-8")
                self.assertFalse(build_cycle.repair_build_failure(
                    self.settings, (model, page), self.worktree, self.run_path, record,
                ))
            repair.assert_not_called()
            log.write_text(duplicate + actual, encoding="utf-8")
            self.assertTrue(build_cycle.repair_build_failure(
                self.settings, (model, page), self.worktree, self.run_path, record,
            ))
            repair.assert_called_once()
            self.assertEqual(repair.call_args.args[2], (page,))
            self.assertEqual(repair.call_args.args[5], f"{page}(2,8): CS0246: Missing PageModel")

    def test_razor_symbolic_paths_require_an_unambiguous_matching_diagnostic(self):
        project = self.worktree / "App.csproj"
        generator = self.worktree / "obj" / "Debug" / build_cycle._RAZOR_GENERATOR
        diagnostic = f"(2,8): error CS0246: Missing PageModel [{project}]\n"
        record = {"build_exit_code": 1, "repair_history": []}
        log = self.run_path / "build.log"
        for filename, generated_name in (
            ("Pages/Ordered-Items.cshtml", "Pages/Ordered_Items_cshtml.g.cs"),
            ("Pages/Ordered.Items.cshtml", "Pages/Ordered_Items_cshtml.g.cs"),
            ("Pages/Admin-v2/OrderedItems.cshtml", "Pages/Admin_v2/OrderedItems_cshtml.g.cs"),
        ):
            page = Path(filename)
            targets = (page, Path(filename + ".cs"))
            source = f"{self.worktree / page}{diagnostic}"
            generated = f"{generator / generated_name}{diagnostic}"
            log.write_text(source + generated, encoding="utf-8")
            with self.subTest(page=page), patch.object(junie_cli, "require_interactive_cli"), patch.object(
                repairs, "attempt_repair",
            ) as repair:
                repair_build = build_cycle.repair_build_failure
                self.assertTrue(repair_build(self.settings, targets, self.worktree, self.run_path, record))
                self.assertEqual(repair.call_args.args[2], (page,))
                repair.reset_mock()
                collision = Path(generated_name.removesuffix("_cshtml.g.cs") + ".cshtml")
                self.assertFalse(repair_build(
                    self.settings, (*targets, collision, Path(f"{collision}.cs")),
                    self.worktree, self.run_path, record,
                ))
                log.write_text(source + generated.replace("App.csproj", "Other.csproj"), encoding="utf-8")
                self.assertFalse(repair_build(self.settings, targets, self.worktree, self.run_path, record))
                repair.assert_not_called()

    # 手修正後の再開で両ファイルを同期し、参照専用の変更は反映前に検出する。
    def test_repair_syncs_both_files_and_rejects_reference_changes(self):
        self.resume(self.edit_with_junie(complete=False))
        model_name, page_name = self.targets
        case_record = self.record()
        case_record.update(status="checker_needs_review", build_exit_code=0, completed_capsules=1)
        self.settings.update(checker_command=["fixture-checker"], max_repair_attempts=1)
        model, page = self.worktree / model_name, self.worktree / page_name
        agent = self.run_path / "agent-workspace"
        run_process = execution.run_process

        def run_checker(command, **kwargs):
            """ビューが未修正のときだけ、そのファイルに指摘を出す。"""
            if command[0] == "git":
                return run_process(command, **kwargs)
            if command[0] == "fixture-checker" and page.read_text(encoding="utf-8-sig") != "repaired using NewName":
                kwargs["stdout"].write(f"RZ1 {page_name}:1 warning: use NewName\n".encode())
                return subprocess.CompletedProcess(command, 1)
            return subprocess.CompletedProcess(command, 0)

        for case in ("success", "junie_edits_reference", "worktree_reference_changes", "missing_reference"):
            with self.subTest(case=case):
                run_records.write_run_record(self.record_path, case_record)
                model.write_bytes(b"latest model with NewName")
                page.write_bytes(b"latest view before repair")
                (agent / model_name).write_bytes(b"stale model with OldName")
                (agent / page_name).write_bytes(b"stale view")
                if case == "missing_reference":
                    model.unlink()

                def repair(command, completion, token, targets, *, report_usage=None, prompts=None):
                    """Junieの起動直前に参照用の内容と、実際に編集するファイルを確認する。"""
                    target, = targets
                    self.assertEqual(target, agent / page_name)
                    self.assertIn(f"repair only `{page_name}`", command[-1])
                    self.assertIn("all saved `.migration-materials/source-*.md` files in capsule order", command[-1])
                    self.assertIn("extract the exact ordered data mechanically", command[-1])
                    self.assertIn("fully qualified type in @model", command[-1])
                    self.assertIn("Razor encodes that expression once", command[-1])
                    self.assertIn(migration_materials.CSHARP_IDENTIFIERS, command[-1])
                    self.assertIn("The harness validates UTF-8 and adds the BOM", command[-1])
                    self.assertEqual(target.read_bytes(), b"latest view before repair")
                    self.assertEqual((agent / model_name).read_bytes(), b"latest model with NewName")
                    target.write_bytes(b"repaired using NewName")
                    if case == "junie_edits_reference":
                        (agent / model_name).write_bytes(b"unexpected agent edit")
                    if case == "worktree_reference_changes":
                        model.write_bytes(b"external edit during repair")
                    completion.write_text(token, encoding="ascii")
                    return 0

                with patch.object(execution, "run_process", side_effect=run_checker):
                    result = self.resume(repair)
                if case == "success":
                    self.assertEqual(result["status"], "checker_succeeded")
                    self.assertEqual(page.read_bytes(), "repaired using NewName".encode("utf-8-sig"))
                    self.assertEqual(model.read_bytes(), b"latest model with NewName")
                else:
                    self.assertEqual(result["status"], "checker_needs_review")
                    self.assertEqual(page.read_bytes(), b"latest view before repair")
                    if case == "junie_edits_reference":
                        self.assertIn("outside the repair targets", result["error"])
                        self.assertEqual(model.read_bytes(), b"latest model with NewName")
                    elif case == "worktree_reference_changes":
                        self.assertIn("worktree target changed during Junie execution", result["error"])
                        self.assertEqual(model.read_bytes(), b"external edit during repair")
                    else:
                        self.assertIn(model_name, result["error"])
                        self.assertFalse(model.exists())

    def test_repair_resumes_with_the_unpublished_draft(self):
        self.resume(self.edit_with_junie(complete=False))
        _, page = self.targets
        record = self.record()
        record.update(status="checker_needs_review", build_exit_code=0, completed_capsules=1)
        run_records.write_run_record(self.record_path, record)
        self.settings.update(checker_command=["fixture-checker"], max_repair_attempts=1)

        run_process = execution.run_process

        def check(command, **kwargs):
            if command[0] == "git":
                return run_process(command, **kwargs)
            if command[0] == "fixture-checker" and (Path(kwargs["cwd"]) / page).read_text(encoding="utf-8-sig") != "ready":
                kwargs["stdout"].write(f"RZ1 {page}:1 error: fix output\n".encode())
                return subprocess.CompletedProcess(command, 1)
            return subprocess.CompletedProcess(command, 0)

        def interrupt(command, completion, token, targets, *, report_usage=None, prompts=None):
            target, = targets
            target.write_text("draft")
            raise KeyboardInterrupt

        def complete(command, completion, token, targets, *, report_usage=None, prompts=None):
            target, = targets
            self.assertIn("partial edits from an interrupted repair", command[-1])
            self.assertEqual(target.read_text(), "draft")
            target.write_text("ready")
            completion.write_text(token)
            return 0

        with patch.object(execution, "run_process", side_effect=check):
            with self.assertRaises(workflow.RunInterrupted):
                self.resume(interrupt)
            self.assertNotEqual((self.worktree / page).read_text(encoding="utf-8-sig"), "draft")
            self.assertEqual(self.record()["repair_history"], [])
            self.assertEqual(self.resume(complete)["status"], "checker_succeeded")
        self.assertEqual(len(self.record()["repair_history"]), 1)

    def test_junie_startup_failures_do_not_consume_a_repair_attempt(self):
        self.resume(self.edit_with_junie(complete=False))
        page = self.targets[1]
        record = self.record()
        record.update(status="checker_needs_review", build_exit_code=0, completed_capsules=1)
        run_records.write_run_record(self.record_path, record)
        self.settings.update(checker_command=["fixture-checker"], max_repair_attempts=1)
        run_process = execution.run_process

        def check(command, **kwargs):
            if command[0] == "git":
                return run_process(command, **kwargs)
            if command[0] == "fixture-checker" and (self.worktree / page).read_text(encoding="utf-8-sig") != "ready":
                kwargs["stdout"].write(f"RZ1 {page}:1 error: fix output\n".encode())
                return subprocess.CompletedProcess(command, 1)
            return subprocess.CompletedProcess(command, 0)

        failures = [
            junie_cli.JunieLaunchError(["junie"], str(self.worktree), None, FileNotFoundError("missing launcher")),
            junie_cli.JunieSessionError(["junie"], 0, "JLine returned dumb"),
        ]
        with patch.object(execution, "run_process", side_effect=check):
            for failure in failures:
                with self.subTest(error=type(failure).__name__):
                    def fail(*args, **kwargs):
                        raise failure
                    self.assertEqual(self.resume(fail)["status"], "checker_needs_review")
                    self.assertEqual(self.record()["repair_history"], [])
                    self.assertIn("repair_pending", self.record())

            def finish(command, completion, token, targets, **kwargs):
                targets[0].write_text("ready")
                completion.write_text(token)
                return 0
            self.assertEqual(self.resume(finish)["status"], "checker_succeeded")
        self.assertEqual(len(self.record()["repair_history"]), 1)
