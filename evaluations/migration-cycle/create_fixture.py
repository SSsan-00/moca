"""Create an isolated real-Junie migration/build/test/checker recovery case."""
from contextlib import redirect_stdout
from pathlib import Path
import io
import json
import runpy
import subprocess
import sys

with redirect_stdout(io.StringIO()):
    fixture = runpy.run_path(str(Path(__file__).parents[1] / "context-integrity/create_fixture.py"))
root, app = fixture["root"], fixture["app"]
project = app / "Case.csproj"
project.write_text(project.read_text().replace("net9.0", "net10.0"), encoding="utf-8")
(app / "NuGet.Config").write_text('<configuration><packageSources><clear/></packageSources></configuration>\n')
(app / "test.py").write_text('''import json, pathlib, subprocess
actual = json.loads(subprocess.check_output(["dotnet", "run", "--no-build"], text=True))
expected = json.loads(pathlib.Path("expected.json").read_text())
assert actual == expected, (actual, expected)
print("PASS: all 24 values and their order match the PHP fixture")
''', encoding="utf-8")
# A compiler error and an independent rule violation exercise both recovery phases.
(app / "break.py").write_text('''from pathlib import Path
import re
target = Path("Pages/Orders/OrderPage.cs")
source = target.read_text()
Path("../before-fault-" + Path.cwd().name + ".cs").write_text(source)
broken, count = re.subn(r'(Rule1Start\\s*=\\s*"R1-START")\\s*;', r'\\1', source)
assert count == 1, "expected exactly one Rule1Start constant"
assert 'R4-END' in broken
target.write_text(broken.replace("R4-END", "R4-BROKEN"))
''', encoding="utf-8")
config = (root / "harness.toml").read_text()
config = config.replace("[scripts]\n", "[scripts]\nafter_build=[" + json.dumps([sys.executable, "test.py"]) + "]\n")
(root / "harness.toml").write_text(config, encoding="utf-8")
subprocess.run(["git", "-C", str(app), "add", "."], check=True)
subprocess.run(["git", "-C", str(app), "commit", "-qm", "Exercise build, output tests and checker recovery"], check=True)
print(root)
