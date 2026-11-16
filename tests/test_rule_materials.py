import io
import json
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from fixtures import HarnessCase
from migration_harness import junie_cli, rule_materials, workflow


class RuleMaterialsTests(HarnessCase):
    def prepare(self):
        (self.project / "source.php").write_text("<?php echo 42;\n", encoding="utf-8")
        (self.project / "Page.cs").write_text("class Page {}\n", encoding="utf-8")
        self.commit_project()
        config = self.config()
        config["units"][0]["inputs"].insert(0, {
            "path": "rules.md", "role": "migration_rules", "encoding": "utf-8",
            "start_line": 1, "end_line": None, "shared": True,
        })
        self.original = "FIRST RULE\r\n" + "保持する😀" * 1600 + "\r\nLAST RULE\r\n"
        (self.project / "rules.md").write_bytes(self.original.encode())
        result = workflow.prepare_run(config, "sample")
        capsule = Path(result["capsule_path"])
        manifest = json.loads((capsule / "manifest.json").read_bytes())
        agent = Path(result["run_path"]) / "agent-workspace"
        agent.mkdir()
        workflow._stage_migration_materials(capsule, manifest, agent)
        return capsule, manifest, agent

    def test_large_rules_are_complete_bounded_and_checked_per_invocation(self):
        capsule, manifest, agent = self.prepare()
        chunks = manifest["rule_chunks"]
        content = "".join((capsule / "materials" / item["file"]).read_bytes().decode() for item in chunks)
        self.assertIn(self.original.replace("\r\n", "\r\n     2 | ", 1).split("LAST RULE")[0], content)
        self.assertIn("LAST RULE", content)
        self.assertTrue(all(len((capsule / "materials" / item["file"]).read_bytes().decode()) <= 3000 for item in chunks))
        self.assertEqual(len(manifest["parts"]), 1)  # ルールだけでカプセル上限を使い切らない。
        token = "a" * 32
        output = subprocess.run([
            sys.executable, str(agent / "migration-read-rules.py"), token, chunks[-1]["file"],
        ], capture_output=True, check=True)
        self.assertIn(b"LAST RULE", output.stdout)
        self.assertEqual(rule_materials.missing_rule_reads(agent, token, chunks), [item["file"] for item in chunks[:-1]])
        self.assertEqual(len(rule_materials.missing_rule_reads(agent, "b" * 32, chunks)), len(chunks))
        (agent / ".migration-materials" / chunks[0]["file"]).write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "saved rule changed"):
            rule_materials.read_rule(agent, token, chunks[0]["file"])
        # 元ファイルを消しても、再開・修正には準備時のルールを再配置できる。
        (self.project / "rules.md").unlink()
        workflow._stage_migration_materials(capsule, manifest, agent, repair=True)
        self.assertEqual((agent / ".migration-materials" / chunks[0]["file"]).read_bytes(), (capsule / "materials" / chunks[0]["file"]).read_bytes())

    def test_missing_reads_are_retried_without_manual_repair(self):
        _, manifest, agent = self.prepare()
        token = "c" * 32
        completion = agent / f"migration-complete-{token}.txt"
        target = agent / "Page.cs"
        calls = []

        def run(command, complete, current_token, targets, **kwargs):
            calls.append(command[-1])
            target.write_text("class Page {}\n")
            if len(calls) == 2:
                with redirect_stdout(io.StringIO()):
                    for chunk in manifest["rule_chunks"]:
                        rule_materials.read_rule(agent, current_token, chunk["file"])
            complete.write_text(current_token)
            return 0

        with patch.object(junie_cli, "run_interactive", side_effect=run):
            result = workflow._run_junie_with_rules(["junie", "--prompt", "migrate"], completion, token, (target,), manifest, report_usage=None)
        self.assertEqual(result, 0)
        self.assertEqual(len(calls), 2)
        self.assertIn("previous completion was rejected", calls[-1])
        self.assertTrue(junie_cli.has_completion_record(completion, token))
        completion.unlink()
        (agent / rule_materials.receipt_name(token)).unlink()
        with patch.object(junie_cli, "run_interactive", side_effect=lambda *a, **kw: completion.write_text(token)) as runner:
            with self.assertRaises(junie_cli.JunieSessionError):
                workflow._run_junie_with_rules(["junie", "--prompt", "migrate"], completion, token, (target,), manifest, report_usage=None)
        self.assertEqual(runner.call_count, 3)
        self.assertFalse(completion.exists())

    def test_chunk_concatenation_preserves_unicode_crlf_and_long_lines(self):
        text = "a\r\n" + "日本語😀" * 2500 + "\r\nend"
        chunks = rule_materials.write_rule_chunks(self.root, [("rule.md", text)])
        self.assertEqual(b"".join((self.root / chunk["file"]).read_bytes() for chunk in chunks), text.encode())

    def test_old_run_reads_pinned_shared_rules_in_chunks(self):
        capsule = self.root / "capsule"
        materials = capsule / "materials"
        materials.mkdir(parents=True)
        original = ("old mandatory rules\r\n" * 200).encode()
        (materials / "shared.md").write_bytes(original)
        agent = self.root / "agent"
        agent.mkdir()
        manifest = {"materials": {"shared.md": workflow.sha256_file(materials / "shared.md")}, "parts": []}
        workflow._stage_migration_materials(capsule, manifest, agent)
        self.assertEqual(b"".join((agent / ".migration-materials" / chunk["file"]).read_bytes() for chunk in manifest["rule_chunks"]), original)
        self.assertTrue((agent / "migration-read-rules.py").is_file())
