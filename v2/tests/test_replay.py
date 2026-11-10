"""AC-6 and AC-9 against the owner's real transcript ($ULTHAR_REPLAY_TRANSCRIPT; skipped
with a message when unset). The real transcript is only ever read in place: copying
it, even to a private temp dir, is refused by this environment's permission policy.

AC-6  one `ulthar ingest` of the whole real transcript yields exactly 10
      compact_summary and 570 owner_msg Episodes (the PM's reference count).
AC-9  full ingest of the real transcript <= 120 s. Then, in the same runtime.db and
      session, a synthetic transcript of the same line count and byte size is
      ingested (the stand-in for "the ingested transcript", since the real one may
      not be copied or modified), and 200 times: one fresh, uniquely identified
      owner-message line is appended (outside the timed interval) and one hook call
      (process start included) is timed -- p95 <= 300 ms. After every call the new
      line's owner_msg Episode must exist (the fresh-insert path ran) and the cursor
      must be at the end of the file. The call is a PreToolUse Bash (the guard-heaviest
      event) in the production hook form (`python -S .../__main__.py hook`).
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
RESULTS = TESTS_DIR / "results" / "replay-latest.json"


def sha256_prefix(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


def owner_line(uuid: str, text: str) -> bytes:
    rec = {"type": "user", "uuid": uuid, "isSidechain": False,
           "timestamp": datetime.now(timezone.utc).isoformat(),
           "message": {"role": "user", "content": text}}
    return (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8")


def _full_ingest(p: Probe, sb: Sandbox, session: str, transcript: Path, record: dict) -> int:
    started = time.perf_counter()
    proc = sb.cli("ingest", "--session", session, "--transcript", str(transcript))
    full_sec = time.perf_counter() - started
    counts = json.loads(proc.stdout) if proc.returncode == 0 else {}
    record.update(full_ingest_sec=round(full_sec, 2), counts=counts,
                  transcript_lines=counts.get("lines"))
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
    return counts.get("lines", 0)


def _timed_appends(p: Probe, sb: Sandbox, session: str, transcript: Path, lines: int,
                   record: dict) -> None:
    payload = json.dumps({
        "hook_event_name": "PreToolUse", "session_id": session,
        "transcript_path": str(transcript), "cwd": str(sb.work), "tool_name": "Bash",
        "tool_input": {"command": "ls -la"}})
    env = sb.env()
    run_tag = os.urandom(4).hex()
    times, bad = [], []
    for i in range(CALLS):
        uuid = f"ac9-{run_tag}-{i}"
        with open(transcript, "ab") as fh:
            fh.write(owner_line(uuid, f"AC-9 timing probe {run_tag} #{i}"))
        t0 = time.perf_counter()
        call = subprocess.run(HOOK_ARGV + ["--event", "PreToolUse"], input=payload,
                              capture_output=True, text=True, encoding="utf-8", env=env)
        times.append((time.perf_counter() - t0) * 1000)
        episode = sb.query("SELECT COUNT(*) FROM episodes WHERE session_id=? AND kind='owner_msg'"
                           " AND dedupe_key=?", (session, f"{uuid}:owner_msg:0"))[0][0]
        cur = sb.query("SELECT byte_offset, line_no FROM ingest_cursors WHERE session_id=?"
                       " AND transcript_path=?", (session, str(transcript)))
        if (call.returncode != 0 or episode != 1
                or cur != [(transcript.stat().st_size, lines + i + 1)]):
            bad.append((i, call.returncode, episode, cur, call.stderr[-200:]))
    owner_total = sb.query("SELECT COUNT(*) FROM episodes WHERE session_id=? AND kind='owner_msg'",
                           (session,))[0][0]
    record.update(p50_ms=round(statistics.median(times), 1), p95_ms=round(p95(times), 1),
                  max_ms=round(max(times), 1), min_ms=round(min(times), 1))
    p.check(f"each of the {CALLS} calls ingested its one fresh line as a new owner_msg Episode",
            not bad, str(bad[:3]))
    p.check(f"owner_msg Episodes = {EXPECTED['owner_msg']} + {CALLS} afterwards",
            owner_total == EXPECTED["owner_msg"] + CALLS, str(owner_total))
    p.check(f"AC-9: hook p95 {record['p95_ms']} ms <= {P95_LIMIT_MS:.0f} ms over {CALLS} calls "
            f"(p50 {record['p50_ms']}, max {record['max_ms']})",
            record["p95_ms"] <= P95_LIMIT_MS)


def synthetic_transcript(path: Path, lines: int, size: int) -> None:
    """`lines` JSONL records totalling ~`size` bytes: assistant/system/user records in
    rotation, padded so per-line work and file size match the real transcript."""
    pad = max(0, size // max(lines, 1) - 200)
    with open(path, "wb") as fh:
        for i in range(lines):
            kind = ("assistant", "system", "user")[i % 3]
            rec = {"type": kind, "uuid": f"syn-{i}", "isSidechain": False,
                   "message": {"role": kind, "content": [{"type": "text", "text":
                               "<system-reminder>" + "x" * pad}]}}
            fh.write((json.dumps(rec) + "\n").encode("utf-8"))


def run_replay(p: Probe, transcript: Path) -> dict:
    sb = Sandbox()
    record: dict = {"calls": CALLS}
    try:
        session = transcript.stem
        lines = _full_ingest(p, sb, session, transcript, record)
        synthetic = sb.root / "synthetic-transcript.jsonl"
        synthetic_transcript(synthetic, lines, transcript.stat().st_size)
        record["synthetic_bytes"] = synthetic.stat().st_size
        pre = sb.cli("ingest", "--session", session, "--transcript", str(synthetic))
        p.check("the same-scale synthetic transcript is ingested first",
                pre.returncode == 0 and json.loads(pre.stdout).get("lines") == lines, pre.stdout)
        _timed_appends(p, sb, session, synthetic, lines, record)
    finally:
        sb.cleanup()
    return record


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
