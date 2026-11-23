import json
import os
import subprocess
import sys
from pathlib import Path

from fixtures import HarnessCase


class RuleReviewTests(HarnessCase):
    def test_record_can_advance_only_after_every_shown_page_is_reviewed(self):
        self.config()
        config = self.root / "test.toml"
        with config.open("a", encoding="utf-8") as stream:
            stream.write('\n[[project.migration_rules]]\npath = "rules.md"\n')
        lines = ["A" * 2700, "B" * 2700, "C" * 2700, "Every migration must preserve IDs."]
        (self.project / "rules.md").write_text("\n".join(lines), encoding="utf-8")
        pack = self.root / "review"
        tool = Path(__file__).resolve().parents[1] / "tools/rule_review.py"

        def run(action, *arguments):
            return subprocess.run([sys.executable, str(tool), action, str(pack), *map(str, arguments)],
                                  capture_output=True, encoding="utf-8",
                                  env={**os.environ, "PYTHONIOENCODING": "utf-8"})

        self.assertEqual(run("start", config).returncode, 0)
        self.assertEqual(run("read", 1, 2).returncode, 0)
        first = {"1": {"end": lines[0], "note": "First page", "required_lines": []}}
        self.assertIn("未確認のページ: 2", run("record", json.dumps(first), "--next").stderr)
        self.assertFalse((pack / "reading-review.json").exists())
        self.assertFalse((pack / "shown/3").exists())
        first["2"] = {"end": lines[1], "note": "Second page", "required_lines": []}
        advanced = run("record", json.dumps(first), "--next")
        self.assertEqual(advanced.returncode, 0, advanced.stderr)
        self.assertIn(lines[3], advanced.stdout)
        self.assertIn("ページ 3 終端", advanced.stdout)
        self.assertIn("未確認のページ: 3", run("check").stderr)
        tail = {"3": {"end": lines[3], "note": "Global obligation", "required_lines": [4]}}
        self.assertEqual(run("record", json.dumps(tail), "--next").returncode, 0)
        self.assertEqual(run("check").returncode, 0)
        self.assertEqual(json.loads((pack / "reading-review.json").read_text()), first | tail)

    def test_large_pack_reads_fixed_pages_and_revalidates_current_originals(self):
        self.config()
        config = self.root / "test.toml"
        with config.open("a", encoding="utf-8") as stream:
            stream.write('\n[[project.migration_rules]]\npath = "rules.md"\nencoding = "utf-8-sig"\n')
        source = self.project / "rules.md"
        source.write_bytes(("\ufeff" + ("日本語" * 1100 + "\r\n" + "末尾" + "x" * 2740 + "\r\n") * 24).encode("utf-8"))
        pack = self.root / "review"
        tool = Path(__file__).resolve().parents[1] / "tools/rule_review.py"

        def run(action, *arguments):
            return subprocess.run(
                [sys.executable, str(tool), action, str(pack), *map(str, arguments)],
                capture_output=True, encoding="utf-8", env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            )

        started = run("start", config)
        self.assertEqual(started.returncode, 0, started.stderr)
        self.assertIn("index --start 11", started.stdout)
        self.assertNotIn("11: ", started.stdout)
        index = run("index", "--start", 11, "--limit", 2)
        self.assertIn("11:", index.stdout)
        self.assertIn("12:", index.stdout)
        self.assertNotIn("13:", index.stdout)
        self.assertNotEqual(run("index", "--limit", 21).returncode, 0)
        result = run("read", 1, 2)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("日本語", result.stdout)
        page = pack / "review-pages" / "1.json"
        saved = page.read_bytes()
        page.write_bytes(saved + b" ")
        self.assertIn("固定ページ 1 が変更されました", run("read", 1).stderr)
        page.write_bytes(saved)
        original = source.read_bytes()
        source.unlink()
        self.assertEqual(run("read", 1, 2).stdout, result.stdout)
        self.assertNotEqual(run("check").returncode, 0)
        source.write_bytes(original + b"Changed")
        self.assertIn("変更されました", run("check").stderr)
        self.assertEqual(run("read", 1, 2).stdout, result.stdout)

    def test_read_batches_require_meaning_notes_before_advancing(self):
        self.config()
        config = self.root / "test.toml"
        with config.open("a", encoding="utf-8") as stream:
            stream.write('\n[[project.migration_rules]]\npath = "rules.md"\n')
        lines = ["A" * 2700, "B" * 2700, "Every migration must preserve IDs."]
        (self.project / "rules.md").write_text("\n".join(lines), encoding="utf-8")
        pack = self.root / "review"
        tool = Path(__file__).resolve().parents[1] / "tools/rule_review.py"

        def run(action, *arguments):
            return subprocess.run([sys.executable, str(tool), action, str(pack), *map(str, arguments)],
                                  capture_output=True, encoding="utf-8",
                                  env={**os.environ, "PYTHONIOENCODING": "utf-8"})

        self.assertEqual(run("start", config).returncode, 0)
        self.assertEqual(run("read", 1).returncode, 0)
        self.assertIn("未確認のページ: 1", run("read", 2).stderr)
        self.assertEqual(run("read", 1).returncode, 0)
        self.assertIn("末尾の引用", run("record", json.dumps({
            "1": {"end": "wrong", "note": "First page", "required_lines": []},
        })).stderr)
        self.assertFalse((pack / "reading-review.json").exists())
        first = {"1": {"end": lines[0], "note": "First page", "required_lines": []}}
        self.assertEqual(run("record", json.dumps(first)).returncode, 0)
        self.assertEqual(run("read", 2).returncode, 0)
        # 今の束の意味を確認するため、確認済みの前段へ戻ることはできる。
        self.assertEqual(run("read", 1).returncode, 0)
        second = {"2": {"end": lines[2], "note": "Contains a global obligation", "required_lines": [3]}}
        batch = self.root / "batch review.json"
        batch.write_text(json.dumps(second), encoding="utf-8-sig")
        self.assertEqual(run("record", "@" + str(batch)).returncode, 0)
        self.assertEqual(json.loads((pack / "reading-review.json").read_text()), first | second)
        self.assertEqual(run("check").returncode, 0)

    def test_unread_tail_and_demoted_mandatory_line_block_application(self):
        config = self.root / "settings.toml"
        original = b'''[project]
root = "app"
worktree_root = "trees"
run_root = "runs"
[[project.migration_rules]]
path = "details.md"
usage = "reference"
description = "View rules"
[[units]]
id = "one"
source = "source.php"
target = "One.cs"
'''
        config.write_bytes(original)
        detail = self.project / "details.md"
        first = "Applies only to views."
        last = "Every migration must preserve correlation IDs."
        detail.write_text(first + "\n" + "x" * 2700 + "\n" + last + "\n", encoding="utf-8")
        pack = self.root / "review"
        candidate = pack / "proposed.toml"
        tool = Path(__file__).resolve().parents[1] / "tools/rule_review.py"

        def run(*arguments, script=tool):
            return subprocess.run(
                [sys.executable, str(script), *map(str, arguments)],
                cwd=self.root, capture_output=True, encoding="utf-8",
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            )

        self.assertEqual(run("start", pack, config).returncode, 0)
        for numbers in [(1, 3), (1, 1), (1, 2, 3)]:
            self.assertNotEqual(run("read", pack, *numbers).returncode, 0)
            self.assertEqual(list((pack / "shown").iterdir()), [])
        batch = run("read", pack, 1, 2)
        self.assertEqual(batch.returncode, 0, batch.stderr)
        self.assertIn(first, batch.stdout)
        self.assertIn(last, batch.stdout)
        self.assertIn("ページ 1 終端", batch.stdout)
        self.assertIn("ページ 2 終端", batch.stdout)
        candidate.write_bytes(original)
        review = {"1": {"end": "x" * 2700, "note": "View details.", "required_lines": []}}
        review_path = pack / "reading-review.json"
        review_path.write_text(json.dumps(review), encoding="utf-8")
        # 一括表示しても、末尾ページを意味確認した記録がなければ反映できない。
        self.assertIn("未確認のページ: 2", run("apply", pack, candidate).stderr)
        self.assertEqual(config.read_bytes(), original)

        self.assertIn(last, run("read", pack, 2).stdout)
        review["2"] = {"end": last, "note": "Applies project-wide.", "required_lines": [3]}
        review_path.write_text(json.dumps(review), encoding="utf-8")
        self.assertEqual(run("check", pack).returncode, 0)
        self.assertIn("必須登録に原文がありません", run("apply", pack, candidate).stderr)
        self.assertEqual(config.read_bytes(), original)

        candidate.write_bytes(original.replace(b'usage = "reference"', b'usage = "required"'))
        detail.write_text(first + "\n" + last, encoding="utf-8")
        self.assertIn("変更されました", run("apply", pack, candidate).stderr)
        self.assertEqual(config.read_bytes(), original)
        detail.write_text(first + "\n" + "x" * 2700 + "\n" + last + "\n", encoding="utf-8")
        sources = pack / "sources"
        sources.mkdir()
        (sources / detail.name).write_bytes(detail.read_bytes())
        rule_pack = tool.with_name("rule_pack.py")
        self.assertEqual(run("snapshot", pack.name, script=rule_pack).returncode, 0)
        plan = {"documents": [
            {"path": "Agent.md", "title": "Required", "parts": [{"source": detail.name, "start": 3, "end": 3}]},
            {"path": "details/views.md", "title": "Views", "description": "When migrating views",
             "parts": [{"source": detail.name, "start": 1, "end": 2}]},
        ]}
        (pack / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
        result = run("build", pack.name, script=rule_pack)
        self.assertEqual(result.returncode, 0, result.stderr)
        rules = (pack / "generated/rules.toml").read_bytes()
        candidate.write_bytes(original[:original.index(b"[[project.migration_rules]]")] + rules
                              + original[original.index(b"[[units]]"):])
        self.assertIn("意味レビューがありません", run("apply", pack.name, candidate).stderr)
        template = run("meaning", pack.name, candidate)
        self.assertEqual(template.returncode, 0, template.stderr)
        evidence = json.loads(template.stdout)
        lines = detail.read_text(encoding="utf-8").splitlines()
        agent = pack / "generated/Agent.md"
        view = pack / "generated/details/views.md"
        evidence["entries"] = [
            self.evidence("obligation", detail, 3, lines[2], agent),
            self.evidence("description", detail, 1, lines[0], view, description="When migrating views"),
        ]
        evidence_path = pack / "meaning-review.json"
        evidence["unresolved"] = [{"reason": "Cross-file conflict requires a decision"}]
        evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
        self.assertIn("未解決事項", run("apply", pack.name, candidate).stderr)
        self.assertEqual(config.read_bytes(), original)
        evidence["unresolved"] = []
        evidence["entries"][1]["source"]["line"] = 2
        evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
        self.assertIn("引用が", run("apply", pack.name, candidate).stderr)
        evidence["entries"][1]["source"]["line"] = 1
        evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
        data = candidate.read_bytes()
        candidate.write_bytes(data + b"\n# candidate edited after review\n")
        self.assertIn("現行版", run("apply", pack.name, candidate).stderr)
        self.assertEqual(config.read_bytes(), original)
        candidate.write_bytes(data)
        result = run("apply", pack.name, candidate)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(config.read_bytes(), candidate.read_bytes())
        self.assertEqual((pack / "config.before.toml").read_bytes(), original)


    @staticmethod
    def evidence(kind, source, line, quote, destination, **values):
        placed = destination.read_text(encoding="utf-8").splitlines().index(quote) + 1
        return {"kind": kind, "reason": "Compared candidate with the fixed original",
                "source": {"path": str(source), "line": line, "quote": quote},
                "destination": {"path": str(destination), "line": placed, "quote": quote}, **values}

    def test_new_registration_requires_same_evidence_and_does_not_alter_existing_settings(self):
        self.config()
        config = self.root / "test.toml"
        pack = self.root / "semantic-review"
        candidate = self.root / "candidate.toml"
        tool = Path(__file__).resolve().parents[1] / "tools/rule_review.py"
        new_rule = self.project / "new.md"
        quote = "Preserve the original customer identifier."
        new_rule.write_text(quote + "\n", encoding="utf-8")
        original = config.read_bytes()
        candidate.write_bytes(original + b'\n[[project.migration_rules]]\npath = "new.md"\n')
        def run(action, *arguments):
            return subprocess.run([sys.executable, str(tool), action, str(pack), *map(str, arguments)],
                                  capture_output=True, encoding="utf-8",
                                  env={**os.environ, "PYTHONIOENCODING": "utf-8"})

        self.assertEqual(run("start", config).returncode, 0)
        template = run("meaning", candidate, "--register-new")
        self.assertEqual(template.returncode, 0, template.stderr)
        evidence = json.loads(template.stdout)
        evidence_path = pack / "meaning-review.json"
        evidence["entries"] = [self.evidence("obligation", new_rule, 1, quote, new_rule)]
        evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
        new_rule.write_text(quote + "\nChanged after review.", encoding="utf-8")
        self.assertIn("現行版", run("apply", candidate, "--register-new").stderr)
        self.assertEqual(config.read_bytes(), original)
        new_rule.write_text(quote + "\n", encoding="utf-8")
        applied = run("apply", candidate, "--register-new")
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertEqual(config.read_bytes(), candidate.read_bytes())

    def test_new_registration_rejects_identical_original_under_new_path(self):
        self.config()
        config = self.root / "test.toml"
        original = config.read_bytes().replace(
            b'\n[[units]]', b'\n[[project.migration_rules]]\npath = "existing.md"\n\n[[units]]', 1,
        )
        config.write_bytes(original)
        quote = "Keep the approved reconciliation code on every migrated record."
        existing = self.project / "existing.md"
        existing.write_bytes((quote + "\r\n").encode())
        clone = self.project / "identical renamed.md"
        clone.write_bytes(existing.read_bytes())
        candidate = self.root / "candidate.toml"
        candidate.write_bytes(original.replace(
            b'\n[[units]]', b'\n[[project.migration_rules]]\npath = "identical renamed.md"\n\n[[units]]', 1,
        ))
        pack = self.root / "review"
        tool = Path(__file__).resolve().parents[1] / "tools/rule_review.py"

        def run(action, *arguments, script=tool):
            return subprocess.run([sys.executable, str(script), action, str(pack), *map(str, arguments)],
                                  capture_output=True, encoding="utf-8",
                                  env={**os.environ, "PYTHONIOENCODING": "utf-8"})

        self.assertEqual(run("start", config).returncode, 0)
        self.assertEqual(run("read", 1).returncode, 0)
        reading = {"1": {"end": quote, "note": "Required reconciliation obligation", "required_lines": [1]}}
        self.assertEqual(run("record", json.dumps(reading)).returncode, 0)
        template = run("meaning", candidate, "--register-new")
        if template.returncode == 0:
            evidence = json.loads(template.stdout)
            evidence["entries"] = [self.evidence("obligation", clone, 1, quote, clone)]
            (pack / "meaning-review.json").write_text(json.dumps(evidence), encoding="utf-8")
        results = {"meaning": template, "apply": run("apply", candidate, "--register-new")}
        for action, result in results.items():
            with self.subTest(action=action):
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("同一バイトの原文", result.stderr)
                self.assertIn("identical renamed.md", result.stderr)
        self.assertEqual(config.read_bytes(), original)

        sources = pack / "sources"
        sources.mkdir()
        (sources / clone.name).write_bytes(clone.read_bytes())
        (pack / "plan.json").write_text(json.dumps({"documents": [
            {"path": "Agent.md", "title": "Renamed reconciliation rules",
             "parts": [{"source": clone.name, "start": 1, "end": 1}]},
        ]}), encoding="utf-8")
        for action in ("snapshot", "build", "verify"):
            result = run(action, script=tool.with_name("rule_pack.py"))
            self.assertEqual(result.returncode, 0, result.stderr)
        generated = (pack / "generated/rules.toml").read_bytes()
        candidate.write_bytes(original.replace(b"\n[[units]]", b"\n" + generated + b"\n[[units]]", 1))
        result = run("meaning", candidate, "--register-new")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("同一バイトの原文", result.stderr)
        self.assertIn(clone.name, result.stderr)
        self.assertEqual(config.read_bytes(), original)

    def test_same_words_in_another_scope_cannot_replace_mandatory_evidence(self):
        self.config()
        config = self.root / "test.toml"
        original = config.read_bytes().replace(b"\n[[units]]", b'''
[[project.migration_rules]]
path = "invoice.md"
[[project.migration_rules]]
path = "demo.md"

[[units]]''', 1)
        config.write_bytes(original)
        invoice = self.project / "invoice.md"
        demo = self.project / "demo.md"
        quote = "Preserve the identifier."
        scope = "In production invoices, this is the original tax identifier."
        invoice.write_text(scope + "\n" + quote + "\n", encoding="utf-8")
        demo.write_text("In demonstration exports, this is a sample row identifier.\n" + quote + "\n", encoding="utf-8")
        candidate = self.root / "candidate.toml"
        pack = self.root / "review"
        tool = Path(__file__).resolve().parents[1] / "tools/rule_review.py"
        candidate.write_bytes(original.replace(b'path = "invoice.md"', b'''
path = "invoice.md"
usage = "reference"
description = "For invoices"'''.strip(), 1))

        def run(action, *arguments):
            result = subprocess.run([sys.executable, str(tool), action, str(pack), *map(str, arguments)],
                                    capture_output=True, encoding="utf-8",
                                    env={**os.environ, "PYTHONIOENCODING": "utf-8"})
            return result

        for action, arguments in (
            ("start", (config,)), ("read", (1, 2)),
            ("record", (json.dumps({
                "1": {"end": quote, "note": "Production invoices retained by default", "required_lines": [2]},
                "2": {"end": quote, "note": "Same wording applies to a different identifier", "required_lines": []},
            }),)),
        ):
            result = run(action, *arguments)
            self.assertEqual(result.returncode, 0, result.stderr)
        template = run("meaning", candidate)
        self.assertEqual(template.returncode, 0, template.stderr)
        review = json.loads(template.stdout)
        review["entries"] = [self.evidence("description", invoice, 1, scope, invoice, description="For invoices")]
        evidence = pack / "meaning-review.json"
        evidence.write_text(json.dumps(review), encoding="utf-8")
        result = run("apply", candidate)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("必須登録に原文の配置根拠がありません", result.stderr)
        self.assertEqual(config.read_bytes(), original)

    def test_fixed_exception_must_stay_with_the_obligation_after_pack_split(self):
        from migration_harness import workflow
        from migration_harness.config import load_config
        from migration_harness.rules.rule_materials import render_guidelines

        config = self.root / "settings.toml"
        original = b'''[project]
root = "app"
worktree_root = "trees"
run_root = "runs"
[[project.migration_rules]]
path = "original.md"
[[units]]
id = "one"
source = "source.php"
target = "One.cs"
# keep this comment
'''
        config.write_bytes(original)
        primary = "Record an audit event on every migrated operation."
        exception = "Except health checks: do not emit an audit event."
        unrelated = "If exporting CSV, preserve column order."
        raw = ("\n".join([primary, exception, unrelated]) + "\n").encode()
        source = self.project / "original.md"
        source.write_bytes(raw)
        (self.project / "source.php").write_bytes(b"<?php echo 'synthetic';\n")
        self.commit_project()
        pack = self.root / "semantic-review"
        candidate = self.root / "candidate.toml"
        tool = Path(__file__).resolve().parents[1] / "tools/rule_review.py"
        pack_tool = tool.with_name("rule_pack.py")

        def run(action, *arguments, script=tool):
            return subprocess.run(
                [sys.executable, str(script), action, str(pack), *map(str, arguments)],
                capture_output=True, encoding="utf-8", env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            )

        def success(result):
            self.assertEqual(result.returncode, 0, result.stderr)
            return result

        success(run("start", config))
        success(run("read", 1))
        success(run("record", json.dumps({"1": {"end": unrelated, "note": "Audit and exception", "required_lines": [1]}})))
        (pack / "sources").mkdir()
        (pack / "sources" / source.name).write_bytes(raw)
        success(run("snapshot", script=pack_tool))

        def plan(last_required):
            (pack / "plan.json").write_text(json.dumps({"documents": [
                {"path": "Agent.md", "title": "Audit", "parts": [
                    {"source": source.name, "start": 1, "end": last_required}]},
                {"path": "details/export.md", "title": "CSV", "description": "When exporting CSV",
                 "parts": [{"source": source.name, "start": last_required + 1, "end": 3}]},
            ]}), encoding="utf-8")
            success(run("build", script=pack_tool))
            success(run("verify", script=pack_tool))
            generated = (pack / "generated/rules.toml").read_bytes()
            candidate.write_bytes(original[:original.index(b"[[project.migration_rules]]")] + generated
                                  + original[original.index(b"[[units]]"):])
            evidence = json.loads(success(run("meaning", candidate)).stdout)
            required = pack / "generated/Agent.md"
            detail = pack / "generated/details/export.md"
            obligation = self.evidence("obligation", source, 1, primary, required)
            evidence["entries"] = [
                obligation,
                self.evidence("exception", source, 2, exception, required if last_required == 2 else detail,
                              related={key: obligation[key] for key in ("source", "destination")}),
                self.evidence("description", source, 3, unrelated, detail, description="When exporting CSV"),
            ]
            (pack / "meaning-review.json").write_text(json.dumps(evidence), encoding="utf-8")

        plan(1)
        self.assertIn("同じ資料", run("apply", candidate).stderr)
        self.assertEqual(config.read_bytes(), original)
        (pack / "generated").rename(pack / "generated-rejected")
        plan(2)
        success(run("apply", candidate))
        self.assertIn(b"# keep this comment", config.read_bytes())
        settings = load_config(config)
        prepared = workflow.prepare_run(settings, "one")
        capsule, manifest = workflow.load_capsule_manifest(Path(prepared["run_path"]))
        rules = render_guidelines(capsule / "materials", manifest, selected=frozenset())
        self.assertIn(primary, rules)
        self.assertIn(exception, rules)
        self.assertNotIn(unrelated, rules)
