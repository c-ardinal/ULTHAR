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


SCHEMA6 = """
CREATE TABLE receipts (id INTEGER PRIMARY KEY, agent_id TEXT, pack TEXT, agent_def_hash TEXT,
  skill_hashes TEXT, ts TEXT, agent_type TEXT);
CREATE TABLE pack_agents (agent_type TEXT PRIMARY KEY, project_id TEXT, pack TEXT NOT NULL,
  role TEXT NOT NULL, def_path TEXT NOT NULL, def_sha TEXT NOT NULL, skills TEXT NOT NULL, ts TEXT);
CREATE TABLE agent_blocks (agent_id TEXT PRIMARY KEY, agent_type TEXT, reason TEXT, ts TEXT);
CREATE TABLE tokens (token_id TEXT PRIMARY KEY, kind TEXT CHECK (kind IN ('contract_confirm',
  'decision_confirm','qual_judgement','substitution_approval','unit_resume','runner',
  'unit_reopen')), bound TEXT, issued_by_hook TEXT, consumed_at TEXT);
INSERT INTO receipts (agent_id, pack, agent_def_hash, skill_hashes, ts, agent_type)
  VALUES ('a-1', 'ui', 'h', '{}', 't', 'ulthar-creator@ui');
INSERT INTO pack_agents VALUES ('ulthar-creator@ui', 'P', 'ui', 'creator', 'x', 'h', '[]', 't');
INSERT INTO agent_blocks VALUES ('a-2', 'ulthar-creator@ui', 'r', 't');
INSERT INTO tokens VALUES ('tok', 'runner', '{}', 'PreToolUse:Bash', NULL);
PRAGMA user_version=6;
"""


def check_migration_6_to_7(p: Probe) -> None:
    """Review r1d-2 D: a schema-6 runtime.db opens as schema 7 -- receipts keep their rows
    and gain project_id / session_id, pack_agents and agent_blocks are recreated with the
    project in their keys (schema 6 was never deployed: `ulthar pack install` again)."""
    import sqlite3  # noqa: PLC0415 -- the raw schema-6 file, without ulthar.db

    from ulthar import db as db_mod  # noqa: PLC0415

    sb = Sandbox()
    try:
        raw = sqlite3.connect(str(sb.db))
        raw.executescript(SCHEMA6)
        raw.close()
        conn = db_mod.connect(sb.db)
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            cols = {r[1] for r in conn.execute("PRAGMA table_info(receipts)")}
            kept = conn.execute("SELECT agent_id, project_id FROM receipts").fetchall()
            keys = {t: [r[1] for r in conn.execute(f"PRAGMA table_info({t})") if r[5]]
                    for t in ("pack_agents", "agent_blocks")}
            tokens = conn.execute("SELECT token_id FROM tokens").fetchall()
            conn.execute("INSERT INTO pack_agents VALUES ('A', 'ulthar-creator@ui', 'ui', 'creator',"
                         " 'x', 'h', '[]', 't')")
            conn.execute("INSERT INTO pack_agents VALUES ('B', 'ulthar-creator@ui', 'ui', 'creator',"
                         " 'y', 'h', '[]', 't')")
            both = conn.execute("SELECT COUNT(*) FROM pack_agents").fetchone()[0]
        finally:
            conn.close()
        ok = (version == db_mod.SCHEMA_VERSION == 8 and  # R1-e's schema 8 follows R1-d's 7
               {"project_id", "session_id"} <= cols
              and kept == [("a-1", None)] and keys == {"pack_agents": ["project_id", "agent_type"],
                                                      "agent_blocks": ["project_id", "agent_id"]}
              and tokens == [("tok",)] and both == 2)
        p.check("schema 6 -> current (8): receipts kept and widened, pack_agents / agent_blocks keyed by "
                "project (one agent type registered in two projects), tokens kept", ok,
                f"{version} {cols} {kept} {keys} {tokens} {both}")
    finally:
        sb.cleanup()


def main() -> int:
    p = Probe()
    check_schema(p)
    check_migration_6_to_7(p)
    return p.result()


if __name__ == "__main__":
    sys.exit(main())
