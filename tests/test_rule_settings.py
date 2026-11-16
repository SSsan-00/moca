import tomlkit

from fixtures import HarnessCase
from migration_harness.config import load_config
from migration_harness.dashboard import _read_settings, _save_settings
from migration_harness.settings_form import build_settings_draft


class RuleSettingsTests(HarnessCase):
    def setUp(self):
        super().setUp()
        self.path = self.root / "settings.toml"
        self.content = '''# Keep project note
[project]
root = "app"
worktree_root = "trees"
run_root = "runs"
future_option = "untouched"

[[project.migration_rules]]
path = "rules.md" # Keep file note
custom = "keep me"

[[units]]
id = "orders"
source = "orders.php"
target = "Orders.cs"
'''
        self.path.write_text(self.content, encoding="utf-8")

    def save(self, content):
        return _save_settings(self.path, content, _read_settings(self.path)[1])

    def rules(self, config):
        return [item for item in config["units"][0]["inputs"] if item["role"] == "migration_rules"]

    def test_form_save_reload_and_raw_edit_round_trip_preserve_comments_and_unknown_fields(self):
        draft = build_settings_draft(self.content)
        rules = draft["form"]["migrationRules"]
        rules[0].update(usage="required", description="必ず守る事項")
        rules.append({
            "role": "migration_rules", "path": "db-examples.md", "usage": "reference",
            "description": "DBアクセスを移行するときに読む、トランザクションの詳細と例",
        })
        draft = build_settings_draft(draft["content"], {"migrationRules": rules})
        self.save(draft["content"])
        reloaded = build_settings_draft(self.path.read_text(encoding="utf-8"))
        self.assertEqual(reloaded["form"], draft["form"])
        self.assertEqual([rule["usage"] for rule in self.rules(load_config(self.path))], ["required", "reference"])
        self.assertIn('# Keep project note', reloaded["content"])
        self.assertIn('path = "rules.md" # Keep file note', reloaded["content"])
        raw = tomlkit.parse(reloaded["content"])
        self.assertEqual(raw["project"]["future_option"], "untouched")
        self.assertEqual(raw["project"]["migration_rules"][0]["custom"], "keep me")

        raw["project"]["migration_rules"][1]["description"] = "更新：DBの接続・再試行の例"
        self.save(tomlkit.dumps(raw))
        reloaded = build_settings_draft(self.path.read_text(encoding="utf-8"))
        self.assertEqual(reloaded["form"]["migrationRules"][1]["description"], "更新：DBの接続・再試行の例")
        self.assertEqual(self.rules(load_config(self.path))[1]["description"], "更新：DBの接続・再試行の例")

    def test_old_project_and_legacy_unit_rules_default_to_required_without_rewriting_them(self):
        content = self.content + '''
[[units.inputs]]
role = "migration_rules"
path = "legacy.md" # Keep legacy note
custom = 42
'''
        original = self.rules(self.save(content))
        self.assertEqual([(rule["usage"], rule["description"]) for rule in original], [("required", ""), ("required", "")])
        draft = build_settings_draft(content)
        self.assertNotIn("usage =", draft["content"])
        self.assertIn('path = "legacy.md" # Keep legacy note', draft["content"])
        self.assertEqual(tomlkit.parse(draft["content"])["project"]["migration_rules"][1]["custom"], 42)
        self.assertEqual(self.rules(self.save(draft["content"])), original)

    def test_legacy_reference_metadata_survives_promotion_and_unrelated_form_edit(self):
        content = self.content + '''
[[units.inputs]]
role = "migration_rules"
path = "legacy-examples.md"
usage = "reference" # Keep usage note
description = "画面を移行するときに読む、入力検証の例" # Keep description note
'''
        original = self.rules(self.save(content))
        draft = build_settings_draft(content)
        entry = draft["form"]["migrationRules"][1]
        self.assertEqual(entry["usage"], "reference")
        draft = build_settings_draft(draft["content"], {"migrationRules": [
            draft["form"]["migrationRules"][0],
            {"index": entry["index"], "role": entry["role"], "path": entry["path"]},
        ]})
        self.assertIn('usage = "reference" # Keep usage note', draft["content"])
        self.assertIn('description = "画面を移行するときに読む、入力検証の例" # Keep description note', draft["content"])
        self.assertEqual(self.rules(self.save(draft["content"])), original)

    def test_switching_back_to_required_retains_optional_description_and_can_clear_it(self):
        rules = build_settings_draft(self.content)["form"]["migrationRules"]
        rules[0].update(usage="reference", description="DBの移行で読む詳細")
        draft = build_settings_draft(self.content, {"migrationRules": rules})
        rules = draft["form"]["migrationRules"]
        rules[0]["usage"] = "required"
        draft = build_settings_draft(draft["content"], {"migrationRules": rules})
        self.assertEqual(self.rules(self.save(draft["content"]))[0]["description"], "DBの移行で読む詳細")
        rules[0]["description"] = ""
        draft = build_settings_draft(draft["content"], {"migrationRules": rules})
        self.assertEqual(self.rules(self.save(draft["content"]))[0]["description"], "")

    def test_invalid_usage_and_description_are_rejected_by_raw_save_and_form(self):
        invalid = [
            ({"usage": value}, "読み込み方") for value in ("", "auto", True, 1, [], {})
        ] + [
            ({"usage": usage, "description": value}, "文字列")
            for usage in ("required", "reference") for value in (True, 1, [], {})
        ] + [
            ({"usage": "reference", **fields}, "読む場面・内容")
            for fields in ({}, {"description": ""}, {"description": " \n\t\u3000"})
        ]
        for fields, error in invalid:
            with self.subTest(fields=fields):
                raw = tomlkit.parse(self.content)
                raw["project"]["migration_rules"][0].update(fields)
                with self.assertRaisesRegex(ValueError, error):
                    self.save(tomlkit.dumps(raw))
                self.assertEqual(self.path.read_text(encoding="utf-8"), self.content)
                with self.assertRaisesRegex(ValueError, error):
                    build_settings_draft(self.content, {"migrationRules": [
                        {"index": 0, "role": "migration_rules", "path": "rules.md", **fields},
                    ]})
        for key, error in (("usage", "読み込み方"), ("description", "文字列")):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, error):
                build_settings_draft(self.content, {"migrationRules": [
                    {"index": 0, "role": "migration_rules", "path": "rules.md", key: None},
                ]})

    def test_inline_rule_tables_preserve_unknown_fields_when_edited(self):
        content = self.content.replace('[[project.migration_rules]]\npath = "rules.md" # Keep file note\ncustom = "keep me"',
                                       'migration_rules = [{path = "rules.md", custom = "keep me"}] # Keep array note')
        rules = build_settings_draft(content)["form"]["migrationRules"]
        rules[0].update(usage="reference", description="DB移行の例")
        draft = build_settings_draft(content, {"migrationRules": rules})
        self.assertIn('# Keep array note', draft["content"])
        self.assertEqual(tomlkit.parse(draft["content"])["project"]["migration_rules"][0]["custom"], "keep me")
        self.assertEqual(self.rules(self.save(draft["content"]))[0]["usage"], "reference")
