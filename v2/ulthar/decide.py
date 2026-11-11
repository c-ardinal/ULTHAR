"""Batch confirmation of decision candidates (DEC-04, TRUST-03, R1-b AC-8/AC-12/AC-13).

`ulthar decide review` prints ONE AskUserQuestion input holding every unconfirmed
candidate; its question carries `DECIDE-REVIEW:<batch_id> sha:<12 hex of the candidate
set>` and the batch is recorded in runtime.db `questions`. The AskUserQuestion
PostToolUse hook reads the answer and issues a one-time decision_confirm token bound
to (project_id, batch_id, sha, question event id, answer event id); only the hook
issues tokens. `ulthar decide confirm --token` consumes it atomically and applies:
  全て承認  every candidate confirmed (binding); what it supersedes -> superseded;
           a global-scope one is also written to the global store (MEM-09);
  全て却下  every candidate rejected;
  一部修正 (or a free-text answer) candidates stay provisional; the correction is
           saved as an owner_msg Episode and sent to extraction.
A candidate set that changed since the question (sha mismatch), a used token, and a
token without its hook-written answer event are refused.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3

from . import extract
from . import store as store_mod

REVIEW_MARK = re.compile(r"DECIDE-REVIEW:([0-9a-f]{6,32}) sha:([0-9a-f]{12})")
APPROVE, PARTIAL, REJECT = "全て承認", "一部修正", "全て却下"
ISSUER = "PostToolUse:AskUserQuestion"
TOKEN_KIND = "decision_confirm"


class ConfirmError(Exception):
    pass


def candidates(conn: sqlite3.Connection | None) -> list[dict]:
    return store_mod.current(conn, "decision", ("provisional",))


def candidates_sha(cands: list[dict]) -> str:
    canon = sorted([c["id"], c["seq"], c.get("text"), c.get("scope"), c.get("tags"),
                    c.get("paths"), c.get("supersedes"), c.get("conflicts_with")] for c in cands)
    return store_mod.sha256(json.dumps(canon, ensure_ascii=False).encode("utf-8")).hexdigest()[:12]


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
    with store.reader() as conn:
        cands = candidates(conn)
        if not cands:
            return None
        blocks = [_candidate_block(conn, i, c) for i, c in enumerate(cands, 1)]
    sha = candidates_sha(cands)
    batch_id = os.urandom(6).hex()
    rt.execute("INSERT INTO questions (qid, project_id, unit_id, status, payload)"
               " VALUES (?, ?, NULL, 'asked', ?)",
               (batch_id, store.project_id, json.dumps(
                   {"kind": "decide_review", "sha": sha,
                    "candidate_ids": [c["id"] for c in cands]})))
    question = (f"DECIDE-REVIEW:{batch_id} sha:{sha}\n\n未確認の決定候補 {len(cands)} 件です。"
                "正規化文・適用範囲・置き換え/矛盾する旧決定を確認してください。\n\n"
                + "\n".join(blocks))
    return {"questions": [{
        "question": question, "header": "決定の確認", "multiSelect": False,
        "options": [
            {"label": APPROVE, "description": "全候補を確定する（置き換え対象の旧決定は superseded）"},
            {"label": PARTIAL, "description": "候補は仮のまま。修正内容を自由記述かメモで書く（抽出に回る）"},
            {"label": REJECT, "description": "全候補を却下する"}]}]}


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


def issue_token(rt: sqlite3.Connection, payload: dict, project_id: str,
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
    row = rt.execute("SELECT payload FROM questions WHERE qid=? AND project_id=?",
                     (batch_id, project_id)).fetchone()
    if row is None or json.loads(row[0]).get("sha") != sha:
        return None, f"unknown batch {batch_id} / sha {sha} for project {project_id}"
    answer, notes = _answer_for(payload.get("tool_response") or {}, text)
    if answer is None:
        return None, "no answer to the review question"
    asked = rt.execute(
        "SELECT id FROM events WHERE session_id=? AND hook='PreToolUse' AND tool='AskUserQuestion'"
        " AND json_extract(detail, '$.tool_use_id')=? ORDER BY id DESC LIMIT 1",
        (str(payload.get("session_id") or "(none)"), payload.get("tool_use_id"))).fetchone()
    if asked is None:
        return None, "no PreToolUse event for this question (tool_use_id)"
    kind, correction = classify(answer, notes)
    token_id = os.urandom(16).hex()
    bound = {"project_id": project_id, "batch_id": batch_id, "sha": sha,
             "question_event_id": asked[0], "answer_event_id": answer_event_id,
             "session_id": payload.get("session_id"), "answer": kind, "answer_text": answer,
             "correction": correction}
    rt.execute("INSERT INTO tokens (token_id, kind, bound, issued_by_hook, consumed_at)"
               " VALUES (?, ?, ?, ?, NULL)",
               (token_id, TOKEN_KIND, json.dumps(bound, ensure_ascii=False), ISSUER))
    rt.execute("UPDATE questions SET status='answered' WHERE qid=?", (batch_id,))
    return token_id, f"decision_confirm token issued ({kind})"


def stop_reminder(store: store_mod.Store, rt: sqlite3.Connection) -> str | None:
    """The block reason for the first Stop that sees this candidate set; else None."""
    with store.reader() as conn:
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


def _approve(store: store_mod.Store, cands: list[dict], token_id: str, writer: dict) -> dict:
    shared = []
    gstore = store_mod.Store.global_store()
    with gstore.reader() as gconn:
        global_ids = {d["id"] for d in store_mod.current(gconn, "decision")}
    global_superseded = []
    with store.transaction(writer) as tx:
        for c in cands:
            done = tx.revise("decision", c["id"], status="confirmed", binding=True,
                             confirm_token_id=token_id)
            if done.get("scope") == "global":
                shared.append(done)
            for old in c.get("supersedes") or []:
                if tx.current_one("decision", old) is not None:
                    tx.revise("decision", old, status="superseded", binding=False)
                elif old in global_ids:
                    global_superseded.append(old)
    if shared or global_superseded:
        with gstore.transaction(writer) as gtx:
            for d in shared:
                gtx.add("decision", {k: v for k, v in d.items()
                                     if k not in ("seq", "writer", "ts", "project_id")}
                        | {"origin_project_id": store.project_id})
            for old in global_superseded:
                gtx.revise("decision", old, status="superseded", binding=False)
    return {"confirmed": len(cands), "global": len(shared)}


def confirm(store: store_mod.Store, rt: sqlite3.Connection, token_id: str,
            project_dir: str) -> dict:
    """Consumes the token and applies the owner's answer; ConfirmError when refused."""
    rt.execute("BEGIN IMMEDIATE")
    try:
        bound = _check_token(rt, token_id, store.project_id)
        with store.reader() as conn:
            cands = candidates(conn)
        if candidates_sha(cands) != bound.get("sha"):
            raise ConfirmError("the candidate set changed since the question (sha mismatch); "
                               "run `ulthar decide review` again")
        writer = {"cli": "decide confirm", "token_id": token_id,
                  "session_id": bound.get("session_id")}
        launch_episode = None
        if bound["answer"] == "approve":
            result = _approve(store, cands, token_id, writer)
        elif bound["answer"] == "reject":
            with store.transaction(writer) as tx:
                for c in cands:
                    tx.revise("decision", c["id"], status="rejected", binding=False,
                              confirm_token_id=token_id)
            result = {"rejected": len(cands)}
        else:
            result = {"kept_provisional": len(cands)}
            text = bound.get("correction") or ""
            if text.strip():
                with store.transaction(writer) as tx:
                    launch_episode = tx.add("episode", {
                        "session_id": bound.get("session_id"), "kind": "owner_msg",
                        "unit_id": None, "text": text, "transcript_ref": None,
                        "source": f"decide-review:{bound.get('batch_id')}"})
                extract.register(rt, store.project_id, bound.get("session_id"),
                                 launch_episode["id"])
                result["correction_episode"] = launch_episode["id"]
        cur = rt.execute("UPDATE tokens SET consumed_at=? WHERE token_id=? AND consumed_at IS NULL",
                         (store_mod.now_iso(), token_id))
        if cur.rowcount != 1:
            raise ConfirmError("token was consumed concurrently")
        rt.execute("COMMIT")
    except BaseException:
        rt.execute("ROLLBACK")
        raise
    if launch_episode is not None:
        extract.launch(rt, launch_episode["id"], project_dir)
    return dict(result, answer=bound["answer"])
