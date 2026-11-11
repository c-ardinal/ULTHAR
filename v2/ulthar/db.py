"""runtime.db -- local SQLite (WAL) state (design §6.2, CORE-03).

The ten §6.2 tables carry exactly the design's column names; list/dict columns
(paths[], post_hashes{}, bound{}, skill_hashes) are JSON text. Support tables:
ingest_cursors, tool_names (CORE-07 transcript ingestion), journal_replayed (makes
the fallback journal's replay idempotent, ulthar.journal), extractions (decision
extraction state per owner_msg Episode: pending/done/failed, DEC-01), injections
(the last Episode seq shown by L0, per project) and reminders (candidate sets the
Stop hook already asked about), store_chain (the chain value of every knowledge-store
append, AC-20, a second copy of index.db's) and confirm_ops (progress of a decide
confirm, so a retry completes it). Episodes themselves live in the knowledge store
(ulthar.store), not here (R1-b AC-2). Schema 5 (R1-c): tokens also hold `unit_resume`
(the owner's UNIT-RESUME) and `runner` (a subagent's review/verify run) kinds.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

SCHEMA_VERSION = 5
BUSY_TIMEOUT_MS = 5000
WAL_AUTOCHECKPOINT_PAGES = 256  # 1 MB of 4 KB pages; see connect()
DB_ENV = "ULTHAR_RUNTIME_DB"

DESIGN_TABLES: dict[str, tuple[str, ...]] = {
    "sessions": ("session_id", "project_id", "role", "agent_id", "started", "last_seen",
                 "transcript_path", "offset", "ctx_used", "ctx_window", "compactions"),
    "leases": ("project_id", "host_id", "session_id", "fence_token", "renewed_at", "expires_at"),
    "events": ("ts", "session_id", "agent_id", "agent_type", "hook", "tool", "decision",
               "guard_id", "intent_key", "fence_token", "paths", "post_hashes", "detail"),
    "tokens": ("token_id", "kind", "bound", "issued_by_hook", "consumed_at"),
    "receipts": ("agent_id", "pack", "agent_def_hash", "skill_hashes", "ts"),
    "evidence": ("unit_id", "manifest_path", "tree_hash", "contract_sha", "runner_agent_id",
                 "result"),
    "questions": ("qid", "project_id", "unit_id", "status", "payload"),
    "capabilities": ("name", "healthy", "last_check", "remaining", "detail"),
    "notifications": ("nid", "project_id", "channel", "kind", "sent_ok", "acked_by_event_id",
                      "retries"),
    "suspicions": ("block_event_id", "allow_event_id", "intent_key", "notified"),
}

_DDL = """
CREATE TABLE IF NOT EXISTS sessions (
  session_id TEXT PRIMARY KEY, project_id TEXT, role TEXT, agent_id TEXT,
  started TEXT, last_seen TEXT, transcript_path TEXT, "offset" INTEGER,
  ctx_used INTEGER, ctx_window INTEGER, compactions INTEGER);
CREATE TABLE IF NOT EXISTS leases (
  project_id TEXT PRIMARY KEY, host_id TEXT, session_id TEXT, fence_token INTEGER,
  renewed_at TEXT, expires_at TEXT);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, session_id TEXT NOT NULL, agent_id TEXT,
  agent_type TEXT, hook TEXT NOT NULL, tool TEXT NOT NULL,
  decision TEXT NOT NULL CHECK (decision IN ('allow','deny','rewrite')),
  guard_id TEXT NOT NULL, intent_key TEXT, fence_token INTEGER, paths TEXT,
  post_hashes TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS events_session ON events(session_id);
CREATE TABLE IF NOT EXISTS tokens (
  token_id TEXT PRIMARY KEY,
  kind TEXT CHECK (kind IN ('contract_confirm','decision_confirm','qual_judgement',
                            'substitution_approval','unit_resume','runner')),
  bound TEXT, issued_by_hook TEXT, consumed_at TEXT);
CREATE TABLE IF NOT EXISTS receipts (
  id INTEGER PRIMARY KEY, agent_id TEXT, pack TEXT, agent_def_hash TEXT, skill_hashes TEXT,
  ts TEXT);
CREATE TABLE IF NOT EXISTS evidence (
  id INTEGER PRIMARY KEY, unit_id TEXT, manifest_path TEXT, tree_hash TEXT,
  contract_sha TEXT, runner_agent_id TEXT, result TEXT);
CREATE TABLE IF NOT EXISTS questions (
  qid TEXT PRIMARY KEY, project_id TEXT, unit_id TEXT,
  status TEXT CHECK (status IN ('queued','asked','answered')), payload TEXT);
