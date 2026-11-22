import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import check
import create_fixture

spec = importlib.util.spec_from_file_location(
    "benchmark_dashboard", create_fixture.HERE.parent / "business-php" / "run_dashboard.py"
)
dashboard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dashboard)


class BenchmarkTests(unittest.TestCase):
    def test_source_scales_without_empty_line_padding(self):
        default, policies = create_fixture.business.generate_php()
        self.assertEqual((len(default.splitlines()), len(policies)), (2_000, 16))
        source, policies = create_fixture.business.generate_php(20_000)
        self.assertEqual(len(source.splitlines()), 20_000)
        self.assertEqual(len(policies), 183)
        for policy in policies:
            name = f"quote_{policy['NUMBER']:03d}"
            self.assertIn(f"function {name}(", source)
            self.assertIn(f"{policy['NUMBER']} => '{name}'", source)
        cases = create_fixture.scenarios(policies)
        self.assertEqual(len(cases), 187)
        self.assertEqual(sum(len(case["requests"]) for case in cases), 955)
        self.assertEqual({query["policy"] for case in cases[:len(policies)] for query in case["requests"]},
                         set(range(len(policies))))

    def test_dashboard_poll_tolerates_transient_windows_replacement(self):
        path = Mock()
        path.read_text.side_effect = [PermissionError(), FileNotFoundError(), '{"status":"prepared"}']
        with patch.object(dashboard.time, "sleep") as delay:
            self.assertEqual(dashboard.read_record(path), {"status": "prepared"})
        self.assertEqual(delay.call_count, 2)
        path.read_text.side_effect = PermissionError
        with patch.object(dashboard.time, "sleep") as delay, self.assertRaises(PermissionError):
            dashboard.read_record(path)
        self.assertEqual(delay.call_count, 19)


    def test_allows_only_single_run_fixture_receipt_locations(self):
        root = Path.cwd().resolve()
        run_id = "9b727941-ee0d-4d5e-bd22-cd2d99238c09"
        workspace = root / "runs" / run_id / "agent-workspace"
        other = root / "runs" / "other" / "agent-workspace"
        token = "a" * 32
        completion = workspace / f"migration-complete-{token}.txt"
        steering = workspace / ".migration-scratch" / f"steering-{token}.txt"
        for receipt in (completion, steering):
            self.assertTrue(dashboard.allow_fixture_receipt(root, run_id, f"Allow changes in this file?\n\n{receipt}"))
        for path in (workspace / "BusinessService.cs", completion.name, other / completion.name):
            self.assertFalse(dashboard.allow_fixture_receipt(root, run_id, f"Allow changes in this file?\n\n{path}"))
        self.assertFalse(dashboard.allow_fixture_receipt(root, run_id, f"Read this file?\n\n{completion}"))


    def test_checker_detects_output_and_database_changes(self):
        source = [{"id": "order", "steps": [{"ok": True, "note": None}], "state": {"orders": [{"paid": 0}]}}]
        self.assertEqual(check.compare(source, source), [])
        self.assertFalse(check.compare(source, [
            {"id": "order", "steps": [{"note": None, "ok": True}], "state": {"orders": [{"paid": 0}]}}
        ]))
        for response, paid, suffix in [
            ({"ok": 1, "note": None}, 0, ".steps[0].ok"),
            ({"ok": True, "note": ""}, 0, ".steps[0].note"),
            ({"ok": True, "note": None}, 1, ".state.orders[0].paid"),
        ]:
            with self.subTest(response=response, paid=paid):
                issues = check.compare(source, [
                    {"id": "order", "steps": [response], "state": {"orders": [{"paid": paid}]}}
                ])
                self.assertEqual(len(issues), 1, issues)
                self.assertIn(suffix, issues[0])
        self.assertTrue(check.compare(source, []))


if __name__ == "__main__":
    unittest.main()
