#!/usr/bin/env python3
"""Run every effex test and report one summary line.

    python3 tests/run_tests.py            # everything
    python3 tests/run_tests.py cli        # only the cli regression
    python3 tests/run_tests.py menu       # only the pty menu tests
    python3 tests/run_tests.py layout     # only the frame layout checks
    python3 tests/run_tests.py stateful   # processor and FFmpeg chain checks

exit 0 = all green, 1 = something failed, 0 with SKIP = no ffmpeg on PATH.
"""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SUITES = {
    "cli": HERE / "cli_regression.py",
    "datamosh": HERE / "datamosh_features.py",
    "menu": HERE / "menu_smoke.py",
    "layout": HERE / "layout.py",
    "install": HERE / "install_prompt.py",
    "stateful": HERE / "stateful_regression.py",
}


def main(argv) -> int:
    wanted = [a for a in argv[1:] if not a.startswith("-")]
    names = wanted or list(SUITES)
    unknown = [n for n in names if n not in SUITES]
    if unknown:
        print(f"unknown suite(s): {', '.join(unknown)} "
              f"(have: {', '.join(SUITES)})")
        return 2

    results = {}
    for name in names:
        print(f"\n=== {name} ===")
        proc = subprocess.run([sys.executable, str(SUITES[name])], cwd=HERE)
        results[name] = proc.returncode

    print("\n=== summary ===")
    for name, rc in results.items():
        print(f"  {name:<6} {'ok' if rc == 0 else 'FAILED'} (rc={rc})")
    failed = [n for n, rc in results.items() if rc != 0]
    if failed:
        print(f"failed: {', '.join(failed)}")
        return 1
    print("all suites green")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
