"""R1-b decision extraction with a fake extractor command (never the real `claude`):
  AC-6  UserPromptSubmit saves the owner_msg Episode, records the extraction pending,
        launches it detached and returns without waiting; success writes provisional
        decisions with raw_episode_id; invalid JSON and a crash are recorded failed
        with no decision written; the extractor's own hook calls launch nothing;
  AC-8  a candidate equal to an active decision is not written; a conflicting one is
        listed by `ulthar decide review` with the old decision's id.
"""
from __future__ import annotations

import json
import sys
import time

from helpers import Probe, Sandbox

WAIT_SEC = 60
SLOW_SEC = 6


def wait_extractions(sb: Sandbox, n: int = 1) -> list[tuple]:
    deadline = time.monotonic() + WAIT_SEC
    while True:
        rows = sb.query("SELECT episode_id, status, detail FROM extractions ORDER BY started")
        if (len(rows) >= n and all(r[1] != "pending" for r in rows)) or time.monotonic() > deadline:
            return rows
        time.sleep(0.2)


def canned(sb: Sandbox, items: list[dict], name: str = "answer.json") -> str:
    path = sb.root / name
    path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    return str(path)


def prompt(sb: Sandbox, text: str, pid: str, session: str = "ex", **env) -> object:
    return sb.hook("UserPromptSubmit", {"session_id": session, "prompt": text, "prompt_id": pid},
                   **env)


def decisions(sb: Sandbox) -> list[dict]:
    rows = sb.kquery("SELECT json FROM current WHERE kind='decision' ORDER BY seq")
    return [json.loads(r[0]) for r in rows]


def check_success(p: Probe) -> None:
    sb = Sandbox()
    try:
        answer = canned(sb, [{"kind": "decision", "text": "ボタンの角丸は 4px に統一する",
                              "scope": "project", "tags": ["design"], "paths": ["src/ui/**"],
                              "supersedes": [], "conflicts_with": []}])
        sb.configure(["sleep", str(SLOW_SEC), answer])
        log = sb.root / "prompts.jsonl"
        sb.cli("db", "init")  # creating runtime.db (fsync-heavy, once) is not under test here
        started = time.perf_counter()
        proc = prompt(sb, "ボタンの角丸がバラバラ。4px に揃えて", "p-1", FAKE_EXTRACTOR_LOG=str(log))
        took = time.perf_counter() - started
        status_now = sb.query("SELECT status FROM extractions")
        ep = sb.episodes("owner_msg")
        p.check(f"AC-6: the hook returned in {took:.2f} s, not waiting for the {SLOW_SEC} s extractor",
                proc.returncode == 0 and took < SLOW_SEC / 2 and status_now == [("pending",)],
                f"rc={proc.returncode} {status_now} {proc.stderr[-300:]}")
        p.check("AC-6: the owner_msg Episode was written first (from the prompt payload)",
                len(ep) == 1 and ep[0]["text"] == "ボタンの角丸がバラバラ。4px に揃えて")
        rows = wait_extractions(sb)
        decs = decisions(sb)
        p.check("AC-6: success -> extraction done and 1 provisional decision with raw_episode_id",
                [r[1] for r in rows] == ["done"] and len(decs) == 1
                and decs[0]["status"] == "provisional" and decs[0]["raw_episode_id"] == ep[0]["id"]
                and decs[0]["paths"] == ["src/ui/**"], f"{rows} {decs}")
        sent = [json.loads(ln) for ln in log.read_text(encoding="utf-8").splitlines()]
        p.check("AC-6: the extractor got the raw message, the active decision list, and "
                "ULTHAR_EXTRACTOR=1", len(sent) == 1 and "ボタンの角丸がバラバラ" in sent[0]["prompt"]
                and "Existing active decisions" in sent[0]["prompt"]
                and sent[0]["ULTHAR_EXTRACTOR"] == "1")
        again = prompt(sb, "ボタンの角丸がバラバラ。4px に揃えて", "p-1")
        p.check("the same prompt (same prompt_id) is not saved or extracted twice",
                again.returncode == 0 and len(sb.episodes("owner_msg")) == 1
                and len(sb.query("SELECT * FROM extractions")) == 1)
    finally:
        sb.cleanup()


def check_failures(p: Probe) -> None:
    for mode in ("badjson", "fail"):
        sb = Sandbox()
        try:
            sb.configure([mode])
            proc = prompt(sb, "ログは英語で書いて", f"{mode}-1")
            rows = wait_extractions(sb)
            p.check(f"AC-6: extractor {mode} -> failed recorded, 0 decisions written",
                    proc.returncode == 0 and [r[1] for r in rows] == ["failed"]
                    and not decisions(sb), f"{rows}")
        finally:
            sb.cleanup()


def check_no_recursion(p: Probe) -> None:
    sb = Sandbox()
    try:
        nested = sb.root / "nested.json"
        nested.write_text(json.dumps({
            "hook_event_name": "UserPromptSubmit", "session_id": "extractor-session",
            "cwd": str(sb.work), "prompt": "the extractor's own prompt", "prompt_id": "nested-1"}),
            encoding="utf-8")
        sb.configure(["recurse", str(nested)])
        prompt(sb, "テストは必ず書くこと", "rec-1")
        rows = wait_extractions(sb)
        nested_events = sb.query("SELECT COUNT(*) FROM events WHERE session_id='extractor-session'")
        p.check("AC-6: the extractor's own hook call ran (it is recorded as an event)",
                nested_events == [(1,)], str(nested_events))
        p.check("AC-6: ... and launched 0 extractions and stored no Episode (recursion 0)",
                len(rows) == 1 and rows[0][1] == "done"
                and not sb.episodes(session="extractor-session"), str(rows))
    finally:
        sb.cleanup()


def check_duplicates_and_conflicts(p: Probe) -> None:
    sb = Sandbox()
    try:
        first = canned(sb, [{"kind": "decision", "text": "色はトークンだけを使う"}], "a1.json")
        sb.configure(["file", first])
        prompt(sb, "色は必ずトークンから使って", "d-1")
        wait_extractions(sb, 1)
        old = decisions(sb)[0]
        second = canned(sb, [
            {"kind": "decision", "text": "色は  トークンだけを使う"},
            {"kind": "decision", "text": "ブランド色だけは直書きしてよい",
             "conflicts_with": [old["id"]]}], "a2.json")
        sb.configure(["file", second])
        prompt(sb, "ブランド色は直書きでいい。色はトークンだけ", "d-2")
        wait_extractions(sb, 2)
        decs = decisions(sb)
        p.check("AC-8: a candidate equal to an active decision is not appended (duplicates 0)",
                len(decs) == 2 and sum(d["text"].replace(" ", "") == "色はトークンだけを使う"
                                       for d in decs) == 1, str([d["text"] for d in decs]))
        review = sb.cli("decide", "review")
        q = json.loads(review.stdout)["questions"][0]["question"] if review.returncode == 0 else ""
        p.check("AC-8: the conflicting candidate is in the review with the old decision's id",
                "矛盾する旧決定" in q and old["id"] in q and "ブランド色だけは直書きしてよい" in q,
                q[:600])
    finally:
        sb.cleanup()


def main() -> int:
    p = Probe()
    print("\n== AC-6: background extraction ==")
    check_success(p)
    check_failures(p)
    check_no_recursion(p)
    print("\n== AC-8: duplicates and conflicts ==")
    check_duplicates_and_conflicts(p)
    return p.result()


if __name__ == "__main__":
    sys.exit(main())
