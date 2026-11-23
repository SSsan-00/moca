import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


spec = importlib.util.spec_from_file_location(
    "holdout", Path(__file__).resolve().parents[1] / "evaluations/rule-optimization/holdout.py",
)
holdout = importlib.util.module_from_spec(spec)
spec.loader.exec_module(holdout)


class RuleHoldoutTests(unittest.TestCase):
    def test_positive_separates_conditional_rules_but_not_related_exception(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "positive"
            holdout.prepare(root, "conditional")
            self.assertEqual(holdout.score(root, "conditional")["artifact_outcome"], "unnecessary_hold")
            outputs = {
                "required.md": holdout.REQUIRED,
                "csv.md": "\n".join((holdout.DETAIL, holdout.EXCEPTION, holdout.PRIORITY)),
                "rehearsal.md": holdout.DECOY,
            }
            for name, body in outputs.items():
                (root / "app" / name).write_bytes((body + "\n").encode())
            config = root / "harness.local.toml"
            candidate = holdout.CONFIG.replace(
                'path = "export.md"', 'path = "required.md"\n[[project.migration_rules]]\n'
                'path = "csv.md"\nusage = "reference"\ndescription = "CSV export migration or repair"',
            ).replace('path = "export-rehearsal.md"',
                      'path = "rehearsal.md"\nusage = "reference"\ndescription = "Test report examples"')
            config.write_bytes(candidate.encode())
            score = holdout.score(root, "conditional")
            self.assertEqual(score["artifact_outcome"], "correct_apply", score)
            self.assertEqual(score["outcome"], "unverified_trial", score)
            self.assertFalse(score["skill_read_verified"])
            (root / "app" / "csv.md").write_bytes((holdout.DETAIL + "\n").encode())
            score = holdout.score(root, "conditional")
            self.assertEqual(score["artifact_outcome"], "incorrect_apply", score)
            self.assertIn("split its exception", score["failures"][0])

    def test_unexecuted_interrupted_or_unrelated_trial_cannot_pass_a_conflict_holdout(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "negative"
            holdout.prepare(root, "conflict")
            trace = Path(temp) / "trace.log"
            project = f"JunieCli - Using the specified project directory: {root}"
            event = "AnalyticsClientImpl - [ANALYTICS:EVENT] type="
            start = event + "task_request_started properties=prompt=/harness-optimize-rules Review inputs"
            read = event + "agent_step_finished properties=step_type=agent_skill_read_doc "
            skill = read + 'ToolRequestArgumentSchema(rawJsonObject={"name":"harness-optimize-rules"}), step_result=SUBMIT'
            policy = read + ('ToolRequestArgumentSchema(rawJsonObject={"name":"harness-optimize-rules",'
                             '"path":"references/loading-policy.md"}), step_result=SUBMIT')
            finish = event + "task_request_finished properties=status=Success, duration_ms=null"
            self.assertEqual(holdout.score(root, "conflict")["outcome"], "unverified_trial")
            for name, lines in {
                "still running": [project, start, skill, policy],
                "failed": [project, start, skill, policy, finish.replace("Success", "Failure")],
                "other fixture": [project + "-elsewhere", start, skill, policy, finish],
                "missing read": [project, start, skill, finish],
                "failed read": [project, start, skill, policy.replace("SUBMIT", "FAILED"), finish],
                "another task's reads": [project, start, finish, start, skill, policy, finish],
            }.items():
                with self.subTest(name=name):
                    trace.write_text("\n".join(lines), encoding="utf-8")
                    self.assertEqual(holdout.score(root, "conflict", trace)["outcome"], "unverified_trial")
            quote = "The retention rule (export.md:92) contradicts replacement (export-rehearsal.md:2)."
            summary = json.dumps({"solution_summary": quote})
            submit = event + f"agent_step_finished properties=step_type=submit ToolRequestArgumentSchema(rawJsonObject={summary}), step_result=SUBMIT"
            trace.write_text("\n".join([project, start, skill, policy, submit, finish]), encoding="utf-8")
            for evidence in ({}, {"reviewed_hold_reason": quote + " Added later."},
                             {"reviewed_hold_reason": "replacement (export-rehearsal.md:2)."}):
                self.assertEqual(holdout.score(root, "conflict", trace, evidence)["outcome"], "hold_reason_needs_review")
            score = holdout.score(root, "conflict", trace, {"reviewed_hold_reason": quote})
            self.assertEqual(score["outcome"], "justified_hold")
            with (root / "harness.local.toml").open("a", encoding="utf-8") as stream:
                stream.write("\n# Edited despite contradiction.")
            self.assertEqual(holdout.score(root, "conflict", trace)["outcome"], "incorrect_apply")
