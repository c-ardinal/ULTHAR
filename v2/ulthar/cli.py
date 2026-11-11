"""`ulthar` command line (CORE-01):
  db init | ingest | status | hook                                  (R1-a)
  decide list|review|confirm, fact add|list, inject, index rebuild,
  store verify                                                     (R1-b)
  extract                         (internal: the detached extraction worker)
Project-scoped commands act on the project of --project-dir (default: the current
directory); see ulthar.store for how that maps to a knowledge_root.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import db as db_mod
from . import decide as decide_mod
from . import worker as worker_mod
from . import ingest as ingest_mod
from . import memory
from . import store as store_mod


def _store(args) -> store_mod.Store:
    return store_mod.Store.for_cwd(args.project_dir or os.getcwd())


def _rt(args):
    return db_mod.connect(db_mod.resolve_path(getattr(args, "db", None)))


def _print_json(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


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
    conn = _rt(args)
    started = time.perf_counter()
    counts = ingest_mod.ingest(conn, args.session, args.transcript, store=_store(args))
    counts["seconds"] = round(time.perf_counter() - started, 3)
    conn.close()
    print(json.dumps(counts, ensure_ascii=False))
    return 0


def cmd_status(args) -> int:
    conn = _rt(args)
    rows = conn.execute(
        "SELECT s.session_id, s.last_seen,"
        " (SELECT COUNT(*) FROM events e WHERE e.session_id = s.session_id)"
        " FROM (SELECT session_id, MAX(last_seen) AS last_seen FROM ("
        "   SELECT session_id, last_seen FROM sessions"
        "   UNION ALL SELECT session_id, NULL FROM events) GROUP BY session_id) s"
        " ORDER BY s.last_seen DESC, s.session_id").fetchall()
    conn.close()
    with _store(args).reader() as kconn:
        episodes = dict(kconn.execute(
            "SELECT session_id, COUNT(*) FROM records WHERE kind='episode' GROUP BY session_id")
            .fetchall()) if kconn is not None else {}
    sessions = {r[0]: r for r in rows}
    for session_id in episodes:
        sessions.setdefault(session_id, (session_id, None, 0))
    print(f"sessions: {len(sessions)}")
    for session_id, last_seen, n_events in sessions.values():
        print(f"{session_id}\tevents={n_events}\tepisodes={episodes.get(session_id, 0)}"
              f"\tlast_seen={last_seen or '-'}")
    return 0


def cmd_decide_list(args) -> int:
    with _store(args).reader() as conn:
        decisions = memory.active_decisions(conn) if not args.all else (
            store_mod.current(conn, "decision"))
    for d in decisions:
        print(f"{d['id']}\t{d.get('status')}\t{d.get('scope')}\t{d.get('text')}")
    print(f"decisions: {len(decisions)}")
    return 0


def cmd_decide_review(args) -> int:
    conn = _rt(args)
    try:
        question = decide_mod.review(_store(args), conn)
    finally:
        conn.close()
    if question is None:
        print("未確認の決定候補はありません。")
        return 0
    _print_json(question)
    return 0


def cmd_decide_confirm(args) -> int:
    conn = _rt(args)
    try:
        result = decide_mod.confirm(_store(args), conn, args.token,
                                    args.project_dir or os.getcwd())
    except decide_mod.ConfirmError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 3
    finally:
        conn.close()
    _print_json(result)
    return 0


def _parse_src(src: str) -> tuple[str, int]:
    path, sep, line = src.rpartition(":")
    if not sep or not path or not line.isdigit() or int(line) < 1:
        raise SystemExit(f"--src must be <path>:<line> (line >= 1): {src!r}")
    return path, int(line)


def cmd_fact_add(args) -> int:
    store = _store(args)
    repo = store.repo or store.root
    raw_path, line = _parse_src(args.src)
    full = (repo / raw_path) if not os.path.isabs(raw_path) else Path(raw_path)
    try:
        rel = full.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        raise SystemExit(f"--src must be inside the project ({repo}): {raw_path}") from None
    blob = memory.blob_hashes(repo, [rel])[rel]
    if blob is None:
        raise SystemExit(f"--src file not found: {full}")
    with store.transaction({"cli": "fact add", "session_id": os.environ.get("CLAUDE_SESSION_ID")}) as tx:
        rec = tx.add("fact", {"text": args.text, "src_path": rel, "src_line": line,
                              "src_blob": blob, "status": "valid",
                              "valid_from": store_mod.now_iso(), "invalid_at": None})
    print(f"{rec['id']}\t{rel}:{line}@{blob}")
    return 0


def cmd_fact_list(args) -> int:
    with _store(args).reader() as conn:
        facts = store_mod.current(conn, "fact")
    for f in facts:
        print(f"{f['id']}\t{f.get('status')}\t{f['src_path']}:{f['src_line']}@{f['src_blob'][:12]}"
              f"\t{f.get('text')}")
    print(f"facts: {len(facts)}")
    return 0


def cmd_inject(args) -> int:
    """Shows what would be injected; records nothing about the injection itself."""
    store = _store(args)
    conn = _rt(args)
    try:
        if args.layer == "L0":
            packed, _ = memory.build_l0(store, conn)
        else:
            packed = memory.build_l1(store, conn, {"cli": "inject", "session_id": args.session})
    finally:
        conn.close()
    print(packed.text)
    print(f"\n-- {args.layer}: {len(packed.text)} chars (limit {packed.limit}), over={packed.over},"
          f" pointers={len(packed.pointerized)}, omitted={len(packed.omitted)}", file=sys.stderr)
    return 0


def cmd_index_rebuild(args) -> int:
    store = _store(args)
    n = store.rebuild()
    print(f"index rebuilt: {store.index_path} ({n} records)")
    return 0


def cmd_store_verify(args) -> int:
    """AC-20: recomputes every JSONL chain value; exit 5 and the first divergence when
    any file differs from what ulthar wrote."""
    problems = _store(args).verify()
    for line in problems:
        print(line)
    print("store verify: OK" if not problems else f"store verify: {len(problems)} problem(s)")
    return 0 if not problems else 5


def cmd_extract(args) -> int:
    return worker_mod.run_worker(args.episode, args.project_dir or os.getcwd())


def _project_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument("--project-dir", help="the project (default: current directory)")
    p.add_argument("--db", help="runtime.db path (default $ULTHAR_RUNTIME_DB or ~/.claude/ulthar/runtime.db)")


def _leaf(sub, name: str, func, help_text: str) -> argparse.ArgumentParser:
    p = sub.add_parser(name, help=help_text)
    _project_arg(p)
    p.set_defaults(func=func)
    return p


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ulthar")
    sub = parser.add_subparsers(dest="command", required=True)
    db_p = sub.add_parser("db", help="runtime.db maintenance")
    db_sub = db_p.add_subparsers(dest="db_command", required=True)
    init_p = db_sub.add_parser("init", help="create runtime.db (SQLite WAL) with the schema")
    init_p.add_argument("--db", help="path (default $ULTHAR_RUNTIME_DB or ~/.claude/ulthar/runtime.db)")
    init_p.set_defaults(func=cmd_db_init)
    ing = _leaf(sub, "ingest", cmd_ingest, "ingest a session transcript into Episodes")
    ing.add_argument("--session", required=True)
    ing.add_argument("--transcript", required=True)
    _leaf(sub, "status", cmd_status, "sessions with event and Episode counts")

    dec = sub.add_parser("decide", help="decisions: list, batch review, confirm")
    dsub = dec.add_subparsers(dest="decide_command", required=True)
    dl = _leaf(dsub, "list", cmd_decide_list, "active decisions (provisional + confirmed)")
    dl.add_argument("--all", action="store_true", help="every status")
    _leaf(dsub, "review", cmd_decide_review,
          "print one AskUserQuestion input with every unconfirmed candidate")
    dc = _leaf(dsub, "confirm", cmd_decide_confirm, "apply the owner's answer (hook-issued token)")
    dc.add_argument("--token", required=True)

    fact = sub.add_parser("fact", help="facts with a source (path:line@blob)")
    fsub = fact.add_subparsers(dest="fact_command", required=True)
    fa = _leaf(fsub, "add", cmd_fact_add, "record a fact with its source")
    fa.add_argument("--src", required=True, help="<path>:<line>")
    fa.add_argument("text")
    _leaf(fsub, "list", cmd_fact_list, "every fact with its status")

    inj = _leaf(sub, "inject", cmd_inject, "show the L0 or L1 injection (display only)")
    inj.add_argument("--layer", required=True, choices=("L0", "L1"))
    inj.add_argument("--session", required=True)

    idx = sub.add_parser("index", help="knowledge index maintenance")
    isub = idx.add_subparsers(dest="index_command", required=True)
    _leaf(isub, "rebuild", cmd_index_rebuild, "regenerate index.db from the JSONL files")

    sto = sub.add_parser("store", help="knowledge store integrity")
    ssub = sto.add_subparsers(dest="store_command", required=True)
    _leaf(ssub, "verify", cmd_store_verify, "recompute the JSONL chain values (AC-20)")

    ex = _leaf(sub, "extract", cmd_extract, "(internal, hook only) run one extraction")
    ex.add_argument("--episode", required=True)
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
    try:
        return args.func(args)
    except store_mod.StoreError as exc:
        print(f"ulthar: {exc}", file=sys.stderr)
        return 4
