"""AC-7: ingestion is idempotent, and a truncated or discontinuous transcript is
recorded as a gap in `events`. Also pins the Episode extraction rules (AC-6's
owner_msg definition) and that a line still being written waits for the next call.
Episodes are read from the project's knowledge store (R1-b AC-2: episodes.jsonl)."""
from __future__ import annotations

import json
import sys

from helpers import Probe, Sandbox, synthetic_transcript, write_jsonl

from ulthar import db as db_mod
from ulthar import ingest as ingest_mod

NEW_KINDS = ingest_mod.KINDS


def _new(counts: dict) -> int:
    return sum(counts[k] for k in NEW_KINDS)


def _ingest(sb: Sandbox, conn, path) -> dict:
    with sb.inproc():
        return ingest_mod.ingest(conn, "s", str(path), store=sb.store())


def _gaps(sb: Sandbox) -> list[str]:
    rows = sb.query("SELECT detail FROM events WHERE guard_id='ingest' ORDER BY id")
    return [json.loads(r[0])["gap"] for r in rows]


def check_extraction_and_idempotence(p: Probe) -> None:
    sb = Sandbox()
    try:
        path = sb.root / "t.jsonl"
        records, expected = synthetic_transcript()
        write_jsonl(path, records)
        conn = db_mod.connect(sb.db)
        first = _ingest(sb, conn, path)
        got = {k: first[k] for k in NEW_KINDS}
        p.check("Episodes by kind match the definitions", got == expected, f"{got} != {expected}")
        texts = {e["text"] for e in sb.episodes("owner_msg")}
        p.check("owner_msg excludes meta, sidechain and every listed prefix",
                texts == {"please build the thing", "second owner message", "third owner message"},
                str(texts))
        second = _ingest(sb, conn, path)
        p.check("AC-7: a second ingest of the same transcript adds 0 Episodes",
                _new(second) == 0 and second["lines"] == 0, str(second))
        with open(path, "ab") as fh:
            fh.write(b'{"type": "user", "uuid": "partial", "message": {"content": "half')
        partial = _ingest(sb, conn, path)
        p.check("a line without its newline is not consumed yet", partial["lines"] == 0,
                str(partial))
        with open(path, "ab") as fh:
            fh.write(b' done"}}\n')
        done = _ingest(sb, conn, path)
        p.check("... and is consumed once complete", done["owner_msg"] == 1, str(done))
        conn.close()
        p.check("no gap recorded on ordinary appends", _gaps(sb) == [], str(_gaps(sb)))
    finally:
        sb.cleanup()


def check_truncation_is_a_gap(p: Probe) -> None:
    sb = Sandbox()
    try:
        path = sb.root / "t.jsonl"
        records, _ = synthetic_transcript()
        write_jsonl(path, records)
        conn = db_mod.connect(sb.db)
        _ingest(sb, conn, path)
        write_jsonl(path, records[:5])
        res = _ingest(sb, conn, path)
        conn.close()
        p.check("AC-7: a truncated transcript records a 'truncated' gap",
                _gaps(sb) == ["truncated"], str(_gaps(sb)))
        p.check("  ... and re-reading what remains adds no duplicate Episodes", _new(res) == 0,
                str(res))
    finally:
        sb.cleanup()


def check_discontinuity_is_a_gap(p: Probe) -> None:
    sb = Sandbox()
    try:
        path = sb.root / "t.jsonl"
        records, _ = synthetic_transcript()
        write_jsonl(path, records)
        conn = db_mod.connect(sb.db)
        _ingest(sb, conn, path)
        extra, _ = synthetic_transcript("x")
        write_jsonl(path, records[:3] + records[4:] + extra[:4])  # one line dropped mid-file
        res = _ingest(sb, conn, path)
        conn.close()
        p.check("AC-7: a dropped line before the cursor records a 'discontinuous' gap",
                _gaps(sb) == ["discontinuous"], str(_gaps(sb)))
        p.check("  ... and only the genuinely new lines become Episodes",
                res["owner_msg"] == 2 and _new(res) == 2, str(res))
    finally:
        sb.cleanup()


def main() -> int:
    p = Probe()
    check_extraction_and_idempotence(p)
    check_truncation_is_a_gap(p)
    check_discontinuity_is_a_gap(p)
    return p.result()


if __name__ == "__main__":
    sys.exit(main())
