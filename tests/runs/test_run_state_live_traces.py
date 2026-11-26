from contextlib import ExitStack
from hashlib import sha256
import json
from pathlib import Path
import subprocess
from threading import Event
from unittest.mock import patch

from tests.support.fixtures import HarnessCase, SavedRunFixture, prepare_fixture, stub_syntax_diagnostics
from migration_harness import execution, workflow
from migration_harness.context.source_context import stage_target_context
from migration_harness.junie import junie_cli
from migration_harness.runs import run_records, stage_scripts
from migration_harness.runs.run_manager import RunManager
from migration_harness.runs.worktree_targets import save_target_edit
from migration_harness.web.dashboard import Dashboard
from tests.runs.test_run_state_models import (
    Fence, Hook, HookModel, Run, SlotModel, VerificationModel, hook_actions, slot_actions, verification_actions,
)


def evolve(model, actions, *steps):
    for step in steps:
        model = dict(actions(model))[step]
    return model


class RunQueueTraceTests(HarnessCase):
    def test_stopped_unconfirmed_run_stays_fenced_when_waiting_run_restarts(self):
        (self.project / "source.php").write_text("<?php echo 1;\n", encoding="utf-8")
        self.commit_project()
        config = self.config()
        unit = config["units"][0]
        config["units"] = [
            {**unit, "id": f"unit-{i}", "target": f"Page{i}.cs", "targets": [f"Page{i}.cs"]} for i in range(2)
        ]
        started = [Event(), Event()]
        release = [Event(), Event()]
        launched = []
        builds = []

        def migrate(command, completion, token, targets, **kwargs):
            index = int(targets[0].stem[-1])
            launched.append(index)
            targets[0].write_text(f"class Page{index} {{}}\n", encoding="utf-8")
            started[index].set()
            if not release[index].wait(10):
                raise TimeoutError("test did not release synthetic Junie")
            if index == 0:
                try:
                    execution.check_cancelled()
                except execution.OperationCancelled as stop:
                    raise junie_cli.JunieStopError("synthetic Junie exit was not confirmed") from stop
                raise AssertionError("the test never stopped the first Run")
            completion.write_text(token, encoding="utf-8")
            return 0

        def process(command, *, label, cancellable=True, **kwargs):
            if command[0] == "git":
                return subprocess.run(command, **kwargs)
            self.assertEqual(command[:2], ["dotnet", "build"])
            builds.append(Path(kwargs["cwd"]).name)
            return subprocess.CompletedProcess(command, 0)

        def release_all():
            for event in release:
                event.set()

        def manager(cleanup):
            view = Dashboard()
            view.publish({"status": "idle", "ready": True})
            queue = RunManager(config, view)
            cleanup.callback(queue.close)
            cleanup.callback(release_all)
            return queue

        def observed(queue):
            stored = json.loads(queue.queue_path.read_bytes())
            jobs = {job["run_id"]: job for job in stored["jobs"]}
            phases, evidence, holders = [], [], []
            for index, run_id in enumerate(runs):
                job = jobs.get(run_id, {})
                record_path = queue.queue_path.parent / run_id / "run.json"
                record = json.loads(record_path.read_bytes()) if record_path.exists() else {}
                target = Path(config["worktree_root"]) / run_id / f"Page{index}.cs"
                if run_id in stored.get("blocked_conversations", {}):
                    self.assertTrue(record.get("migration_stop_unconfirmed"), "RUN-DURABLE-FENCE: missing Run record")
                    self.assertFalse(target.exists(), "RUN-UNCONFIRMED: an unconfirmed draft was adopted")
                    phases.append(Run.BLOCK)
                    evidence.append(Fence.SAVED_BLOCK)
                elif job.get("running"):
                    self.assertEqual(record["status"], "junie_running")
                    self.assertFalse(target.exists(), "RUN-ADOPT-BEFORE-EXIT")
                    phases.append(Run.STOP if queue.get(run_id).control.cancelled.is_set() else Run.WORK)
                    evidence.append(Fence.STARTED_JOB)
                    holders.append(index)
                elif job:
                    self.assertFalse(record_path.exists(), "RUN-STARTED-AFTER-RESTART")
                    phases.append(Run.WAIT)
                    evidence.append(Fence.NO)
                else:
                    self.assertEqual(record["status"], "build_succeeded")
                    self.assertEqual(target.read_text(encoding="utf-8-sig"), f"class Page{index} {{}}\n")
                    phases.append(Run.DONE)
                    evidence.append(Fence.NO)
            self.assertEqual(set(queue.workers), {runs[index] for index in holders}, "RUN-OWNER")
            self.assertEqual(list(queue.pending), [runs[i] for i, phase in enumerate(phases) if phase == Run.WAIT])
            return SlotModel(tuple(phases), tuple(evidence), tuple(holders))

        with (
            patch.object(junie_cli, "require_interactive_cli"),
            patch.object(junie_cli, "run_interactive", side_effect=migrate),
            patch.object(execution, "run_process", side_effect=process),
            stub_syntax_diagnostics(),
            ExitStack() as cleanup,
        ):
            queue = manager(cleanup)
            queue.limit = 1
            runs = queue.submit({"action": "run", "unit_ids": ["unit-0", "unit-1"]})["run_ids"]
            self.assertTrue(started[0].wait(8), queue.get(runs[0]).state)
            model = evolve(SlotModel(), slot_actions, "start 0")
            self.assertEqual(observed(queue), model, "RUN-OWNER: start 0")
            with queue.condition:
                queue.limit = 0  # Leave the waiting operation persisted until the new manager starts it.
            view = queue.get(runs[0])
            self.assertTrue(view.request_stop({"operation_id": view.state["operation_id"]}))
            model = evolve(model, slot_actions, "stop 0")
            self.assertEqual(observed(queue), model, "RUN-CAPACITY: start 0 -> stop 0")
            release[0].set()
            with queue.condition:
                self.assertTrue(queue.condition.wait_for(lambda: runs[0] not in queue.jobs, timeout=8))
            model = evolve(model, slot_actions, "unknown exit 0", "persist fence 0")
            self.assertEqual(observed(queue), model, "RUN-DURABLE-FENCE: unknown exit 0 -> persist fence 0")
            queue.close()

            queue = manager(cleanup)
            model = evolve(model, slot_actions, "restart")
            self.assertEqual(observed(queue), model, "RUN-WAITING-LOST: restart")
            with self.assertRaisesRegex(ValueError, "synthetic Junie exit"):
                queue.submit({"action": "resume-run", "run_id": runs[0]})
            queue.start_pending()
            self.assertTrue(started[1].wait(8))
            model = evolve(model, slot_actions, "start 1")
            self.assertEqual(observed(queue), model, "RUN-STARTED-AFTER-RESTART: start 1")
            release[1].set()
            with queue.condition:
                self.assertTrue(queue.condition.wait_for(lambda: not queue.jobs, timeout=8))
            model = evolve(model, slot_actions, "confirmed exit 1", "adopt 1")
            self.assertEqual(observed(queue), model, "RUN-UNCONFIRMED: adopt 1 without adopting 0")
            self.assertEqual(launched, [0, 1])
            self.assertEqual(builds, [runs[1]])


