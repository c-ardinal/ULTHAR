"""`ulthar relay` (SES-04 second half, R1-e AC-4; spec 事前決定 2 with P-21's fallback), CLI only.

Run by the window (the hook appends its runner token, so the CLI knows the window's
session) while it holds the project's lease. In order:
  1. the handover conditions, every one reported when unmet (nothing else is done):
     (a) every unit's state agrees between the store and runtime.db -- the effects of
         each unit's latest operation (tokens consumed, evidence rows) are in runtime.db;
     (b) no question awaits the owner's answer -- a recorded AskUserQuestion that is
         not invalidated and has no hook-issued token whose answer the session transcript
         confirms (the R1-c AC-5 check: a forged PostToolUse's token does not count);
     (c) every subagent seen running in the window's session tree has stopped
         (SubagentStop) or is registered (its SubagentStart was recorded);
  2. a `relay` Episode (the handover summary) in the knowledge store;
  3. the launch command `relay.launch` (argv list; default LAUNCH_DEFAULT), windowless,
     waited for (LAUNCH_TIMEOUT_SEC; killed past it, so no process of ours is left);
  4. on success, in one runtime.db transaction: the lease goes into handover (the old
     token closed; the next session that calls a hook takes it with a higher token,
     ulthar.lease -- P-21: `claude --bg` ignores `--session-id`), relay_due is cleared,
     a `cli` event records the relay; then a notification. A failed launch moves
     nothing and leaves relay_due set.
"""
from __future__ import annotations

import json
import shutil
import subprocess

from . import decide
from . import flow
from . import isolation
from . import lease as lease_mod
from . import notify
from . import store as store_mod
from . import tokens

LAUNCH_DEFAULT = ["claude", "--bg",
                  "ulthar relay による引き継ぎです。注入された L0 と直近の relay Episode（引き継ぎの要約）を"
                  "確認してから作業を続けてください。"]
LAUNCH_TIMEOUT_SEC = 120


class RelayError(Exception):
    pass


def _launch_argv(cfg: dict) -> list[str]:
    raw = (cfg.get("relay") or {}).get("launch", LAUNCH_DEFAULT) if isinstance(cfg.get("relay"), dict) \
        else LAUNCH_DEFAULT
    if not isinstance(raw, list) or not raw or not all(isinstance(a, str) and a for a in raw):
        raise RelayError(".claude/ulthar.json: relay.launch must be a non-empty list of strings")
    return [shutil.which(raw[0]) or raw[0], *raw[1:]]


# ---------------------------------------------------------------------------- conditions

def _units_problems(store: store_mod.Store, rt) -> list[str]:
    with store.reader(rt) as conn:
        units = store_mod.current(conn, "unit")
    out = []
    for u in units:
        op = u.get("op") or {}
        loose = [t[:8] for t in op.get("consume") or [] if not rt.execute(
            "SELECT 1 FROM tokens WHERE token_id=? AND consumed_at IS NOT NULL", (t,)).fetchone()]
        missing = [e["result"]["run_id"] for e in op.get("evidence") or [] if not rt.execute(
            "SELECT 1 FROM evidence WHERE unit_id=? AND json_extract(result, '$.run_id')=?",
            (u["id"], e["result"]["run_id"])).fetchone()]
        if loose or missing:
            out.append(f"unit {u['id']} ({u.get('state')}): the store's last operation is not in "
                       f"runtime.db (tokens not consumed {loose}, evidence missing {missing}); "
                       f"`ulthar unit show {u['id']}` completes it")
    return out


def _verified(rt, conn, token_id: str, kind: str, issuer: str, bound: dict) -> bool:
    """The token's answer is the owner's: the hook-recorded question and answer events and
    the session transcript's tool_result, checked as R1-c AC-5 (tokens.valid_owner_tokens)
    and R1-b's decide confirm do -- a forged PostToolUse's token row never counts."""
    if kind == decide.TOKEN_KIND:
        try:
            decide._check_token(rt, token_id, str(bound.get("project_id")), resuming=True)
        except decide.ConfirmError:
            return False
        return decide._answered_in_transcript(conn, str(bound.get("session_id") or "(none)"),
                                              bound.get("tool_use_id"), str(bound.get("question") or ""),
                                              str(bound.get("answer_text") or ""))
    return tokens._owner_problem(rt, conn, token_id, issuer, bound, set()) is None


def unanswered(rt, store: store_mod.Store, ingest: bool = False) -> list[tuple[str, str | None, str]]:
    """(qid, unit, kind) of the project's questions that await the owner's answer: queued,
    or asked / answered without a token whose answer passes the transcript check
    (`_verified`). `ingest`: bring the answering sessions' transcripts in first."""
    rows = rt.execute("SELECT qid, unit_id, status, payload FROM questions WHERE project_id=? AND"
                      " status IN ('queued', 'asked', 'answered')", (store.project_id,)).fetchall()
    found = {}
    for qid, _u, _s, _p in rows:
        found[qid] = [(t, k, json.loads(b or "{}"), i) for t, k, b, i in rt.execute(
            "SELECT token_id, kind, bound, issued_by_hook FROM tokens WHERE"
            " json_extract(bound, '$.qid')=? OR json_extract(bound, '$.batch_id')=?", (qid, qid))]
    if ingest:
        for session in {b.get("session_id") for toks in found.values() for _t, _k, b, _i in toks
                        if b.get("session_id")}:
            decide._ingest_session(store, rt, session)
    out = []
    with store.reader(rt) as conn:
        for qid, unit_id, status, raw in rows:
            q = json.loads(raw or "{}")
            if q.get("invalidated"):
                continue
            answered = status != "queued" and any(
                _verified(rt, conn, t, k, i, b) for t, k, b, i in found[qid])
            if not answered:
                out.append((qid, unit_id, str(q.get("kind") or "decide_review")))
    return out


