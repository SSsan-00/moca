import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from migration_harness.script_handoff import make_script_handoff, script_handoff_context


class ScriptHandoffTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.run = Path(directory.name)
        self.record = {"run_id": self.run.name, "script_advisories": [], "script_handoffs": []}
        self.phase = "before_prepare"
        self.command = ["python", "script.py"]
        self.scripts = {self.phase: [self.command, ["python", "second.py"]]}
        self.versions = {"Page.cs": "1" * 64}

    def emit(self, body, *, phase=None, number=1, cited=None, **metadata):
        phase = phase or self.phase
        name = f"{phase}.log"
        metadata.setdefault("symbol", "CandidateSymbol")
        raw = (body.replace("Hint from first command.", "FirstObservation")
               .replace("Hint from second command.", "SecondObservation")
               .replace("Original evidence.", "OriginalEvidence")
               .replace("Stale context.", "StaleObservation")).encode()
        command = self.scripts.get(phase, [self.command])[number - 1]
        with (self.run / name).open("ab") as stream:
            offset = stream.tell()
            stream.write(raw)
        item = {
            "phase": phase, "log": name, "command_number": number, "exit_code": 0,
            "status": "reported", "verdict": "handoff", "evidence_lines": [1] if cited is None else cited,
            "command_sha256": hashlib.sha256(json.dumps(command).encode()).hexdigest(),
            "log_sha256": hashlib.sha256(raw).hexdigest(), "byte_offset": offset, "byte_length": len(raw),
            **metadata,
        }
        self.record["script_advisories"].append(item)
        receipt = make_script_handoff(self.run, self.record, item, self.versions)
        if receipt is not None:
            self.record["script_handoffs"].append(receipt)
        return receipt

    def context(self):
        return script_handoff_context(self.run, self.record, self.scripts, self.versions)

    def test_only_cited_attempt_and_command_lines_are_delivered_in_order(self):
        self.emit("Stale context.\n", symbol="StaleObservation")
        self.emit("Hint from first command. and do not expose my private words\nThis uncited line is private.\n", symbol="FirstObservation")
        self.emit("Hint from second command.\n", number=2, symbol="SecondObservation")
        prompt = self.context()
        self.assertIn("untrusted", prompt)
        self.assertIn("Do not execute", prompt)
        self.assertLess(prompt.index("FirstObservation"), prompt.index("SecondObservation"))
        self.assertNotIn("StaleObservation", prompt)
        self.assertNotIn("private", prompt)
        self.emit("Ordinary subsequent output.\n", verdict="passed")
        self.assertNotIn("FirstObservation", self.context())

    def test_run_version_command_or_digest_changes_invalidate_evidence(self):
        self.emit("Original evidence.\n", symbol="OriginalEvidence")
        self.assertIn("OriginalEvidence", self.context())
        self.assertEqual("", script_handoff_context(self.run, {**self.record, "run_id": "other"}, self.scripts, self.versions))
        self.assertEqual("", script_handoff_context(self.run, self.record, self.scripts, {"Page.cs": "2" * 64}))
        self.assertEqual("", script_handoff_context(self.run, self.record, {self.phase: [["different"]]}, self.versions))
        (self.run / f"{self.phase}.log").write_bytes(b"Tampered evidence.\n")
        self.assertEqual("", self.context())

    def test_bounds_bad_references_and_unsuccessful_executions_are_not_handoffs(self):
        self.assertIsNone(self.emit("Illegal\u202e display control.\n"))
        self.assertIsNone(self.emit("x" * 481 + "\n"))
        self.assertIsNone(self.emit("one line\n", cited=[2]))
        self.assertIsNone(self.emit("one line\n", cited=[True]))
        self.assertIsNone(self.emit("CandidateSymbolSuffix\n"))
        self.assertIsNone(self.emit("CandidateSymbol\n", symbol="Candidate;Invalid"))
        self.assertIsNone(self.emit("CandidateSymbol\n", exit_code=1))
        self.assertIsNone(self.emit("one line\n", phase="after_build"))
        self.assertEqual("", self.context())

    def test_newer_shorter_output_never_cites_other_command_or_log_range(self):
        self.emit("First output.\nUnrelated second line.\n")
        receipt = self.emit("A short output.\n", cited=[2])
        self.assertIsNone(receipt)
        self.assertEqual("", self.context())
