"""`ulthar` command line (CORE-01):
  db init | ingest | status | hook                                  (R1-a)
  decide list|review|confirm, fact add|list, inject, index rebuild,
  store verify                                                     (R1-b)
  issue add|list, unit new|show|advance|list, contract submit|lint|show,
  review record, verify                                            (R1-c)
  pack install|list, run, unit advance <u> integrated|done|reopen|contract_draft,
  unit show <u> --ask reopen --ac <id|なし>                          (R1-d)
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

from . import contract as contract_mod
from . import db as db_mod
from . import decide as decide_mod
from . import evidence as evidence_mod
from . import flow as flow_mod
from . import worker as worker_mod
from . import ingest as ingest_mod
from . import memory
from . import packinstall
from . import packs as packs_mod
from . import runcmd
from . import stages
from . import store as store_mod
from . import tokens as tokens_mod


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


# ---------------------------------------------------------------------------- R1-c

def _actor() -> dict:
    return {"session_id": os.environ.get("CLAUDE_SESSION_ID")}


def _with_rt(args, fn):
    conn = _rt(args)
    try:
        return fn(conn)
    finally:
        conn.close()


def cmd_issue_add(args) -> int:
    rec = flow_mod.add_issue(_store(args), args.title, _actor())
    print(f"{rec['id']}\t{rec['title']}")
    return 0


def cmd_issue_list(args) -> int:
    with _store(args).reader() as conn:
        issues = store_mod.current(conn, "issue")
    for i in issues:
        print(f"{i['id']}\t{i.get('status')}\t{i.get('title')}")
    print(f"issues: {len(issues)}")
    return 0


def cmd_unit_new(args) -> int:
    rec = flow_mod.new_unit(_store(args), args.issue, _actor())
    print(f"{rec['id']}\t{rec['state']}\tissue {rec['issue_id']}")
    return 0


def cmd_unit_list(args) -> int:
    store = _store(args)
    with store.reader() as conn:
        ids = [u["id"] for u in store_mod.current(conn, "unit")]
    units = _with_rt(args, lambda rt: [flow_mod.recover(store, rt, i) for i in ids])
    for u in units:
        print(f"{u['id']}\t{u.get('state')}\tissue {u.get('issue_id')}"
              f"\tcontract v{(u.get('contract') or {}).get('version') or '-'}")
    print(f"units: {len(units)}")
    return 0


def cmd_unit_show(args) -> int:
    store = _store(args)
    if args.ask == "reopen":
        _print_json(_with_rt(args, lambda rt: stages.reopen_question(store, rt, args.unit, args.ac)))
        return 0
    if args.ask:
        ask = flow_mod.qual_question if args.ask == "qual" else flow_mod.resume_question
        _print_json(_with_rt(args, lambda rt: ask(store, rt, args.unit)))
        return 0
    _print_json(_with_rt(args, lambda rt: flow_mod.recover(store, rt, args.unit)))
    return 0


def cmd_unit_advance(args) -> int:
    store = _store(args)
    rec = _with_rt(args, lambda rt: flow_mod.advance(
        store, rt, args.unit, args.to, reason=args.reason, cause=args.cause, actor=_actor(),
        runner_token=args.runner_token))
    last = rec["history"][-1]
    print(f"{rec['id']}\t{last['from']} -> {rec['state']}")
    if args.to == "done" and rec["state"] != "done":
        print(f"the verify on the integration branch failed: {json.dumps(last.get('diff'), ensure_ascii=False)}",
              file=sys.stderr)
        return 1
    if rec["state"] == "integrated":
        print(f"integration commit {rec['integration']['merge_commit']} on {rec['integration']['branch']}")
    return 0


def cmd_contract_submit(args) -> int:
    store = _store(args)
    data = Path(args.file).read_bytes()
    rec = _with_rt(args, lambda rt: flow_mod.submit(store, rt, args.unit, data, _actor()))
    ref = rec["contract"]
    print(f"{rec['id']}\t{rec['state']}\tcontract v{ref['version']} sha:{ref['sha'][:12]}"
          f"\t{store.dir / ref['path']}")
    return 0


def cmd_contract_lint(args) -> int:
    store = _store(args)
    with store.reader() as conn:
        unit = flow_mod.read_unit(conn, args.unit)
    doc = flow_mod.contract_doc(store, unit.get("contract"))
    found = contract_mod.lint(doc, store.repo or store.root, stages.packs_or_error(store))
    for rule, msg in found:
        print(f"{rule}\t{msg}")
    print("contract lint: OK" if not found else f"contract lint: {len(found)} violation(s)")
    return 0 if not found else 1


def cmd_contract_show(args) -> int:
    store = _store(args)
    _print_json(_with_rt(args, lambda rt: flow_mod.contract_question(store, rt, args.unit)))
    return 0


def cmd_review_record(args) -> int:
    store = _store(args)
    rec = _with_rt(args, lambda rt: flow_mod.record_review(
        store, rt, args.unit, args.kind, args.result, args.blocker, args.unmapped_change,
        args.runner_token))
    review = rec["reviews"][-1]
    print(f"{rec['id']}\t{review['kind']} review {review['result']} by {review['agent_id']}"
          f"\tblockers={len(review['blockers'])} unmapped={len(review['unmapped_changes'])}")
    return 0


def cmd_verify(args) -> int:
    store = _store(args)
    manifest, rec = _with_rt(args, lambda rt: flow_mod.verify(
        store, rt, args.unit, args.self, args.runner_token))
    for r in manifest["acs"]:
        print(f"{r['id']}\t{r['kind']}\t{'PASS' if r['passed'] else 'FAIL'}\tvalue={r['value']}"
              f"\texit={r['exit_code']}")
    print(f"tree {manifest['tree_hash']}  manifest run {manifest['run_id']}  unit {rec['state']}"
          + (f" ({rec.get('halt_reason')})" if rec["state"] == "halted" else ""))
    return 0 if manifest["all_quant_passed"] and rec["state"] != "halted" else 1


def cmd_pack_install(args) -> int:
    store = _store(args)
    rows = _with_rt(args, lambda rt: packinstall.install(store, rt, Path(args.agents_dir)))
    for r in rows:
        print(f"{r['agent_type']}\t{r['def_path']}\tskills={','.join(s['name'] for s in r['skills'])}")
    print(f"pack agents installed: {len(rows)}")
    return 0


def cmd_pack_list(args) -> int:
    store = _store(args)
    for p in packs_mod.for_store(store):
        print(f"pack {p['name']}\tpaths={','.join(p['path_globs'])}\tskills={','.join(p['skills'])}"
              f"\tdefault_acs={len(p['default_acs'])}")
    rows = _with_rt(args, packinstall.listing)
    for r in rows:
        print(f"{r['agent_type']}\t{'intact' if r['intact'] else 'CHANGED'}\t{r['def_path']}")
    print(f"registered pack agents: {len(rows)}")
    return 0 if all(r["intact"] for r in rows) else 1


def cmd_run(args) -> int:
    store = _store(args)
    res = _with_rt(args, lambda rt: runcmd.run(store, rt, args.unit, args.name, args.runner_token))
    for path, blob in sorted(res["changed"].items()):
        print(f"{blob or '(deleted)'}\t{path}")
    for problem in res["problems"]:
        print(f"FAILED: {problem}", file=sys.stderr)
    print(f"event {res['event_id']} {res['decision']}  exit={res['exit_code']}  "
          f"fence_token={res['fence_token']}  changed={len(res['changed'])}")
    return 0 if res["decision"] == "allow" and res["exit_code"] == 0 else 1


REFUSALS = (flow_mod.FlowError, contract_mod.ContractError, tokens_mod.TokenError,
            evidence_mod.EvidenceError, packs_mod.PackError)


def _r1c_parsers(sub) -> None:
    iss = sub.add_parser("issue", help="issues (ISS-01)")
    isub = iss.add_subparsers(dest="issue_command", required=True)
    _leaf(isub, "add", cmd_issue_add, "add an issue").add_argument("--title", required=True)
    _leaf(isub, "list", cmd_issue_list, "every issue")

    unit = sub.add_parser("unit", help="units and their process (design §7)")
    usub = unit.add_subparsers(dest="unit_command", required=True)
    _leaf(usub, "new", cmd_unit_new, "a unit (state intake) of an issue").add_argument(
        "--issue", required=True)
    show = _leaf(usub, "show", cmd_unit_show, "the unit's record, or an owner question")
    show.add_argument("unit")
    show.add_argument("--ask", choices=("qual", "resume", "reopen"),
                      help="print (and record) the QUAL-JUDGE / UNIT-RESUME / UNIT-REOPEN "
                           "AskUserQuestion input")
    show.add_argument("--ac", help="with --ask reopen: the AC that should have caught it, or なし")
    adv = _leaf(usub, "advance", cmd_unit_advance, "move the unit (window only)")
    adv.add_argument("unit")
    adv.add_argument("to")
    adv.add_argument("--reason", help="required for a return")
    adv.add_argument("--cause", choices=flow_mod.HALT_CAUSES, help="required to leave halted")
    adv.add_argument("--runner-token", help="(added by the hook; `integrated` only)")
    _leaf(usub, "list", cmd_unit_list, "every unit with its state")

    con = sub.add_parser("contract", help="goal contracts (YAML)")
    csub = con.add_subparsers(dest="contract_command", required=True)
    sm = _leaf(csub, "submit", cmd_contract_submit, "submit a new contract version")
    sm.add_argument("unit")
    sm.add_argument("--file", required=True)
    _leaf(csub, "lint", cmd_contract_lint, "lint the unit's current contract").add_argument("unit")
    _leaf(csub, "show", cmd_contract_show,
          "print (and record) the CONTRACT-CONFIRM AskUserQuestion input").add_argument("unit")

    rev = sub.add_parser("review", help="the reviewer's records")
    rsub = rev.add_subparsers(dest="review_command", required=True)
    rr = _leaf(rsub, "record", cmd_review_record, "record a contract or verify review (reviewer)")
    rr.add_argument("--unit", required=True)
    rr.add_argument("--kind", required=True, choices=flow_mod.REVIEW_KINDS)
    rr.add_argument("--result", required=True, choices=("pass", "fail"))
    rr.add_argument("--blocker", action="append", default=[])
    rr.add_argument("--unmapped-change", action="append", default=[])
    rr.add_argument("--runner-token", help="(added by the hook)")

    ver = _leaf(sub, "verify", cmd_verify, "run the confirmed contract's verify commands")
    ver.add_argument("unit")
    ver.add_argument("--self", action="store_true", help="the builder's own loop (build)")
    ver.add_argument("--runner-token", help="(added by the hook)")

    pk = sub.add_parser("pack", help="skill packs (R1-d)")
    psub = pk.add_subparsers(dest="pack_command", required=True)
    _leaf(psub, "install", cmd_pack_install, "generate and register the pack agents").add_argument(
        "--agents-dir", required=True, help="where the agent definitions go (created if missing)")
    _leaf(psub, "list", cmd_pack_list, "the packs and the registered pack agents")
    run_p = _leaf(sub, "run", cmd_run, "run a write_cmds command of the confirmed contract")
    run_p.add_argument("unit")
    run_p.add_argument("name")
    run_p.add_argument("--runner-token", help="(added by the hook)")


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

    _r1c_parsers(sub)

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
    sys.stderr.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except REFUSALS as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 3
    except store_mod.StoreError as exc:
        print(f"ulthar: {exc}", file=sys.stderr)
        return 4
