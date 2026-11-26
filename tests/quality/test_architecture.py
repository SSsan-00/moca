import tempfile
import unittest
from pathlib import Path

from tests.quality.architecture_rules import declared_distributions, dependency_violations, source_imports


APPROVED = {"tomlkit"}
ROOT = Path(__file__).resolve().parents[2]


class ArchitectureTests(unittest.TestCase):
    def test_product_dependencies_match_contract(self):
        imports, modules = source_imports(ROOT / "src" / "migration_harness")
        declared = declared_distributions(ROOT / "pyproject.toml")
        test_modules = {path.stem for path in (ROOT / "tests").rglob("*.py")}
        self.assertEqual(dependency_violations(imports, modules, declared, test_modules), [])

    def fixture(self, **files):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name) / "src" / "migration_harness"
        for name, content in {"__init__.py": "", **files}.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        return source_imports(root)

    def test_import_resolution_distinguishes_package_members_local_and_type_only(self):
        imports, modules = self.fixture(**{
            "cli.py": (
                "import typing as t\n"
                "from typing import TYPE_CHECKING as only_types\n"
                "from .runs import run_records\n"
                "from .runs import exported\n"
                "if t.TYPE_CHECKING:\n"
                "    import migration_harness.web.routes as routes\n"
                "else:\n"
                "    from .web import routes\n"
                "if only_types:\n"
                "    from .runs.run_records import Record\n"
                "async def open_view():\n"
                "    from .web.routes import Dashboard\n"
                "class UI:\n"
                "    from .runs import run_records\n"
            ),
            "runs/__init__.py": "from .run_records import Record as exported\n",
            "runs/run_records.py": "",
            "web/routes.py": "",
        })
        edges = {(edge.source, edge.target, edge.symbol, edge.scope, edge.line) for edge in imports}
        self.assertIn(("migration_harness.runs", "migration_harness.runs.run_records", "Record", "module", 1), edges)
        self.assertEqual({edge[1:] for edge in edges if edge[0] == "migration_harness.cli"}, {
            ("typing", None, "module", 1),
            ("typing", "TYPE_CHECKING", "module", 2),
            ("migration_harness.runs.run_records", None, "module", 3),
            ("migration_harness.runs", "exported", "module", 4),
            ("migration_harness.web.routes", None, "type", 6),
            ("migration_harness.web.routes", None, "module", 8),
            ("migration_harness.runs.run_records", "Record", "type", 10),
            ("migration_harness.web.routes", "Dashboard", "local", 12),
            ("migration_harness.runs.run_records", None, "module", 14),
        })
        self.assertEqual(dependency_violations(imports, modules, APPROVED), [])

    def test_rebound_type_guard_does_not_hide_a_runtime_cycle(self):
        imports, modules = self.fixture(**{
            "runs/first.py": "from ..verification import second\n",
            "verification/second.py": (
                "from typing import TYPE_CHECKING as guard\n"
                "def start(guard):\n"
                "    if guard:\n"
                "        from ..runs import first\n"
            ),
        })
        problems = dependency_violations(imports, modules, APPROVED)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("Import cycle:", problems[0])
        self.assertIn("migration_harness.verification.second -> migration_harness.runs.first [local]", problems[0])

    def test_rejects_reversed_layers_test_imports_unresolved_imports_and_extra_distributions(self):
        imports, modules = self.fixture(**{
            "runs/example.py": (
                "from ..verification import engine\n"
                "from ..web.dashboard import Dashboard\n"
                "def test_only():\n"
                "    import fixtures\n"
                "    import tests.helpers\n"
                "    import tomlkit\n"
                "    import unapproved_tool\n"
            ),
            "files.py": "from .runs import example\n",
            "verification/engine.py": "from ..workflow import run\nfrom ..cli import main\nfrom ..bad import missing\n",
            "workflow.py": "",
            "web/dashboard.py": "",
            "cli.py": "",
        })
        violations = "\n".join(dependency_violations(imports, modules, {"unapproved-tool"}, {"fixtures"}))
        for detail in (
            "runs/example.py:2: migration_harness.runs.example -> migration_harness.web.dashboard:Dashboard [module]: forbidden runs -> web",
            "verification/engine.py:1: migration_harness.verification.engine -> migration_harness.workflow:run [module]: forbidden verification -> workflow",
            "verification/engine.py:2: migration_harness.verification.engine -> migration_harness.cli:main [module]: forbidden verification -> cli",
            "verification/engine.py:3: migration_harness.verification.engine -> migration_harness.bad:missing [module]: unresolved product import",
            "files.py:1: migration_harness.files -> migration_harness.runs.example [module]: forbidden common -> runs",
            "fixtures [local]: product must not import tests",
            "tests.helpers [local]: product must not import tests",
            "tomlkit [local]: external import is not approved and declared",
            "unapproved_tool [local]: external import is not approved and declared",
            "pyproject.toml: unapproved runtime distribution: unapproved-tool",
            "pyproject.toml: approved runtime distribution is not declared: tomlkit",
        ):
            with self.subTest(detail=detail):
                self.assertIn(detail, violations)
        self.assertNotIn("forbidden runs -> verification", violations)

    def test_reports_local_cycle_through_package_reexport_but_not_type_only_back_import(self):
        sources = {
            "runs/__init__.py": "from .loader import Loader\n",
            "runs/loader.py": "from ..verification import verification_file\n",
            "verification/verification_file.py": "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from ..runs import Loader\n",
        }
        imports, modules = self.fixture(**sources)
        self.assertEqual(dependency_violations(imports, modules, APPROVED), [])

        sources["verification/verification_file.py"] += "def start():\n    from ..runs import Loader\n"
        imports, modules = self.fixture(**sources)
        problems = dependency_violations(imports, modules, APPROVED)
        self.assertEqual(len(problems), 1, problems)
        for detail in (
            "Import cycle:",
            "runs/__init__.py:1: migration_harness.runs -> migration_harness.runs.loader:Loader [module]",
            "runs/loader.py:1: migration_harness.runs.loader -> migration_harness.verification.verification_file [module]",
            "verification/verification_file.py:5: migration_harness.verification.verification_file -> migration_harness.runs:Loader [local]",
        ):
            self.assertIn(detail, problems[0])

    def test_local_worker_exception_allows_only_reviewed_symbols_and_reciprocal_cycle(self):
        sources = {
            "runs/run_manager.py": "def work():\n    from ..web.dashboard import execute_dashboard_action\n",
            "web/dashboard.py": "def open_dashboard():\n    from ..runs.run_manager import RunManager\n",
        }
        imports, modules = self.fixture(**sources)
        self.assertEqual(dependency_violations(imports, modules, APPROVED), [])

        sources["runs/run_manager.py"] += "from ..web.dashboard import execute_dashboard_action, Dashboard\n"
        sources["web/dashboard.py"] += "from ..verification import worker\n"
        sources["verification/worker.py"] = "from ..runs.run_manager import RunManager\n"
        imports, modules = self.fixture(**sources)
        violations = "\n".join(dependency_violations(imports, modules, APPROVED))
        self.assertIn("web.dashboard:execute_dashboard_action [module]: forbidden runs -> web", violations)
        self.assertIn("web.dashboard:Dashboard [module]: forbidden runs -> web", violations)
        self.assertIn("Import cycle:", violations)
        self.assertIn("migration_harness.verification.worker -> migration_harness.runs.run_manager:RunManager", violations)
