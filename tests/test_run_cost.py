import json
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from migration_harness.junie.usage import UsageDelta
from migration_harness.runs import run_records


class RunCostTests(unittest.TestCase):
    def test_reported_cost_is_recorded_once_and_missing_cost_is_not_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "run.json"
            path.write_text(json.dumps({"status": "junie_running"}), encoding="utf-8")
            recorded = {"status": "available", "rate": 150.0, "date": "2026-11-24", "source": "Frankfurter"}
            with patch.object(run_records, "_exchange_rate", return_value=recorded) as lookup:
                first = run_records.junie_usage_recorder(root, "migration", 1)
                first([0, 0, 0, 0, 0])
                first(UsageDelta([30, 4, 10, 0, 2], 0.125, 1))
                run_records.junie_usage_recorder(root, "build_repair")([0, 0, 0, 0, 0])
            lookup.assert_called_once()
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["cost_exchange"], recorded)
            self.assertEqual(saved["token_usage"][0]["cost_usd"], 0.125)
            self.assertEqual(saved["token_usage"][0]["cost_known"], 1)
            self.assertEqual(saved["token_usage"][0]["cost_missing"], 1)
            run_records.write_run_record(path, {"status": "building"})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["cost_exchange"], recorded)

    def test_exchange_rate_validation_and_failure(self):
        body = json.dumps({"base": "USD", "quote": "JPY", "date": "2026-11-24", "rate": 150.5}).encode()
        with patch.object(run_records, "urlopen", return_value=io.BytesIO(body)) as request:
            self.assertEqual(run_records._exchange_rate(), {
                "status": "available", "rate": 150.5, "date": "2026-11-24", "source": "Frankfurter",
            })
        request.assert_called_once()
        with patch.object(run_records, "urlopen", side_effect=OSError("offline")):
            self.assertEqual(run_records._exchange_rate(), {"status": "unavailable"})

    def test_invalid_or_zero_reported_cost_stays_unknown(self):
        usage = UsageDelta()
        usage.record({"agentEvent": {"kind": "LlmResponseMetadataEvent", "modelUsage": [
            {"inputTokens": 1, "outputTokens": 2, "cost": 0},
            {"inputTokens": 1, "outputTokens": 2, "cost": float("nan")},
            {"inputTokens": 1, "outputTokens": 2, "cost": 0.01},
        ]}})
        self.assertEqual(usage[-1], 3)
        self.assertEqual(usage.cost_known, 1)
        self.assertEqual(usage.cost_usd, 0.01)

    def test_known_model_uses_prices_only_when_reported_cost_is_missing(self):
        usage = UsageDelta()
        usage.record({"agentEvent": {"kind": "LlmResponseMetadataEvent", "modelUsage": [
            {"model": "claude-sonnet-4-6", "inputTokens": 1000, "outputTokens": 100, "cacheInputTokens": 500},
            {"model": "claude-sonnet-4-6", "inputTokens": 1000, "outputTokens": 100, "cost": 0.02},
            {"model": "unknown-model", "inputTokens": 1000, "outputTokens": 100},
            {"model": "gpt-5", "inputTokens": 1000, "outputTokens": 100, "cacheCreateTokens": 1},
        ]}})
        self.assertEqual(usage.cost_known, 2)
        self.assertAlmostEqual(usage.cost_usd, 0.02465)

    def test_existing_usage_does_not_get_a_current_rate_on_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "run.json").write_text(json.dumps({"status": "prepared", "token_usage": [{
                "phase": "migration", "capsule": 1, "input": 1, "output": 1, "cache_read": 0,
                "cache_write": 0, "responses": 1,
            }]}), encoding="utf-8")
            with patch.object(run_records, "_exchange_rate") as lookup:
                run_records.junie_usage_recorder(root, "migration", 1)([0, 0, 0, 0, 0])
            lookup.assert_not_called()
            saved = json.loads((root / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["cost_exchange"], {"status": "unavailable"})