def _agent_problems(rt, session_id: str) -> list[str]:
    rows = rt.execute(
        "SELECT agent_id, SUM(hook='SubagentStart'), SUM(hook='SubagentStop') FROM events"
        " WHERE session_id=? AND agent_id IS NOT NULL GROUP BY agent_id", (session_id,)).fetchall()
    return [f"subagent {a} is running and was never registered (no SubagentStart recorded): "
            "stop it first" for a, started, stopped in rows if not stopped and not started]


def conditions(store: store_mod.Store, rt, session_id: str) -> list[str]:
    problems = _units_problems(store, rt)
    problems += [f"question {qid} ({kind}{', unit ' + u if u else ''}) awaits the owner's answer"
                 for qid, u, kind in unanswered(rt, store, ingest=True)]
    return problems + _agent_problems(rt, session_id)


# ---------------------------------------------------------------------------- relay

def _summary(store: store_mod.Store, rt, session_id: str, reason: str | None) -> str:
    with store.reader(rt) as conn:
        units = store_mod.current(conn, "unit")
    ctx = rt.execute("SELECT ctx_used, ctx_window, compactions FROM sessions WHERE session_id=?",
                     (session_id,)).fetchone()
    lines = [f"引き継ぎ（ulthar relay）: 窓口 {session_id} から。契機: {reason or '窓口の判断'}。"]
    if ctx and ctx[0] is not None:
        lines.append(f"コンテキスト使用量 {ctx[0]}/{ctx[1] or '?'} トークン、要約 {ctx[2] or 0} 回。")
    lines.append("単位: " + ("、".join(f"{u['id']}={u.get('state')}" for u in units) or "なし"))
    return "\n".join(lines)


def _launch(argv: list[str], cwd: str) -> tuple[bool, str]:
    try:
        proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                errors="replace",
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError as exc:
        return False, f"{argv[0]}: {type(exc).__name__}: {exc}"
    try:
        out, err = proc.communicate(timeout=LAUNCH_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        return False, f"{argv[0]} did not return within {LAUNCH_TIMEOUT_SEC} s (killed)"
    tail = ((out or "") + (err or "")).strip()[-400:]
    return proc.returncode == 0, f"exit {proc.returncode}: {tail}"


def _event(rt, session_id: str, decision: str, detail: dict) -> int:
    return rt.execute("INSERT INTO events (ts, session_id, hook, tool, decision, guard_id, detail)"
                      " VALUES (?,?, 'cli', 'relay', ?, 'relay', ?)",
                      (store_mod.now_iso(), session_id, decision,
                       json.dumps(detail, ensure_ascii=False))).lastrowid


def run(store: store_mod.Store, rt, token: str | None, project_dir: str, db_path) -> dict:
    bound = flow.spend_runner(rt, token, "relay", None, store.project_id)
    session_id = bound["session_id"]
    held = lease_mod.state(rt, store.project_id)
    if held is None or held["session_id"] != session_id:
        raise RelayError(f"session {session_id} does not hold the project's window lease "
                         f"({held or 'no valid lease'}): only the holding window relays")
    argv = _launch_argv(store.config)
    problems = conditions(store, rt, session_id)
    if problems:
        raise RelayError("the handover conditions are not met (nothing was done):\n  "
                         + "\n  ".join(problems))
    reason, _ = isolation.flags(rt, store.project_id)
    text = _summary(store, rt, session_id, reason)
    with store.transaction({"cli": "relay", "session_id": session_id}, rt) as tx:
        episode = tx.add("episode", {"session_id": session_id, "kind": "relay", "unit_id": None,
                                     "text": text, "transcript_ref": None})
    ok, why = _launch(argv, project_dir)
    rt.execute("BEGIN IMMEDIATE")
    try:
        if not ok:
            _event(rt, session_id, "deny", {"launch": argv[0], "failed": why,
                                            "episode": episode["id"]})
            rt.execute("COMMIT")
            raise RelayError(f"the next window could not be started ({why}); the lease stays "
                             "with this window and the relay is still due")
        closed = lease_mod.handover(rt, store.project_id, session_id, store_mod.now_iso())
        isolation.clear_relay_due(rt, store.project_id)
        event_id = _event(rt, session_id, "allow", {"launch": argv[0], "launched": why,
                                                    "closed_token": closed, "episode": episode["id"]})
        rt.execute("COMMIT")
    except RelayError:
        raise
    except BaseException:
        rt.execute("ROLLBACK")
        raise
    nid = notify.send(rt, db_path, store.project_id, "relay",
                      f"リレー: 窓口 {session_id} から新しい窓口へ引き継ぎました（{why[:120]}）。",
                      False, project_dir)
    return {"episode": episode["id"], "event_id": event_id, "closed_token": closed,
            "launched": why, "notification": nid}
