import hashlib
import json
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from migration_harness import review_anchors, workflow
from migration_harness.config import load_config
from migration_harness.diff_viewer import capture_workspace


PHP = {"path": "Source.php", "role": "php", "start_line": 1, "end_line": 2}
RULES = {"path": "rules.md", "role": "migration_rules", "start_line": 1, "end_line": 1}
CONSTRAINT = {"path": "constraints.md", "role": "target_constraints", "shared": True, "start_line": 1, "end_line": 1}


class CapsulePreparationTests(unittest.TestCase):
    def setUp(self):
        fixture = tempfile.TemporaryDirectory()
        self.addCleanup(fixture.cleanup)
        self.root = Path(fixture.name).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        self.php = self.project / PHP["path"]
        self.php.write_bytes(b"<?php\necho 100;\n")
        (self.project / "Target.cs").write_bytes(b"class Target {}\n")
        self.git("init", "-q")
        self.git("config", "core.autocrlf", "false")
        self.git("add", "--", "Source.php", "Target.cs")
        self.git(
            "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
            "-c", "commit.gpgSign=false", "commit", "-qm", "Fixture",
        )

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.project), *args], check=True, capture_output=True, encoding="utf-8",
        ).stdout

    def config_path(self, *inputs):
        path = self.root / "harness.toml"
        sections = [
            '[project]\nroot = "project"\nworktree_root = "worktrees"\nrun_root = "runs"\nmax_context_chars = 1000',
            '[[units]]\nid = "example"\ntarget = "Target.cs"',
        ]
        for item in inputs:
            fields = [f"{key} = {json.dumps(value)}" for key, value in item.items()]
            sections.append("\n".join(["[[units.inputs]]", *fields]))
        path.write_text("\n\n".join(sections), encoding="utf-8")
        return path

    def prepare(self, *inputs):
        config = load_config(self.config_path(*inputs))
        result = workflow.prepare_run(config, "example")
        run_path = Path(result["run_path"])
        return config, result, self.read_json(run_path / "capsule" / "manifest.json")

    def read_json(self, path):
        return json.loads(path.read_text(encoding="utf-8"))

    def test_shared_rules_and_constraints_survive_interruption_and_resume(self):
        long_line = "$value = '" + "abcd" * 655 + "';"
        self.php.write_bytes(f"<?php\r\n{long_line}\r\necho $value;\r\n".encode("ascii"))
        rules = self.project / RULES["path"]
        rules.write_bytes(b"REQUIRED_MIGRATION_RULE ````\r\n")
        constraint = self.project / CONSTRAINT["path"]
        constraint.write_bytes(b"REQUIRED_OUTPUT_CONSTRAINT\n")
        config = load_config(self.config_path({**PHP, "end_line": 3}, CONSTRAINT, RULES))
        observed_contexts = []

        def junie(command, completion, token, target):
            observed_contexts.append((target.parent / ".migration-context.md").read_bytes())
            if len(observed_contexts) == 2:
                target.write_bytes(b"class Partial {}\n")
                raise KeyboardInterrupt
            if len(observed_contexts) == 3:
                self.assertEqual(target.read_bytes(), b"class Partial {}\n")
                self.assertIn("This capsule was interrupted", command[-1])
            completion.write_text(token, encoding="ascii")
            return 0

        with (
            patch.object(workflow, "_validate_junie_execution"),
            patch.object(workflow, "_run_interactive_junie", side_effect=junie),
            patch.object(workflow, "_verify_worktree", side_effect=lambda config, unit, result, record: result),
        ):
            with self.assertRaises(workflow.RunInterrupted) as interrupted:
                workflow.run_with_junie(config, "example")
            run_id = interrupted.exception.run_id
            run_path = Path(config["run_root"]) / run_id
            record_path = run_path / "run.json"
            interrupted_record = self.read_json(record_path)
            self.assertEqual(interrupted_record["completed_capsules"], 1)
            self.assertEqual(interrupted_record["current_capsule"], 2)
            rules.write_bytes(b"CHANGED_RULE\n")
            constraint.unlink()
            result = workflow.resume_with_junie(config, run_id)

        manifest = self.read_json(run_path / "capsule" / "manifest.json")
        parts = manifest["parts"]
        saved_contexts = [Path(part["path"]).read_bytes() for part in parts]
        self.assertGreater(len(parts), 2)
        self.assertEqual(observed_contexts, saved_contexts[:2] + saved_contexts[1:])
        self.assertEqual(result["completed_capsules"], len(parts))
        self.assertEqual(self.read_json(record_path)["resume_attempts"], 1)
        self.assertEqual([entry["path"] for entry in manifest["shared_sources"]], ["constraints.md", "rules.md"])
        focus_ranges = []
        for part, content in zip(parts, saved_contexts, strict=True):
            self.assertEqual(content.count(b"REQUIRED_MIGRATION_RULE"), 1)
            self.assertIn(b"REQUIRED_MIGRATION_RULE ````\r\n`````\n", content)
            self.assertEqual(content.count(b"REQUIRED_OUTPUT_CONSTRAINT"), 1)
            self.assertEqual(len(content.decode("utf-8")), part["chars"])
            self.assertLessEqual(part["chars"], config["max_context_chars"])
            self.assertEqual([entry["path"] for entry in part["sources"]], ["Source.php"])
            for entry in part["sources"][0]["character_slices"]:
                self.assertEqual(entry["line"], 2)
                focus_ranges.append((entry["focus_start_column"], entry["focus_end_column"]))
        self.assertEqual(focus_ranges[0][0], 1)
        self.assertEqual(focus_ranges[-1][1], len(long_line))
        for earlier, later in zip(focus_ranges, focus_ranges[1:]):
            self.assertEqual(earlier[1] + 1, later[0])
        self.assertEqual(result["context_chars"], sum(part["chars"] for part in parts))
        worktrees = self.git("worktree", "list", "--porcelain", "-z").split("\0")
        worktree_paths = [Path(entry.removeprefix("worktree ")) for entry in worktrees if entry.startswith("worktree ")]
        self.assertIn(Path(result["worktree_path"]), worktree_paths)
        with closing(sqlite3.connect(Path(config["run_root"]) / "checkpoints.sqlite")) as db:
            count = db.execute("SELECT COUNT(*) FROM checkpoints WHERE thread_id = ?", (run_id,)).fetchone()[0]
        self.assertGreater(count, 0)

    def test_excess_shared_rules_fail_before_creating_a_worktree(self):
        (self.project / RULES["path"]).write_bytes(b"x" * 1001 + b"\n")
        config = load_config(self.config_path(PHP, RULES))
        with self.assertRaisesRegex(ValueError, r"shared context exceeds max_context_chars \(1000\): rules.md"):
            workflow.prepare_run(config, "example")
        self.assertFalse(Path(config["worktree_root"]).exists())
        self.assertEqual(list(Path(config["run_root"]).glob("*/run.json")), [])

    def test_narrow_segment_budget_can_fit_focus_without_overlap(self):
        line = b"//" + b"x" * 120
        self.php.write_bytes(line + b"\r")
        (self.project / RULES["path"]).write_bytes(b"r" * 530 + b"\n")
        config, result, manifest = self.prepare({**PHP, "end_line": 1}, RULES)
        self.assertGreater(result["capsule_count"], 1)
        pieces = []
        for part in manifest["parts"]:
            self.assertLessEqual(part["chars"], config["max_context_chars"])
            pieces.extend(part["sources"][0]["character_slices"])
        last = pieces[-1]
        self.assertEqual(last["focus_end_column"], len(line))
        self.assertEqual(last["focus_start_column"], last["context_start_column"])
        final_capsule = Path(result["context_paths"][-1]).read_bytes()
        self.assertIn(line[last["context_start_column"] - 1:] + b"\r", final_capsule)
        self.assertNotIn(line[last["context_start_column"] - 1:] + b"\r\n", final_capsule)

    def test_shared_rules_alone_cannot_create_a_migration_capsule(self):
        (self.project / RULES["path"]).write_bytes(b"Required rule\n")
        config = load_config(self.config_path(RULES))
        with self.assertRaisesRegex(ValueError, "requires at least one input outside shared context"):
            workflow.prepare_run(config, "example")
        self.assertFalse(Path(config["worktree_root"]).exists())

    def test_shared_option_must_be_boolean(self):
        with self.assertRaisesRegex(ValueError, "input shared must be a boolean"):
            load_config(self.config_path({**PHP, "shared": "true"}))

    def test_viewer_and_anchors_share_original_php_after_edit_and_delete(self):
        source_text = '<?php\r\necho "日本語";\r\n// Last line\r\n'
        raw = source_text.encode("euc-jp")
        self.php.write_bytes(raw)
        second = self.project / "Second.PHP"
        second_raw = "<?php\necho 200;\n".encode("utf-8-sig")
        second.write_bytes(second_raw)
        config, result, manifest = self.prepare(
            {**PHP, "start_line": 2}, {**PHP, "start_line": 3, "end_line": 3}, {**PHP, "path": second.name},
        )
        run_path = Path(result["run_path"])
        run_id = result["run_id"]
        pins = manifest["php_sources"]
        self.assertEqual([(pin["path"], pin["encoding"]) for pin in pins], [
            (self.php.name, "euc-jp"), (second.name, "utf-8-sig"),
        ])
        for pin, original in zip(pins, [raw, second_raw], strict=True):
            self.assertEqual((run_path / "capsule" / pin["snapshot"]).read_bytes(), original)
            self.assertEqual(pin["sha256"], hashlib.sha256(original).hexdigest())
        context = Path(result["context_path"]).read_bytes().decode("utf-8")
        self.assertIn('echo "日本語";\r\n', context)
        self.assertEqual(context.count("     1 | <?php"), 1)

        self.php.write_bytes(b"<?php\necho 999;\n")
        target = Path(result["worktree_path"]) / "Target.cs"
        target.write_bytes(b"class Reviewed {}\r\n")
        target_hashes = {}
        _, edited = capture_workspace(config, run_id, target_hashes=target_hashes)
        self.assertEqual(target_hashes["Target.cs"], hashlib.sha256(target.read_bytes()).hexdigest())
        self.assertEqual(edited["workspace"]["leftText"], source_text.replace("\r\n", "\n") + "<?php\necho 200;\n")
        self.assertEqual(edited["workspace"]["rightText"], "class Reviewed {}\n")
        self.php.unlink()
        second.unlink()
        _, deleted = capture_workspace(config, run_id)
        self.assertEqual(edited["workspace"], deleted["workspace"])

        edited["workspace"]["anchors"]["manualAnchors"] = [{"leftLineNo": 1, "rightLineNo": 0}]
        reference = self.root / "reference.json"
        reference.write_text(json.dumps(edited), encoding="utf-8")

        def junie(command, completion, token, proposal):
            numbered_php = (proposal.parent / "left.txt").read_text(encoding="utf-8")
            self.assertIn('2\techo "日本語";\n', numbered_php)
            self.assertIn("5\techo 200;\n", numbered_php)
            proposal.write_text('[{"leftLineNo": 2, "rightLineNo": 1}]', encoding="utf-8")
            completion.write_text(token, encoding="ascii")

        with (
            patch.object(review_anchors, "_validate_junie_execution"),
            patch.object(review_anchors, "_run_interactive_junie", side_effect=junie),
        ):
            snapshot, count = review_anchors.generate_anchors(config, run_id, reference)
        self.assertEqual(count, 1)
        self.assertEqual(self.read_json(snapshot)["workspace"], edited["workspace"])

    def test_missing_or_changed_pin_fails_even_if_live_php_exists(self):
        config, result, manifest = self.prepare(PHP)
        pin_path = Path(result["capsule_path"]) / manifest["php_sources"][0]["snapshot"]
        pin_path.write_bytes(b"<?php\necho 999;\n")
        with self.assertRaisesRegex(ValueError, "ハッシュが一致しません: Source.php"):
            capture_workspace(config, result["run_id"])
        pin_path.unlink()
        with self.assertRaisesRegex(ValueError, "php-sources/0001.php"):
            capture_workspace(config, result["run_id"])

    def test_legacy_manifest_reads_live_php_and_resumes_saved_context(self):
        self.php.write_bytes('<?php\necho "日本語";\n'.encode("euc-jp"))
        config, result, manifest = self.prepare(PHP)
        manifest.pop("php_sources")
        manifest.pop("shared_sources")
        manifest_path = Path(result["capsule_path"]) / "manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        expected_context = Path(result["context_path"]).read_bytes()
        self.php.write_bytes('<?php\necho "変更";\n'.encode("euc-jp"))
        config["units"][0]["inputs"][0]["encoding"] = "utf-8"
        _, snapshot = capture_workspace(config, result["run_id"])
        self.assertEqual(snapshot["workspace"]["leftText"], '<?php\necho "変更";\n')
        self.php.unlink()
        with self.assertRaisesRegex(ValueError, "Source.php"):
            capture_workspace(config, result["run_id"])

        def junie(command, completion, token, target):
            self.assertEqual((target.parent / ".migration-context.md").read_bytes(), expected_context)
            return 0

        with (
            patch.object(workflow, "_validate_junie_execution"),
            patch.object(workflow, "_run_interactive_junie", side_effect=junie) as invocation,
        ):
            resumed = workflow.resume_with_junie(config, result["run_id"])
        invocation.assert_called_once()
        self.assertEqual(resumed["status"], "interrupted")

    def test_windows_text_write_preserves_lf_crlf_and_heredoc(self):
        original_open = Path.open

        def windows_open(path, mode="r", buffering=-1, encoding=None, errors=None, newline=None):
            if path.name == "context.md" and mode == "w" and newline is None:
                newline = "\r\n"
            return original_open(path, mode, buffering, encoding, errors, newline)

        for ending in ("\n", "\r\n", "\r"):
            with self.subTest(ending=repr(ending)):
                lines = ["<?php", "$s = <<<TXT", "日本語", "second line", "TXT;"]
                self.php.write_bytes((ending.join(lines) + ending).encode("utf-8"))
                with patch.object(Path, "open", windows_open):
                    config, result, manifest = self.prepare({**PHP, "end_line": len(lines), "encoding": "utf-8"})
                content = Path(result["context_path"]).read_bytes()
                expected = "".join(f"{number:>6} | {line}{ending}" for number, line in enumerate(lines, 1))
                self.assertIn(expected.encode("utf-8"), content)
                self.assertTrue(content.startswith(b"# Migration unit: example\n"))
                self.assertNotIn(b"\r\r\n", content)
                chars = len(content.decode("utf-8"))
                self.assertEqual(chars, manifest["parts"][0]["chars"])
                self.assertEqual(chars, result["context_chars"])
                self.assertLessEqual(chars, config["max_context_chars"])


if __name__ == "__main__":
    unittest.main()
