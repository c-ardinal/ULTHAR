#!/usr/bin/env python3
"""v2/tests/run_all.py -- every v2 check in one command:

    C:/Python314/python.exe -X utf8 v2/tests/run_all.py

  * v2 tests: test_*.py here (AC-1..4, 7, 8, 11; AC-6/AC-9 replay in test_replay.py,
    which skips with a message when $ULTHAR_REPLAY_TRANSCRIPT is unset);
  * the four v1 guard suites (git discipline, tool discipline, commander, grants:
    464 checks at 971632f) from v1/, run against the v2 dispatcher.
Each module exposes main() -> int (no pytest). A crash in one module is one failure
and the loop continues. runtime.db and HOME used by the tests live in temp dirs.
"""
from __future__ import annotations

import importlib
import os
import re
import sys
import tempfile
import time
import traceback
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
V1_DIR = TESTS_DIR / "v1"
V1_BASELINE = {"test_git_discipline": 54, "test_tool_discipline": 219, "test_commander": 31,
               "test_grants": 160}
COUNT_PATTERNS = (re.compile(r"RESULT: (\d+) passed, (\d+) failed"),
                  re.compile(r"(\d+)/(\d+) passed"))


def _counts(text: str) -> tuple[int, int] | None:
    m = COUNT_PATTERNS[0].search(text)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = COUNT_PATTERNS[1].search(text)
    if m:
        return int(m.group(1)), int(m.group(2)) - int(m.group(1))
    return None


class _Tee(StringIO):
    def write(self, s: str) -> int:
        sys.__stdout__.write(s)
        return super().write(s)


def run_module(name: str) -> tuple[bool, tuple[int, int] | None]:
    print(f"\n--- {name} ---", flush=True)
    buf = _Tee()
    started = time.perf_counter()
    try:
        with redirect_stdout(buf):
            mod = importlib.import_module(name)
            rc = mod.main()
    except Exception as exc:  # noqa: BLE001 -- isolation is the point
        traceback.print_exc()
        print(f"{name}: CRASHED ({type(exc).__name__}: {exc})")
        return False, None
    counts = _counts(buf.getvalue())
    print(f"{name}: {'PASS' if rc == 0 else f'FAIL (exit {rc})'}"
          f" in {time.perf_counter() - started:.1f}s")
    return rc == 0, counts


def main() -> int:
    os.environ.setdefault("ULTHAR_RUNTIME_DB",
                          str(Path(tempfile.mkdtemp(prefix="ulthar-runall-")) / "runtime.db"))
    sys.path[:0] = [str(TESTS_DIR), str(V1_DIR)]
    ok = True
    print("=" * 70 + "\nV2 TESTS\n" + "=" * 70)
    for f in sorted(TESTS_DIR.glob("test_*.py")):
        passed, _ = run_module(f.stem)
        ok = ok and passed
    print("\n" + "=" * 70 + "\nV1 GUARD SUITES AGAINST THE V2 DISPATCHER (AC-5)\n" + "=" * 70)
    v1_total = 0
    for name, baseline in V1_BASELINE.items():
        passed, counts = run_module(name)
        n_pass = counts[0] if counts else 0
        v1_total += n_pass
        if n_pass < baseline:
            print(f"{name}: {n_pass} passed < baseline {baseline} (count went down)")
            passed = False
        ok = ok and passed
    want = sum(V1_BASELINE.values())
    print(f"\nAC-5: v1 checks passed {v1_total}/{want}")
    ok = ok and v1_total >= want
    print("\n" + "=" * 70 + "\nRUN_ALL: " + ("PASS" if ok else "FAIL") + "\n" + "=" * 70)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
