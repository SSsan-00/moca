"""Run one of the two disjoint Windows regression suites."""

import argparse
from pathlib import Path
import unittest


def test_cases(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from test_cases(test)
        else:
            yield test


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("shard", choices=("conversation", "remaining"))
    args = parser.parse_args()
    suite = unittest.defaultTestLoader.discover(str(Path(__file__).resolve().parent))
    conversation = args.shard == "conversation"
    selected = unittest.TestSuite(
        test for test in test_cases(suite)
        # Module import failures also end in the module name.
        if ("test_junie_conversation" in test.id().split(".")) == conversation
    )
    if not selected.countTestCases():
        parser.error(f"{args.shard} shard is empty")
    return 0 if unittest.TextTestRunner(verbosity=2).run(selected).wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
