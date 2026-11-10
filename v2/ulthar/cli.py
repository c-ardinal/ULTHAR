"""`ulthar` command line (CORE-01): db init | ingest | status | hook."""
from __future__ import annotations

import argparse
import json
import sys
import time

from . import db as db_mod
from . import ingest as ingest_mod


def cmd_db_init(args) -> int:
    path = db_mod.resolve_path(args.db)
    conn = db_mod.connect(path)
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    conn.close()
    print(f"runtime.db ready: {path} (journal_mode={mode}, tables={len(tables)})")
    return 0


def cmd_ingest(args) -> int:
    conn = db_mod.connect(db_mod.resolve_path(args.db))
    started = time.perf_counter()
    counts = ingest_mod.ingest(conn, args.session, args.transcript)
    counts["seconds"] = round(time.perf_counter() - started, 3)
    conn.close()
    print(json.dumps(counts, ensure_ascii=False))
    return 0


def cmd_status(args) -> int:
    conn = db_mod.connect(db_mod.resolve_path(args.db))
    rows = conn.execute(
        "SELECT s.session_id, s.last_seen,"
        " (SELECT COUNT(*) FROM events e WHERE e.session_id = s.session_id),"
        " (SELECT COUNT(*) FROM episodes p WHERE p.session_id = s.session_id)"
        " FROM (SELECT session_id, MAX(last_seen) AS last_seen FROM ("
        "   SELECT session_id, last_seen FROM sessions"
        "   UNION ALL SELECT session_id, NULL FROM episodes"
        "   UNION ALL SELECT session_id, NULL FROM events) GROUP BY session_id) s"
        " ORDER BY s.last_seen DESC, s.session_id").fetchall()
    conn.close()
    print(f"sessions: {len(rows)}")
    for session_id, last_seen, n_events, n_episodes in rows:
        print(f"{session_id}\tevents={n_events}\tepisodes={n_episodes}\tlast_seen={last_seen or '-'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ulthar")
    sub = parser.add_subparsers(dest="command", required=True)
    db_p = sub.add_parser("db", help="runtime.db maintenance")
    db_sub = db_p.add_subparsers(dest="db_command", required=True)
    init_p = db_sub.add_parser("init", help="create runtime.db (SQLite WAL) with the schema")
    init_p.add_argument("--db", help="path (default $ULTHAR_RUNTIME_DB or ~/.claude/ulthar/runtime.db)")
    init_p.set_defaults(func=cmd_db_init)
    ing = sub.add_parser("ingest", help="ingest a session transcript into Episodes")
    ing.add_argument("--session", required=True)
    ing.add_argument("--transcript", required=True)
    ing.add_argument("--db")
    ing.set_defaults(func=cmd_ingest)
    st = sub.add_parser("status", help="sessions with event and Episode counts")
    st.add_argument("--db")
    st.set_defaults(func=cmd_status)
    sub.add_parser("hook", help="hook dispatcher entry: ulthar hook --event <E> < payload",
                   add_help=False)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["hook"]:
        from . import hook  # noqa: PLC0415 -- the hook path skips argparse setup below
        return hook.main(argv[1:])
    sys.stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    return args.func(args)