CREATE TABLE IF NOT EXISTS capabilities (
  name TEXT PRIMARY KEY, healthy INTEGER, last_check TEXT, remaining TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS notifications (
  nid TEXT PRIMARY KEY, project_id TEXT, channel TEXT, kind TEXT, sent_ok INTEGER,
  acked_by_event_id INTEGER, retries INTEGER);
CREATE TABLE IF NOT EXISTS suspicions (
  id INTEGER PRIMARY KEY, block_event_id INTEGER, allow_event_id INTEGER, intent_key TEXT,
  notified INTEGER);
CREATE TABLE IF NOT EXISTS ingest_cursors (
  session_id TEXT NOT NULL, transcript_path TEXT NOT NULL, byte_offset INTEGER NOT NULL,
  line_no INTEGER NOT NULL, tail_len INTEGER NOT NULL, tail_sha TEXT NOT NULL,
  PRIMARY KEY (session_id, transcript_path));
CREATE TABLE IF NOT EXISTS journal_replayed (
  jid TEXT PRIMARY KEY, replayed_at TEXT);
CREATE TABLE IF NOT EXISTS tool_names (
  session_id TEXT NOT NULL, tool_use_id TEXT NOT NULL, name TEXT NOT NULL,
  PRIMARY KEY (session_id, tool_use_id));
CREATE TABLE IF NOT EXISTS extractions (
  episode_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, session_id TEXT,
  status TEXT NOT NULL CHECK (status IN ('pending','done','failed')),
  started TEXT, finished TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS extractions_project ON extractions(project_id, status);
CREATE TABLE IF NOT EXISTS injections (
  project_id TEXT NOT NULL, layer TEXT NOT NULL, last_seq INTEGER NOT NULL, ts TEXT,
  PRIMARY KEY (project_id, layer));
CREATE TABLE IF NOT EXISTS store_chain (
  store_dir TEXT NOT NULL, name TEXT NOT NULL, size INTEGER NOT NULL, chain TEXT NOT NULL,
  ts TEXT, PRIMARY KEY (store_dir, name, size));
CREATE TABLE IF NOT EXISTS confirm_ops (
  token_id TEXT PRIMARY KEY, stage TEXT NOT NULL, detail TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS reminders (
  project_id TEXT NOT NULL, candidates_sha TEXT NOT NULL, ts TEXT,
  PRIMARY KEY (project_id, candidates_sha));
"""


def resolve_path(explicit: str | None = None) -> Path:
    """--db > $ULTHAR_RUNTIME_DB > ~/.claude/ulthar/runtime.db (CORE-03)."""
    raw = explicit or os.environ.get(DB_ENV)
    if raw:
        return Path(raw)
    return Path.home() / ".claude" / "ulthar" / "runtime.db"


def _widen_token_kinds(conn: sqlite3.Connection) -> None:
    """A schema-4 tokens table (its CHECK lacks R1-c's unit_resume/runner kinds) is
    rebuilt with the current definition, rows kept."""
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='tokens'").fetchone()[0]
    if "'runner'" in sql:
        return
    conn.executescript(
        "BEGIN IMMEDIATE; ALTER TABLE tokens RENAME TO tokens_v4;"
        + _DDL[_DDL.index("CREATE TABLE IF NOT EXISTS tokens"):_DDL.index("CREATE TABLE IF NOT EXISTS receipts")]
        + "INSERT INTO tokens SELECT * FROM tokens_v4; DROP TABLE tokens_v4; COMMIT;")


def connect(path: Path) -> sqlite3.Connection:
    """Opens (creating if needed) the database in WAL mode with the schema applied.
    autocommit (isolation_level=None): callers open explicit transactions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_DDL)
        _widen_token_kinds(conn)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    # Hook latency (AC-9): WAL commits without fsync, and no checkpoint+WAL delete
    # on every close -- the WAL auto-checkpoints at its page threshold instead.
    conn.execute("PRAGMA synchronous=NORMAL")
    # A fresh process's first read costs ~1.4 ms per MB of un-checkpointed WAL, and
    # every hook call is a fresh process (~25 KB of WAL per call): a 1 MB bound instead
    # of SQLite's 4 MB default saves ~2 ms per call on average. Same PASSIVE checkpoint
    # (it never waits for other connections), just ~4x as often (about every 40 calls).
    conn.execute(f"PRAGMA wal_autocheckpoint={WAL_AUTOCHECKPOINT_PAGES}")
    conn.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)
    return conn
