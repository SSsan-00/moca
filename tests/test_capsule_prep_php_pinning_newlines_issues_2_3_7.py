import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from fixtures import HarnessCase
from migration_harness import capsules, cli, junie_cli, review_anchors, workflow
from migration_harness.config import load_config
from migration_harness.diff_viewer import capture_workspace


PHP = {"path": "Source.php", "role": "php", "start_line": 1, "end_line": 2}
RULES = {"path": "rules.md", "role": "migration_rules", "start_line": 1, "end_line": 1}
CONSTRAINT = {"path": "constraints.md", "role": "target_constraints", "shared": True, "start_line": 1, "end_line": 1}


class CapsulePreparationTests(HarnessCase):
    """カプセルの上限・固定した原文・改行を準備経路から検証する。"""

    def setUp(self):
        super().setUp()
        self.php = self.project / PHP["path"]
        self.php.write_bytes(b"<?php\necho 100;\n")
        (self.project / "Target.cs").write_bytes(b"class Target {}\n")
        self.commit_project()

    def config_path(self, *inputs):
        """入力の順序や範囲をテストごとに変更できるTOMLを作る。"""
        path = self.root / "harness.toml"
        sections = [
            '[project]\nroot = "app"\nworktree_root = "worktrees"\nrun_root = "runs"\nmax_context_chars = 1000',
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
        """現在の状態を、ディスクに保存されたUTF-8のJSONから読む。"""
        return json.loads(path.read_text(encoding="utf-8"))

    def test_japanese_unit_runs_entire_php_with_only_source_and_targets(self):
        source_lines = ["<?php", *[f"// メニュー項目 {number:02} " + "x" * 70 for number in range(30)], "if ("]
        source_bytes = "\r\n".join(source_lines).encode("utf-8-sig")
        self.php.write_bytes(source_bytes)
        targets = ["Pages/Menu.cshtml.cs", "Pages/Menu.cshtml"]
        path = self.config_path()
        project_settings = path.read_text(encoding="utf-8").split("[[units]]", 1)[0]
        path.write_text(
            project_settings + '[[units]]\nid = "メニュー"\nsource = "Source.php"\n'
            + f"targets = {json.dumps(targets)}\n",
            encoding="utf-8",
        )
        observed_lines = []
        final_reviews = []

        def junie(command, completion, token, agent_targets, *, report_usage=None, prompts=None):
            self.assertIn("`メニュー`", command[-1])
            final_reviews.append("Check exact branch conditions and selector strings" in command[-1])
            self.assertIn("Verify the full accumulated sequence", command[-1])
            self.assertIn("values, order, duplicates and PHP literal decoding", command[-1])
            self.assertIn("Use direct source and target reads", command[-1])
            self.assertIn("For long or repetitive data, use mechanical extraction and comparison", command[-1])
            self.assertIn("Do not create a temporary script when direct reads or the supplied helper suffice", command[-1])
            context = (completion.parent / ".migration-context.md").read_bytes().decode("utf-8")
            observed_lines.extend(re.findall(r"^ *([0-9]+) \| ([^\r\n]*)", context, re.MULTILINE))
            accumulated = "".join(p.read_bytes().decode("utf-8") for p in sorted(
                (completion.parent / ".migration-materials").glob("source-*.md")
            ))
            self.assertEqual(re.findall(r"^ *([0-9]+) \| ([^\r\n]*)", accumulated, re.MULTILINE), observed_lines)
            for target in agent_targets:
                target.write_text(f"migrated {target.name}", encoding="utf-8")
            completion.write_text(token, encoding="ascii")
            return 0

        run_process = subprocess.run

        def build_or_run_git(command, **kwargs):
            if command == ["dotnet", "build"]:
                return subprocess.CompletedProcess(command, 0)
            return run_process(command, **kwargs)

        args = ["php-to-csharp-harness", "run", "--config", str(path), "--unit", "メニュー"]
        with (
            patch("sys.argv", args), redirect_stdout(io.StringIO()),
            patch.object(junie_cli, "require_interactive_cli"),
            patch.object(junie_cli, "run_interactive", side_effect=junie) as agent,
            patch.object(workflow.subprocess, "run", side_effect=build_or_run_git),
        ):
            cli.main()

        self.assertGreater(agent.call_count, 1)
        self.assertEqual(final_reviews, [False] * (agent.call_count - 1) + [True])
        self.assertEqual(observed_lines, [(str(number), line) for number, line in enumerate(source_lines, 1)])
        record_path, = (self.root / "runs").glob("*/run.json")
        record = self.read_json(record_path)
        self.assertEqual(record["unit_id"], "メニュー")
        self.assertEqual(record["status"], "build_succeeded")
        self.assertEqual(record["completed_capsules"], agent.call_count)
        for name in targets:
            target = Path(record["worktree_path"]) / name
            self.assertEqual(target.read_bytes(), f"migrated {target.name}".encode("utf-8-sig"))
        manifest = self.read_json(record_path.parent / "capsule" / "manifest.json")
        pin, = manifest["php_sources"]
        self.assertEqual((record_path.parent / "capsule" / pin["snapshot"]).read_bytes(), source_bytes)
        ledger = workflow.migration_ledger(load_config(path), record["run_id"])
        covered_lines = [
            number for capsule in ledger["capsules"] for source in capsule["sources"]
            for number in range(source["start_line"], source["end_line"] + 1)
        ]
        self.assertEqual(covered_lines, list(range(1, len(source_lines) + 1)))

    def test_all_capsules_keep_rules_and_can_access_pinned_reference_pairs(self):
        self.php.write_bytes(b"<?php\n" + b"echo 100;\n" * 100)
        shared_inputs = [
            {"path": "common.md", "role": "migration_rules"},
            {"path": "razor.md", "role": "migration_rules"},
            {"path": "orders/example.php", "role": "reference_php", "shared": True},
            {"path": "orders/Example.cs", "role": "reference_csharp", "shared": True},
            {"path": "search/example.php", "role": "reference_php", "shared": True},
            {"path": "search/Example.cshtml", "role": "reference_razor", "shared": True},
        ]
        for index, item in enumerate(shared_inputs):
            path = self.project / item["path"]
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(f"first {index}\nlast {index}".encode("ascii"))

        path = self.config_path({**PHP, "end_line": 101}, shared_inputs[1])
        common = [
            '[[project.migration_rules]]\npath = "common.md"\nstart_line = 2\nend_line = 2',
            *[
                "[[project.references]]\n" + "\n".join(
                    f"{key} = {json.dumps(item[key])}" for key in ("role", "path")
                ) + "\nstart_line = 1\nend_line = 1"
                for item in shared_inputs[2:]
            ],
        ]
        path.write_text(path.read_text(encoding="utf-8") + "\n\n" + "\n\n".join([
            '[[units]]\nid = "second"\nsource = "Source.php"\ntarget = "Target.cs"',
            *common,
        ]), encoding="utf-8")
        config = load_config(path)
        self.assertEqual(config["max_repair_attempts"], 2)
        large_example = self.project / shared_inputs[-1]["path"]
        large_example.write_text("first 5\n" + "x" * 1200 + "\nlast 5", encoding="utf-8")
        for unit_id in ("example", "second"):
            result = workflow.prepare_run(config, unit_id)
            capsule = Path(result["capsule_path"])
            manifest = self.read_json(capsule / "manifest.json")
            self.assertGreater(result["capsule_count"], 1)
            self.assertEqual(
                [(entry["path"], entry["start_line"], entry["end_line"]) for entry in manifest["shared_sources"]],
                [(item["path"], 1, 2) for item in shared_inputs[:2]],
            )
            references = manifest["reference_sources"]
            self.assertEqual([entry["path"] for entry in references], [item["path"] for item in shared_inputs[2:]])
            self.assertEqual([entry["end_line"] for entry in references], [2, 2, 2, 3])
            agent = self.root / f"agent-{unit_id}"
            agent.mkdir()
            prompt = workflow._stage_migration_materials(capsule, manifest, agent, repair=True)
            self.assertTrue((agent / ".migration-materials/guidelines.md").is_file())
            self.assertIn("references.json", prompt)
            self.assertIn("last 5", (agent / ".migration-materials" / references[-1]["file"]).read_text())
            rules = "".join((agent / ".migration-materials" / entry["file"]).read_text() for entry in manifest["rule_files"])
            for part in manifest["parts"]:
                self.assertEqual([entry["path"] for entry in part["sources"]], [PHP["path"]])
                context = Path(part["path"]).read_text(encoding="utf-8")
                for index in range(2):
                    self.assertIn(f"     1 | first {index}\n     2 | last {index}", rules)
                    self.assertNotIn(f"first {index}", context)
                self.assertNotIn("last 5", context)
            first, second = manifest["parts"][:2]
            following = Path(first["path"]).with_name("boundary.md").read_text()
            previous = Path(second["path"]).with_name("boundary.md").read_text()
            self.assertIn(f"{second['sources'][0]['start_line']:>6} |", following)
            self.assertIn(f"{first['sources'][0]['end_line']:>6} |", previous)

    def test_scripts_run_in_phase_order_and_resume_verification_after_failure(self):
        log = self.root / "script-order.txt"
        script = (
            "from pathlib import Path\n"
            "import sys\n"
            f"with Path({str(log)!r}).open('a', encoding='utf-8') as stream:\n"
            "    print(sys.argv[1], Path.cwd().name, file=stream)\n"
        )

        def command(name):
            return [sys.executable, "-c", script, name]
        phases = ["before_prepare", "after_prepare", "before_migration", "after_migration", "before_build",
                  "after_build", "before_checker", "after_checker"]
        config_path = self.config_path(PHP)
        config_path.write_text(config_path.read_text(encoding="utf-8") + "\n\n[scripts]\n" + "\n".join(
            f"{phase} = {json.dumps([command(phase)])}" for phase in phases
        ), encoding="utf-8")
        config = load_config(config_path)
        config["scripts"]["before_prepare"].append(command("second_prepare"))
        draft = self.root / "generated files" / "Services" / "Draft.cs"
        draft.parent.mkdir(parents=True)
        draft.write_bytes(b"class Draft {}\r\n")
        config["scripts"]["before_migration"].append(["@copy-migration-draft", str(draft), "Target.cs"])
        config["scripts"]["after_build"].append([sys.executable, "-c", "raise SystemExit(7)"])
        config["checker_command"] = [sys.executable, "-c", "pass"]
        subprocess_run = subprocess.run
        calls = []

        def run_process(args, **kwargs):
            if args in (["dotnet", "build"], config["checker_command"]):
                calls.append("dotnet" if args[0] == "dotnet" else "fixture-checker")
                return subprocess.CompletedProcess(args, 0)
            return subprocess_run(args, **kwargs)

        def migrate(args, completion, token, targets, *, report_usage=None, prompts=None):
            target, = targets
            calls.append("junie")
            self.assertEqual(target.read_bytes(), draft.read_bytes())
            target.write_text("class Migrated {}", encoding="utf-8")
            completion.write_text(token, encoding="ascii")
            return 0

        with (
            patch.object(junie_cli, "require_interactive_cli"),
            patch.object(junie_cli, "run_interactive", side_effect=migrate),
            patch.object(workflow.subprocess, "run", side_effect=run_process),
            redirect_stdout(io.StringIO()),
        ):
            with self.assertRaises(workflow.RunScriptError) as failure:
                workflow.run_with_junie(config, "example")
            run_id = failure.exception.run_id
            record_path = Path(config["run_root"]) / run_id / "run.json"
            record = self.read_json(record_path)
            self.assertEqual(record["status"], "script_failed")
            self.assertEqual(record["script_phase"], "after_build", record["error"])
            self.assertIn("終了コード 7", record["error"])
            self.assertFalse(workflow.migration_ledger(config, run_id)["completion"]["verification_passed"])
            config["scripts"]["after_build"].pop()
            result = workflow.resume_with_junie(config, run_id)

        project = self.project.name
        self.assertEqual(log.read_text(encoding="utf-8").splitlines(), [
            f"before_prepare {project}", f"second_prepare {project}",
            *[f"{phase} {run_id}" for phase in phases[1:6]],
            *[f"{phase} {run_id}" for phase in phases[4:]],
        ])
        self.assertEqual(calls, ["junie", "dotnet", "dotnet", "fixture-checker"])
        self.assertEqual(result["status"], "checker_succeeded")
        self.assertTrue(workflow.migration_ledger(config, run_id)["completion"]["verification_passed"])

    def test_preparation_resume_skips_successful_commands_within_the_same_phase(self):
        path = self.config_path(PHP)
        config = load_config(path)
        counter = self.root / "command-results.txt"
        append = [sys.executable, "-c",
                  "import sys; open(sys.argv[1], 'a').write('done\\n')", str(counter)]
        config["scripts"] = {"after_prepare": [append, [sys.executable, "-c", "raise SystemExit(9)"]]}
        with self.assertRaises(workflow.RunScriptError) as failure:
            workflow.prepare_run(config, "example")
        run_id = failure.exception.run_id
        config["scripts"]["after_prepare"][1] = [sys.executable, "-c", "pass"]

        def migrate(args, completion, token, targets, *, report_usage=None, prompts=None):
            target, = targets
            target.write_text("class Migrated {}")
            completion.write_text(token)
            return 0

        with (
            patch.object(junie_cli, "require_interactive_cli"),
            patch.object(junie_cli, "run_interactive", side_effect=migrate),
            patch.object(workflow, "_verify_worktree", side_effect=lambda config, unit, result, record: result),
        ):
            result = workflow.resume_with_junie(config, run_id)
        self.assertEqual(result["completed_capsules"], 1)
        self.assertEqual(counter.read_text().splitlines(), ["done"])

    # 中断と再開をまたいで、各カプセルに同じ共通資料が含まれることを検証する。
    def test_shared_rules_and_constraints_survive_interruption_and_resume(self):
        long_line = "$value = '" + "abcd" * 655 + "';"
        self.php.write_bytes(f"<?php\r\n{long_line}\r\necho $value;\r\n".encode("ascii"))
        rules = self.project / RULES["path"]
        rules.write_bytes(b"REQUIRED_MIGRATION_RULE ````\r\n")
        constraint = self.project / CONSTRAINT["path"]
        constraint.write_bytes(b"REQUIRED_OUTPUT_CONSTRAINT\n")
        config = load_config(self.config_path({**PHP, "end_line": 3}, CONSTRAINT, RULES))
        observed_contexts = []

        def junie(command, completion, token, targets, *, report_usage=None, prefer_task=False, prompts=None):
            target, = targets
            guidelines = Path(command[command.index("--ide-guidelines") + 1])
            self.assertIn(b"REQUIRED_MIGRATION_RULE", guidelines.read_bytes())
            self.assertNotIn("migration-read-rules.py", command[-1])
            observed_contexts.append((target.parent / ".migration-context.md").read_bytes())
            if len(observed_contexts) == 2:
                target.write_bytes(b"class Partial {}\n")
                junie_cli.session_closed_observer.get()()
                raise KeyboardInterrupt
            if len(observed_contexts) == 3:
                self.assertEqual(target.read_bytes(), b"class Partial {}\n")
                self.assertIn("This capsule was interrupted", command[-1])
            completion.write_text(token, encoding="ascii")
            return 0

        with (
            patch.object(junie_cli, "require_interactive_cli"),
            patch.object(junie_cli, "run_interactive", side_effect=junie),
            patch.object(workflow, "_verify_worktree", side_effect=lambda config, unit, result, record: result) as verify,
        ):
            with self.assertRaises(workflow.RunInterrupted) as interrupted:
                workflow.run_with_junie(config, "example")
            run_id = interrupted.exception.run_id
            run_path = Path(config["run_root"]) / run_id
            record_path = run_path / "run.json"
            interrupted_record = self.read_json(record_path)
            self.assertEqual(interrupted_record["completed_capsules"], 1)
            self.assertEqual(interrupted_record["current_capsule"], 2)
            saved_draft = run_path / interrupted_record["stopped_migration_draft"]["snapshot"]
            self.assertEqual((saved_draft / "0.draft").read_bytes(), b"class Partial {}\n")
            previous = Path(interrupted_record["worktree_path"]) / interrupted_record["target"]
            self.assertNotIn(b"Partial", previous.read_bytes())
            rules.write_bytes(b"CHANGED_RULE\n")
            constraint.unlink()
            plan = workflow.plan_migration_resume(config, run_id, 2)
            request = {**plan, "id": "fixture-range"}
            agent_target = run_path / "agent-workspace" / interrupted_record["target"]
            prior = agent_target.read_bytes()
            agent_target.write_bytes(b"class ChangedSincePlan {}")
            with self.assertRaisesRegex(ValueError, "更新"):
                workflow.resume_with_junie(config, run_id, verification_request=request)
            agent_target.write_bytes(prior)
            bounded = workflow.resume_with_junie(config, run_id, verification_request=request)
            self.assertEqual(bounded["completed_capsules"], 2)
            self.assertEqual(bounded["status"], "interrupted")
            saved = self.read_json(record_path)
            self.assertEqual(saved["current_capsule"], 3)
            self.assertEqual(saved["resume_operations"][-1]["capsule_range"], [2, 2])
            verify.assert_not_called()
            result = workflow.resume_with_junie(config, run_id)
            saved = self.read_json(record_path)
            saved.update(status="build_succeeded", build_exit_code=0, checker_reviews=[{"old": True}],
                         migration_reviews={"old": True}, migration_mappings={"old": True})
            record_path.write_text(json.dumps(saved), encoding="utf-8")
            worktree_target = Path(saved["worktree_path"]) / saved["target"]
            latest = worktree_target.read_bytes()
            outside = worktree_target.parent / "outside.txt"
            outside.write_bytes(b"operator's edit")
            rewind = workflow.plan_migration_resume(config, run_id, 2, rewind_to=2)
            self.assertEqual(rewind["capsule_range"], [2, 2])
            worktree_target.write_bytes(b"manual conflict")
            with self.assertRaises(ValueError):
                workflow.resume_with_junie(config, run_id, verification_request={**rewind, "id": "replay"})
            worktree_target.write_bytes(latest)

            def replay(_command, completion, token, targets, **_kwargs):
                self.assertNotIn(b"Partial", targets[0].read_bytes() if targets[0].exists() else b"")
                self.assertEqual(worktree_target.read_bytes(), latest)
                targets[0].write_bytes(b"class Replayed {}")
                completion.write_text(token, encoding="ascii")
                return 0

            with patch.object(workflow, "_restore_snapshot_targets", side_effect=OSError("fixture interruption")):
                with self.assertRaisesRegex(OSError, "fixture interruption"):
                    workflow.resume_with_junie(config, run_id, verification_request={**rewind, "id": "replay"})
            self.assertEqual(worktree_target.read_bytes(), latest)
            with patch.object(junie_cli, "run_interactive", side_effect=replay):
                replayed = workflow.resume_with_junie(config, run_id)
            self.assertEqual(replayed["completed_capsules"], 2)
            self.assertEqual(outside.read_bytes(), b"operator's edit")
            after = self.read_json(record_path)
            self.assertNotIn("build_exit_code", after)
            self.assertNotIn("checker_reviews", after)
            self.assertNotIn("migration_reviews", after)
            self.assertNotIn("migration_mappings", after)
            backup = run_path / after["resume_operations"][-1]["rewind_backup"]["snapshot"] / "0.draft"
            self.assertEqual(backup.read_bytes(), latest)

        manifest = self.read_json(run_path / "capsule" / "manifest.json")
        parts = manifest["parts"]
        saved_contexts = [Path(part["path"]).read_bytes() for part in parts]
        self.assertGreater(len(parts), 2)
        self.assertEqual(observed_contexts, saved_contexts[:2] + saved_contexts[1:])
        self.assertEqual(result["completed_capsules"], len(parts))
        self.assertEqual(self.read_json(record_path)["resume_attempts"], 3)
        self.assertEqual([entry["path"] for entry in manifest["shared_sources"]], ["rules.md", "constraints.md"])
        saved_rules = b"".join((run_path / "capsule/materials" / chunk["file"]).read_bytes() for chunk in manifest["rule_files"])
        self.assertIn(b"REQUIRED_MIGRATION_RULE ````\r\n`````\n", saved_rules)
        self.assertNotIn(b"CHANGED_RULE", saved_rules)
        focus_ranges = []
        for part, content in zip(parts, saved_contexts, strict=True):
            self.assertNotIn(b"REQUIRED_MIGRATION_RULE", content)
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
        worktrees = self.git("worktree", "list", "--porcelain", "-z").stdout.decode("utf-8").split("\0")
        worktree_paths = [Path(entry.removeprefix("worktree ")) for entry in worktrees if entry.startswith("worktree ")]
        self.assertIn(Path(result["worktree_path"]), worktree_paths)
        self.assertFalse((Path(config["run_root"]) / "checkpoints.sqlite").exists())

    # 共通資料が上限を超えた場合にworktreeが残らないことを検証する。
    def test_excess_shared_constraints_fail_before_creating_a_worktree(self):
        (self.project / RULES["path"]).write_bytes(b"x" * 1001 + b"\n")
        config = load_config(self.config_path(PHP, {**CONSTRAINT, "path": RULES["path"]}))
        original_worktrees = self.git("worktree", "list", "--porcelain", "-z").stdout
        with self.assertRaisesRegex(ValueError, r"shared context exceeds max_context_chars \(1000\): rules.md"):
            workflow.prepare_run(config, "example")
        self.assertFalse(Path(config["worktree_root"]).exists())
        self.assertEqual(self.git("worktree", "list", "--porcelain", "-z").stdout, original_worktrees)
        record_path, = Path(config["run_root"]).glob("*/run.json")
        record = json.loads(record_path.read_bytes())
        self.assertEqual(record["status"], "preparation_failed")
        self.assertNotIn("worktree_path", record)
        self.assertIsNone(record["active_stage"])

    # 共通資料で余裕が少ない場合も、必須範囲を欠落させないことを検証する。
    def test_narrow_segment_budget_can_fit_focus_without_overlap(self):
        line = b"//" + b"x" * 120
        self.php.write_bytes(line + b"\r")
        (self.project / RULES["path"]).write_bytes(b"r" * 530 + b"\n")
        config, result, manifest = self.prepare({**PHP, "end_line": 1}, {**CONSTRAINT, "path": RULES["path"]})
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

    def test_long_physical_lines_bound_candidate_scans_by_capsule_size(self):
        budget = 10_000
        for size in (256 * 1024, 2 * 1024 * 1024):
            with self.subTest(size=size):
                body = "<?php /*" + ("abc``日本語" * (size // 8)) + "*/"
                self.php.write_bytes((body + "\r\n").encode())
                config = load_config(self.config_path({**PHP, "end_line": 1, "encoding": "utf-8"}))
                config["max_context_chars"] = budget
                scanned = 0
                find_backticks = capsules._max_backtick_run

                def observe(text):
                    nonlocal scanned
                    self.assertLessEqual(len(text), budget)
                    scanned += len(text)
                    return find_backticks(text)

                with patch.object(capsules, "_max_backtick_run", side_effect=observe):
                    result = workflow.prepare_run(config, "example")
                _, manifest = capsules.load_capsule_manifest(Path(result["run_path"]))
                self.assertLess(scanned, len(body) * 17)
                slices = [item for part in manifest["parts"] for source in part["sources"]
                          for item in source["character_slices"]]
                self.assertEqual(slices[0]["focus_start_column"], 1)
                self.assertEqual(slices[-1]["focus_end_column"], len(body))
                self.assertTrue(all(left["focus_end_column"] + 1 == right["focus_start_column"]
                                    for left, right in zip(slices, slices[1:])))

    def test_conflicting_php_encoding_for_path_aliases_is_rejected_before_worktree(self):
        self.php.write_bytes(b'<?php\n$text = "\xc2\xa2";\n')
        reference = {**PHP, "path": "./Source.php", "role": "reference_php", "shared": True, "encoding": "euc-jp"}
        config = load_config(self.config_path(reference, {**PHP, "encoding": "utf8"}))
        with self.assertRaisesRegex(ValueError, r"Source.php: inconsistent.*reference_php.*euc-jp.*php.*utf-8"):
            workflow.prepare_run(config, "example")
        self.assertFalse(Path(config["worktree_root"]).exists())

    def test_shared_reference_and_migration_use_one_effective_php_encoding(self):
        self.php.write_bytes(b"\xef\xbb\xbf<?php\necho 42;\n")
        reference = {**PHP, "path": "./Source.php", "role": "reference_php", "shared": True, "encoding": "auto"}
        config, result, manifest = self.prepare(reference, {**PHP, "encoding": "utf_8_sig"})
        self.assertEqual([(pin["path"], pin["encoding"]) for pin in manifest["php_sources"]], [("Source.php", "utf-8-sig")])
        self.assertEqual(manifest["parts"][0]["sources"][0]["encoding"], "utf-8-sig")
        _, comparison = capture_workspace(config, result["run_id"])
        self.assertEqual(comparison["workspace"]["leftText"], "<?php\necho 42;\n")

    # 移行対象がない設定を移行カプセルとして数えないことを検証する。
    def test_shared_rules_alone_cannot_create_a_migration_capsule(self):
        (self.project / RULES["path"]).write_bytes(b"Required rule\n")
        config = load_config(self.config_path(RULES))
        with self.assertRaisesRegex(ValueError, "requires at least one input outside shared context"):
            workflow.prepare_run(config, "example")
        self.assertFalse(Path(config["worktree_root"]).exists())

    # 共通資料の指定に文字列を使った場合に設定を拒否することを検証する。
    def test_shared_option_must_be_boolean(self):
        with self.assertRaisesRegex(ValueError, "input shared must be a boolean"):
            load_config(self.config_path({**PHP, "shared": "true"}))

    # 元PHPを変更・削除しても、アンカーと通常比較で同じ固定版を使うことを検証する。
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
        config["diff_viewer_html"] = "viewer.html"

        def junie(command, completion, token, targets, *, report_usage=None, prefer_task=False, prompts=None):
            proposal, = targets
            numbered_php = (proposal.parent / "left.txt").read_text(encoding="utf-8")
            self.assertIn('2\techo "日本語";\n', numbered_php)
            self.assertIn("5\techo 200;\n", numbered_php)
            proposal.write_text('[{"leftLineNo": 2, "rightLineNo": 1}]', encoding="utf-8")
            completion.write_text(token, encoding="ascii")

        with (
            patch.object(junie_cli, "require_interactive_cli"),
            patch.object(junie_cli, "run_interactive", side_effect=junie),
        ):
            snapshot, count = review_anchors.generate_anchors(config, run_id, reference)
        self.assertEqual(count, 1)
        self.assertEqual(self.read_json(snapshot)["workspace"], edited["workspace"])

    def test_anchors_work_without_a_reference_and_reuse_reviewed_examples(self):
        config, run, _ = self.prepare(PHP)
        run_id = run["run_id"]
        record_path = Path(run["run_path"]) / "run.json"
        record = self.read_json(record_path)
        record.update(status="build_succeeded", completed_capsules=1)
        record_path.write_text(json.dumps(record), encoding="utf-8")
        observed = []

        def junie(command, completion, token, targets, *, report_usage=None, prefer_task=False, prompts=None):
            proposal, = targets
            guide = self.read_json(proposal.parent / "guide.json")
            observed.append(guide["examples"])
            proposal.write_text('[{"leftLineNo": 2, "rightLineNo": 1}]', encoding="utf-8")
            completion.write_text(token, encoding="ascii")

        with (
            patch.object(junie_cli, "require_interactive_cli") as validate,
            patch.object(junie_cli, "run_interactive", side_effect=junie),
        ):
            with self.assertRaisesRegex(ValueError, "差分ビューアーを有効に"):
                review_anchors.generate_anchors(config, run_id)
            validate.assert_not_called()
            config["diff_viewer_html"] = "viewer.html"
            review_anchors.generate_anchors(config, run_id)
            workflow.review_migration_ranges(config, run_id, ["1:1"], "confirmed", "行の対応を確認")
            review_anchors.generate_anchors(config, run_id)
            (Path(run["worktree_path"]) / "Target.cs").write_text("class Changed {}\n", encoding="utf-8")
            review_anchors.generate_anchors(config, run_id)
        self.assertEqual(observed, [[], [{"left": "echo 100;", "right": "class Target {}"}], []])

    # 固定版が欠落したときに現在のPHPへ黙って切り替えないことを検証する。
    def test_missing_or_changed_pin_fails_even_if_live_php_exists(self):
        config, result, manifest = self.prepare(PHP)
        pin_path = Path(result["capsule_path"]) / manifest["php_sources"][0]["snapshot"]
        pin_path.write_bytes(b"<?php\necho 999;\n")
        with self.assertRaisesRegex(ValueError, "ハッシュが一致しません: Source.php"):
            capture_workspace(config, result["run_id"])
        pin_path.unlink()
        with self.assertRaisesRegex(ValueError, "php-sources/0001.php"):
            capture_workspace(config, result["run_id"])

    # 固定版を持たないRunの比較と、保存済みカプセルからの再開を検証する。
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

        def junie(command, completion, token, targets, *, report_usage=None, prefer_task=False, prompts=None):
            target, = targets
            self.assertEqual((target.parent / ".migration-context.md").read_bytes(), expected_context)
            return 0

        with (
            patch.object(junie_cli, "require_interactive_cli"),
            patch.object(junie_cli, "run_interactive", side_effect=junie) as invocation,
        ):
            resumed = workflow.resume_with_junie(config, result["run_id"])
        invocation.assert_called_once()
        self.assertEqual(resumed["status"], "interrupted")

    # Windowsの既定のテキスト出力で旧処理のCRLF重複を再現する。
    @unittest.skipUnless(os.name == "nt", "observes Windows default text translation")
    def test_legacy_windows_writer_doubles_crlf(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy = Path(directory) / "old-context.md"
            legacy.write_text("excerpt\r\n", encoding="utf-8")
            self.assertEqual(legacy.read_bytes(), b"excerpt\r\r\n")

    # 改行が異なるPHP抜粋とmanifestの文字数が一致することを検証する。
    def test_windows_text_write_preserves_lf_crlf_and_heredoc(self):
        original_open = Path.open

        def windows_open(path, mode="r", buffering=-1, encoding=None, errors=None, newline=None):
            """改行を明示しない書き込みにWindows相当の変換を適用する。"""
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
