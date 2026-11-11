"""R1-b batch confirmation:
  AC-12 `ulthar decide review` -> one AskUserQuestion input (marker, sha, 3 options);
        the AskUserQuestion PostToolUse hook issues a one-time token; `decide confirm`
        applies 全て承認 / 全て却下 / 一部修正, and refuses a changed candidate set, a
        reused token and a token not issued through the hook;
  AC-13 the main session's Stop is blocked once per unconfirmed candidate set.
"""
from __future__ import annotations

import json
import sys

from helpers import Probe, Sandbox

from ulthar import decide as decide_mod


def add_decisions(sb: Sandbox, *specs: dict) -> list[dict]:
    with sb.inproc(), sb.store().transaction({"hook": "test-setup"}) as tx:
        return [tx.add("decision", {"status": "provisional", "raw_episode_id": "E-setup",
                                    "binding": False, "confirm_token_id": None, **spec})
                for spec in specs]


def current(sb: Sandbox, rec_id: str) -> dict:
    return json.loads(sb.kquery("SELECT json FROM current WHERE kind='decision' AND id=?",
                                (rec_id,))[0][0])


def ask_and_answer(sb: Sandbox, answer: str, tool_use_id: str, notes: str | None = None):
    """review -> PreToolUse/PostToolUse AskUserQuestion through the hook -> token id."""
    review = sb.cli("decide", "review")
    question_input = json.loads(review.stdout)
    q = question_input["questions"][0]["question"]
    base = {"session_id": "dec", "tool_name": "AskUserQuestion", "tool_input": question_input,
            "tool_use_id": tool_use_id}
    pre = sb.hook("PreToolUse", base)
    response = {"questions": question_input["questions"], "answers": {q: answer},
                "annotations": {q: {"notes": notes}} if notes else {}}
    post = sb.hook("PostToolUse", dict(base, tool_response=response))
    row = sb.query("SELECT detail FROM events WHERE hook='PostToolUse' ORDER BY id DESC LIMIT 1")
    token = json.loads(row[0][0]).get("issued_token") if row else None
    return review, q, pre, post, token


def check_review_format(p: Probe) -> None:
    sb = Sandbox()
    try:
        add_decisions(sb, {"text": "見出しは 2 段まで"}, {"text": "API は snake_case", "tags": ["api"]})
        review, q, pre, post, token = ask_and_answer(sb, decide_mod.APPROVE, "tu-fmt")
        body = json.loads(review.stdout)
        opts = [o["label"] for o in body["questions"][0]["options"]]
        p.check("AC-12: review prints one AskUserQuestion input with DECIDE-REVIEW:<batch> sha:<12>",
                review.returncode == 0 and len(body["questions"]) == 1
                and decide_mod.REVIEW_MARK.search(q) is not None
                and "見出しは 2 段まで" in q and "API は snake_case" in q, review.stdout[:400])
        p.check("AC-12: options are exactly 全て承認 / 一部修正 / 全て却下",
                opts == ["全て承認", "一部修正", "全て却下"], str(opts))
        p.check("AC-12: the PostToolUse hook issued a token and told the session",
                pre.returncode == 0 and post.returncode == 0 and bool(token)
                and token in post.stdout, post.stdout[:300] + post.stderr[-300:])
        bound = json.loads(sb.query("SELECT bound FROM tokens WHERE token_id=?", (token,))[0][0])
        mark = decide_mod.REVIEW_MARK.search(q)
        p.check("AC-12: the token is bound to project, batch, sha, question and answer events",
                bound["batch_id"] == mark.group(1) and bound["sha"] == mark.group(2)
                and bound["project_id"] == sb.store().project_id
                and bound["question_event_id"] < bound["answer_event_id"], str(bound))
    finally:
        sb.cleanup()


