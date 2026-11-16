"""保存した移行先を別フォルダで実行し、値の全件一致とヘルパー依存を確認する。"""

import json
from itertools import zip_longest
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


root = Path(sys.argv[1]).resolve()
values = json.loads((root / "app/expected.json").read_text(encoding="utf-8"))
results = []
for label in sys.argv[2:]:
    timing = json.loads((root / f"{label}.json").read_text(encoding="utf-8"))
    run_path = Path(timing["path"])
    run = json.loads((run_path / "run.json").read_text(encoding="utf-8"))
    worktree = Path(run["worktree_path"])
    initial_path = worktree / ".initial-verification.json"
    initial = json.loads(initial_path.read_text(encoding="utf-8")) if initial_path.exists() else None
    snapshots = []
    if initial is not None and initial["build_exit_code"] == 0:
        snapshots.append(("initial", ".before-repair.cs.txt", initial["checker_exit_code"] == 0))
    if run["status"] == "checker_succeeded":
        snapshots.append(("final", "Pages/Orders/OrderPage.cs", True))
    for stage, target, helpers_required in snapshots:
        with tempfile.TemporaryDirectory(prefix="harness-large-target-check-") as directory:
            check = Path(directory)
            for filename in ("Case.csproj", "Program.cs", "Pages/Orders/LocalLabels.cs", "Pages/inc/SharedSuffix.cs"):
                shutil.copyfile(root / "app" / filename, check / Path(filename).name)
            shutil.copyfile(worktree / target, check / "OrderPage.cs")
            observed = []
            for change_helpers in (False, True):
                if change_helpers:
                    for filename, old, new in (("LocalLabels.cs", "ORDER:", "CHANGED:"),
                                               ("SharedSuffix.cs", ":END", ":HELPER")):
                        file = check / filename
                        file.write_text(file.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")
                build = subprocess.run(["dotnet", "build", "Case.csproj"], cwd=check, capture_output=True)
                assert build.returncode == 0, build.stdout.decode(errors="replace")
                actual = json.loads(subprocess.check_output(["dotnet", "run", "--no-build"], cwd=check))
                observed.append(actual)
            mismatches = [index for index, (actual, expected) in enumerate(zip_longest(observed[0], values))
                          if actual != expected]
            changed_values = ["CHANGED:" + value.removeprefix("ORDER:").removesuffix(":END") + ":HELPER"
                              for value in observed[0]]
            helpers_used = observed[1] == changed_values
            if helpers_required:
                assert not mismatches, (label, stage, "original output", mismatches[:10])
                assert helpers_used, (label, stage, "helper changes did not propagate")
            results.append({"label": label, "stage": stage, "value_count": len(observed[0]),
                            "mismatch_positions_zero_based": mismatches,
                            "helper_changes_propagated": helpers_used})
(root / "large-behavior.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
print(json.dumps(results, indent=2))
