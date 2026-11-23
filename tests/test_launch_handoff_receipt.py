import json
import re
import unittest
from collections import deque
from pathlib import Path
from queue import Queue
from unittest.mock import Mock, patch

from fixtures import HarnessCase
from migration_harness import workflow
from migration_harness.context.context_handoff import record_handoff
from migration_harness.junie import junie_cli


class LaunchHandoffReceiptTests(HarnessCase):
    def test_budget_stop_keeps_last_launched_receipt_until_next_accepted_prompt(self):
        initial = b"class Page {}\n"
        adopted = "class Adopted {}\n".encode("utf-8-sig")
        (self.project / "source.php").write_text("<?php echo 1;\n", encoding="utf-8")
        (self.project / "Page.cs").write_bytes(initial)
        self.commit_project()
        config = self.config()
        run_id = "00000000-0000-4000-8000-000000000168"
        run = Path(config["run_root"]) / run_id
        record_path = run / "run.json"
        original_run = junie_cli.run_interactive
        notes, new_receipts = [], []
        invocations = []

        def saved():
            return json.loads(record_path.read_bytes())

        def launch(command, **options):
            prompt, completion, token, (target,) = invocations[-1]
            worktree = Path(saved()["worktree_path"])
            self.assertEqual(initial, target.read_bytes())
            if not notes:
                for i in range(4):
                    notes.append(record_handoff(run, worktree, (Path("Page.cs"),), {}, "php-0001", 1, 1,
                                                f"operator memo {i}"))
            else:
                self.assertEqual(adopted, (worktree / "Page.cs").read_bytes())
                match = re.search(r"launch memo reference ID ([0-9a-f]{64})", command[-1])
                self.assertIsNotNone(match)
                new_receipts.append(match.group(1))
                self.assertEqual(prompt, command[-1])
            target.write_bytes(adopted)
            completion.write_text(token, encoding="ascii")
            return Mock(wait=Mock(return_value=0), poll=Mock(return_value=0))

        def remember(command, completion, token, targets, **kwargs):
            invocations.append((command[-1], completion, token, targets))
            return original_run(command, completion, token, targets, **kwargs)

        def skip_external_build(_config, _unit, result, record):
            record["status"] = "build_succeeded"
            workflow.write_run_record(record_path, record)
            return result

        def notes_from(receipt, through=1):
            return workflow.migration_ledger(
                config, run_id, operator_through=through, operator_receipt=receipt,
            )["unresolved_operator_notes"]

        with (
            patch.object(junie_cli, "require_interactive_cli"),
            patch.object(junie_cli, "_save_terminal_mode", return_value=lambda: None),
            patch.object(junie_cli, "run_interactive", side_effect=remember),
            patch.object(junie_cli, "_launch_session_process", side_effect=launch) as start,
            patch.object(workflow, "_verify_worktree", side_effect=skip_external_build),
        ):
            workflow.run_with_junie(config, "sample", run_id=run_id)
            plan = workflow.plan_migration_resume(config, run_id, 1, rewind_to=1)
            workflow.resume_with_junie(config, run_id, verification_request={**plan, "id": "first"})
            self.assertEqual(2, start.call_count)
            receipt, = new_receipts
            self.assertEqual(receipt, saved()["operator_handoff_receipt"]["id"])
            expected = [note["id"] for note in reversed(notes)]
            self.assertEqual(expected, [note["id"] for note in notes_from(receipt)])

            current = Path(saved()["worktree_path"])
            record_handoff(run, current, (Path("Page.cs"),), {}, "php-0001", 1, 1, "only for adopted target")
            self.assertEqual(expected, [note["id"] for note in notes_from(receipt)])
            plan = workflow.plan_migration_resume(config, run_id, 1, rewind_to=1)
            restricted = {**config, "junie_input_limits": {"max_input_bytes": 1}}
            with self.assertRaises(workflow.CheckerRepairBlocked) as error:
                workflow.resume_with_junie(restricted, run_id, verification_request={**plan, "id": "blocked"})
            self.assertEqual("input_budget", error.exception.reason)
            self.assertEqual(2, start.call_count)
            blocked = saved()
            self.assertTrue(blocked["last_junie_input"]["blocked"])
            self.assertGreater(blocked["last_junie_input"]["bytes"], 1)
            self.assertEqual(receipt, blocked["operator_handoff_receipt"]["id"])
            before_lookup = record_path.read_bytes()
            self.assertEqual(expected, [note["id"] for note in notes_from(receipt)])
            with self.assertRaises(ValueError):
                notes_from(receipt, 2)
            self.assertEqual(before_lookup, record_path.read_bytes())

            start.side_effect = OSError("no process created")
            with self.assertRaisesRegex(OSError, "no process created"):
                workflow.resume_with_junie(config, run_id)
            self.assertEqual(receipt, saved()["operator_handoff_receipt"]["id"])
            self.assertEqual(expected, [note["id"] for note in notes_from(receipt)])

            start.side_effect = launch
            workflow.resume_with_junie(config, run_id)
            self.assertEqual(4, start.call_count)
            self.assertEqual(2, len(new_receipts))
            self.assertNotEqual(receipt, new_receipts[-1])
            self.assertEqual(new_receipts[-1], saved()["operator_handoff_receipt"]["id"])
            self.assertFalse(saved()["last_junie_input"]["blocked"])
            persisted = json.dumps(saved()["operator_handoff_receipt"])
            for private in ("Page.cs", "source.php", "operator memo", "adopted target"):
                self.assertNotIn(private, persisted)
            self.assertEqual(expected, [note["id"] for note in notes_from(new_receipts[-1])])
            with self.assertRaises(ValueError):
                notes_from(receipt)


    def test_acp_accepts_only_a_flushed_initial_prompt(self):
        for failure in (OSError("failed pipe"), None):
            with self.subTest(failure=failure):
                session = object.__new__(junie_cli._AcpSession)
                session.session_id = "synthetic-session"
                session.command = ["junie", "--prompt", "ordinary prompt"]
                session.writes = deque()
                session.outgoing = Queue()
                accept = Mock()
                observer = junie_cli.launch_accepted_observer.set(accept)
                try:
                    session._on_session_ready()
                    accept.assert_not_called()
                    data, future = session.outgoing.get_nowait()
                    message = json.loads(data)
                    self.assertEqual("session/prompt", message["method"])
                    self.assertNotIn("after_flush", message["params"])
                    if failure:
                        future.set_exception(failure)
                        with self.assertRaises(junie_cli.JunieSessionError):
                            session._check_writes()
                        accept.assert_not_called()
                    else:
                        future.set_result(None)
                        session._check_writes()
                        session._check_writes(closing=True)
                        accept.assert_called_once_with()
                finally:
                    junie_cli.launch_accepted_observer.reset(observer)


if __name__ == "__main__":
    unittest.main()
