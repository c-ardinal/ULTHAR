"""Batch confirmation of decision candidates (DEC-04, TRUST-03, R1-b AC-8/AC-12/AC-13).

`ulthar decide review` prints ONE AskUserQuestion input holding every unconfirmed
candidate; its question carries `DECIDE-REVIEW:<batch_id> sha:<12 hex of the candidate
set>`. The batch -- the exact input, the reviewed candidate versions (id, seq) and the
sha -- is recorded in runtime.db `questions`.

Issuing (AskUserQuestion PostToolUse hook): a one-time decision_confirm token is issued
only when the session's own transcript, as ingested by that hook call, holds the
AskUserQuestion tool_use with this tool_use_id and exactly the recorded review input.
A callable hook endpoint is therefore not proof on its own (and Claude's tools may not
call it at all, AC-4 a2). The token is bound to (project, batch, sha, tool_use_id,
question and answer event ids, the answer).

Confirming (`ulthar decide confirm --token`, main session or owner): the transcript is
ingested again and must hold the owner's tool_result for that tool_use_id with the
bound answer. Then, by stages that each check what is already done -- so a retry after
any failure completes the missing stages without reapplying or refusing them:
  全て承認  every reviewed candidate confirmed (binding); what it supersedes ->
           superseded; a global-scope one is also written to the global store;
  全て却下  every reviewed candidate rejected;
  一部修正 (or a free-text answer) candidates stay provisional; the correction is saved
           (once) as an owner_msg Episode that needs extraction, and extracted;
and last the token is consumed. Every revision carries `op_id` = the token id.
Refused: an unknown/forged/used token, an answer not in the transcript, a reviewed
candidate changed by anything but this operation, a new candidate outside the batch.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3

from . import extract
from . import ingest as ingest_mod
from . import memory
from . import store as store_mod

REVIEW_MARK = re.compile(r"DECIDE-REVIEW:([0-9a-f]{6,32}) sha:([0-9a-f]{12})")
APPROVE, PARTIAL, REJECT = "全て承認", "一部修正", "全て却下"
ISSUER = "PostToolUse:AskUserQuestion"
TOKEN_KIND = "decision_confirm"
CONFIRM_HOOK = "decide-confirm"


class ConfirmError(Exception):
    pass


def candidates(conn: sqlite3.Connection | None) -> list[dict]:
    return store_mod.current(conn, "decision", ("provisional",))


def candidates_sha(cands: list[dict]) -> str:
    canon = sorted([c["id"], c["seq"], c.get("text"), c.get("scope"), c.get("tags"),
                    c.get("paths"), c.get("supersedes"), c.get("conflicts_with")] for c in cands)
    return store_mod.sha256(json.dumps(canon, ensure_ascii=False).encode("utf-8")).hexdigest()[:12]


def canonical(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _ref(conn, rec_id: str) -> str:
    old = store_mod.get(conn, "decision", rec_id)
    return f"{rec_id}「{old.get('text') if old else '(不明)'}」"


def _candidate_block(conn, i: int, c: dict) -> str:
    lines = [f"{i}. {c['id']}「{c.get('text')}」",
             f"   範囲: {c.get('scope') or 'project'} / tags: {', '.join(c.get('tags') or []) or '-'}"
             f" / paths: {', '.join(c.get('paths') or []) or '-'}"]
    if c.get("supersedes"):
        lines.append("   置き換える旧決定: " + "、".join(_ref(conn, x) for x in c["supersedes"]))
    if c.get("conflicts_with"):
        lines.append("   矛盾する旧決定: " + "、".join(_ref(conn, x) for x in c["conflicts_with"]))
    return "\n".join(lines)


def review(store: store_mod.Store, rt: sqlite3.Connection) -> dict | None:
    """The AskUserQuestion input for every unconfirmed candidate (None: there are none)."""
    with store.reader(rt) as conn:
        cands = candidates(conn)
        if not cands:
            return None
        blocks = [_candidate_block(conn, i, c) for i, c in enumerate(cands, 1)]
    sha = candidates_sha(cands)
    batch_id = os.urandom(6).hex()
    question = (f"DECIDE-REVIEW:{batch_id} sha:{sha}\n\n未確認の決定候補 {len(cands)} 件です。"
                "正規化文・適用範囲・置き換え/矛盾する旧決定を確認してください。\n\n"
                + "\n".join(blocks))
    payload = {"questions": [{
        "question": question, "header": "決定の確認", "multiSelect": False,
        "options": [
            {"label": APPROVE, "description": "全候補を確定する（置き換え対象の旧決定は superseded）"},
            {"label": PARTIAL, "description": "候補は仮のまま。修正内容を自由記述かメモで書く（抽出に回る）"},
            {"label": REJECT, "description": "全候補を却下する"}]}]}
    rt.execute("INSERT INTO questions (qid, project_id, unit_id, status, payload)"
               " VALUES (?, ?, NULL, 'asked', ?)",
               (batch_id, store.project_id, json.dumps(
                   {"kind": "decide_review", "sha": sha, "input": payload,
                    "candidates": [[c["id"], c["seq"]] for c in cands]}, ensure_ascii=False)))
    return payload


# ---------------------------------------------------------------------------- transcript

def _transcript_episode(conn, session_id: str, kind: str, tool_use_id: str) -> dict | None:
    if conn is None or not tool_use_id:
        return None
    row = conn.execute(
        "SELECT json FROM records WHERE kind='episode' AND session_id=?"
        " AND json_extract(json, '$.kind')=? AND json_extract(json, '$.tool_use_id')=?"
        " ORDER BY seq DESC LIMIT 1", (session_id, kind, tool_use_id)).fetchone()
    return json.loads(row[0]) if row else None


def _asked_in_transcript(conn, session_id: str, tool_use_id: str, review_input: dict) -> bool:
    ask = _transcript_episode(conn, session_id, "ask", tool_use_id)
    if ask is None:
        return False
    try:
        return canonical(json.loads(ask.get("text") or "null")) == canonical(review_input)
    except ValueError:
        return False


def _answered_in_transcript(conn, session_id: str, tool_use_id: str, answer_text: str) -> bool:
    """The tool_result text reads `..."<question>"="<answer>"...` (Claude Code's format)."""
    answer = _transcript_episode(conn, session_id, "answer", tool_use_id)
    return answer is not None and f'="{answer_text}"' in (answer.get("text") or "")


# ---------------------------------------------------------------------------- hook side

def _answer_for(tool_response: dict, question: str) -> tuple[str | None, str]:
    answers = tool_response.get("answers") if isinstance(tool_response, dict) else None
    if not isinstance(answers, dict):
        return None, ""
    answer = answers.get(question)
    if answer is None:
        answer = next((v for k, v in answers.items() if REVIEW_MARK.search(str(k))), None)
    notes = (tool_response.get("annotations") or {}).get(question) if isinstance(
        tool_response.get("annotations"), dict) else None
    note_text = " ".join(str(v) for v in (notes.values() if isinstance(notes, dict) else [notes])
                         if v) if notes else ""
    return (str(answer) if answer is not None else None), note_text


def classify(answer: str, notes: str) -> tuple[str, str]:
    if answer == APPROVE:
        return "approve", ""
    if answer == REJECT:
        return "reject", ""
    correction = "\n".join(t for t in ((answer if answer != PARTIAL else ""), notes) if t.strip())
    return "partial", correction


def _batch(rt: sqlite3.Connection, batch_id: str, project_id: str) -> dict | None:
    row = rt.execute("SELECT payload FROM questions WHERE qid=? AND project_id=?",
                     (batch_id, project_id)).fetchone()
    return json.loads(row[0]) if row else None


def issue_token(rt: sqlite3.Connection, payload: dict, store: store_mod.Store,
                answer_event_id: int) -> tuple[str | None, str]:
    """Called inside the transaction that stored this PostToolUse's events row.
    Returns (token_id or None, note for the event detail)."""
    tool_input = payload.get("tool_input") or {}
    for q in tool_input.get("questions") or []:
        text = str((q or {}).get("question") or "")
        mark = REVIEW_MARK.search(text)
        if mark:
            break
    else:
        return None, "not a decide review"
    batch_id, sha = mark.groups()
    batch = _batch(rt, batch_id, store.project_id)
    if batch is None or batch.get("sha") != sha:
        return None, f"unknown batch {batch_id} / sha {sha} for project {store.project_id}"
    if canonical(tool_input) != canonical(batch.get("input")):
        return None, "the question differs from the recorded review input"
    session_id = str(payload.get("session_id") or "(none)")
    tool_use_id = payload.get("tool_use_id")
    with store.reader(rt) as conn:
        if not _asked_in_transcript(conn, session_id, tool_use_id, batch["input"]):
            return None, ("no AskUserQuestion tool_use with this tool_use_id and review input "
                          "in the session transcript: not issued")
    answer, notes = _answer_for(payload.get("tool_response") or {}, text)
    if answer is None:
        return None, "no answer to the review question"
    asked = rt.execute(
        "SELECT id FROM events WHERE session_id=? AND hook='PreToolUse' AND tool='AskUserQuestion'"
        " AND json_extract(detail, '$.tool_use_id')=? ORDER BY id DESC LIMIT 1",
        (session_id, tool_use_id)).fetchone()
    if asked is None:
        return None, "no PreToolUse event for this question (tool_use_id)"
    kind, correction = classify(answer, notes)
    token_id = os.urandom(16).hex()
    bound = {"project_id": store.project_id, "batch_id": batch_id, "sha": sha,
             "tool_use_id": tool_use_id, "question_event_id": asked[0],
             "answer_event_id": answer_event_id, "session_id": payload.get("session_id"),
             "answer": kind, "answer_text": answer, "correction": correction}
    rt.execute("INSERT INTO tokens (token_id, kind, bound, issued_by_hook, consumed_at)"
               " VALUES (?, ?, ?, ?, NULL)",
               (token_id, TOKEN_KIND, json.dumps(bound, ensure_ascii=False), ISSUER))
    rt.execute("UPDATE questions SET status='answered' WHERE qid=?", (batch_id,))
    return token_id, f"decision_confirm token issued ({kind})"


def stop_reminder(store: store_mod.Store, rt: sqlite3.Connection) -> str | None:
    """The block reason for the first Stop that sees this candidate set; else None."""
    with store.reader(rt) as conn:
        cands = candidates(conn)
    if not cands:
        return None
    sha = candidates_sha(cands)
    cur = rt.execute("INSERT OR IGNORE INTO reminders VALUES (?, ?, ?)",
                     (store.project_id, sha, store_mod.now_iso()))
    if cur.rowcount == 0:
        return None
    return (f"[ULTHAR] 未確認の決定候補が {len(cands)} 件あります（候補集合 sha:{sha}）。"
            "終了する前に `ulthar decide review` を実行し、その出力をそのまま AskUserQuestion の"
            "入力にしてオーナーにまとめて確認してください（この催促は同じ候補集合について 1 回だけです）。")


# ---------------------------------------------------------------------------- CLI side

def _check_token(rt: sqlite3.Connection, token_id: str, project_id: str) -> dict:
    row = rt.execute("SELECT kind, bound, issued_by_hook, consumed_at FROM tokens WHERE token_id=?",
                     (token_id,)).fetchone()
    if row is None:
        raise ConfirmError("unknown token")
    kind, bound_raw, issuer, consumed = row
    if kind != TOKEN_KIND or issuer != ISSUER:
        raise ConfirmError(f"not a hook-issued {TOKEN_KIND} token")
    if consumed:
        raise ConfirmError(f"token already used at {consumed}")
    bound = json.loads(bound_raw or "{}")
    if bound.get("project_id") != project_id:
        raise ConfirmError(f"token is for project {bound.get('project_id')}, not {project_id}")
    answer = rt.execute("SELECT hook, tool, detail FROM events WHERE id=?",
                        (bound.get("answer_event_id"),)).fetchone()
    if (answer is None or answer[:2] != ("PostToolUse", "AskUserQuestion")
            or json.loads(answer[2] or "{}").get("issued_token") != token_id):
        raise ConfirmError("token has no matching hook-recorded answer event (forged?)")
    asked = rt.execute("SELECT hook, tool FROM events WHERE id=?",
                       (bound.get("question_event_id"),)).fetchone()
    if asked != ("PreToolUse", "AskUserQuestion"):
        raise ConfirmError("token has no matching question event")
    return bound


def _ingest_session(store: store_mod.Store, rt: sqlite3.Connection, session_id: str) -> None:
    """Brings the owner's answer in: the session transcript the hooks recorded."""
    row = rt.execute("SELECT transcript_path FROM sessions WHERE session_id=?",
                     (session_id,)).fetchone()
    if row and row[0]:
        ingest_mod.ingest(rt, session_id, row[0], hook=CONFIRM_HOOK, store=store)


def _plan(store: store_mod.Store, rt: sqlite3.Connection, bound: dict, token_id: str) -> dict:
    """The reviewed candidates, split into those this operation still has to apply and
    those it already applied; ConfirmError for anything else."""
    batch = _batch(rt, bound["batch_id"], store.project_id) or {}
    reviewed = [tuple(x) for x in batch.get("candidates") or []]
    if not reviewed:
        raise ConfirmError(f"batch {bound['batch_id']} is unknown")
    session_id = str(bound.get("session_id") or "(none)")
    with store.reader(rt) as conn:
        if not _answered_in_transcript(conn, session_id, bound.get("tool_use_id"),
                                       bound.get("answer_text") or ""):
            raise ConfirmError("the owner's answer to this question is not in the session "
                               "transcript")
        versions = []
        for rec_id, seq in reviewed:
            row = conn.execute("SELECT json FROM records WHERE kind='decision' AND id=? AND seq=?",
                               (rec_id, seq)).fetchone()
            if row is None:
                raise ConfirmError(f"reviewed candidate {rec_id}@{seq} is not in the store")
            versions.append(json.loads(row[0]))
        if candidates_sha(versions) != bound.get("sha"):
            raise ConfirmError("the reviewed candidates do not match the question (sha mismatch)")
        todo, done = [], []
        for v in versions:
            cur = store_mod.get(conn, "decision", v["id"])
            if cur["seq"] == v["seq"]:
                todo.append(cur)
            elif cur.get("op_id") == token_id:
                done.append(cur)
            else:
                raise ConfirmError(f"candidate {v['id']} changed since the question "
                                   "(sha mismatch); run `ulthar decide review` again")
        batch_ids = {rec_id for rec_id, _ in reviewed}
        extra = [c["id"] for c in candidates(conn) if c["id"] not in batch_ids]
    if extra and bound["answer"] != "partial":
        raise ConfirmError(f"the candidate set changed since the question (sha mismatch: new "
                           f"{extra}); run `ulthar decide review` again")
    return {"todo": todo, "done": done, "all": todo + done}


def _revise_if_needed(tx, rec_id: str, token_id: str, **changes) -> None:
    cur = tx.current_one("decision", rec_id)
    if cur is not None and cur.get("op_id") != token_id:
        tx.revise("decision", rec_id, op_id=token_id, **changes)


def _approve(store: store_mod.Store, rt, plan: dict, token_id: str, writer: dict) -> dict:
    with store.transaction(writer, rt) as tx:
        for c in plan["todo"]:
            tx.revise("decision", c["id"], status="confirmed", binding=True,
                      confirm_token_id=token_id, op_id=token_id)
        for c in plan["all"]:
            for old in c.get("supersedes") or []:
                cur = tx.current_one("decision", old)
                if cur is not None and cur.get("status") in memory.ACTIVE:
                    _revise_if_needed(tx, old, token_id, status="superseded", binding=False)
        confirmed = {c["id"]: tx.current_one("decision", c["id"]) for c in plan["all"]}
    shared = [d for d in confirmed.values() if d.get("scope") == "global"]
    olds = {old for c in plan["all"] for old in c.get("supersedes") or []}
    gstore = _global_store()
    if shared or olds:
        with gstore.transaction(writer, rt) as gtx:
            for d in shared:
                if (gtx.current_one("decision", d["id"]) or {}).get("op_id") != token_id:
                    gtx.add("decision", {k: v for k, v in d.items()
                                         if k not in ("seq", "writer", "ts", "project_id")}
                            | {"origin_project_id": store.project_id, "op_id": token_id})
            for old in olds:
                cur = gtx.current_one("decision", old)
                if cur is not None and cur.get("status") in memory.ACTIVE:
                    _revise_if_needed(gtx, old, token_id, status="superseded", binding=False)
    return {"confirmed": len(plan["all"]), "global": len(shared)}


def _global_store() -> store_mod.Store:
    return store_mod.Store.global_store()


def _partial(store: store_mod.Store, rt, bound: dict, token_id: str, writer: dict) -> str | None:
    """Saves the correction once (dedupe key confirm:<token>) and registers its extraction."""
    text = bound.get("correction") or ""
    if not text.strip():
        return None
    session_id = bound.get("session_id")
    with store.transaction(writer, rt) as tx:
        ep = tx.find_dedupe("episode", session_id, f"confirm:{token_id}") or tx.add(
            "episode", {"session_id": session_id, "kind": "owner_msg", "unit_id": None,
                        "text": text, "transcript_ref": None, "op_id": token_id,
                        "status": memory.NEEDS_EXTRACTION,
                        "source": f"decide-review:{bound.get('batch_id')}"},
            dedupe_key=f"confirm:{token_id}")
    extract.register(rt, store.project_id, session_id, ep["id"])
    return ep["id"]


def confirm(store: store_mod.Store, rt: sqlite3.Connection, token_id: str,
            project_dir: str) -> dict:
    """Applies the owner's answer, completing whatever an earlier attempt left undone,
    then consumes the token. ConfirmError when refused."""
    bound = _check_token(rt, token_id, store.project_id)
    _ingest_session(store, rt, str(bound.get("session_id") or "(none)"))
    plan = _plan(store, rt, bound, token_id)
    writer = {"cli": "decide confirm", "token_id": token_id,
              "session_id": bound.get("session_id")}
    correction = None
    if bound["answer"] == "approve":
        result = _approve(store, rt, plan, token_id, writer)
    elif bound["answer"] == "reject":
        with store.transaction(writer, rt) as tx:
            for c in plan["todo"]:
                tx.revise("decision", c["id"], status="rejected", binding=False,
                          confirm_token_id=token_id, op_id=token_id)
        result = {"rejected": len(plan["all"])}
    else:
        correction = _partial(store, rt, bound, token_id, writer)
        result = {"kept_provisional": len(plan["all"])}
        if correction:
            result["correction_episode"] = correction
    cur = rt.execute("UPDATE tokens SET consumed_at=? WHERE token_id=? AND consumed_at IS NULL",
                     (store_mod.now_iso(), token_id))
    if cur.rowcount != 1:
        raise ConfirmError("token was consumed concurrently")
    if correction is not None:
        row = rt.execute("SELECT status, detail FROM extractions WHERE episode_id=?",
                         (correction,)).fetchone()
        if row and row[0] == "pending" and row[1] is None:
            extract.launch(rt, correction, project_dir)
    return dict(result, answer=bound["answer"])
