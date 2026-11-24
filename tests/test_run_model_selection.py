import json
import tempfile
import unittest
from pathlib import Path

from migration_harness.config import validate_junie_model
from migration_harness.runs.run_records import edit_run_record, run_model_options, write_run_record


class RunModelSelectionTests(unittest.TestCase):
    def test_later_record_write_preserves_selection_and_next_launch_reads_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.json"
            stale = {"status": "junie_running", "junie_model": "old", "junie_effort": "low"}
            path.write_text(json.dumps(stale), encoding="utf-8")
            with edit_run_record(path) as (_, changes):
                changes.update(junie_model="new", junie_effort="high", junie_model_revision="revision")
            write_run_record(path, stale)
            self.assertEqual(run_model_options(path.parent, stale, {}), {"model": "new", "effort": "high"})
            self.assertEqual(json.loads(path.read_text())["junie_model_revision"], "revision")

    def test_rejects_invalid_models_and_efforts(self):
        for model, effort in [("--bad", "high"), ("two models", None), ("", "high"), ("valid", "bad")]:
            with self.subTest(model=model, effort=effort), self.assertRaises(ValueError):
                validate_junie_model(model, effort)
        self.assertEqual(validate_junie_model("", ""), (None, None))