def check_three_answers(p: Probe) -> None:
    applied = 0
    sb = Sandbox()
    try:
        old = add_decisions(sb, {"text": "余白は 8px 刻み", "status": "confirmed", "binding": True})[0]
        new = add_decisions(sb, {"text": "余白は 4px 刻み", "supersedes": [old["id"]]},
                            {"text": "影は使わない"})
        _, _, _, _, token = ask_and_answer(sb, decide_mod.APPROVE, "tu-a")
        res = sb.cli("decide", "confirm", "--token", token)
        states = [current(sb, d["id"]) for d in new]
        ok = (res.returncode == 0 and all(s["status"] == "confirmed" and s["binding"]
                                          and s["confirm_token_id"] == token for s in states)
              and current(sb, old["id"])["status"] == "superseded")
        applied += ok
        p.check("AC-12: 全て承認 -> confirmed (binding), the replaced decision superseded", ok,
                res.stdout + res.stderr[-300:])
        again = sb.cli("decide", "confirm", "--token", token)
        p.check("AC-12: reusing a consumed token is refused",
                again.returncode != 0 and "already used" in again.stderr, again.stderr[-300:])

        rej = add_decisions(sb, {"text": "全部のボタンを赤に"})
        _, _, _, _, token = ask_and_answer(sb, decide_mod.REJECT, "tu-r")
        res = sb.cli("decide", "confirm", "--token", token)
        ok = res.returncode == 0 and current(sb, rej[0]["id"])["status"] == "rejected"
        applied += ok
        p.check("AC-12: 全て却下 -> rejected", ok, res.stdout + res.stderr[-300:])

        part = add_decisions(sb, {"text": "角丸は 4px"})
        _, _, _, _, token = ask_and_answer(sb, "角丸はカードだけ 8px、他は 4px", "tu-p")
        before = len(sb.episodes("owner_msg"))
        res = sb.cli("decide", "confirm", "--token", token)
        episodes = sb.episodes("owner_msg")
        extraction = sb.query("SELECT status FROM extractions WHERE episode_id=?",
                              (episodes[-1]["id"],)) if episodes else []
        ok = (res.returncode == 0 and current(sb, part[0]["id"])["status"] == "provisional"
              and len(episodes) == before + 1
              and episodes[-1]["text"] == "角丸はカードだけ 8px、他は 4px" and bool(extraction))
        applied += ok
        p.check("AC-12: 一部修正 -> candidates stay provisional, the correction is an owner_msg "
                "Episode sent to extraction", ok, f"{res.stdout} {res.stderr[-300:]} {extraction}")
        p.check(f"AC-12: answers applied {applied}/3", applied == 3)
    finally:
        sb.cleanup()


def check_refusals(p: Probe) -> None:
    sb = Sandbox()
    try:
        add_decisions(sb, {"text": "表は zebra にしない"})
        _, _, _, _, token = ask_and_answer(sb, decide_mod.APPROVE, "tu-s")
        add_decisions(sb, {"text": "後から増えた候補"})
        res = sb.cli("decide", "confirm", "--token", token)
        unused = sb.query("SELECT consumed_at FROM tokens WHERE token_id=?", (token,))
        p.check("AC-12: a candidate set changed after the question (sha mismatch) is refused",
                res.returncode != 0 and "sha mismatch" in res.stderr and unused == [(None,)],
                res.stderr[-300:])
        other = sb.query("SELECT id FROM events WHERE hook='PreToolUse' ORDER BY id LIMIT 1")[0][0]
        forged_bound = {"project_id": sb.store().project_id, "batch_id": "0" * 12, "sha": "0" * 12,
                        "question_event_id": other, "answer_event_id": other, "answer": "approve"}
        sb.query("INSERT INTO tokens VALUES ('forged-token', ?, ?, ?, NULL)",
                 (decide_mod.TOKEN_KIND, json.dumps(forged_bound), decide_mod.ISSUER))
        forged = sb.cli("decide", "confirm", "--token", "forged-token")
        made_up = sb.cli("decide", "confirm", "--token", "never-issued")
        p.check("AC-12: a token written without the hook's answer event (forged) is refused",
                forged.returncode != 0 and "forged" in forged.stderr
                and made_up.returncode != 0, forged.stderr[-300:] + made_up.stderr[-200:])
    finally:
        sb.cleanup()


def check_stop_reminder(p: Probe) -> None:
    sb = Sandbox()
    try:
        empty = sb.hook("Stop", {"session_id": "stop", "stop_hook_active": False})
        p.check("AC-13: 0 candidates -> Stop is not blocked", empty.returncode == 0, empty.stderr)
        add_decisions(sb, {"text": "ログは JSON で出す"})
        sub = sb.hook("SubagentStop", {"session_id": "stop", "agent_id": "a", "agent_type": "t"})
        first = sb.hook("Stop", {"session_id": "stop", "stop_hook_active": False})
        second = sb.hook("Stop", {"session_id": "stop", "stop_hook_active": True})
        p.check("AC-13: first Stop with candidates is blocked, asking for `ulthar decide review`",
                first.returncode == 2 and "ulthar decide review" in first.stderr
                and sub.returncode == 0, first.stderr[-300:])
        p.check("AC-13: the second Stop for the same candidate set is not blocked",
                second.returncode == 0, second.stderr[-300:])
        add_decisions(sb, {"text": "新しい候補"})
        third = sb.hook("Stop", {"session_id": "stop", "stop_hook_active": False})
        p.check("  ... a changed candidate set is reminded about once more", third.returncode == 2)
    finally:
        sb.cleanup()


def main() -> int:
    p = Probe()
    print("\n== AC-12: batch confirmation ==")
    check_review_format(p)
    check_three_answers(p)
    check_refusals(p)
    print("\n== AC-13: Stop reminder ==")
    check_stop_reminder(p)
    return p.result()


if __name__ == "__main__":
    sys.exit(main())
