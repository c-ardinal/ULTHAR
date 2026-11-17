#!/usr/bin/env python3
"""v2/tests/run_all.py -- every v2 check in one command:

    C:/Python314/python.exe -X utf8 v2/tests/run_all.py

  * v2 tests: test_*.py here -- R1-a AC-1..4, 7, 8, 11 and R1-b AC-1, 3..6, 8..15,
    17..20 (test_store, test_integrity, test_protect, test_extract, test_memory,
    test_decide, test_cli_r1b, test_spawn, test_imports) and R1-c AC-1..13, 15, 16
    (test_unitflow, test_contract, test_gates, test_verify, test_scope, test_e2e_r1c,
    test_cli_r1c, test_imports) and R1-d AC-1..15 (test_packs: AC-1..5, 14; test_packflow:
    AC-6, 7, 14; test_lease: AC-8; test_integrate: AC-9, 10, 12, 14; test_gitref: AC-11;
    test_e2e_r1d: AC-13; test_imports: AC-15; test_posthash: AC-9 (b) post-hashes);
    the real-transcript replay
    test_replay.py (R1-a AC-6/AC-9, R1-b AC-2/AC-16, R1-c AC-14, R1-d AC-16) skips with a message when
    $ULTHAR_REPLAY_TRANSCRIPT is unset, and runs only its counts (no timed stage, not
    judged, no perf record) with ULTHAR_SKIP_PERF=1;
  * the four v1 guard suites (git discipline, tool discipline, commander, grants:
    464 checks at 971632f) from v1/, run against the v2 dispatcher.
Each module exposes main() -> int (no pytest). A crash in one module is one failure
and the loop continues. runtime.db, HOME and the ulthar state dir used by the
tests live in temp dirs; no test launches the real `claude` (tests/fake_extractor.py).
"""
from __future__ import annotations

import importlib
import json
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
SPLIT_RESULTS = TESTS_DIR / "results" / "v1-split.json"
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


def run_module(name: str, before_main=None) -> tuple[bool, tuple[int, int] | None]:
    print(f"\n--- {name} ---", flush=True)
    buf = _Tee()
    started = time.perf_counter()
    try:
        with redirect_stdout(buf):
            mod = importlib.import_module(name)
            if before_main is not None:
                before_main(mod)
            rc = mod.main()
    except Exception as exc:  # noqa: BLE001 -- isolation is the point
        traceback.print_exc()
        print(f"{name}: CRASHED ({type(exc).__name__}: {exc})")
        return False, None
    counts = _counts(buf.getvalue())
    print(f"{name}: {'PASS' if rc == 0 else f'FAIL (exit {rc})'}"
          f" in {time.perf_counter() - started:.1f}s")
    return rc == 0, counts


def _report_split(report: dict, want: int) -> bool:
    """B3: how many v1 checks were decided through the production entry."""
    totals = report["totals"]
    entry = totals.get("entry", 0)
    unit = sum(v for k, v in totals.items() if k != "entry")
    print(f"AC-5 split: {entry} checks via the production entry (hook.process) + {unit} "
          f"unit-only = {entry + unit} (of {want})  {totals}")
    for fn in report["functions"]:
        kinds = {k: v for k, v in fn.items() if k not in ("module", "function")}
        if set(kinds) != {"entry"}:
            print(f"  unit-level: {fn['module']}.{fn['function']}: {kinds}")
    SPLIT_RESULTS.parent.mkdir(parents=True, exist_ok=True)
    SPLIT_RESULTS.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return entry + unit == want


LEFTOVER_GRACE_SEC = 5


def _report_leftovers() -> bool:
    """R1-b AC-19: no extractor process (or descendant) a test started is still alive:
    processes whose command line names a test sandbox (the worker's --project-dir, the
    fake extractor's arguments) or the fake extractor. A short grace lets a worker that
    has just recorded its result exit."""
    from helpers import FAKE_EXTRACTOR, SANDBOX_PREFIX, processes_matching  # noqa: PLC0415

    deadline = time.monotonic() + LEFTOVER_GRACE_SEC
    while True:
        left = processes_matching(SANDBOX_PREFIX) + processes_matching(FAKE_EXTRACTOR.name)
        left = list(dict(left).items())
        if not left or time.monotonic() >= deadline:
            break
        time.sleep(0.5)
    print(f"\nAC-19: extractor processes left after the run: {len(left)}")
    for pid, cmd in left:
        print(f"  pid {pid}: {cmd}")
    return not left


def main() -> int:
    scratch = Path(tempfile.mkdtemp(prefix="ulthar-runall-"))
    os.environ.setdefault("ULTHAR_RUNTIME_DB", str(scratch / "runtime.db"))
    # in-process hook calls (the v1 suites) write knowledge stores, the host id and the
    # global store under here, never under the real ~/.claude/ulthar
    os.environ.setdefault("ULTHAR_STATE_DIR", str(scratch / "state"))
    sys.path[:0] = [str(TESTS_DIR), str(V1_DIR)]
    ok = True
    print("=" * 70 + "\nV2 TESTS\n" + "=" * 70)
    for f in sorted(TESTS_DIR.glob("test_*.py")):
        passed, _ = run_module(f.stem)
        ok = ok and passed
    print("\n" + "=" * 70 + "\nV1 GUARD SUITES AGAINST THE V2 DISPATCHER (AC-5)\n" + "=" * 70)
    v1_total = 0
    import v1_compat  # noqa: PLC0415 -- after sys.path is set; routes v1 names to v2
    for name, baseline in V1_BASELINE.items():
        passed, counts = run_module(name, v1_compat.instrument)
        n_pass = counts[0] if counts else 0
        v1_total += n_pass
        if n_pass < baseline:
            print(f"{name}: {n_pass} passed < baseline {baseline} (count went down)")
            passed = False
        ok = ok and passed
    want = sum(V1_BASELINE.values())
    print(f"\nAC-5: v1 checks passed {v1_total}/{want}")
    ok = ok and v1_total >= want
    ok = _report_split(v1_compat.split_report(), want) and ok
    ok = _report_leftovers() and ok
    print("\n" + "=" * 70 + "\nRUN_ALL: " + ("PASS" if ok else "FAIL") + "\n" + "=" * 70)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
