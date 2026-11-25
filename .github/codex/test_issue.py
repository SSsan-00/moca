import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import issue


REPO = "example-owner/example-project"
SHA = "a" * 40
# GitHub PR #60, comment 6982495964. Keep the review markers from the actual comment.
REVIEW_SHA = "951a9dd08ee09c65520a0ac99567f399b38d2029"
SUMMARY = (Path(__file__).parent / "fixtures" / "codex-review-summary-v1.txt").read_text(encoding="utf-8")


class IssueTests(unittest.TestCase):
    def test_issue_number(self):
        self.assertEqual(issue.issue_number("42"), 42)
        for value in ("0", "-1", "01", "１２", "1\n", "1;whoami", "2147483648"):
            with self.subTest(value=value), self.assertRaises(issue.Stop):
                issue.issue_number(value)

    def test_only_explicit_human_dispatchers_are_allowed(self):
        self.assertTrue(issue.allowed_dispatcher("Maintainer", "another, maintainer"))
        self.assertFalse(issue.allowed_dispatcher("outsider", "*"))
        self.assertFalse(issue.allowed_dispatcher("robot[bot]", "robot[bot]"))

    @patch.object(issue, "api")
    def test_issue_must_be_open_and_not_a_pr(self, api):
        for payload in ({"state": "closed"}, {"state": "open", "pull_request": {}}):
            api.return_value = payload
            with self.assertRaisesRegex(issue.Stop, "open Issue"):
                issue.snapshot(7)

    @patch.dict(os.environ, {"GITHUB_REPOSITORY_OWNER": "SSsan-00"})
    @patch.object(issue, "api")
    def test_duplicate_pr_and_orphan_branch(self, api):
        for responses in (([{"number": 12}],), ([], [{"ref": "refs/heads/codex/issue-7"}])):
            api.side_effect = responses
            with self.assertRaisesRegex(issue.Stop, "already"):
                issue.no_duplicate(7)

    @patch.dict(os.environ, {"GITHUB_SHA": SHA, "SNAPSHOT": issue.digest(b"original")})
    @patch.object(issue, "snapshot", return_value=b"updated")
    @patch.object(issue, "api", return_value={"object": {"sha": "b" * 40}})
    def test_freshness(self, api, snapshot):
        with self.assertRaisesRegex(issue.Stop, "main advanced"):
            issue.fresh_main()
        with self.assertRaisesRegex(issue.Stop, "discussion changed"):
            issue.unchanged_issue(7)

    def test_patch_uses_staged_paths_and_modes(self):
        original = os.getcwd()
        with tempfile.TemporaryDirectory() as folder:
            os.chdir(folder)
            try:
                subprocess.run(["git", "init", "-q"], check=True)
                Path(".github").mkdir()
                Path(".github/control.yml").write_text("original\n", encoding="utf-8")
                Path("src/migration_harness").mkdir(parents=True)
                source = Path("src/migration_harness/demo.py")
                source.write_text("original = True\n", encoding="utf-8")
                issue.git("add", ".")
                issue.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                          "-c", "commit.gpgsign=false", "commit", "-qm", "base")
                for path, mode, accepted in ((source, "100644", True),
                                              (Path(".github/control.yml"), "100644", False),
                                              (source, "100755", False)):
                    with self.subTest(path=path, mode=mode):
                        diff = (f"diff --git a/{path.as_posix()} b/{path.as_posix()}\n"
                                + (f"old mode 100644\nnew mode {mode}\n" if mode != "100644"
                                   else f"--- a/{path.as_posix()}\n+++ b/{path.as_posix()}\n"
                                   f"@@ -1 +1 @@\n-{path.read_text().strip()}\n+changed = True\n"))
                        if accepted:
                            issue.validate_patch(diff.encode())
                            self.assertEqual(source.read_text(), "original = True\n")
                        else:
                            with self.assertRaisesRegex(issue.Stop, "restricted"):
                                issue.validate_patch(diff.encode())
                        issue.git("reset", "-q", "HEAD")
            finally:
                os.chdir(original)

    @patch.dict(os.environ, {"GITHUB_REPOSITORY": REPO})
    @patch.object(issue, "paginated")
    def test_both_ci_events_and_complete_matrix_are_required(self, paginated):
        base = {"id": 1, "head_sha": SHA, "head_branch": "codex/issue-7",
                "path": issue.CI, "head_repository": {"full_name": REPO},
                "status": "completed", "conclusion": "success", "run_attempt": 1}
        push = base | {"event": "push"}
        pr = base | {"id": 2, "event": "pull_request"}
        jobs = [{"name": name, "head_sha": SHA, "status": "completed", "conclusion": "success"}
                for name in sorted(issue.CI_JOBS)]
        paginated.side_effect = ([push], jobs)
        self.assertEqual(issue.ci_ready("codex/issue-7", SHA), ["missing pull_request CI"])
        paginated.side_effect = ([push, pr | {"status": "in_progress"}], jobs)
        self.assertEqual(issue.ci_ready("codex/issue-7", SHA), ["pull_request CI pending"])
        missing_shard = [job for job in jobs if job["name"] != "windows tests (3.13, conversation-editor)"]
        paginated.side_effect = ([push, pr], jobs, missing_shard)
        with self.assertRaisesRegex(issue.Stop, "full successful matrix"):
            issue.ci_ready("codex/issue-7", SHA)
        failed_shard = [job | {"conclusion": "failure"} if job["name"] == "windows tests (3.13, conversation-editor)"
                        else job for job in jobs]
        paginated.side_effect = ([push, pr], jobs, failed_shard)
        with self.assertRaisesRegex(issue.Stop, "full successful matrix"):
            issue.ci_ready("codex/issue-7", SHA)
        for outcome in ("failure", "cancelled", "skipped"):
            paginated.side_effect = ([push, pr | {"conclusion": outcome}], jobs)
            with self.subTest(outcome=outcome), self.assertRaisesRegex(issue.Stop, outcome):
                issue.ci_ready("codex/issue-7", SHA)
        paginated.side_effect = ([push, pr], jobs, jobs)
        self.assertEqual(issue.ci_ready("codex/issue-7", SHA), [])

    @patch.dict(os.environ, {"REVIEWERS": "maintainer"})
    @patch.object(issue, "paginated")
    def test_review_must_be_current_and_confirm_completion(self, paginated):
        bot = {"user": {"login": issue.CODEX_BOT, "type": "Bot", "id": issue.CODEX_BOT_ID},
               "body": f"Codex Review: Didn't find any major issues. Swish!\n\n**Reviewed commit:** `{SHA[:10]}`",
               "updated_at": "2026-11-18T00:00:00Z"}
        approval = {"user": {"login": "maintainer", "type": "User", "id": 7}, "commit_id": SHA,
                    "author_association": "OWNER",
                    "state": "APPROVED", "submitted_at": "2026-11-18T00:01:00Z",
                    "body": "Codex-Issue: verified\nJunie: passed"}
        paginated.side_effect = ([bot | {"body": "Codex review timed out."}],)
        self.assertIn("clean Codex", issue.review_ready(42, SHA))
        paginated.side_effect = ([bot], [approval | {"commit_id": "b" * 40}])
        self.assertIn("maintainer approval", issue.review_ready(42, SHA))
        paginated.side_effect = ([bot], [approval | {"body": "Codex-Issue: verified\nJunie: pending"}])
        self.assertIn("maintainer approval", issue.review_ready(42, SHA))
        paginated.side_effect = ([bot], [approval])
        self.assertIsNone(issue.review_ready(42, SHA))

    def test_actual_review_summary_requires_a_clean_completed_exact_head(self):
        self.assertTrue(issue.clean_review({"body": SUMMARY}, REVIEW_SHA))
        self.assertFalse(issue.clean_review({"body": SUMMARY}, REVIEW_SHA[:10]))
        self.assertFalse(issue.clean_review({"body": SUMMARY}, SHA))
        replacements = (
            ("v1", "v2"),
            ('{"code_review":{', '{not-json'),
            ('"code_review":', '"other_review":'),
            ('"status":"completed"', '"status":"in_progress"'),
            ('"status":"completed"', '"status":"failed"'),
            ('"status":"completed"', '"status":null'),
            ('"has_any_findings":false', '"has_any_findings":true'),
            ('"has_unresolved_findings":false', '"has_unresolved_findings":true'),
            ('"has_any_findings":false', '"has_any_findings":"false"'),
            ('"has_unresolved_findings":false', '"has_unresolved_findings":null'),
            ('"has_any_findings":false,', ''),
            ('"has_any_findings":false', '"has_any_findings":true,"has_any_findings":false'),
        )
        for original, replacement in replacements:
            with self.subTest(replacement=replacement):
                self.assertIn(original, SUMMARY)
                self.assertFalse(issue.clean_review({"body": SUMMARY.replace(original, replacement)}, REVIEW_SHA))
        self.assertFalse(issue.clean_review({"body": SUMMARY + SUMMARY}, REVIEW_SHA))

    @patch.dict(os.environ, {"REVIEWERS": "maintainer"})
    @patch.object(issue, "paginated")
    def test_summary_still_requires_expected_bot_and_maintainer_approval(self, paginated):
        bot = {"user": {"login": issue.CODEX_BOT, "type": "Bot", "id": issue.CODEX_BOT_ID},
               "body": SUMMARY, "updated_at": "2026-11-18T15:08:42Z"}
        authorized = {"user": {"login": "maintainer", "type": "User", "id": 7},
                      "commit_id": REVIEW_SHA, "author_association": "OWNER", "state": "APPROVED",
                      "submitted_at": "2026-11-18T15:09:00Z",
                      "body": "Codex-Issue: verified\nJunie: not-required"}
        for field, value in (("login", "other[bot]"), ("id", 123), ("type", "User")):
            with self.subTest(field=field):
                paginated.side_effect = ([bot | {"user": bot["user"] | {field: value}}],)
                self.assertIn("clean Codex", issue.review_ready(60, REVIEW_SHA))
        paginated.side_effect = ([bot], [])
        self.assertIn("maintainer approval", issue.review_ready(60, REVIEW_SHA))
        paginated.side_effect = ([bot], [authorized])
        self.assertIsNone(issue.review_ready(60, REVIEW_SHA))
        later = {"user": bot["user"], "submitted_at": authorized["submitted_at"]}
        paginated.side_effect = ([bot], [authorized, later])
        self.assertIn("another review", issue.review_ready(60, REVIEW_SHA))

    @patch.object(issue, "paginated", return_value=[])
    def test_unprotected_main_blocks_merge(self, paginated):
        with self.assertRaisesRegex(issue.Stop, "strict ruleset"):
            issue.rules_ready()


if __name__ == "__main__":
    unittest.main()
