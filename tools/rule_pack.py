from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from migration_harness.rules.rule_pack import *
from migration_harness.rules.rule_pack import main

if __name__ == "__main__":
    main()
