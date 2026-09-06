#!/usr/bin/env python3
"""hooks/tests/test_log_cap.py -- lib/io.append_jsonl's byte cap.

Regression cover for the review finding "the log cap favors the newest entry but
can erase all preceding evidence without actually bounding the file". The old cap
truncated the file to empty once it reached max_bytes and then appended, so:
  - it bounded nothing (one entry larger than max_bytes landed in full, and the
    real ceiling was max_bytes plus an unbounded line), and
  - it kept the NEWEST entries, when the first ones are what show why a guard
    started hot-spinning.

Nothing covered the limit at all before this file. Every check here asserts on
REAL byte counts of a real file in a temp dir.

    python tests/test_log_cap.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable

TESTS_DIR = Path(__file__).resolve().parent
HOOKS_DIR = TESTS_DIR.parent
sys.path.insert(0, str(HOOKS_DIR))

from lib import io as io_mod  # noqa: E402

# Non-ASCII on purpose: entries are written ensure_ascii=False, so a cap that
# compared characters instead of bytes would pass on ASCII fixtures and blow the
# limit on the Japanese messages this project actually logs.
ENTRY = {"ts": "2026-09-05T12:00:00+09:00", "guard": "tool_discipline",
         "message": "ガード が 落ちた -- 日本語のメッセージ"}
CAP = 1024


class Probe:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0

    def check(self, name: str, cond: bool, detail: str = "") -> None:
        if cond:
            self.passed += 1
            print(f"OK   {name}")
        else:
            self.failed += 1
            print(f"FAIL {name}" + (f"\n       {detail}" if detail else ""))


def _lines(path: Path) -> list[dict]:
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()]


def _footer(path: Path) -> dict | None:
    last = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
    return last if last.get(io_mod.LOG_CAP_MARKER) else None


def test_footer_is_fixed_width(p: Probe) -> None:
    """The whole design rests on rewriting the footer in place, which is only
    safe while every possible footer is the same length."""
    widths = {len(io_mod._cap_footer(n)) for n in (0, 1, 9, 10**6, 10**12 - 1, 10**15)}
    p.check(f"footer width is constant ({io_mod.LOG_CAP_FOOTER_BYTES} bytes)",
            widths == {io_mod.LOG_CAP_FOOTER_BYTES}, str(widths))


def test_hot_spin_is_bounded_and_keeps_the_head(p: Probe) -> None:
    root = Path(tempfile.mkdtemp(prefix="ulthar-logcap-"))
    try:
        log = root / "logs" / "errors.jsonl"
        entry_bytes = len(json.dumps(ENTRY, ensure_ascii=False).encode("utf-8")) + 1
        n = 200
        for i in range(n):
            io_mod.append_jsonl(log, dict(ENTRY, seq=i), max_bytes=CAP)

        size = log.stat().st_size
        p.check(f"200 entries x {entry_bytes}B under a {CAP}B cap: file is {size}B <= {CAP}B",
                size <= CAP, f"size={size}")

        lines = _lines(log)  # raises if any line is not valid JSON
        kept = [ln for ln in lines if not ln.get(io_mod.LOG_CAP_MARKER)]
        p.check(f"every one of the {len(lines)} lines is still valid JSON", True)
        p.check("the entries kept are the FIRST ones (the cause), not the newest",
                [ln["seq"] for ln in kept] == list(range(len(kept))),
                str([ln.get("seq") for ln in kept]))

        foot = _footer(log)
        p.check("a capped footer line records that entries were dropped", foot is not None,
                str(lines[-1]))
        p.check(f"the footer counts every drop exactly: {n} - {len(kept)} kept",
                foot is not None and int(foot["dropped_entries"]) == n - len(kept),
                str(foot))
        p.check("the footer says how to resume logging",
                foot is not None and "Delete this file" in foot["note"], str(foot))

        # Steady state: more entries do not grow the file, only the counter.
        for i in range(50):
            io_mod.append_jsonl(log, dict(ENTRY, seq=1000 + i), max_bytes=CAP)
        p.check(f"50 further drops do not grow the file ({log.stat().st_size}B)",
                log.stat().st_size == size, f"{log.stat().st_size} != {size}")
        p.check("50 further drops are counted",
                int(_footer(log)["dropped_entries"]) == n - len(kept) + 50,
                str(_footer(log)))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_a_smaller_entry_never_lands_below_the_footer(p: Probe) -> None:
    """Capped means capped. A big entry can trip the cap while leaving room a
    LATER, smaller entry would fit into -- and appending it would put an entry
    below the footer, fork the count across two footers, and break the head into
    a non-contiguous record. The footer is therefore checked before the fit."""
    root = Path(tempfile.mkdtemp(prefix="ulthar-logcap-below-"))
    try:
        log = root / "errors.jsonl"
        cap = 2048
        for i in range(6):
            io_mod.append_jsonl(log, dict(ENTRY, seq=i), max_bytes=cap)
        head_size = log.stat().st_size
        io_mod.append_jsonl(log, {"traceback": "あ" * 300}, max_bytes=cap)  # ~900B: trips the cap
        capped_size = log.stat().st_size
        io_mod.append_jsonl(log, dict(ENTRY, seq=99), max_bytes=cap)  # 133B: WOULD fit

        p.check(f"the small later entry is dropped, not appended below the footer "
                f"({head_size}B head, {capped_size}B capped, {log.stat().st_size}B now, "
                f"cap {cap}B)",
                log.stat().st_size == capped_size, f"{log.stat().st_size} != {capped_size}")
        lines = _lines(log)
        p.check("exactly one footer, and it is the last line",
                sum(1 for ln in lines if ln.get(io_mod.LOG_CAP_MARKER)) == 1
                and lines[-1].get(io_mod.LOG_CAP_MARKER), str(lines[-1]))
        p.check("both dropped entries are counted in that one footer",
                _footer(log)["dropped_entries"] == "000000000002", str(_footer(log)))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_single_oversized_entry_is_bounded(p: Probe) -> None:
    """The old cap's real hole: one entry bigger than max_bytes landed in full."""
    root = Path(tempfile.mkdtemp(prefix="ulthar-logcap-big-"))
    try:
        log = root / "errors.jsonl"
        big = {"traceback": "あ" * 20000}
        big_bytes = len(json.dumps(big, ensure_ascii=False).encode("utf-8"))
        io_mod.append_jsonl(log, big, max_bytes=CAP)
        size = log.stat().st_size
        p.check(f"a {big_bytes}B entry under a {CAP}B cap leaves {size}B on disk",
                size <= CAP and size == io_mod.LOG_CAP_FOOTER_BYTES, f"size={size}")
        p.check("the oversized entry is counted as dropped, not silently discarded",
                _footer(log)["dropped_entries"] == "000000000001", str(_footer(log)))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_legacy_oversized_file_is_capped_once(p: Probe) -> None:
    """A file the pre-cap code left over the limit -- possibly ending mid-line
    after a kill -- must gain exactly one footer, not a mangled last line."""
    root = Path(tempfile.mkdtemp(prefix="ulthar-logcap-legacy-"))
    try:
        log = root / "errors.jsonl"
        body = ('{"legacy": "エントリ"}\n' * 100) + '{"half-written'
        log.write_bytes(body.encode("utf-8"))
        before = log.stat().st_size
        io_mod.append_jsonl(log, ENTRY, max_bytes=CAP)
        after = log.stat().st_size
        p.check(f"legacy oversized file grows by exactly one footer "
                f"({before}B -> {after}B, footer {io_mod.LOG_CAP_FOOTER_BYTES}B)",
                after == before + 1 + io_mod.LOG_CAP_FOOTER_BYTES, f"{before} -> {after}")
        p.check("the truncated legacy last line is left alone, not merged with the footer",
                log.read_text(encoding="utf-8").splitlines()[-2] == '{"half-written')
        p.check("the footer is parseable and counts the dropped entry",
                _footer(log)["dropped_entries"] == "000000000001", str(_footer(log)))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_no_cap_is_still_unbounded(p: Probe) -> None:
    """max_bytes=None is plain, unbounded append -- the fallback when a caller
    passes no cap (neither append_jsonl nor append_jsonl_drop_oldest bounds
    anything without one)."""
    root = Path(tempfile.mkdtemp(prefix="ulthar-logcap-none-"))
    try:
        log = root / "decisions.jsonl"
        for i in range(200):
            io_mod.append_jsonl(log, dict(ENTRY, seq=i))
        lines = _lines(log)
        p.check(f"no cap: all 200 entries kept ({log.stat().st_size}B, no footer)",
                len(lines) == 200 and not any(ln.get(io_mod.LOG_CAP_MARKER) for ln in lines),
                str(len(lines)))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_drop_oldest_bounds_the_file_and_keeps_the_newest(p: Probe) -> None:
    """commander.decision_log's cap (lib/io.append_jsonl_drop_oldest, wired via
    guards/commander.py). Opposite of append_jsonl: a reader of a decision log
    wants the MOST RECENT entries -- 'did commander just block me, and why' --
    not the oldest, so this keeps the tail and drops the head, with no footer."""
    root = Path(tempfile.mkdtemp(prefix="ulthar-logcap-tail-"))
    try:
        log = root / "logs" / "commander-guard.jsonl"
        n = 200
        for i in range(n):
            io_mod.append_jsonl_drop_oldest(log, dict(ENTRY, seq=i), max_bytes=CAP)

        size = log.stat().st_size
        p.check(f"{n} entries under a {CAP}B cap: file is {size}B <= {CAP}B",
                size <= CAP, f"size={size}")

        lines = _lines(log)  # raises if any line is not valid JSON
        p.check(f"every one of the {len(lines)} lines is still valid JSON", True)
        p.check("no cap-footer marker is ever written by this cap",
                not any(ln.get(io_mod.LOG_CAP_MARKER) for ln in lines), str(lines[-1:]))
        p.check("the entries kept are the LAST ones (the newest), not the first",
                [ln["seq"] for ln in lines] == list(range(n - len(lines), n)),
                str([ln.get("seq") for ln in lines]))

        # A later, smaller entry lands fine (unlike append_jsonl, nothing freezes).
        io_mod.append_jsonl_drop_oldest(log, {"tail": "marker-entry"}, max_bytes=CAP)
        newest = _lines(log)[-1]
        p.check("a fresh entry after the cap trips still lands (log never freezes)",
                newest == {"tail": "marker-entry"}, str(newest))
        p.check(f"the file stays under the cap after the fresh append ({log.stat().st_size}B)",
                log.stat().st_size <= CAP, f"size={log.stat().st_size}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_drop_oldest_single_oversized_entry_is_kept(p: Probe) -> None:
    """This cap bounds ACCUMULATION, not one oversized line -- a decision-log
    entry's size does not vary the way a traceback's does, so there is nothing
    to gain by dropping the only line on disk."""
    root = Path(tempfile.mkdtemp(prefix="ulthar-logcap-tail-big-"))
    try:
        log = root / "decisions.jsonl"
        big = {"transcript": "あ" * 20000}
        io_mod.append_jsonl_drop_oldest(log, big, max_bytes=CAP)
        lines = _lines(log)
        p.check("a single entry bigger than the cap is kept whole, not dropped",
                len(lines) == 1 and lines[0] == big, str(lines))
    finally:
        shutil.rmtree(root, ignore_errors=True)


_WORKER_SCRIPT = """\
import sys
sys.path.insert(0, {hooks_dir!r})
from pathlib import Path
from lib.io import append_jsonl_drop_oldest

path = Path({log!r})
writer = {writer!r}
for seq in range({n!r}):
    append_jsonl_drop_oldest(path, {{"writer": writer, "seq": seq}}, max_bytes={max_bytes!r})
"""


def _spawn_workers(root: Path, bodies: list[str], env: dict | None = None) -> None:
    """Real OS processes (not threads, not a sequential loop) writing the SAME
    file at the same time -- matching the actual production hazard: every
    guarded Write/Edit spawns its OWN dispatcher process, so two overlapping
    tool calls are two processes racing this exact function. Threads do NOT
    expose it (one interpreter, one file object cache); only real processes do,
    which is why every concurrency test in this file spawns subprocesses."""
    scripts = []
    for w, body in enumerate(bodies):
        script_path = root / f"writer_{w}.py"
        script_path.write_text(body, encoding="utf-8")
        scripts.append(script_path)
    procs = [subprocess.Popen([sys.executable, str(s)], env=env) for s in scripts]
    for proc in procs:
        # Generous: this is bounding a burst of real process spawns against
        # system load (AV scanning, disk contention), not the lock logic --
        # every retry/sleep this module does is itself bounded to well under
        # a second per call (see _acquire_append_lock).
        rc = proc.wait(timeout=180)
        assert rc == 0, f"writer subprocess exited {rc}"


def _run_concurrent_writers(root: Path, log: Path, writers: int, n: int, max_bytes: int) -> None:
    _spawn_workers(root, [
        _WORKER_SCRIPT.format(hooks_dir=str(HOOKS_DIR), log=str(log), writer=w, n=n, max_bytes=max_bytes)
        for w in range(writers)
    ])


def _settle(log: Path, max_bytes: int, tries: int = 40, delay: float = 0.1,
            append: Callable[..., None] | None = None) -> int:
    """Repeatedly issues an ordinary call -- standing in for however many of
    the next real hook invocations it takes -- until no append-lock/pending
    sibling remains. A lock left behind by a killed holder (this box's own
    Defender/AV churn on rapid small-file creation was observed to kill a
    worker mid-hold at least once while writing this test) is only
    reclaimable once it clears io.lib's stale-age threshold: recovery is
    BOUNDED, not instant, and this honors that bound rather than asserting
    instantaneous cleanup after a single call. Returns how many settle calls
    it actually made (each appends one throwaway {"writer": "settle"} line)."""
    for i in range(1, tries + 1):
        (append or io_mod.append_jsonl_drop_oldest)(log, {"writer": "settle", "seq": 0}, max_bytes=max_bytes)
        if not list(log.parent.glob(f"{log.name}.*")):
            return i
        time.sleep(delay)
    return tries


def test_concurrent_writers_never_lose_an_entry(p: Probe) -> None:
    """THE regression test for the MEDIUM finding: real concurrent dispatcher
    processes appending to commander.decision_log at once must never silently
    drop one of their entries. max_bytes is set far larger than the total data
    written, so no entry is EVER legitimately eligible for drop-oldest
    rotation -- every single one of writers*n entries must survive somewhere
    durable on disk. A sequential/single-process test cannot exercise this at
    all; this is why the finding said sequential tests cannot catch it.

    A call that loses the lock race writes its entry to a `.pending.*`
    sibling instead of the main file (see append_jsonl_drop_oldest) -- durable
    on disk immediately, but not yet folded into the main log until some
    later call wins the lock. One settle call (standing in for the next real
    hook invocation) sweeps any such stragglers before this checks the main
    file, so the check is deterministic rather than racing the last writer's
    own sweep timing."""
    root = Path(tempfile.mkdtemp(prefix="ulthar-logcap-concurrent-"))
    try:
        log = root / "logs" / "commander-guard.jsonl"
        writers, n = 6, 40
        max_bytes = 50_000_000
        _run_concurrent_writers(root, log, writers=writers, n=n, max_bytes=max_bytes)
        settle_calls = _settle(log, max_bytes)

        parsed, corrupt = _parse_tolerant(log)
        p.check(f"all {len(parsed) + len(corrupt)} lines are valid JSON "
                f"(no corruption from the race): {len(corrupt)} corrupt",
                not corrupt, str(corrupt[:3]))

        seen = {(d["writer"], d["seq"]) for d in parsed if d.get("writer") != "settle"}
        expected = {(w, s) for w in range(writers) for s in range(n)}
        missing = expected - seen
        lost = len(missing)
        p.check(
            f"{writers} concurrent processes x {n} entries each: "
            f"{len(seen)}/{len(expected)} present after settling ({settle_calls} settle call(s)), {lost} lost",
            lost == 0,
            f"missing={sorted(missing)[:10]}{'...' if lost > 10 else ''}",
        )
        settle_lines = sum(1 for d in parsed if d.get("writer") == "settle")
        p.check("no duplicate/phantom entries either", len(parsed) == len(expected) + settle_lines,
                f"got {len(parsed)} lines, expected {len(expected)} + {settle_lines} settle")
        leftovers = list(log.parent.glob(f"{log.name}.*"))
        p.check(f"settled after {settle_calls} call(s): no append lock or pending file left behind",
                leftovers == [], str(leftovers))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_concurrent_writers_with_real_rotation_stay_bounded_and_valid(p: Probe) -> None:
    """Same real-process concurrency, but with max_bytes small enough that
    drop-oldest rotation genuinely fires WHILE writers are still racing --
    the exact window the old read-modify-truncate-write was unsafe in. Every
    line must still parse and the file must never exceed the cap.

    Design residue (documented on append_jsonl_drop_oldest): a call that loses
    the lock race writes a `.pending.*` sibling instead of touching the main
    file, folded in by whichever call next wins the lock -- so immediately
    after N racing writers all exit, a pending file CAN legitimately still be
    sitting there unswept, and the log can legitimately still read oversized.
    That is "eventually bounded", not "never loses an entry" -- both stated
    invariants, not in tension. This test proves both halves: mid-race
    boundedness is not asserted as instantaneous, but ONE further ordinary
    call (exactly what the next real PreToolUse hook invocation would be)
    must sweep and trim everything down to a valid, capped, fully-cleaned-up
    state -- proving nothing was actually lost, only deferred."""
    root = Path(tempfile.mkdtemp(prefix="ulthar-logcap-concurrent-rotate-"))
    try:
        log = root / "logs" / "commander-guard.jsonl"
        cap = 4096
        writers, n = 5, 40
        _run_concurrent_writers(root, log, writers=writers, n=n, max_bytes=cap)

        # However many of the next ordinary calls it takes (a stand-in for
        # the next real hook invocations) must fold in every pending file
        # and settle the log -- see _settle's docstring for why this is a
        # bounded retry, not a single call.
        settle_calls = _settle(log, cap)

        size = log.stat().st_size
        p.check(f"settled after {settle_calls} call(s): file is {size}B <= {cap}B cap", size <= cap, f"size={size}")

        raw = log.read_text(encoding="utf-8").splitlines()
        parsed = [json.loads(ln) for ln in raw]  # raises if any line is corrupt/truncated
        p.check(f"all {len(parsed)} surviving lines are valid JSON (no half-written line)", True)
        p.check("the file is not empty (rotation reduced it, did not wipe it)", len(parsed) > 0)

        leftovers = list(log.parent.glob(f"{log.name}.*"))
        p.check(f"settled after {settle_calls} call(s): no append lock, pending file, or .trim.tmp left behind",
                leftovers == [], str(leftovers))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_crash_mid_rotation_leaves_one_whole_version(p: Probe) -> None:
    """Simulates a process killed between writing the trimmed temp file and
    the atomic os.replace() that commits it -- the crash-safety half of the
    finding ('a crash during write_bytes can leave the log empty or partially
    written'). os.replace is patched to raise, mimicking the process dying
    before that single atomic syscall runs. Asserts the ORIGINAL file is left
    completely intact (not the new trimmed content, not a merge, not empty) --
    exactly one whole version, never a half-written one."""
    root = Path(tempfile.mkdtemp(prefix="ulthar-logcap-crash-"))
    try:
        log = root / "logs" / "commander-guard.jsonl"
        cap = 1024
        for i in range(10):
            io_mod.append_jsonl_drop_oldest(log, dict(ENTRY, seq=i), max_bytes=None)  # no rotation yet
        before = log.read_bytes()

        new_entry = dict(ENTRY, seq=999)
        new_line = (json.dumps(new_entry, ensure_ascii=False) + "\n").encode("utf-8")

        real_replace = io_mod.os.replace
        io_mod.os.replace = lambda *a, **k: (_ for _ in ()).throw(OSError("simulated crash before swap"))
        try:
            io_mod.append_jsonl_drop_oldest(log, new_entry, max_bytes=cap)
            p.check("simulated crash: OSError from the interrupted swap propagates (not swallowed)", False)
        except OSError:
            p.check("simulated crash: OSError from the interrupted swap propagates (not swallowed)", True)
        finally:
            io_mod.os.replace = real_replace

        after = log.read_bytes()
        p.check("the append that triggered rotation still landed (single-write append precedes the swap)",
                after == before + new_line, f"after={after!r}")
        lines = after.decode("utf-8").splitlines()
        p.check(f"every one of {len(lines)} lines is still valid JSON -- no half-written trimmed file landed",
                all(_is_valid_json(ln) for ln in lines))
        p.check("nothing was silently trimmed away: this is the OLD whole version plus the new append, "
                "not the (never-committed) trimmed version",
                len(lines) == 11, f"got {len(lines)} lines")

        # And a normal (non-crashing) rotation afterwards still converges and commits.
        io_mod.append_jsonl_drop_oldest(log, dict(ENTRY, seq=1000), max_bytes=cap)
        final_size = log.stat().st_size
        p.check(f"a real (non-crashing) rotation after the simulated crash commits fine ({final_size}B <= {cap}B)",
                final_size <= cap, f"size={final_size}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _parse_tolerant(path: Path) -> tuple[list[dict], list[str]]:
    """Corruption is a RESULT to report, not a crash. An unlocked concurrent
    append does not only LOSE lines on this platform -- it also produces torn
    ones (measured under the reverted-fix mutant: a zero-length/NUL-filled
    line, JSONDecodeError at char 0). Parsing with a list comprehension made
    that abort the test before it could count how many entries were lost, so
    the more interesting number never got measured. Both are counted here and
    both are asserted zero by the caller."""
    parsed: list[dict] = []
    corrupt: list[str] = []
    for ln in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            parsed.append(json.loads(ln))
        except ValueError:
            corrupt.append(ln)
    return parsed, corrupt


def _is_valid_json(line: str) -> bool:
    try:
        json.loads(line)
        return True
    except ValueError:
        return False

# --------------------------------------------------------------------------
# The plain-append hazard, on the OTHER logs: paths.error_log and
# logging.config_error_log (lib/io.append_jsonl, and everything routed to it).
# --------------------------------------------------------------------------

_APPEND_JSONL_WORKER = """\
import sys
sys.path.insert(0, {hooks_dir!r})
from pathlib import Path
from lib.io import append_jsonl

path = Path({log!r})
for seq in range({n!r}):
    append_jsonl(path, {{"writer": {writer!r}, "seq": seq}}, max_bytes={max_bytes!r})
"""


def test_concurrent_append_jsonl_never_loses_an_entry(p: Probe) -> None:
    """THE regression test for the finding that generalised the decision-log
    fix: append_jsonl's pre-cap path was a plain unlocked `open(path, "ab")`
    single write, on the codebase-wide assumption that one small append
    syscall is atomic across processes. It is not, on this platform.

    Every dispatcher invocation is its own process and several tool calls can
    be in flight at once, so paths.error_log and logging.config_error_log --
    the two logs an investigator reaches for AFTER something went wrong -- were
    exposed on every write. max_bytes is far larger than the total written, so
    the cap never legitimately drops anything: all writers*n entries must
    survive."""
    root = Path(tempfile.mkdtemp(prefix="ulthar-logcap-append-concurrent-"))
    try:
        log = root / "logs" / "dispatch-errors.jsonl"
        writers, n = 8, 60
        max_bytes = 50_000_000
        _spawn_workers(root, [
            _APPEND_JSONL_WORKER.format(hooks_dir=str(HOOKS_DIR), log=str(log),
                                        writer=w, n=n, max_bytes=max_bytes)
            for w in range(writers)
        ])
        settle_calls = _settle(log, max_bytes, append=io_mod.append_jsonl)

        parsed, corrupt = _parse_tolerant(log)
        p.check(f"all {len(parsed) + len(corrupt)} lines are valid JSON "
                f"(no corruption from the race): {len(corrupt)} corrupt",
                not corrupt, str(corrupt[:3]))

        seen = {(d["writer"], d["seq"]) for d in parsed if d.get("writer") != "settle"}
        expected = {(w, s) for w in range(writers) for s in range(n)}
        missing = expected - seen
        p.check(
            f"{writers} concurrent processes x {n} append_jsonl entries each: "
            f"{len(seen)}/{len(expected)} present after settling ({settle_calls} settle call(s)), "
            f"{len(missing)} lost",
            not missing,
            f"missing={sorted(missing)[:10]}{'...' if len(missing) > 10 else ''}",
        )
        p.check("no cap footer was written (max_bytes was never approached)",
                not any(d.get(io_mod.LOG_CAP_MARKER) for d in parsed))
        leftovers = list(log.parent.glob(f"{log.name}.*"))
        p.check("no append lock or pending file left behind", leftovers == [], str(leftovers))
    finally:
        shutil.rmtree(root, ignore_errors=True)


_SHARED_LOG_WORKER = """\
import sys
sys.path.insert(0, {hooks_dir!r})
from pathlib import Path
import dispatch
from lib.ctx import build_ctx

home = Path({home!r})
cfg = {{"paths": {{"error_log": "~/.claude/hooks/logs/dispatch-errors.jsonl"}},
       "logging": {{"config_error_log": "~/.claude/hooks/logs/config-errors.jsonl",
                   "max_error_log_bytes": {max_bytes!r}}}}}
ctx = build_ctx({{"_home_override": str(home), "cwd": str(home)}}, cfg,
                "PreToolUse", home / ".claude" / "hooks")
for seq in range({n!r}):
    if {kind!r} == "ctx":
        ctx.log_config_error("phase-{writer}-%d" % seq, "j", "e")
        ctx.log_error("guard-{writer}-%d" % seq, RuntimeError("boom"), [])
    else:
        dispatch._best_effort_config_error_log("phase-{writer}-%d" % seq, "e")
"""


def test_two_different_writers_of_one_log_share_one_lock(p: Probe) -> None:
    """The config-error log has TWO independent writers by configuration:
    lib/ctx.py's Ctx.log_config_error and dispatch.py's
    _best_effort_config_error_log, which fires when config.json or Ctx itself
    cannot be built -- i.e. on EVERY tool call for as long as that lasts,
    which is exactly when concurrent writes pile up and exactly when the log
    is the only evidence there is.

    They must contend for the SAME lock, not each hold a different one over
    the same path. That is guaranteed structurally rather than by convention:
    both route through append_jsonl, whose lock name is derived from the LOG
    PATH (lib/io._lock_path), so neither writer has to know the other exists.
    This test runs both kinds concurrently against one file and asserts
    nothing is lost.

    The subprocess environment's USERPROFILE/HOME are overridden to a temp
    dir: _best_effort_config_error_log resolves its path from the raw
    environment (it runs when there is no cfg to read), so without this the
    test would write into the owner's live config-error log."""
    root = Path(tempfile.mkdtemp(prefix="ulthar-logcap-shared-writers-"))
    try:
        home = root / "home"
        log = home / ".claude" / "hooks" / "logs" / "config-errors.jsonl"
        pairs, n = 4, 60  # 4 Ctx writers + 4 fallback writers
        max_bytes = 50_000_000
        bodies = []
        for kind in ("ctx", "fallback"):
            for i in range(pairs):
                bodies.append(_SHARED_LOG_WORKER.format(
                    hooks_dir=str(HOOKS_DIR), home=str(home), n=n, max_bytes=max_bytes,
                    kind=kind, writer=f"{kind}-{i}"))
        env = os.environ | {"USERPROFILE": str(home), "HOME": str(home)}
        _spawn_workers(root, bodies, env=env)
        settle_calls = _settle(log, max_bytes, append=io_mod.append_jsonl)

        parsed, corrupt = _parse_tolerant(log)
        p.check(f"no torn line in the shared log ({len(corrupt)} corrupt)",
                not corrupt, str(corrupt[:3]))
        seen = {d["phase"] for d in parsed if "phase" in d}
        expected = {f"phase-{kind}-{i}-{seq}"
                    for kind in ("ctx", "fallback") for i in range(pairs) for seq in range(n)}
        missing = expected - seen
        p.check(
            f"{pairs} Ctx.log_config_error + {pairs} dispatch fallback processes x {n} entries each, "
            f"one shared log: {len(seen & expected)}/{len(expected)} present after settling "
            f"({settle_calls} settle call(s)), {len(missing)} lost",
            not missing,
            f"missing={sorted(missing)[:10]}{'...' if len(missing) > 10 else ''}",
        )
        p.check("both writers really did land in the SAME file (Ctx entries carry guard_scope, "
                "the fallback's do not)",
                any("guard_scope" in d for d in parsed) and any(
                    "guard_scope" not in d and "phase" in d for d in parsed))
        # Ctx.log_error is the third writer named in the finding. It routes to
        # paths.error_log -- a DIFFERENT file, written by the same processes at
        # the same time, so this also shows a per-PATH lock does not serialise
        # two unrelated logs into one another.
        err_log = home / ".claude" / "hooks" / "logs" / "dispatch-errors.jsonl"
        _settle(err_log, max_bytes, append=io_mod.append_jsonl)
        err, err_corrupt = _parse_tolerant(err_log)
        err_seen = {d["guard"] for d in err if "guard" in d}
        err_expected = {f"guard-ctx-{i}-{seq}" for i in range(pairs) for seq in range(n)}
        p.check(
            f"Ctx.log_error, concurrently into paths.error_log: "
            f"{len(err_seen & err_expected)}/{len(err_expected)} present, "
            f"{len(err_expected - err_seen)} lost, {len(err_corrupt)} corrupt",
            not (err_expected - err_seen) and not err_corrupt,
            f"missing={sorted(err_expected - err_seen)[:10]}",
        )
        leftovers = list(log.parent.glob(f"{log.name}.*")) + list(err_log.parent.glob(f"{err_log.name}.*"))
        p.check("no append lock or pending file left behind for either log",
                leftovers == [], str(leftovers))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_lock_timeout_parks_the_entry_instead_of_losing_it(p: Probe) -> None:
    """The timeout branch, forced deterministically rather than raced for.

    "Never block a tool call" means the lock wait is bounded (250ms), and a
    bounded wait must expire sometimes. The whole point of this design is that
    expiring is NOT "lose the entry" -- that was the defect. A held lock is
    faked with this process's own pid and a fresh timestamp, so it is both
    alive and not stale and therefore cannot be reclaimed; the entry must go
    to a `.pending.*` sibling, and the next ordinary call (which does get the
    lock) must fold it in."""
    root = Path(tempfile.mkdtemp(prefix="ulthar-logcap-timeout-"))
    try:
        log = root / "logs" / "dispatch-errors.jsonl"
        cap = 50_000
        io_mod.append_jsonl(log, {"tag": "first"}, max_bytes=cap)

        lock = io_mod._lock_path(log)
        lock.write_text(f"{os.getpid()}\n{time.time()}\n", encoding="utf-8")

        started = time.monotonic()
        io_mod.append_jsonl(log, {"tag": "parked"}, max_bytes=cap)
        waited = time.monotonic() - started
        budget = io_mod._APPEND_LOCK_RETRY_ATTEMPTS * io_mod._APPEND_LOCK_RETRY_SLEEP_SEC

        p.check(f"the wait was bounded ({waited:.3f}s, budget {budget:.3f}s) -- "
                f"a PreToolUse writer never blocks a tool call", waited < budget * 2 + 1.0,
                f"waited={waited:.3f}s")
        tags = [d.get("tag") for d in _lines(log)]
        p.check("the entry is NOT in the main log yet (the lock was held)", tags == ["first"], str(tags))
        pending = list(log.parent.glob(f"{log.name}.pending.*"))
        p.check(f"it was parked in exactly one .pending.* sibling, not dropped ({len(pending)} found)",
                len(pending) == 1, str(pending))
        p.check("the parked file holds the real entry, intact",
                json.loads(pending[0].read_bytes().decode("utf-8")) == {"tag": "parked"})

        lock.unlink()  # the holder finishes / is reclaimed
        io_mod.append_jsonl(log, {"tag": "later"}, max_bytes=cap)

        tags = [d.get("tag") for d in _lines(log)]
        p.check("the next ordinary call folded the parked entry in, ahead of its own",
                tags == ["first", "parked", "later"], str(tags))
        leftovers = list(log.parent.glob(f"{log.name}.*"))
        p.check("the pending file and the lock are gone", leftovers == [], str(leftovers))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_a_parked_entry_still_obeys_the_cap_when_folded_in(p: Probe) -> None:
    """A parked entry must meet the SAME cap policy it would have met had it
    landed on time. Folding pending content in with a bare append would let it
    sail past append_jsonl's byte ceiling -- the ceiling exists to bound a
    hot-spin, and a hot-spin is precisely when entries get parked."""
    root = Path(tempfile.mkdtemp(prefix="ulthar-logcap-timeout-cap-"))
    try:
        log = root / "logs" / "dispatch-errors.jsonl"
        cap = io_mod.LOG_CAP_FOOTER_BYTES + 200
        for i in range(10):  # fills the cap and starts the footer
            io_mod.append_jsonl(log, dict(ENTRY, seq=i), max_bytes=cap)
        p.check(f"the log is capped and bounded ({log.stat().st_size}B <= {cap}B)",
                log.stat().st_size <= cap and _footer(log) is not None)
        dropped_before = int(_footer(log)["dropped_entries"])

        lock = io_mod._lock_path(log)
        lock.write_text(f"{os.getpid()}\n{time.time()}\n", encoding="utf-8")
        io_mod.append_jsonl(log, dict(ENTRY, seq=999), max_bytes=cap)
        lock.unlink()
        io_mod.append_jsonl(log, dict(ENTRY, seq=1000), max_bytes=cap)

        size = log.stat().st_size
        p.check(f"the parked entry did not blow the cap on the way in ({size}B <= {cap}B)",
                size <= cap, f"size={size}")
        footer = _footer(log)
        p.check("the parked entry was counted as dropped, not silently discarded and not appended",
                footer is not None and int(footer["dropped_entries"]) == dropped_before + 2,
                str(footer))
        p.check("no pending file or lock left behind",
                list(log.parent.glob(f"{log.name}.*")) == [])
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _dead_pid() -> int:
    """A pid that has certainly exited. (It could in principle be recycled
    before the assertion runs -- which is the very hazard the third test below
    covers, and would only ever make these tests stricter, never falsely green.)"""
    proc = subprocess.Popen([sys.executable, "-c", "pass"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    proc.wait()
    return proc.pid


def _age_lock(lock_path: Path, seconds: float) -> None:
    """Backdate a lock by `seconds` -- both its mtime (what an OWNERLESS lock is
    judged by) and the timestamp inside its owner record when it has one.
    Backdating beats sleeping: the bounds under test are 2s and 60s."""
    stamp = time.time() - seconds
    lines = lock_path.read_text(encoding="utf-8").splitlines()
    if len(lines) >= 2:
        lock_path.write_text(f"{lines[0]}\n{stamp}\n", encoding="utf-8")
    os.utime(lock_path, (stamp, stamp))


def test_a_lock_with_no_readable_owner_is_recovered(p: Probe) -> None:
    """REVIEW FINDING. The owner record is written immediately AFTER the
    O_CREAT|O_EXCL that creates the lock file, so an interruption between the
    two leaves a lock that exists and names nobody. That was categorically
    never stale ("no pid -> cannot confirm death"), which is backwards: nothing
    can prove such a lock is held, and nothing ever will. Every later writer
    then burned the full retry budget and parked its entry -- and since no
    writer could ever become the next holder, nothing folded those entries back
    in. The log froze permanently."""
    root = Path(tempfile.mkdtemp(prefix="ulthar-logcap-ownerless-"))
    try:
        log = root / "logs" / "dispatch-errors.jsonl"
        io_mod.append_jsonl(log, {"tag": "first"}, max_bytes=50_000)
        lock = io_mod._lock_path(log)
        lock.write_text("", encoding="utf-8")  # created, never owned

        p.check("a BRAND-NEW ownerless lock is still respected (an acquire in "
                "flight must not be robbed microseconds after it starts)",
                not io_mod._append_lock_is_stale(lock))

        _age_lock(lock, io_mod._APPEND_LOCK_STALE_AGE_SEC + 1)
        p.check("an ownerless lock older than the stale age IS reclaimable",
                io_mod._append_lock_is_stale(lock))

        started = time.monotonic()
        io_mod.append_jsonl(log, {"tag": "after"}, max_bytes=50_000)
        waited = time.monotonic() - started
        tags = [d.get("tag") for d in _lines(log)]
        p.check("the entry lands in the LOG, not in a pending file -- the lock was "
                "reclaimed rather than waited out", tags == ["first", "after"], str(tags))
        p.check(f"and it did not pay the retry budget to get there ({waited:.3f}s)",
                waited < 0.2, f"waited={waited:.3f}s")
        leftovers = list(log.parent.glob(f"{log.name}.*"))
        p.check("no pending file, no lock, no stuck-lock note left behind",
                leftovers == [], str(leftovers))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_a_lock_whose_pid_was_reused_is_recovered(p: Probe) -> None:
    """REVIEW FINDING. A CONFIRMED-DEAD pid is sound evidence that a lock is
    free; a LIVE pid is not evidence that it is held, because pids are
    recycled. A lock left by a killed holder whose pid was later handed to an
    unrelated live process could never be reclaimed -- permanently, not for a
    while. The additional evidence is the lock's own age against a bound no
    legitimate holder can exceed: a holder appends one line and rewrites a
    fixed-width footer.

    The live pid used here is this test process's own, which is the strongest
    form of the case -- it is definitely alive."""
    root = Path(tempfile.mkdtemp(prefix="ulthar-logcap-pidreuse-"))
    try:
        log = root / "logs" / "dispatch-errors.jsonl"
        io_mod.append_jsonl(log, {"tag": "first"}, max_bytes=50_000)
        lock = io_mod._lock_path(log)

        lock.write_text(f"{os.getpid()}\n{time.time()}\n", encoding="utf-8")
        p.check("a live pid holding a FRESH lock is left alone (the healthy case)",
                not io_mod._append_lock_is_stale(lock))

        _age_lock(lock, io_mod._APPEND_LOCK_STALE_AGE_SEC + 1)
        p.check("a live pid past the stale age is STILL left alone -- age at that "
                "scale reclaims nothing on its own",
                not io_mod._append_lock_is_stale(lock))

        _age_lock(lock, io_mod._APPEND_LOCK_MAX_HOLD_SEC + 1)
        p.check(f"held past {io_mod._APPEND_LOCK_MAX_HOLD_SEC:.0f}s, a live pid stops "
                "being evidence that the lock is held", io_mod._append_lock_is_stale(lock))

        io_mod.append_jsonl(log, {"tag": "after"}, max_bytes=50_000)
        tags = [d.get("tag") for d in _lines(log)]
        p.check("the entry lands in the log instead of stranding forever",
                tags == ["first", "after"], str(tags))
        leftovers = list(log.parent.glob(f"{log.name}.*"))
        p.check("nothing is left behind", leftovers == [], str(leftovers))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_a_stuck_lock_says_so_and_says_how_to_clear_it(p: Probe) -> None:
    """Even with better reclaim, a lock reclaim cannot CLEAR (a removal that
    keeps failing) freezes the log. Silence there is the same defect in a
    smaller box: the log looks frozen, pending files pile up, and every write
    adds the retry budget to the PreToolUse path with nothing anywhere naming
    the cause. The one documented manual-recovery message in this project names
    the pm-delete-allow ledger lock -- a different lock, in different code.

    Reclaim is stubbed rather than raced against a real Windows sharing
    violation: deterministic, and platform-independent."""
    root = Path(tempfile.mkdtemp(prefix="ulthar-logcap-stuck-"))
    real_reclaim = io_mod._append_lock_reclaim
    try:
        log = root / "logs" / "dispatch-errors.jsonl"
        io_mod.append_jsonl(log, {"tag": "first"}, max_bytes=50_000)
        lock = io_mod._lock_path(log)
        lock.write_text(f"{_dead_pid()}\n{time.time()}\n", encoding="utf-8")
        _age_lock(lock, io_mod._APPEND_LOCK_MAX_HOLD_SEC + 1)

        io_mod._append_lock_reclaim = lambda _lock_path: None  # removal always fails
        io_mod.append_jsonl(log, {"tag": "parked"}, max_bytes=50_000)

        note = log.with_name(f"{log.name}{io_mod.STUCK_LOCK_NOTE_SUFFIX}")
        p.check("a stuck lock leaves an operator-facing note beside the log",
                note.exists(), str(sorted(str(f) for f in log.parent.iterdir())))
        text = note.read_text(encoding="utf-8") if note.exists() else ""
        p.check("the note names THIS lock file, and says it is not the "
                "pm-delete-allow lock the other recovery message is about",
                str(lock) in text and "pm-delete-allow" in text, text[:500])
        p.check("the note gives a usable recovery step (delete the lock) and says "
                "where the un-logged entries are meanwhile",
                "delete the lock file" in text.lower() and f"{log.name}.pending.*" in text,
                text[:700])
        p.check("the entry itself was parked, not lost",
                len(list(log.parent.glob(f"{log.name}.pending.*"))) == 1)

        io_mod._append_lock_reclaim = real_reclaim
        lock.unlink()  # the operator follows the note
        io_mod.append_jsonl(log, {"tag": "later"}, max_bytes=50_000)
        tags = [d.get("tag") for d in _lines(log)]
        p.check("after the named step, the parked entry is folded in",
                tags == ["first", "parked", "later"], str(tags))
        p.check("and the note is gone -- it described a state that no longer holds",
                not note.exists())
    finally:
        io_mod._append_lock_reclaim = real_reclaim
        shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    p = Probe()
    test_footer_is_fixed_width(p)
    test_hot_spin_is_bounded_and_keeps_the_head(p)
    test_a_smaller_entry_never_lands_below_the_footer(p)
    test_single_oversized_entry_is_bounded(p)
    test_legacy_oversized_file_is_capped_once(p)
    test_no_cap_is_still_unbounded(p)
    test_drop_oldest_bounds_the_file_and_keeps_the_newest(p)
    test_drop_oldest_single_oversized_entry_is_kept(p)
    test_concurrent_writers_never_lose_an_entry(p)
    test_concurrent_writers_with_real_rotation_stay_bounded_and_valid(p)
    test_crash_mid_rotation_leaves_one_whole_version(p)
    test_concurrent_append_jsonl_never_loses_an_entry(p)
    test_two_different_writers_of_one_log_share_one_lock(p)
    test_lock_timeout_parks_the_entry_instead_of_losing_it(p)
    test_a_parked_entry_still_obeys_the_cap_when_folded_in(p)
    test_a_lock_with_no_readable_owner_is_recovered(p)
    test_a_lock_whose_pid_was_reused_is_recovered(p)
    test_a_stuck_lock_says_so_and_says_how_to_clear_it(p)
    print(f"\nRESULT: {p.passed} passed, {p.failed} failed")
    return 1 if p.failed else 0


if __name__ == "__main__":
    sys.exit(main())
