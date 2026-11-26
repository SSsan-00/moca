"""Keep the small business benchmark static and reject incomplete migrations."""
import importlib.util
from pathlib import Path
import unittest


def load(name):
    path = Path(__file__).resolve().parents[2] / "evaluations/business-php" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BusinessEvaluationTests(unittest.TestCase):
    def test_static_check_rejects_wrong_values_missing_sql_and_unsafe_casts(self):
        compare = load("check").compare
        expected = [{"Id": 0, "IslandBlock": True, "Coupon": "日本語'\\😀", "Shipping": 500}]
        source = "row($db, 'SELECT * FROM orders WHERE tenant=:t'); reject('invalid');"
        inspection = dict(policies=expected, strings=["SELECT * FROM orders WHERE tenant=@tenant", "invalid"],
                          problems=[], transactionBinding=True, nullBinding=True, parameterBinding=True)
        self.assertEqual(compare(expected, source, inspection)[0], [])
        for field, value in (("IslandBlock", 1), ("Shipping", 501), ("Coupon", "日本語😀")):
            changed = inspection | {"policies": [expected[0] | {field: value}]}
            self.assertTrue(compare(expected, source, changed)[0], field)
        for changed in ({"policies": []}, {"policies": expected * 2}, {"strings": ["invalid"]},
                        {"problems": ["Direct (int) unboxing of object"]}, {"transactionBinding": False},
                        {"nullBinding": False}, {"parameterBinding": False}):
            self.assertTrue(compare(expected, source, inspection | changed)[0], changed)

    def test_sql_normalization_preserves_literals_and_parameter_reuse(self):
        normalize = load("check").normalize_sql
        self.assertEqual(normalize("SELECT * FROM t WHERE a=:x OR b=:x"),
                         normalize("select * from t where a=@name or b=@name"))
        self.assertNotEqual(normalize("SELECT ':x' WHERE a=:x OR b=:x"),
                            normalize("SELECT ':x' WHERE a=:x OR b=:y"))
        self.assertNotEqual(normalize("SELECT 'A B'"), normalize("SELECT 'ab'"))

    def test_generated_source_has_business_branches_and_exact_dispatch(self):
        source, policies = load("create_fixture").generate_php()
        self.assertEqual(len(source.splitlines()), 2_000)
        self.assertEqual(len(policies), 16)
        self.assertNotIn("@MAX_QTY@", source)
        for p in policies:
            self.assertEqual(source.count(f"function quote_{p['ID']}("), 1)
            self.assertEqual(source.count(f"{p['NUMBER']} => 'quote_{p['ID']}'"), 1)
        self.assertGreater(source.count("$db,"), 32)
        self.assertEqual({p["ROUND"] for p in policies}, {"floor", "ceil", "half"})


if __name__ == "__main__":
    unittest.main()
