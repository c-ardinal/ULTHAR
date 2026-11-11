"""R1-c hook-issued tokens (TRUST-03, GOAL-06, QG-04; R1-c AC-4/5/7/10). Hook side and
the CLI's checks; no YAML here.

Runner token (AC-4, AC-7): the PreToolUse of a Bash / PowerShell call that is exactly
one `ulthar review record ...` or `ulthar verify ...` from a subagent issues a one-time
token bound to (session_id, agent_id, tool_use_id, project, command, unit) and appends
`--runner-token <id>` to the command through `updatedInput`. The CLI consumes it and
writes that agent_id as the runner. A main-session call gets no token (the CLI then
records nothing); a command that already names `--runner-token`, or that wraps the
call in anything else (pipes, redirections, a second command), is blocked.

Owner tokens (AC-5, AC-10): the CLI records the exact AskUserQuestion input in
runtime.db `questions` (`ulthar contract show`, `ulthar unit show --ask qual|resume`):
  CONTRACT-CONFIRM:<unit> v<n> sha:<12>   options 確定 / 修正        -> contract_confirm
  QUAL-JUDGE:<unit> sha:<12>               one question per qualitative AC, 合格 / 不合格
                                           -> one qual_judgement token per AC
  UNIT-RESUME:<unit> sha:<12>              options 再開 / 再開しない  -> unit_resume
The AskUserQuestion PostToolUse issues them as R1-b's decide review does (ulthar.decide):
only for exactly the recorded input, only when the session's transcript holds that
tool_use, and only for the issuing answer. Consuming one (`ulthar unit advance`)
requires the transcript's tool_result to map exactly every bound question to its bound
answer -- so a PostToolUse whose claimed answer differs from the owner's is refused
there, and nothing is written.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3

from . import decide
from . import shellwords
from . import store as store_mod

RUNNER_KIND = "runner"
RUNNER_FLAG = "--runner-token"
RUNNER_GUARD = "unit_runner"
CONTRACT_MARK = re.compile(r"CONTRACT-CONFIRM:(U-[0-9a-f]+) v(\d+) sha:([0-9a-f]{12})")
QUAL_MARK = re.compile(r"QUAL-JUDGE:(U-[0-9a-f]+) sha:([0-9a-f]{12})")
RESUME_MARK = re.compile(r"UNIT-RESUME:(U-[0-9a-f]+) sha:([0-9a-f]{12})")
CONFIRM, REVISE = "確定", "修正"
PASS, FAIL = "合格", "不合格"
RESUME, NO_RESUME = "再開", "再開しない"
OWNER_KINDS = {"contract_confirm": CONTRACT_MARK, "qual_judgement": QUAL_MARK,
               "unit_resume": RESUME_MARK}


class TokenError(Exception):
    pass


# ---------------------------------------------------------------------------- runner (hook)

def runner_command(args: list[str]) -> str | None:
    words = [a.casefold() for a in args[:2]]
    if words[:1] == ["verify"]:
        return "verify"
    if words == ["review", "record"]:
        return "review record"
    return None


def _unit_arg(command: str, args: list[str]) -> str | None:
    if command == "verify":
        return next((a for a in args[1:] if not a.startswith("-")), None)
    for i, a in enumerate(args):
        if a == "--unit" and i + 1 < len(args):
            return args[i + 1]
        if a.startswith("--unit="):
            return a.split("=", 1)[1]
    return None


def runner_request(payload: dict) -> tuple[str | None, dict | None]:
    """(block message, request) for a shell call; request = {command, unit}."""
    if payload.get("tool_name") not in ("Bash", "PowerShell"):
        return None, None
    tool = payload["tool_name"]
    command = str((payload.get("tool_input") or {}).get("command") or "")
    calls = [(c, args) for args in shellwords.ulthar_calls(command, tool)
             if (c := runner_command(args))]
    if not calls:
        return None, None
    if RUNNER_FLAG in command.casefold():
        return (f"BLOCKED (ulthar runner token): `{RUNNER_FLAG}` is added by the hook only "
                "(R1-c AC-4/AC-7); run the command without it."), None
    for toks in shellwords.variants(command, tool):
        segs = shellwords.segments(toks)
        if len(segs) != 1 or not shellwords.leading_ulthar(segs[0]):
            continue
        words = list(segs[0])
        while words and re.match(r"^\w+=", words[0]):
            words = words[1:]
        args = shellwords.call_args(words, 0) or []
        name = runner_command(args)
        if name:
            return None, {"command": name, "unit": _unit_arg(name, args)}
    return ("BLOCKED (ulthar runner token): run `ulthar review record ...` / `ulthar verify "
            "...` as a command of its own (no pipe, redirection or second command): the "
            "hook appends the runner token to it."), None


def issue_runner(rt: sqlite3.Connection, token_id: str, payload: dict, project_id: str,
                 request: dict, event_id: int) -> None:
    bound = {"project_id": project_id, "session_id": payload.get("session_id"),
             "agent_id": payload.get("agent_id"), "tool_use_id": payload.get("tool_use_id"),
             "command": request["command"], "unit": request["unit"], "event_id": event_id}
    rt.execute("INSERT INTO tokens (token_id, kind, bound, issued_by_hook, consumed_at)"
               " VALUES (?, ?, ?, ?, NULL)",
               (token_id, RUNNER_KIND, json.dumps(bound, ensure_ascii=False),
                f"PreToolUse:{payload.get('tool_name')}"))


def updated_command(payload: dict, token_id: str) -> dict:
    tool_input = dict(payload.get("tool_input") or {})
    tool_input["command"] = f"{str(tool_input.get('command') or '').rstrip()} {RUNNER_FLAG} {token_id}"
    return tool_input


# ---------------------------------------------------------------------------- runner (CLI)

def consume_runner(rt: sqlite3.Connection, token_id: str | None, command: str, unit_id: str,
                   project_id: str) -> dict:
    """Checks and consumes a runner token inside the caller's runtime.db write
    transaction; returns its binding (with the runner's agent_id). TokenError when
    refused."""
    if not token_id:
        raise TokenError("no runner token: this command records only when a subagent runs it "
                         "(the hook adds the token); a main-session or hand-made call records "
                         "nothing")
    row = rt.execute("SELECT kind, bound, issued_by_hook, consumed_at FROM tokens"
                     " WHERE token_id=?", (token_id,)).fetchone()
    if row is None or row[0] != RUNNER_KIND or not str(row[2]).startswith("PreToolUse:"):
        raise TokenError("unknown runner token")
    if row[3]:
        raise TokenError(f"runner token already used at {row[3]}")
    bound = json.loads(row[1] or "{}")
    if (bound.get("project_id"), bound.get("command"), bound.get("unit")) != (
            project_id, command, unit_id):
        raise TokenError(f"runner token is for {bound.get('command')} {bound.get('unit')} "
                         f"in {bound.get('project_id')}, not {command} {unit_id}")
    if not bound.get("agent_id"):
        raise TokenError("runner token has no agent_id (a main-session call records nothing)")
    event = rt.execute("SELECT hook, agent_id, detail FROM events WHERE id=?",
                       (bound.get("event_id"),)).fetchone()
    if (event is None or event[0] != "PreToolUse" or event[1] != bound["agent_id"]
            or json.loads(event[2] or "{}").get("issued_runner_token") != token_id):
        raise TokenError("runner token has no matching hook event (forged?)")
    rt.execute("UPDATE tokens SET consumed_at=? WHERE token_id=?", (store_mod.now_iso(), token_id))
    return bound


# ---------------------------------------------------------------------------- owner (CLI asks)

def record_question(rt: sqlite3.Connection, project_id: str, unit_id: str, kind: str,
                    tool_input: dict, **extra) -> str:
    qid = os.urandom(6).hex()
    rt.execute("INSERT INTO questions (qid, project_id, unit_id, status, payload)"
               " VALUES (?, ?, ?, 'asked', ?)",
               (qid, project_id, unit_id, json.dumps(
                   {"kind": kind, "input": tool_input, **extra}, ensure_ascii=False)))
    return qid


def ask_input(questions: list[tuple[str, str, list[tuple[str, str]]]]) -> dict:
    """[(question, header, [(label, description)])] -> an AskUserQuestion input."""
    return {"questions": [{"question": q, "header": h, "multiSelect": False,
                           "options": [{"label": lab, "description": d} for lab, d in opts]}
                          for q, h, opts in questions]}


# ---------------------------------------------------------------------------- owner (hook)

def _marked(tool_input: dict) -> tuple[str, re.Match] | None:
    for q in tool_input.get("questions") or []:
        text = str((q or {}).get("question") or "")
        for kind, mark in OWNER_KINDS.items():
            m = mark.search(text)
            if m:
                return kind, m
    return None


def _unit(conn, unit_id: str) -> dict:
    return store_mod.get(conn, "unit", unit_id) or {}


def _still_open(kind: str, unit: dict, q: dict) -> str | None:
    """Why the recorded question no longer applies to the unit (None: it does)."""
    if kind == "contract_confirm":
        cur = unit.get("contract") or {}
        if (cur.get("sha"), cur.get("version")) != (q.get("sha"), q.get("version")):
            return "the unit's current contract version or sha differs (sha mismatch)"
        if unit.get("state") not in ("contract_draft", "halted"):
            return f"the unit is {unit.get('state')}, not awaiting a contract confirmation"
    elif kind == "qual_judgement":
        if unit.get("state") != "owner_review" or (unit.get("confirmed") or {}).get("sha") != q.get("sha"):
            return "the unit is not in owner_review with this contract"
    elif unit.get("state") != "halted" or unit.get("halt_id") != q.get("halt_id"):
        return "the unit is not halted (or halted again since the question)"
    return None


def _verdicts(kind: str, answers: list[str], q: dict) -> list[tuple[dict, str]]:
    """(extra binding, verdict) for each token to issue."""
    if kind == "contract_confirm":
        return [({"version": q.get("version")}, "confirm")] if answers[0] == CONFIRM else []
    if kind == "unit_resume":
        return [({"halt_id": q.get("halt_id")}, "resume")] if answers[0] == RESUME else []
    verdict = {PASS: "pass", FAIL: "fail"}
    return [({"ac_id": ac}, verdict[a]) for ac, a in zip(q.get("acs") or [], answers)
            if a in verdict]


def issue_owner_tokens(rt: sqlite3.Connection, payload: dict, store: store_mod.Store,
                       answer_event_id: int) -> tuple[list[str], str]:
    """Called inside the transaction that stored this PostToolUse's events row."""
    tool_input = payload.get("tool_input") or {}
    found = _marked(tool_input)
    if found is None:
        return [], "not a unit question"
    kind, mark = found
    unit_id = mark.group(1)
    rows = rt.execute("SELECT qid, payload FROM questions WHERE project_id=? AND unit_id=?"
                      " AND status='asked'", (store.project_id, unit_id)).fetchall()
    match = [(qid, json.loads(p)) for qid, p in rows
             if decide.canonical(json.loads(p).get("input")) == decide.canonical(tool_input)
             and json.loads(p).get("kind") == kind]
    if not match:
        return [], f"no recorded {kind} question with exactly this input for {unit_id}"
    qid, q = match[-1]
    session_id = str(payload.get("session_id") or "(none)")
    tool_use_id = payload.get("tool_use_id")
    with store.reader(rt) as conn:
        why = _still_open(kind, _unit(conn, unit_id), q)
        if why:
            return [], f"not issued: {why}"
        if not decide._asked_in_transcript(conn, session_id, tool_use_id, q["input"]):
            return [], ("no AskUserQuestion tool_use with this tool_use_id and input in the "
                        "session transcript: not issued")
    questions = [str(x.get("question")) for x in q["input"]["questions"]]
    claimed = (payload.get("tool_response") or {}).get("answers")
    if not isinstance(claimed, dict) or not all(isinstance(claimed.get(t), str) for t in questions):
        return [], "not every question has an answer: not issued"
    answers = [claimed[t] for t in questions]
    asked = rt.execute(
        "SELECT id FROM events WHERE session_id=? AND hook='PreToolUse' AND tool='AskUserQuestion'"
        " AND json_extract(detail, '$.tool_use_id')=? ORDER BY id DESC LIMIT 1",
        (session_id, tool_use_id)).fetchone()
    if asked is None:
        return [], "no PreToolUse event for this question (tool_use_id)"
    issued = []
    for extra, verdict in _verdicts(kind, answers, q):
        token_id = os.urandom(16).hex()
        bound = {"project_id": store.project_id, "unit_id": unit_id, "contract_sha": q.get("sha"),
                 "qid": qid, "tool_use_id": tool_use_id, "session_id": payload.get("session_id"),
                 "question_event_id": asked[0], "answer_event_id": answer_event_id,
                 "questions": questions, "answers": answers, "verdict": verdict, **extra}
        rt.execute("INSERT INTO tokens (token_id, kind, bound, issued_by_hook, consumed_at)"
                   " VALUES (?, ?, ?, ?, NULL)",
                   (token_id, kind, json.dumps(bound, ensure_ascii=False), decide.ISSUER))
        issued.append(token_id)
    rt.execute("UPDATE questions SET status='answered' WHERE qid=?", (qid,))
    return issued, (f"{kind}: {len(issued)} token(s) issued" if issued
                     else f"{kind}: the answer issues no token ({', '.join(answers)})")


# ---------------------------------------------------------------------------- owner (CLI checks)

def expected_results(questions: list[str], answers: list[str]) -> list[str]:
    """Claude Code's AskUserQuestion tool_result text for these pairs (several questions
    are joined with ", ", as the real transcripts show)."""
    pairs = ", ".join(f'"{q}"="{a}"' for q, a in zip(questions, answers))
    return [f"{decide.RESULT_PREFIX}{pairs}{suffix}" for suffix in decide.RESULT_SUFFIXES]


def owner_sessions(rt: sqlite3.Connection, unit_id: str) -> set[str]:
    """Sessions of this unit's unconsumed owner tokens (to ingest before checking them)."""
    out = set()
    for (raw,) in rt.execute("SELECT bound FROM tokens WHERE consumed_at IS NULL AND kind IN"
                             " ('contract_confirm','qual_judgement','unit_resume')"):
        bound = json.loads(raw or "{}")
        if bound.get("unit_id") == unit_id and bound.get("session_id"):
            out.add(bound["session_id"])
    return out


def valid_owner_tokens(rt: sqlite3.Connection, conn, project_id: str, unit_id: str, kind: str,
                       spent: set[str]) -> tuple[list[tuple[str, dict]], list[str]]:
    """(valid tokens newest first, why others were refused) of `kind` for the unit:
    hook-issued, unconsumed, not referenced by a unit transition (`spent`), with their
    answer and question events, and the transcript's tool_result mapping exactly the
    bound questions to the bound answers."""
    good, bad = [], []
    for token_id, raw, issuer in rt.execute(
            "SELECT token_id, bound, issued_by_hook FROM tokens WHERE kind=? AND"
            " consumed_at IS NULL ORDER BY rowid DESC", (kind,)).fetchall():
        bound = json.loads(raw or "{}")
        if bound.get("unit_id") != unit_id or bound.get("project_id") != project_id:
            continue
        why = _owner_problem(rt, conn, token_id, issuer, bound, spent)
        if why:
            bad.append(f"{kind} {token_id[:8]}: {why}")
        else:
            good.append((token_id, bound))
    return good, bad


def _owner_problem(rt, conn, token_id: str, issuer: str, bound: dict, spent: set[str]) -> str | None:
    if issuer != decide.ISSUER:
        return "not issued by the AskUserQuestion hook"
    if token_id in spent:
        return "already used by a unit transition"
    answer = rt.execute("SELECT hook, tool, detail FROM events WHERE id=?",
                        (bound.get("answer_event_id"),)).fetchone()
    if (answer is None or answer[:2] != ("PostToolUse", "AskUserQuestion")
            or token_id not in (json.loads(answer[2] or "{}").get("issued_tokens") or [])):
        return "no matching hook-recorded answer event (forged?)"
    asked = rt.execute("SELECT hook, tool FROM events WHERE id=?",
                       (bound.get("question_event_id"),)).fetchone()
    if asked != ("PreToolUse", "AskUserQuestion"):
        return "no matching question event"
    result = decide._transcript_episode(conn, str(bound.get("session_id") or "(none)"), "answer",
                                        bound.get("tool_use_id"))
    if result is None or (result.get("text") or "") not in expected_results(
            bound.get("questions") or [], bound.get("answers") or []):
        return "the owner's answer is not in the session transcript exactly as bound"
    return None