class VerificationTraceTests(HarnessCase):
    def test_rebuild_disqualifies_old_revision_and_requires_checker_or_approval(self):
        original, edited = "class Initial {}\n", "class Edited {}\n"
        (self.project / "source.php").write_text("<?php echo 1;\n", encoding="utf-8")
        (self.project / "Page.cs").write_text(original, encoding="utf-8", newline="\n")
        self.commit_project()
        config = self.config()
        config.update(checker_command=["fixture-checker"], max_repair_attempts=0)
        prepared = prepare_fixture(config, "sample")
        run_id = prepared["run_id"]
        record_path = Path(prepared["run_path"]) / "run.json"
        worktree = Path(prepared["worktree_path"])
        target = worktree / "Page.cs"

        def read_record():
            return json.loads(record_path.read_bytes())

        stage_target_context(worktree, (Path("Page.cs"),), record_path.parent / "capsule",
                             record_path.parent / "agent-workspace", policy={"roots": []})
        run_records.write_run_record(record_path, {
            **read_record(), "completed_capsules": 1, "status": "junie_finished",
            "target_hashes": {"Page.cs": sha256(target.read_bytes()).hexdigest()},
        })
        commands = []
        target_versions = [sha256(target.read_bytes()).hexdigest()]

        def observe(model, label):
            record = read_record()
            version = target_versions.index(sha256(target.read_bytes()).hexdigest())
            evidence = record.get("verification", {}).get("target_hashes", {}).get("Page.cs")
            built = None
            if evidence is not None and record.get("build_exit_code") == 0:
                built = (record.get("build_operation_id"), target_versions.index(evidence))
            checker_revision = None
            if record["status"] in {"checker_succeeded", "checker_needs_review", "checker_accepted"}:
                checker_revision = target_versions.index(evidence)
            expected = (
                model.revision, (f"build-{model.build.operation}", model.build.revision) if model.build else None,
                model.check_success.revision if model.check_success == model.build and model.check_success else None,
                model.check_failure.revision if model.check_failure == model.build and model.check_failure else None,
                model.approved.revision if model.approved == model.build and model.approved else None,
                model.complete, model.started_builds,
            )
            if checker_revision is not None and record["status"] != "checker_succeeded":
                digest = record["review_file_hashes"]["Page.cs"]
                self.assertEqual(digest, evidence, "VERIFY-RESULT-IDENTITY: checker referred to another build")
            approved = next((row for row in record.get("checker_reviews", []) if row["target_sha256"] == evidence), None)
            observed = (
                version, built,
                checker_revision if record["status"] == "checker_succeeded" else None,
                checker_revision if record["status"] in {"checker_needs_review", "checker_accepted"} else None,
                target_versions.index(approved["target_sha256"]) if approved else None,
                workflow.migration_ledger(config, run_id)["completion"]["complete"],
                commands.count("build"),
            )
            self.assertEqual(observed, expected, label)

        def process(command, *, cwd, stdout, **kwargs):
            self.assertEqual(Path(cwd), worktree, "VERIFY-RUN-ISOLATION")
            if command[:2] == ["dotnet", "build"]:
                commands.append("build")
                if commands.count("build") == 2:
                    saved = read_record()
                    self.assertEqual(saved["status"], "building")
                    self.assertEqual(saved["verification"]["target_hashes"]["Page.cs"], target_versions[0])
                    self.assertFalse(workflow.migration_ledger(config, run_id)["completion"]["complete"])
                    save_target_edit(worktree, (Path("Page.cs"),), {
                        "target": "Page.cs", "version": target_versions[0], "content": edited,
                    })
                    target_versions.append(sha256(target.read_bytes()).hexdigest())
                return subprocess.CompletedProcess(command, 0)
            self.assertEqual(command, ["fixture-checker"])
            commands.append("checker")
            if commands.count("checker") == 1:
                return subprocess.CompletedProcess(command, 0)
            stdout.write(b"FIXTURE Page.cs:1 warning: operator decision required\n")
            return subprocess.CompletedProcess(command, 1)

        def resume():
            plan = workflow.plan_stage_resume(config, run_id, workflow.BUILD_THROUGH_STAGE)
            operation_id = f"build-{commands.count('build') + 1}"
            control = execution.ExecutionControl(lambda _: None, operation_id=operation_id)
            with execution.control_scope(control):
                return workflow.resume_with_junie(config, run_id, verification_request={**plan, "id": operation_id})

        model = VerificationModel(0, checker_required=True)
        with patch.object(execution, "run_process", side_effect=process), stub_syntax_diagnostics():
            resume()
            model = evolve(model, verification_actions, "build success", "request checker", "checker success", "publish complete")
            observe(model, "VERIFY-CHECKER: initial version verified")
            with self.assertRaisesRegex(ValueError, "ビルド中に移行先が更新"):
                resume()
            model = evolve(model, verification_actions, "build success", "external edit")
            self.assertNotIn("publish complete", dict(verification_actions(model)))
            observe(model, "VERIFY-STALE-BUILD: edit during build -> reject old result")
            self.assertEqual(target.read_text(encoding="utf-8-sig"), edited)
            self.assertEqual(commands, ["build", "checker", "build"])

            resume()
            model = evolve(model, verification_actions, "build success", "request checker", "checker failure")
            observe(model, "VERIFY-CHECKER: a required checker failed for the edited version")
            finding = workflow.review_checker(config, run_id, None, None)["findings"][0]
            workflow.review_checker(config, run_id, finding[0], "Fixture finding reviewed")
            model = evolve(model, verification_actions, "approve finding with reason")
            observe(model, "VERIFY-APPROVAL: acceptance bound to the current version")
            self.assertEqual(read_record()["checker_reviews"][-1]["reason"], "Fixture finding reviewed")

        # Negative control: a bad saved receipt must produce a different projection.
        record = read_record()
        record["verification"]["target_hashes"]["Page.cs"] = target_versions[0]
        run_records.write_run_record(record_path, record)
        with self.assertRaisesRegex(AssertionError, "VERIFY-RESULT-IDENTITY"):
            observe(model, "VERIFY-STALE-BUILD: deliberately wrong receipt")


