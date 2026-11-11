"""AC-6 and AC-9 against the owner's real transcript ($ULTHAR_REPLAY_TRANSCRIPT; skipped
with a message when unset). The real transcript is only ever read in place: copying
it, even to a private temp dir, is refused by this environment's permission policy.

AC-6  one `ulthar ingest` of the whole real transcript yields exactly 10
      compact_summary and 570 owner_msg Episodes (the PM's reference count).
R1-b AC-2: the same counts are read back from <knowledge_root>/.ulthar/episodes.jsonl,
      and runtime.db holds no Episode.
AC-9  full ingest of the real transcript <= 120 s. Then, in the same runtime.db and
      session, a synthetic transcript of the same line count and byte size is
      ingested (the stand-in for "the ingested transcript", since the real one may
      not be copied or modified), and 200 times: one fresh, uniquely identified
      owner-message line is appended (outside the timed interval) and one hook call
      (process start included) is timed -- p95 <= 300 ms. After every call the new
      line's owner_msg Episode must exist (the fresh-insert path ran) and the cursor
      must be at the end of the file. The call is a PreToolUse Bash (the guard-heaviest
      event) in the production hook form (`python -S .../__main__.py hook`).
R1-b AC-16: under the same conditions (one fresh line appended before every timed call,
      200 calls each, process start included): SubagentStart with 200 decisions in
      the store (L1 assembly), and UserPromptSubmit (prompt saved, extraction launched
      detached; the fake extractor, never the real claude) -- each p95 <= 300 ms.
The measured values are written to v2/tests/results/replay-latest.json and appended
to replay-runs.jsonl (AC-9 / AC-16 are judged on 3 consecutive full runs).
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
DECISIONS = 200  # R1-b AC-16: SubagentStart is measured with 200 decisions in the store
CONFIRMED = 20
RESULTS = TESTS_DIR / "results" / "replay-latest.json"
RUNS = TESTS_DIR / "results" / "replay-runs.jsonl"


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
    p.check(f"AC-9 / R1-b AC-16: full ingest {full_sec:.2f} s <= {FULL_INGEST_LIMIT_SEC:.0f} s",
            full_sec <= FULL_INGEST_LIMIT_SEC)
    in_jsonl = {}
    for ep in sb.jsonl("episodes.jsonl"):
        in_jsonl[ep["kind"]] = in_jsonl.get(ep["kind"], 0) + 1
    record["jsonl_counts"] = in_jsonl
    p.check(f"R1-b AC-2: episodes.jsonl holds compact_summary = {in_jsonl.get('compact_summary')}"
            f", owner_msg = {in_jsonl.get('owner_msg')} (exactly {EXPECTED})",
            all(in_jsonl.get(k) == v for k, v in EXPECTED.items()))
    no_table = sb.query("SELECT COUNT(*) FROM sqlite_master WHERE name='episodes'")[0][0] == 0
    p.check("R1-b AC-2: runtime.db has no episodes table (Episodes are not written there)",
            no_table)
    again = sb.cli("ingest", "--session", session, "--transcript", str(transcript))
    again_counts = json.loads(again.stdout) if again.returncode == 0 else {}
    p.check("AC-7 on the real transcript: a second ingest adds 0 Episodes",
            again.returncode == 0 and not any(again_counts.get(k) for k in EXPECTED)
            and again_counts.get("lines") == 0, again.stdout[-300:])
    return counts.get("lines", 0)


def _timed(sb: Sandbox, transcript: Path, event: str, payload_for, check_call) -> tuple[list, list]:
    """CALLS times: append one fresh owner line (untimed), then time one hook call of
    `event` (process start included). Returns (times_ms, bad calls)."""
    env = sb.env()
    run_tag = os.urandom(4).hex()
    times, bad = [], []
    for i in range(CALLS):
        uuid = f"perf-{event}-{run_tag}-{i}"
        with open(transcript, "ab") as fh:
            fh.write(owner_line(uuid, f"timing probe {event} {run_tag} #{i}"))
        body = json.dumps(payload_for(i, run_tag))
        t0 = time.perf_counter()
        call = subprocess.run(HOOK_ARGV + ["--event", event], input=body,
                              capture_output=True, text=True, encoding="utf-8", env=env)
        times.append((time.perf_counter() - t0) * 1000)
        problem = check_call(i, uuid, run_tag, call)
        if problem:
            bad.append((i, call.returncode, problem, call.stderr[-200:]))
    return times, bad


def _stats(times: list) -> dict:
    slowest = sorted(range(len(times)), key=lambda i: -times[i])[:5]
    return {"p50_ms": round(statistics.median(times), 1), "p95_ms": round(p95(times), 1),
            "max_ms": round(max(times), 1), "min_ms": round(min(times), 1),
            "slowest": [[i, round(times[i], 1)] for i in slowest]}


def _timed_pretool(p: Probe, sb: Sandbox, session: str, transcript: Path, lines: int,
                   record: dict) -> None:
    def payload(i, tag):
        return {"hook_event_name": "PreToolUse", "session_id": session,
                "transcript_path": str(transcript), "cwd": str(sb.work), "tool_name": "Bash",
                "tool_input": {"command": "ls -la"}}

    def check(i, uuid, tag, call):
        episode = sb.kquery("SELECT COUNT(*) FROM records WHERE kind='episode' AND session_id=?"
                            " AND dedupe_key=?", (session, f"{uuid}:owner_msg:0"))[0][0]
        cur = sb.query("SELECT byte_offset, line_no FROM ingest_cursors WHERE session_id=?"
                       " AND transcript_path=?", (session, str(transcript)))
        if call.returncode != 0 or episode != 1 or cur != [(transcript.stat().st_size,
                                                             lines + i + 1)]:
            return f"episode={episode} cursor={cur}"
        return None

    times, bad = _timed(sb, transcript, "PreToolUse", payload, check)
    owner_total = len(sb.episodes("owner_msg", session))
    record.update(_stats(times))
    record.setdefault("events", {})["PreToolUse"] = _stats(times)
    p.check(f"each of the {CALLS} calls ingested its one fresh line as a new owner_msg Episode",
            not bad, str(bad[:3]))
    p.check(f"owner_msg Episodes = {EXPECTED['owner_msg']} + {CALLS} afterwards",
            owner_total == EXPECTED["owner_msg"] + CALLS, str(owner_total))
    p.check(f"AC-9 / R1-b AC-16: PreToolUse p95 {record['p95_ms']} ms <= {P95_LIMIT_MS:.0f} ms "
            f"over {CALLS} calls (p50 {record['p50_ms']}, max {record['max_ms']})",
            record["p95_ms"] <= P95_LIMIT_MS)


def _seed_decisions(sb: Sandbox) -> None:
    with sb.inproc(), sb.store().transaction({"hook": "perf-setup"}) as tx:
        for i in range(DECISIONS):
            confirmed = i < CONFIRMED
            tx.add("decision", {"status": "confirmed" if confirmed else "provisional",
                                "text": f"perf decision {i}: UI の余白・角丸・色はトークンに従う（例 {i}）",
                                "raw_episode_id": "E-perf", "scope": "project",
                                "tags": ["design"], "paths": ["src/ui/**"],
                                "binding": confirmed, "confirm_token_id": None})


def _timed_subagent_start(p: Probe, sb: Sandbox, session: str, transcript: Path,
                          record: dict) -> None:
    _seed_decisions(sb)

    def payload(i, tag):
        return {"hook_event_name": "SubagentStart", "session_id": session,
                "transcript_path": str(transcript), "cwd": str(sb.work),
                "agent_id": f"perf-{tag}-{i}", "agent_type": "ulthar-creator"}

    def check(i, uuid, tag, call):
        try:
            ctx = json.loads(call.stdout)["hookSpecificOutput"]["additionalContext"]
        except (ValueError, KeyError, TypeError):
            return "no additionalContext"
        if call.returncode != 0 or "perf decision 0:" not in ctx or len(ctx) > 10_000:
            return f"rc={call.returncode} len={len(ctx)}"
        return None

    times, bad = _timed(sb, transcript, "SubagentStart", payload, check)
    stats = record.setdefault("events", {})["SubagentStart"] = _stats(times)
    p.check(f"each SubagentStart injected L1 with {DECISIONS} decisions in the store (<= 10,000 chars)",
            not bad, str(bad[:3]))
    p.check(f"R1-b AC-16: SubagentStart (L1 assembly) p95 {stats['p95_ms']} ms <= "
            f"{P95_LIMIT_MS:.0f} ms (p50 {stats['p50_ms']}, max {stats['max_ms']})",
            stats["p95_ms"] <= P95_LIMIT_MS)


def _timed_user_prompt(p: Probe, sb: Sandbox, session: str, transcript: Path,
                       record: dict) -> None:
    def payload(i, tag):
        return {"hook_event_name": "UserPromptSubmit", "session_id": session,
                "transcript_path": str(transcript), "cwd": str(sb.work),
                "prompt": f"perf owner prompt {tag} #{i}", "prompt_id": f"perf-{tag}-{i}"}

    def check(i, uuid, tag, call):
        rows = sb.kquery("SELECT id FROM records WHERE kind='episode' AND session_id=? AND"
                         " json_extract(json, '$.prompt_id')=?", (session, f"perf-{tag}-{i}"))
        launched = sb.query("SELECT status FROM extractions WHERE episode_id=?",
                            (rows[0][0],)) if rows else []
        if call.returncode != 0 or len(rows) != 1 or not launched:
            return f"episodes={rows} extraction={launched}"
        return None

    times, bad = _timed(sb, transcript, "UserPromptSubmit", payload, check)
    stats = record.setdefault("events", {})["UserPromptSubmit"] = _stats(times)
    deadline = time.monotonic() + 120
    while sb.query("SELECT COUNT(*) FROM extractions WHERE status='pending'")[0][0]:
        if time.monotonic() > deadline:
            break
        time.sleep(0.5)
    statuses = dict(sb.query("SELECT status, COUNT(*) FROM extractions GROUP BY status"))
    record["extractions"] = statuses
    p.check(f"each UserPromptSubmit saved its prompt and launched an extraction "
            f"(afterwards: {statuses})", not bad and statuses.get("done") == CALLS, str(bad[:3]))
    p.check(f"R1-b AC-16: UserPromptSubmit (extraction launch) p95 {stats['p95_ms']} ms <= "
            f"{P95_LIMIT_MS:.0f} ms (p50 {stats['p50_ms']}, max {stats['max_ms']})",
            stats["p95_ms"] <= P95_LIMIT_MS)


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
        _timed_pretool(p, sb, session, synthetic, lines, record)
        _timed_subagent_start(p, sb, session, synthetic, record)
        _timed_user_prompt(p, sb, session, synthetic, record)
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
    with open(RUNS, "a", encoding="utf-8", newline="\n") as fh:  # AC-9: every run is kept
        fh.write(json.dumps(record) + "\n")
    print(f"measurements written to {RESULTS}")
    return p.result()


if __name__ == "__main__":
    sys.exit(main())
