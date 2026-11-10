"""AC-1: `ulthar db init` creates a WAL SQLite runtime.db holding the ten §6.2 tables
with every design column name. The expected names are transcribed here from
docs/plans/v2/design.md §6.2, independently of ulthar.db."""
from __future__ import annotations

import sys

from helpers import Probe, Sandbox

EXPECTED = {
    "sessions": "session_id project_id role agent_id started last_seen transcript_path offset "
                "ctx_used ctx_window compactions",
    "leases": "project_id host_id session_id fence_token renewed_at expires_at",
    "events": "ts session_id agent_id agent_type hook tool decision guard_id intent_key "
              "fence_token paths post_hashes detail",
    "tokens": "token_id kind bound issued_by_hook consumed_at",
    "receipts": "agent_id pack agent_def_hash skill_hashes ts",
    "evidence": "unit_id manifest_path tree_hash contract_sha runner_agent_id result",
    "questions": "qid project_id unit_id status payload",
    "capabilities": "name healthy last_check remaining detail",
    "notifications": "nid project_id channel kind sent_ok acked_by_event_id retries",
    "suspicions": "block_event_id allow_event_id intent_key notified",
}


def check_schema(p: Probe) -> None:
    sb = Sandbox()
    try:
        proc = sb.cli("db", "init", "--db", str(sb.db))
        p.check("db init exits 0", proc.returncode == 0, proc.stderr[-500:])
        mode = sb.query("PRAGMA journal_mode")[0][0]
        p.check("journal_mode is wal", mode == "wal", mode)
        tables = {r[0] for r in sb.query("SELECT name FROM sqlite_master WHERE type='table'")}
        present = [t for t in EXPECTED if t in tables]
        p.check(f"§6.2 tables present: {len(present)}/10", len(present) == 10,
                str(sorted(set(EXPECTED) - tables)))
        missing = []
        for table, cols in EXPECTED.items():
            have = {r[1] for r in sb.query(f'PRAGMA table_info("{table}")')}
            missing += [f"{table}.{c}" for c in cols.split() if c not in have]
        p.check(f"missing columns: {len(missing)}", not missing, str(missing))
        again = sb.cli("db", "init", "--db", str(sb.db))
        p.check("db init is idempotent", again.returncode == 0, again.stderr[-300:])
    finally:
        sb.cleanup()


def main() -> int:
    p = Probe()
    check_schema(p)
    return p.result()


if __name__ == "__main__":
    sys.exit(main())
