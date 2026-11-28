from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from tests.support.fixtures import HarnessCase, prepare_fixture
from migration_harness.context.capsules import load_capsule_manifest, verify_pinned_php
from migration_harness.runs.run_manager import RunManager
from migration_harness.runs.run_records import execution_lock
from migration_harness.web.dashboard import Dashboard


_RUN_A = "00000000-0000-4000-8000-000000000001"
_RUN_B = "00000000-0000-4000-8000-000000000002"


class SavedDashboardQueueValidationTests(TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        self.config = {
            "project_root": str(root / "project"),
            "worktree_root": str(root / "trees"),
            "run_root": str(root / "runs"),
            "units": [{"id": "a", "target": "Page.cs"}, {"id": "b", "target": "Other.cs"}],
        }
        self.queue = Path(self.config["run_root"]) / "dashboard-queue.json"
        self.queue.parent.mkdir()

    def document(self, *jobs):
        return {"project_root": self.config["project_root"], "jobs": list(jobs),
                "cancelled_conversations": {}, "blocked_conversations": {}}

    def test_current_queue_retains_ids_order_and_bytes_across_read_and_restore(self):
        jobs = [{"run_id": run_id, "unit_id": unit, "action": "run", "operation_id": unit * 32}
                for run_id, unit in ((_RUN_A, "a"), (_RUN_B, "b"))]
        data = json.dumps(self.document(*jobs), indent=4).encode()
        self.queue.write_bytes(data)
        temporary = self.queue.with_suffix(".tmp")
        temporary.write_bytes(b"an incomplete later write")
        manager = RunManager(self.config, Dashboard())
        self.addCleanup(manager.close)
        self.assertEqual(temporary.read_bytes(), b"an incomplete later write")
        self.assertEqual(list(manager.pending), [_RUN_A, _RUN_B])
        self.assertEqual({key: manager.jobs[key]["unit_id"] for key in manager.pending}, {_RUN_A: "a", _RUN_B: "b"})
        self.assertEqual(list(manager.jobs.values()), jobs)
        self.assertFalse(manager.workers)
        self.assertEqual(self.queue.read_bytes(), data)

    def test_started_job_is_fenced_and_an_independent_waiting_record_is_kept(self):
        waiting = {"run_id": _RUN_B, "unit_id": "b", "action": "run", "operation_id": "a" * 32}
        started = {"run_id": _RUN_A, "unit_id": "a", "action": "run", "running": True, "operation_id": "b" * 32}
        self.queue.write_text(json.dumps(self.document(started, waiting)), encoding="utf-8")
        manager = RunManager(self.config, Dashboard())
        self.addCleanup(manager.close)
        self.assertEqual(list(manager.pending), [_RUN_B])
        self.assertFalse(manager.workers)
        self.assertEqual(manager.jobs[_RUN_B], waiting)
        self.assertEqual(manager.get(_RUN_A).state["status"], "preparation_interrupted")
        saved = json.loads(self.queue.read_bytes())
        self.assertEqual(saved["jobs"], [waiting])
        self.assertIn(_RUN_A, saved["blocked_conversations"])

    def test_corrupt_queue_is_rejected_before_restore_without_writing_or_retaining_lock(self):
        job = {"run_id": _RUN_A, "unit_id": "a", "action": "run", "operation_id": "a" * 32}
        root = self.document(job)
        bad = (
            b'{"jobs": ',
            b'{"jobs": [],"jobs":[]}',
            b'{"jobs": NaN}',
            b"[]",
            {**root, "schema_version": 9},
            *({k: v for k, v in root.items() if k != omitted} for omitted in root),
            {**root, "jobs": [{k: v for k, v in job.items() if k != "operation_id"}]},
            {**root, "jobs": [{**job, "operation_id": None, "running": True}]},
            {**root, "project_root": "different-project"},
            {"project_root": root["project_root"]},
            {**root, "jobs": {}},
            {**root, "jobs": [False]},
            {**root, "cancelled_conversations": []},
            {**root, "blocked_conversations": {_RUN_A: False}},
            {**root, "jobs": [{**job, "run_id": 42}]},
            {**root, "jobs": [{**job, "running": 0}]},
            {**root, "jobs": [{**job, "operation_id": "typo"}]},
            {**root, "jobs": [{**job, "action": "retired-operation"}]},
            {**root, "jobs": [{**job, "junie_model": "model-a"}]},
            {**root, "jobs": [{**job, "junie_model": None, "junie_effort": "high"}]},
            {**root, "jobs": [{**job, "junie_model": "model-a", "junie_effort": "typo"}]},
            {**root, "jobs": [{**job, "action": "resume-run", "running": True, "junie_model": None, "junie_effort": None}]},
            {**root, "jobs": [job, {**job, "unit_id": "b", "operation_id": "b" * 32}]},
            {**root, "jobs": [job, {**job, "run_id": _RUN_B, "operation_id": "b" * 32}]},
            {**root, "jobs": [{**job, "operation_id": "a" * 32},
                              {"run_id": _RUN_B, "unit_id": "b", "action": "run", "operation_id": "a" * 32}]},
        )
        for payload in bad:
            with self.subTest(payload=payload):
                data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.queue.write_bytes(data)
                with patch.object(RunManager, "start_pending") as launch, self.assertRaises(ValueError):
                    RunManager(self.config, Dashboard())
                launch.assert_not_called()
                self.assertEqual(self.queue.read_bytes(), data)
                with execution_lock(self.config, "dashboard"):
                    pass

    def test_missing_queue_is_new_but_a_lone_temporary_is_not(self):
        temporary = self.queue.with_suffix(".tmp")
        unfinished = b'{"jobs": '
        temporary.write_bytes(unfinished)
        with self.assertRaisesRegex(ValueError, "途中保存"):
            RunManager(self.config, Dashboard())
        self.assertFalse(self.queue.exists())
        self.assertEqual(temporary.read_bytes(), unfinished)
        temporary.unlink()
        manager = RunManager(self.config, Dashboard())
        self.addCleanup(manager.close)
        self.assertFalse(manager.pending)
        self.assertEqual(json.loads(self.queue.read_bytes())["jobs"], [])


class CapsuleManifestValidationTests(HarnessCase):
    def setUp(self):
        super().setUp()
        (self.project / "source.php").write_bytes(b"<?php echo 1;\n")
        self.commit_project()
        self.settings = self.config()
        prepared = prepare_fixture(self.settings, "sample")
        self.run_path = Path(prepared["run_path"])
        self.manifest_path = Path(prepared["capsule_path"]) / "manifest.json"
        self.manifest = json.loads(self.manifest_path.read_bytes())

    def test_current_capsule_reopens_and_798136e_is_rejected_without_rewriting(self):
        original = self.manifest_path.read_bytes()
        capsule, current = load_capsule_manifest(self.run_path)
        verify_pinned_php(capsule, current)
        source, = current["php_sources"]
        self.assertEqual(source["sha256"], sha256(b"<?php echo 1;\n").hexdigest())
        self.assertEqual(self.manifest_path.read_bytes(), original)

        legacy = {key: current[key] for key in ("unit_id", "max_context_chars", "total_context_chars",
                                                "segmentation_policy", "syntax_boundaries_analyzed", "split_line_coverage")}
        legacy["parts"] = [{key: part[key] for key in ("index", "path", "chars", "sources")} for part in current["parts"]]
        recorded = json.dumps(legacy, indent=4).encode()
        self.manifest_path.write_bytes(recorded)
        with self.assertRaisesRegex(ValueError, "missing or invalid sha256"):
            load_capsule_manifest(self.run_path)
        self.assertEqual(self.manifest_path.read_bytes(), recorded)

    def test_partial_and_inconsistent_capsule_manifest_preserves_recorded_bytes(self):
        for expected, change in (
            ("保存形式", lambda doc: doc.update(schema_version=3)),
            ("capsule list", lambda doc: doc.pop("parts")),
            ("capsule indexes", lambda doc: doc["parts"][0].update(index=True)),
            ("context path", lambda doc: doc["parts"][0].pop("path")),
            ("character count", lambda doc: doc["parts"][0].update(chars=False)),
            ("character count", lambda doc: doc.update(total_context_chars=1)),
            ("invalid source range", lambda doc: doc["parts"][0]["sources"][0].update(end_line=0)),
            ("invalid source range", lambda doc: doc["parts"][0].pop("sources")),
            ("invalid character slice", lambda doc: doc["parts"][0]["sources"][0].update(character_slices=[{}])),
            ("invalid sha256", lambda doc: doc["parts"][0].update(sha256="partial hash")),
            ("invalid segment_sha256", lambda doc: doc["parts"][0].pop("segment_sha256")),
            ("invalid segment_offset", lambda doc: doc.pop("segment_offset")),
            ("invalid material versions", lambda doc: doc.pop("materials")),
            ("invalid material versions", lambda doc: doc.update(materials={"x": None})),
            ("保存済みPHP", lambda doc: doc.pop("php_sources")),
            ("固定PHPの一覧", lambda doc: doc.update(php_sources=[])),
            ("固定PHPの文字コード", lambda doc: doc["parts"][0]["sources"][0].update(encoding="cp932")),
            ("invalid rule_files", lambda doc: doc.update(rule_files={})),
            ("保存済みPHP", lambda doc: doc["php_sources"].append(doc["php_sources"][0].copy())),
        ):
            with self.subTest(expected=expected, change=change):
                damaged = deepcopy(self.manifest)
                change(damaged)
                data = json.dumps(damaged).encode()
                self.manifest_path.write_bytes(data)
                with self.assertRaisesRegex(ValueError, expected):
                    load_capsule_manifest(self.run_path)
                self.assertEqual(self.manifest_path.read_bytes(), data)

        for data in (b'{"parts": [', b'{"parts": [],"parts": []}', b'{"parts": Infinity}', b'\xff'):
            with self.subTest(data=data):
                self.manifest_path.write_bytes(data)
                with self.assertRaises(ValueError):
                    load_capsule_manifest(self.run_path)
                self.assertEqual(self.manifest_path.read_bytes(), data)
