"""AC-6 and AC-9 against the owner's real transcript, read in place via
$ULTHAR_REPLAY_TRANSCRIPT (never copied or modified; skipped with a message when unset).

AC-6  one `ulthar ingest` of the whole transcript yields exactly 10 compact_summary
      and 570 owner_msg Episodes (the PM's reference count).
AC-9  full ingest <= 120 s; and with the transcript ingested except for one new line,
      one hook call (process start included) takes p95 <= 300 ms over 200 calls.
      "One line appended" is reproduced without touching the file: before each call
      the session's cursor is moved back to the start of the transcript's last line,
      so every call finds exactly one unread line -- the same work as a real append.
      The call is a PreToolUse Bash (the guard-heaviest event) using the production
      hook form (`python -S .../__main__.py hook`).
The measured values are written to v2/tests/results/replay-latest.json.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from helpers import HOOK_ARGV, TESTS_DIR, Probe, Sandbox

ENV = "ULTHAR_REPLAY_TRANSCRIPT"
SHA_PREFIX = "48a2d2d5afe28ea0"
EXPECTED = {"compact_summary": 10, "owner_msg": 570}
FULL_INGEST_LIMIT_SEC = 120.0
P95_LIMIT_MS = 300.0
CALLS = 200
TAIL_PROBE_BYTES = 8 * 1024 * 1024
RESULTS = TESTS_DIR / "results" / "replay-latest.json"


def sha256_prefix(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def last_two_lines(path: Path) -> tuple[int, bytes, bytes]:
    """(start offset of the last line, the line before it, the last line)."""
    size = path.stat().st_size
    with open(path, "rb") as fh:
        start = max(0, size - TAIL_PROBE_BYTES)
        fh.seek(start)
        tail = fh.read()
    lines = tail.splitlines(keepends=True)
    if len(lines) < 3:
        raise RuntimeError("tail probe too small for the last two lines")
    return size - len(lines[-1]), lines[-2], lines[-1]


def p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


def run_replay(p: Probe, transcript: Path) -> dict:
    from ulthar.ingest import _line_sig  # the cursor's own signature, so the rollback is exact

    sb = Sandbox()
    record: dict = {"transcript_lines": None, "calls": CALLS}
    try:
        session = transcript.stem
        started = time.perf_counter()
        proc = sb.cli("ingest", "--session", session, "--transcript", str(transcript))
        full_sec = time.perf_counter() - started
        counts = json.loads(proc.stdout) if proc.returncode == 0 else {}
        record.update(full_ingest_sec=round(full_sec, 2), counts=counts)
        record["transcript_lines"] = counts.get("lines")
        p.check(f"ulthar ingest exit 0 ({counts.get('lines')} lines)", proc.returncode == 0,
                proc.stderr[-500:])
        for kind, want in EXPECTED.items():
            p.check(f"AC-6: {kind} = {counts.get(kind)} (expected exactly {want})",
                    counts.get(kind) == want)
        p.check(f"AC-9: full ingest {full_sec:.2f} s <= {FULL_INGEST_LIMIT_SEC:.0f} s",
                full_sec <= FULL_INGEST_LIMIT_SEC)
        again = sb.cli("ingest", "--session", session, "--transcript", str(transcript))
        again_counts = json.loads(again.stdout) if again.returncode == 0 else {}
        p.check("AC-7 on the real transcript: a second ingest adds 0 Episodes",
                again.returncode == 0 and not any(again_counts.get(k) for k in EXPECTED)
                and again_counts.get("lines") == 0, again.stdout[-300:])

        last_start, prev_line, _last = last_two_lines(transcript)
        line_total = counts.get("lines", 0)
        payload = json.dumps({
            "hook_event_name": "PreToolUse", "session_id": session,
            "transcript_path": str(transcript), "cwd": str(sb.work), "tool_name": "Bash",
            "tool_input": {"command": "ls -la"}})
        env = sb.env()
        episodes_before = sb.query("SELECT COUNT(*) FROM episodes")[0][0]
        times, bad = [], []
        for i in range(CALLS):
            _rollback_cursor(sb, session, transcript, last_start, line_total - 1,
                             len(prev_line), _line_sig(prev_line))
            t0 = time.perf_counter()
            call = subprocess.run(HOOK_ARGV + ["--event", "PreToolUse"], input=payload,
                                  capture_output=True, text=True, encoding="utf-8", env=env)
            times.append((time.perf_counter() - t0) * 1000)
            cur = sb.query("SELECT byte_offset, line_no FROM ingest_cursors WHERE session_id=?",
                           (session,))
            if call.returncode != 0 or cur != [(transcript.stat().st_size, line_total)]:
                bad.append((i, call.returncode, cur, call.stderr[-200:]))
        episodes_after = sb.query("SELECT COUNT(*) FROM episodes")[0][0]
        record.update(p50_ms=round(statistics.median(times), 1), p95_ms=round(p95(times), 1),
                      max_ms=round(max(times), 1), min_ms=round(min(times), 1))
        p.check(f"each of the {CALLS} calls ingested exactly the one new line and allowed",
                not bad, str(bad[:3]))
        p.check("the re-read last line adds no duplicate Episode",
                episodes_after == episodes_before, f"{episodes_before} -> {episodes_after}")
        p.check(f"AC-9: hook p95 {record['p95_ms']} ms <= {P95_LIMIT_MS:.0f} ms over {CALLS} calls "
                f"(p50 {record['p50_ms']}, max {record['max_ms']})",
                record["p95_ms"] <= P95_LIMIT_MS)
    finally:
        sb.cleanup()
    return record


def _rollback_cursor(sb: Sandbox, session: str, transcript: Path, offset: int, line_no: int,
                     tail_len: int, tail_sig: str) -> None:
    from ulthar import db as db_mod  # noqa: PLC0415 -- production settings, see Sandbox.query

    conn = db_mod.connect(sb.db)  # autocommit: the UPDATE is committed on execute
    try:
        conn.execute("UPDATE ingest_cursors SET byte_offset=?, line_no=?, tail_len=?, tail_sha=?"
                     " WHERE session_id=? AND transcript_path=?",
                     (offset, line_no, tail_len, tail_sig, session, str(transcript)))
    finally:
        conn.close()


def main() -> int:
    raw = os.environ.get(ENV)
    if not raw:
        print(f"SKIP test_replay: ${ENV} is not set -- AC-6 and AC-9 were NOT checked.")
        return 0
    p = Probe()
    transcript = Path(raw)
    prefix = sha256_prefix(transcript) if transcript.is_file() else "(missing)"
    p.check(f"transcript sha256 prefix {prefix} == {SHA_PREFIX} (else: undecidable, report to PM)",
            prefix == SHA_PREFIX)
    if prefix != SHA_PREFIX:
        return p.result()
    record = run_replay(p, transcript)
    record.update(measured_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  sha256_prefix=prefix, python=sys.version.split()[0],
                  platform=platform.platform(), passed=p.passed, failed=p.failed)
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    RESULTS.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(f"measurements written to {RESULTS}")
    return p.result()


if __name__ == "__main__":
    sys.exit(main())
