from pathlib import Path
import unittest

from migration_harness.context_plan import navigation_plan


class ContextPlanTests(unittest.TestCase):
    def test_migration_and_resume_do_not_assign_future_capsules(self):
        manifest = {"unit_id": "billing", "parts": [{"index": i} for i in range(1, 5)]}
        targets = (Path("Bill.cshtml"), Path("Bill.cshtml.cs"))
        record = {"target_hashes": {"Bill.cshtml": "test-fingerprint"}, "completed_capsules": 1}
        for stage in ["migration", "resume"]:
            plan, hint = navigation_plan(manifest, record, stage, targets, current=2)
            self.assertEqual(stage, plan["stage"])
            self.assertEqual(2, plan["through_capsule"])
            self.assertEqual([("current", 2, 2), ("prior", 1, 1)], [
                (row["role"], row["first_capsule"], row["last_capsule"]) for row in plan["sources"]
            ])
            self.assertEqual("test-fingerprint", plan["targets"][0]["recorded_sha256"])
            self.assertEqual(2, len(plan["targets"]))
            self.assertIn("full mandatory and selected rules", hint)
        self.assertEqual(1, record["completed_capsules"])

    def test_plan_separates_the_staged_draft_from_the_adopted_target_version(self):
        manifest = {"unit_id": "billing", "parts": [{"index": 1}]}
        targets = (Path("Bill.cshtml"), Path("Bill.cshtml.cs"))
        record = {"target_hashes": {"Bill.cshtml": "adopted", "Bill.cshtml.cs": "same"}}
        plan, _ = navigation_plan(
            manifest, record, "resume", targets, current=1,
            staged_versions={"Bill.cshtml": None, "Bill.cshtml.cs": "same"},
        )
        self.assertEqual([
            {"path": "Bill.cshtml", "recorded_sha256": "adopted", "staged_sha256": None},
            {"path": "Bill.cshtml.cs", "recorded_sha256": "same", "staged_sha256": "same"},
        ], plan["targets"])
        self.assertEqual("adopted", record["target_hashes"]["Bill.cshtml"])

    def test_repair_directs_to_actual_diagnostic_and_full_assigned_source(self):
        manifest = {"unit_id": "billing", "parts": [{"index": 1}, {"index": 2}]}
        plan, hint = navigation_plan(manifest, {}, "repair", (Path("Bill.cs"),), diagnostic=Path("checker.txt"))
        source, = plan["sources"]
        self.assertEqual("repair-reference", source["role"])
        self.assertEqual((1, 2), (source["first_capsule"], source["last_capsule"]))
        self.assertEqual("checker.txt", plan["diagnostic"])
        self.assertIn("checker.txt", hint)
        self.assertIn("do not exclude evidence", hint)
