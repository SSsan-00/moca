import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from migration_harness.rules.rule_materials import render_guidelines, write_rule_files


spec = importlib.util.spec_from_file_location(
    "holdout", Path(__file__).resolve().parents[2] / "evaluations/rule-optimization/holdout.py",
)
holdout = importlib.util.module_from_spec(spec)
spec.loader.exec_module(holdout)


class RuleHoldoutTests(unittest.TestCase):
    def test_positive_separates_conditional_rules_but_not_related_exception(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "positive"
            holdout.prepare(root, "conditional")
            self.assertEqual(holdout.score(root, "conditional")["artifact_outcome"], "unnecessary_hold")
            source = holdout.originals("conditional")
            primary = source["export.md"].splitlines(keepends=True)
            files = {
                "required.md": primary[-1],
                "csv.md": "".join(primary[:-1]),
                "rehearsal.md": source["export-rehearsal.md"],
            }
            for name, body in files.items():
                (root / "app" / name).write_bytes(body.encode())
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
            detail = root / "app" / "csv.md"
            unchanged = (root / "app" / "export.md").read_bytes()
            for name, text, expected in (
                ("missing repeated line", "".join(primary[:-2]), "missing (1) or extra (0)"),
                ("extra repeated line", files["csv.md"] + primary[-2], "missing (0) or extra (1)"),
                ("lost exception", files["csv.md"].replace(holdout.EXCEPTION + "\n", ""), "split its exception"),
            ):
                with self.subTest(name=name):
                    detail.write_bytes(text.encode())
                    score = holdout.score(root, "conditional")
                    self.assertEqual(score["artifact_outcome"], "incorrect_apply", score)
                    self.assertTrue(any(expected in message for message in score["failures"]), score)
                    self.assertEqual((root / "app" / "export.md").read_bytes(), unchanged)

            detail.write_bytes(files["csv.md"].replace(holdout.EXCEPTION + "\n", "").encode())
            (root / "app" / "rehearsal.md").write_bytes((files["rehearsal.md"] + holdout.EXCEPTION + "\n").encode())
            split = holdout.score(root, "conditional")
            self.assertEqual(split["artifact_outcome"], "incorrect_apply")
            self.assertEqual(len(split["failures"]), 1, split)
            self.assertIn("split its exception", split["failures"][0])

            detail.write_bytes(files["csv.md"].encode())
            (root / "app" / "rehearsal.md").write_bytes(files["rehearsal.md"].encode())
            config.write_bytes(candidate.replace('usage = "reference"\ndescription = "CSV', 'usage = "required"\ndescription = "CSV').encode())
            broadened = holdout.score(root, "conditional")
            self.assertEqual(broadened["artifact_outcome"], "incorrect_apply")
            self.assertEqual(broadened["failures"], ["unrelated or CSV-only instructions became mandatory"])
            config.write_bytes(candidate.encode())

            (root / "app" / "export.md").rename(root / "app" / "unavailable-source.md")
            missing = holdout.score(root, "conditional")
            self.assertEqual(missing["artifact_outcome"], "incorrect_apply")
            self.assertIn("original source bytes changed or original is missing", missing["failures"])

    def test_original_scope_is_retained_for_repair_and_respects_an_explicit_exclusion(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            required = holdout.REQUIRED + "\n"
            detail = "\r\n".join((holdout.DETAIL, holdout.EXCEPTION, holdout.PRIORITY)) + "\r\n"
            decoy = holdout.DECOY + "\n"
            rules = write_rule_files(directory, [
                ({"path": "policy.md", "usage": "required"}, required),
                ({"path": "csv.md", "usage": "reference", "description": "When editing Export.cs"}, detail),
                ({"path": "csv-rehearsal.md", "usage": "reference", "description": "Only for test summaries"}, decoy),
            ])
            mandatory, export, rehearsal = [entry["file"] for entry in rules]
            manifest = {"rule_files": rules}
            for stage, targets, selected, excluded, expected in (
                ("migration", (Path("Example.cs"),), frozenset(), frozenset(), [mandatory]),
                ("repair", (Path("Export.cs"),), frozenset({export}), frozenset(), [mandatory, export]),
                ("operator exclusion", (Path("Export.cs"),), frozenset({export}), frozenset({export}), [mandatory]),
            ):
                with self.subTest(stage=stage):
                    supplied = []
                    text = render_guidelines(directory, manifest, targets=targets, selected=selected,
                                             excluded=excluded, supplied_rules=supplied)
                    self.assertEqual([item["file"] for item in supplied], expected)
                    self.assertIn(required, text)
                    self.assertEqual(detail in text, stage == "repair")
                    if stage != "repair":
                        for line in detail.splitlines():
                            self.assertNotIn(line, text)
                    self.assertNotIn(decoy, text)

            for name, body in ((mandatory, required), (export, detail), (rehearsal, decoy)):
                self.assertEqual((directory / name).read_bytes(), body.encode())

    def test_unexecuted_interrupted_or_unrelated_trial_cannot_pass_a_conflict_holdout(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "negative"
            holdout.prepare(root, "conflict")
            trace = Path(temp) / "trace.log"
            project = f"JunieCli - Using the specified project directory: {root}"
            other = root.parent / "other-project"
            other.mkdir()
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
                "other fixture": [project.replace(str(root), str(other)), start, skill, policy, finish],
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
            alias = root.parent / other.name / ".." / root.name
            same_project = project.replace(str(root), str(alias))
            trace.write_text("\n".join([same_project, start, skill, policy, submit, finish]), encoding="utf-8")
            for evidence in ({}, {"reviewed_hold_reason": quote + " Added later."},
                             {"reviewed_hold_reason": "replacement (export-rehearsal.md:2)."}):
                self.assertEqual(holdout.score(root, "conflict", trace, evidence)["outcome"], "hold_reason_needs_review")
            score = holdout.score(root, "conflict", trace, {"reviewed_hold_reason": quote})
            self.assertEqual(score["outcome"], "justified_hold")
            with (root / "harness.local.toml").open("a", encoding="utf-8") as stream:
                stream.write("\n# Edited despite contradiction.")
            self.assertEqual(holdout.score(root, "conflict", trace)["outcome"], "incorrect_apply")
