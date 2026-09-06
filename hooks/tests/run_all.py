#!/usr/bin/env python3
"""hooks/tests/run_all.py -- runs the unit tests first, then parity.py on Windows.

CONTRACT.md #6: "run_all.py runs the unit tests first, then parity.py when on
Windows." Acceptance row 1: `python hooks/tests/run_all.py` -- all fixtures pass.

Discovers and runs every `test_*.py` module in this directory (test_contract.py
plus, once U-1..U-5 land, test_tool_discipline.py / test_git_discipline.py / ...),
each of which exposes a `main() -> int` returning 0/1 the way test_contract.py does
-- no pytest dependency, consistent with the runtime being stdlib + psutil only.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
HOOKS_DIR = TESTS_DIR.parent
sys.path.insert(0, str(HOOKS_DIR))
sys.path.insert(0, str(TESTS_DIR))


def discover_unit_test_modules() -> list[str]:
    names = []
    for f in sorted(TESTS_DIR.glob("test_*.py")):
        names.append(f.stem)
    return names


def main(argv: list[str]) -> int:
    overall_ok = True
    total_passed = 0
    total_failed = 0

    print("=" * 70)
    print("UNIT TESTS")
    print("=" * 70)
    for name in discover_unit_test_modules():
        print(f"\n--- {name} ---")
        # pins: an exception raised while IMPORTING or RUNNING one test module must
        # not abort discovery entirely -- that used to hide every alphabetically
        # later module behind a single crash. Each module is isolated: a broken
        # module counts as ONE failure and the loop continues, so the report
        # always reflects every module, not just the ones before the first crash.
        try:
            mod = importlib.import_module(name)
            if not hasattr(mod, "main"):
                # pins: a discovered test_*.py module with no main() is a FAILURE,
                # not a silent skip -- otherwise a module disabled entirely
                # (renamed main, botched merge) goes unnoticed.
                print(f"FAIL {name}: no main() entry point (module is not running)")
                overall_ok = False
                continue
            rc = mod.main()
        except Exception as exc:  # noqa: BLE001 -- isolation is the point
            import traceback

            traceback.print_exc()
            print(f"{name}: CRASHED ({type(exc).__name__}: {exc})")
            overall_ok = False
            continue
        overall_ok = overall_ok and (rc == 0)
        if rc == 0:
            print(f"{name}: PASS")
        else:
            print(f"{name}: FAIL (exit {rc})")

    print("\n" + "=" * 70)
    print("PARITY (Windows only)")
    print("=" * 70)
    import parity  # tests/parity.py

    # `python run_all.py --no-parity` (or ULTHAR_SKIP_PARITY=1) runs every unit test
    # module but skips spawning powershell against the real .ps1 masters -- see
    # parity.py's own --no-parity for why this exists.
    parity_argv = ["--no-parity"] if "--no-parity" in argv else []
    parity_rc = parity.main(parity_argv)
    overall_ok = overall_ok and (parity_rc == 0)

    # parity.py's own retirement message (printed above) already explains WHY;
    # this line gives the summary a status word to scan for that is neither
    # PASS nor FAIL nor SKIP -- retired-and-exit-0 must not read as "passing" a
    # check that no longer runs, the same distinction the module-loop above
    # draws between "PASS" and "no main() entry point". Must state only what is
    # true on ANY machine running this distribution -- the .ps1 masters and any
    # acceptance record of a past comparison are both absent here, not just
    # unmentioned.
    if getattr(parity, "LAST_RUN_RETIRED", False):
        print("parity: RETIRED (masters gone -- see message above; "
              "nothing was compared in this run)")
    elif parity_rc == 0:
        print("parity: PASS")
    else:
        print(f"parity: FAIL (exit {parity_rc})")

    print("\n" + "=" * 70)
    print("RUN_ALL: " + ("PASS" if overall_ok else "FAIL"))
    print("=" * 70)
    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