class SyntheticCrash(BaseException):
    pass


class HookTraceTests(SavedRunFixture):
    def test_unknown_hook_is_not_replayed_until_explicit_verified_retry(self):
        phase = "before_migration"
        counter = self.worktree / "hook-count.txt"
        self.config["scripts"] = {phase: [["fixture-hook"]]}
        self.save_record({**self.record(), "status": "prepared", "completed_capsules": 0})
        attempts = []

        def applied():
            return int(counter.read_text(encoding="utf-8")) if counter.exists() else 0

        def invoke(command, *, cwd, stdout, **kwargs):
            self.assertEqual(command, ["fixture-hook"])
            self.assertEqual(Path(cwd), self.worktree)
            attempt = self.record()["script_inflight"]
            self.assertEqual((attempt["phase"], attempt["number"]), (phase, 1), "HOOK-NOT-RECORDED")
            attempts.append(attempt["id"])
            counter.write_text(str(applied() + 1), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0)

        def run():
            stage_scripts.run_script_phase(self.config, phase, self.worktree, self.run_path, self.record(), once=True)

        def observe(model, condition):
            record = self.record()
            if phase in record.get("completed_script_phases", ()):
                outcome = Hook.SUCCESS
                self.assertNotIn("script_inflight", record, condition)
            else:
                outcome = Hook.UNKNOWN if record.get("script_inflight") else Hook.READY
            self.assertEqual((outcome, applied(), len(attempts)), (model.stage, model.attempts, model.attempts), condition)

        save = run_records.write_run_record

        def lose_result(path, record, **kwargs):
            if record.get("script_command_progress", {}).get(phase):
                raise SyntheticCrash
            return save(path, record, **kwargs)

        model = HookModel()
        observe(model, "HOOK-NOT-RECORDED: initial")
        with patch.object(execution, "run_process", side_effect=invoke):
            with patch.object(run_records, "write_run_record", side_effect=lose_result), self.assertRaises(SyntheticCrash):
                run()
            model = evolve(model, hook_actions, "persist hook attempt", "spawn hook", "restart")
            observe(model, "HOOK-INCOMPLETE-ATTEMPT-LOST: restart with no saved result")
            persisted = self.record_path.read_bytes()
            with self.assertRaisesRegex(ValueError, "実行結果不明"):
                run()
            self.assertEqual(self.record_path.read_bytes(), persisted)
            observe(model, "HOOK-UNKNOWN-RESULT: implicit resume is blocked")

            plan = stage_scripts.script_recovery(self.config, self.run_id)
            self.assertTrue(plan["can_retry"])
            self.target.write_text("class EditedAfterReview {}\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "対象版が変わりました"):
                stage_scripts.script_recovery(self.config, self.run_id, retry_version=plan["version"])
            self.assertEqual(self.record_path.read_bytes(), persisted)
            observe(model, "HOOK-UNSAFE-RETRY: target data changed after the retry was planned")

            current = stage_scripts.script_recovery(self.config, self.run_id)
            self.assertTrue(current["can_retry"])
            self.assertNotEqual(current["version"], plan["version"])
            result = stage_scripts.script_recovery(self.config, self.run_id, retry_version=current["version"])
            self.assertTrue(result["retried"])
            model = evolve(model, hook_actions, "explicit retry with verified command", "spawn hook", "save hook success")
            observe(model, "HOOK-UNSAFE-RETRY: explicit retry records the result")
            self.assertNotEqual(attempts[0], attempts[1])
            run()
            observe(model, "HOOK-SUCCESS-REPLAY: acknowledged success is not replayed")
