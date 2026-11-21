import re
from pathlib import Path
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent

class WrapperFixtures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        scratch = tempfile.TemporaryDirectory(prefix="wrapper-check-")
        cls.addClassCleanup(scratch.cleanup)
        artifacts = Path(scratch.name)
        subprocess.run(
            ["dotnet", "build", str(HERE / "WrapperCheck.csproj"),
             "--nologo", "--artifacts-path", str(artifacts)],
            check=True, timeout=300,
        )
        cls.checker, = (artifacts / "bin").rglob("WrapperCheck.dll")

    def check_source(self, filename, expected_code, expected_detail):
        completed = subprocess.run(
            ["dotnet", str(self.checker), str(HERE / "fixtures/Contract.cs"),
             str(HERE / "fixtures" / filename)],
            capture_output=True, encoding="utf-8", check=False, timeout=60,
        )
        self.assertEqual(expected_code, completed.returncode, completed.stdout + completed.stderr)
        self.assertIn(expected_detail, completed.stdout)
        if expected_code:
            self.assertRegex(completed.stdout, re.escape(filename) + r":\d+:\d+")

    def test_fixtures(self):
        for filename, code, detail in (
            ("Valid.cs", 0, "特に気になる点はありません。"),
            ("WrongSymbol.cs", 1, "found Unrelated.PhpDb.Execute(long)"),
            ("WrongOverload.cs", 1, "found Synthetic.Legacy.PhpDb.Execute(long, bool)"),
            ("WrongArgument.cs", 1, "expected argument type long before conversion, found int"),
            ("UnknownReference.cs", 2, "WRAP001 unknown"),
            ("UnknownDynamic.cs", 2, "WRAP001 unknown"),
            ("Absent.cs", 1, "required Execute invocation is absent"),
        ):
            with self.subTest(filename=filename):
                self.check_source(filename, code, detail)

if __name__ == "__main__":
    unittest.main()
