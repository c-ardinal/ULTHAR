"""R1-b memory injection:
  AC-5  a fact whose source blob changed becomes stale and leaves L0/L1;
  AC-9  "the design decision never reached the subagent": an owner remark -> extraction ->
        Agent launch injects the decision via SubagentStart; a launch before the
        extraction finished injects the remark verbatim; another project's memory never;
  AC-10 budget: 10,000 chars pass untouched, 10,001 become pointers (confirmed decisions
        never), confirmed decisions over the limit block Agent launches (exit 2) and,
        over L0's 6,000, the main session's writes; reasons shown;
  AC-11 L0 on SessionStart: startup, compact, and no Episode since the previous L0.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time

from helpers import Probe, Sandbox, write_jsonl
from test_extract import canned, decisions, wait_extractions

from ulthar import db as db_mod
from ulthar import memory

SESSION = "mem"


def context_of(proc) -> str:
    if proc.returncode != 0 or not proc.stdout.strip():
        return ""
    return json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]


def subagent_start(sb: Sandbox, work=None, agent: str = "a-1"):
    payload = {"session_id": SESSION, "agent_id": agent, "agent_type": "ulthar-creator"}
    if work is not None:
        payload["cwd"] = str(work)
    return sb.hook("SubagentStart", payload)


def add(sb: Sandbox, kind: str, specs: list[dict], work=None) -> list[dict]:
    with sb.inproc(), sb.store(work).transaction({"hook": "test-setup"}) as tx:
        return [tx.add(kind, s) for s in specs]


def decision(text: str, status: str = "provisional", **extra) -> dict:
    return {"status": status, "text": text, "raw_episode_id": "E-setup",
            "binding": status == "confirmed", "confirm_token_id": None, **extra}


# ---------------------------------------------------------------------------- AC-5

def check_fact_staleness(p: Probe) -> None:
    sb = Sandbox()
    try:
        subprocess.run(["git", "init", "-q", str(sb.work)], check=True)
        src = sb.work / "src" / "theme.css"
        src.parent.mkdir()
        src.write_text(":root { --radius: 4px; }\n", encoding="utf-8")
        added = sb.cli("fact", "add", "--src", "src/theme.css:1", "角丸の値は theme.css の --radius")
        before = context_of(subagent_start(sb))
        src.write_text(":root { --radius: 6px; }\n", encoding="utf-8")
        after = context_of(subagent_start(sb, agent="a-2"))
        facts = [json.loads(r[0]) for r in sb.kquery("SELECT json FROM current WHERE kind='fact'")]
        l0 = context_of(sb.hook("SessionStart", {"session_id": SESSION, "source": "startup"}))
        p.check("AC-5: fact add records the source blob hash (git hash-object)",
                added.returncode == 0 and len(facts) == 1 and len(facts[0]["src_blob"]) == 40,
                added.stderr[-300:])
        p.check("  ... and the valid fact is in L1", "theme.css の --radius" in before, before[-400:])
        p.check("AC-5: after the source changed the fact is stale (1/1)",
                facts[0]["status"] == "stale" and facts[0]["invalid_at"], str(facts))
        p.check("AC-5: the stale fact is in neither L1 nor L0 (0 injected)",
                "theme.css の --radius" not in after and "theme.css の --radius" not in l0)
    finally:
        sb.cleanup()


# ---------------------------------------------------------------------------- AC-9

def check_design_decision_reaches_subagent(p: Probe) -> None:
    sb = Sandbox()
    try:
        remark = "ボタンの角丸がページごとにバラバラ。全部 4px に揃えて、今後もそうして"
        answer = canned(sb, [{"kind": "decision", "text": "ボタンの角丸は全画面で 4px に統一する",
                              "scope": "project", "tags": ["design", "ui"],
                              "paths": ["src/ui/**"]}])
        sb.configure(["file", answer])
        sb.hook("UserPromptSubmit", {"session_id": SESSION, "prompt": remark, "prompt_id": "r-1"})
        wait_extractions(sb, 1)
        launch = sb.hook("PreToolUse", {"session_id": SESSION, "tool_name": "Agent",
                                        "tool_input": {"subagent_type": "ulthar-creator",
                                                       "prompt": "設定画面を作って"}})
        ctx = context_of(subagent_start(sb))
        dec = decisions(sb)[0]
        p.check("AC-9: the Agent launch is allowed", launch.returncode == 0, launch.stderr[-300:])
        p.check("AC-9: SubagentStart injects the extracted design decision (1/1)",
                dec["id"] in ctx and "ボタンの角丸は全画面で 4px に統一する" in ctx, ctx[:600])

        slow = canned(sb, [{"kind": "decision", "text": "モーダルは右上に閉じるボタン"}], "slow.json")
        sb.configure(["sleep", "8", slow])
        early = "モーダルの閉じるボタンは必ず右上。左上は禁止"
        sb.hook("UserPromptSubmit", {"session_id": SESSION, "prompt": early, "prompt_id": "r-2"})
        ctx2 = context_of(subagent_start(sb, agent="a-2"))
        p.check("AC-9: a launch before extraction finished gets the remark verbatim as "
                "the owner's unextracted message (1/1)",
                "オーナーの未整理の発言" in ctx2 and early in ctx2, ctx2[:800])

        other = sb.root / "other-project"
        other.mkdir()
        sb.configure(["fail"], work=other)
        add(sb, "decision", [decision("OTHER-PROJECT-DECISION-7731", "confirmed")], work=other)
        sb.hook("UserPromptSubmit", {"session_id": "other", "cwd": str(other),
                                     "prompt": "OTHER-PROJECT-REMARK-7731", "prompt_id": "o-1"})
        time.sleep(1.5)
        ctx3 = context_of(subagent_start(sb, agent="a-3"))
        p.check("AC-9: another project's decisions and remarks are not injected (0)",
                bool(ctx3) and "7731" not in ctx3 and "OTHER-PROJECT" not in ctx3, ctx3[:400])
        wait_extractions(sb, 3)
    finally:
        sb.cleanup()


# ---------------------------------------------------------------------------- AC-10

def _l1(sb: Sandbox) -> memory.Packed:
    with sb.inproc():
        conn = db_mod.connect(sb.db)
        try:
            return memory.build_l1(sb.store(), conn, {"hook": "test"})
        finally:
            conn.close()


def check_budget_boundary(p: Probe) -> None:
    sb = Sandbox()
    try:
        confirmed = add(sb, "decision", [decision("確定: 主要ボタンは青", "confirmed")])[0]
        filler = add(sb, "decision", [decision("x")])[0]
        base = len(_l1(sb).text)
        need = memory.L1_MAX_CHARS - base + 1  # the text of length 1 -> length `need`

        def set_len(n: int) -> memory.Packed:
            with sb.inproc(), sb.store().transaction({"hook": "test"}) as tx:
                tx.revise("decision", filler["id"], text="仮" * n)
            return _l1(sb)

        exact = set_len(need)
        p.check(f"AC-10: an L1 of exactly {memory.L1_MAX_CHARS} chars passes untouched",
                len(exact.text) == memory.L1_MAX_CHARS and not exact.over
                and not exact.pointerized, f"len={len(exact.text)} ptr={exact.pointerized}")
        one_more = set_len(need + 1)
        p.check(f"AC-10: {memory.L1_MAX_CHARS + 1} chars -> the provisional decision becomes a "
                "pointer (ID + 60-char heading)",
                not one_more.over and one_more.pointerized == [filler["id"]]
                and len(one_more.text) <= memory.L1_MAX_CHARS
                and f"- {filler['id']} {'仮' * memory.HEADING_CHARS}\n" in one_more.text + "\n",
                f"len={len(one_more.text)} ptr={one_more.pointerized}")
        p.check("AC-10: ... while the confirmed decision stays whole (truncation 0)",
                "確定: 主要ボタンは青" in one_more.text and confirmed["id"] not in one_more.pointerized)
    finally:
        sb.cleanup()


def check_budget_blocks(p: Probe) -> None:
    sb = Sandbox()
    try:
        texts = [f"拘束的決定 {i}: " + "長" * 1200 for i in range(9)]  # > 10,000 chars
        add(sb, "decision", [decision(t, "confirmed") for t in texts])
        launch = sb.hook("PreToolUse", {"session_id": SESSION, "tool_name": "Agent",
                                        "tool_input": {"prompt": "go"}})
        resume = sb.hook("PreToolUse", {"session_id": SESSION, "tool_name": "SendMessage",
                                        "tool_input": {"to": "a-1", "message": "next"}})
        p.check("AC-10: confirmed decisions over the L1 limit block Agent and SendMessage "
                "(exit 2) with the reason (1/1)",
                launch.returncode == 2 and resume.returncode == 2 and "上限" in launch.stderr
                and "Agent" in launch.stderr, launch.stderr[-400:])
        ctx = context_of(subagent_start(sb))
        p.check("AC-10: no confirmed decision is ever cut short (truncated 0)",
                all(t in ctx for t in texts) or not any(t[:40] in ctx and t not in ctx
                                                        for t in texts), ctx[:300])
        write = sb.hook("PreToolUse", {"session_id": SESSION, "tool_name": "Write",
                                       "tool_input": {"file_path": str(sb.work / "a.txt"),
                                                      "content": "x"}})
        shell_write = sb.hook("PreToolUse", {"session_id": SESSION, "tool_name": "Bash",
                                             "tool_input": {"command": "python gen.py --out a.txt"}})
        shell_read = sb.hook("PreToolUse", {"session_id": SESSION, "tool_name": "Bash",
                                            "tool_input": {"command": "git status"}})
        in_agent = sb.hook("PreToolUse", {"session_id": SESSION, "agent_id": "a-1",
                                          "agent_type": "t", "tool_name": "Write",
                                          "tool_input": {"file_path": str(sb.work / "b.txt"),
                                                         "content": "x"}})
        p.check("AC-10: main session with L0 over 6,000: Write and a writing Bash command are "
                "blocked with the reason",
                write.returncode == 2 and shell_write.returncode == 2 and "L0" in write.stderr,
                write.stderr[-300:])
        p.check("  ... read-only Bash and a subagent's write are not blocked by L0",
                shell_read.returncode == 0 and in_agent.returncode == 0,
                shell_read.stderr[-200:] + in_agent.stderr[-200:])
    finally:
        sb.cleanup()


# ---------------------------------------------------------------------------- AC-11

def _line(uuid: str, text: str, **flags) -> dict:
    return {"type": "user", "uuid": uuid, "timestamp": "2026-11-11T00:00:00Z",
            "message": {"role": "user", "content": text}, **flags}


def check_l0(p: Probe) -> None:
    sb = Sandbox()
    try:
        transcript = sb.root / "main.jsonl"
        write_jsonl(transcript, [_line("u1", "最初のオーナー発言 alpha"),
                                 _line("u2", "二つ目の発言 beta")])
        conf = add(sb, "decision", [decision("確定: 文言は です・ます 調", "confirmed")])[0]
        add(sb, "decision", [decision("仮: アイコンは 24px"), decision("仮: 余白 8px")])
        base = {"session_id": SESSION, "transcript_path": str(transcript)}
        start = context_of(sb.hook("SessionStart", dict(base, source="startup")))
        ok1 = (conf["text"] in start and "2 件" in start and "[データ]" in start
               and "最初のオーナー発言 alpha" in start and len(start) <= memory.L0_MAX_CHARS)
        p.check("AC-11 startup: confirmed decision in full, 2 candidates, Episode headings "
                f"marked as data ({len(start)} chars <= 6000)", ok1, start[:600])
        write_jsonl(transcript, [_line("u3", "This session is being continued: summary gamma",
                                       isCompactSummary=True),
                                 _line("u4", "要約後の発言 delta")], mode="a")
        compact = context_of(sb.hook("SessionStart", dict(base, source="compact")))
        ok2 = (conf["text"] in compact and "gamma" in compact and "delta" in compact
               and "alpha" not in compact and len(compact) <= memory.L0_MAX_CHARS)
        p.check("AC-11 compact: only the Episodes since the previous L0 are listed", ok2,
                compact[:600])
        resume = context_of(sb.hook("SessionStart", dict(base, source="resume")))
        ok3 = (conf["text"] in resume and "前回以降の Episode はありません" in resume
               and "delta" not in resume and len(resume) <= memory.L0_MAX_CHARS)
        p.check("AC-11 nothing new since the previous L0: decisions and count, no headings", ok3,
                resume[:600])
        p.check(f"AC-11: {sum((ok1, ok2, ok3))}/3 as specified", ok1 and ok2 and ok3)
        episode_lines = [ln for ln in start.splitlines() if "[データ]" in ln]
        p.check("AC-14: Episodes appear in L0 only as marked headings (ID + <= 60 chars)",
                episode_lines and all(len(ln) <= 2 + len("[データ] ") + 15 + len("(compact_summary) ")
                                      + memory.HEADING_CHARS for ln in episode_lines),
                str(episode_lines[:3]))
    finally:
        sb.cleanup()


def main() -> int:
    p = Probe()
    print("\n== AC-5: fact staleness ==")
    check_fact_staleness(p)
    print("\n== AC-9: L1 and the launch barrier ==")
    check_design_decision_reaches_subagent(p)
    print("\n== AC-10: budget ==")
    check_budget_boundary(p)
    check_budget_blocks(p)
    print("\n== AC-11: L0 ==")
    check_l0(p)
    return p.result()


if __name__ == "__main__":
    sys.exit(main())
