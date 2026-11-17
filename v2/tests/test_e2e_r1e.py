"""R1-e AC-14 (design §10, R1-e acceptance cases) end to end, through the production hook entry
(`__main__.py hook`) and the CLI, on one reproduced session: the owner asks for a UI change,
the window drives a ui-pack unit, creators and an evaluator work as subagents.
  (1) a second window's write is stopped (the lease, AC-1);
  (2) the window's context usage passes the threshold -> everything but the relay stops (AC-2/3);
  (3) Codex is unavailable -> a substitute evaluation without the owner's approval is not
      usable for acceptance (AC-6/7);
  (4) a direct Linear MCP write is stopped (AC-8);
  (5) after a block, the same intent allowed through another subagent is notified as an
      evasion suspicion -- it reaches the ntfy stand-in on 127.0.0.1 (AC-9/10)."""
from __future__ import annotations

import sys

from helpers import Probe, write_jsonl
from r1c_helpers import cli, transcript
from r1d_helpers import (CREATOR, EVALUATOR, WINDOW, advance, new_r1d_sandbox, run_ulthar,
                         start_agent, to_build, write)
from r1e_helpers import (OTHER, STUB_FAIL, NtfyStub, assistant_line, call, set_config,
                         wait_sent)


def check_cases(p: Probe) -> None:
    stub = NtfyStub()
    sb = new_r1d_sandbox()
    try:
        set_config(sb, notify={"ntfy_url": stub.url("e2e")})
        sb.hook("UserPromptSubmit", {"session_id": WINDOW, "prompt": "設定画面のボタンを減らして",
                                     "prompt_id": "e2e-r1e-1"})
        unit_id = to_build(sb)
        held = 0

        start_agent(sb, "gp-1", "general-purpose")
        blocked = write(sb, "gp-1", "src/ui/a.txt", "gp\n", "general-purpose", tool="Edit")
        start_agent(sb, "bld-1", CREATOR)
        allowed = write(sb, "bld-1", "src/ui/a.txt", "ui v1\n", CREATOR, tool="Edit")
        sent = wait_sent(sb)
        got = stub.wait(1) and any("回避の疑い" in r["body"] for r in stub.received)
        ok = blocked.returncode == 2 and allowed.returncode == 0 and bool(got) and sent and sent[0][1] == 1
        held += ok
        p.check("AC-14 (5): a block, then the same path allowed through another subagent -> an "
                "evasion suspicion notified (received by the ntfy stand-in)", ok,
                f"{blocked.stderr[-200:]} {allowed.stderr[-200:]} {sent} {len(stub.received)}")

        second = write(sb, None, "src/ui/a.txt", "other window\n", session=OTHER)
        sub = write(sb, "other-sub", "src/ui/a.txt", "x\n", CREATOR, session=OTHER)
        ok = all(r.returncode == 2 and "window lease" in r.stderr for r in (second, sub))
        held += ok
        p.check("AC-14 (1): a second window's write (and its subagent's) is stopped by the lease",
                ok, second.stderr[-300:] + sub.stderr[-300:])

        mcp = call(sb, "mcp__linear-server__save_issue", {"title": "ボタンを減らす", "team": "X"})
        ok = mcp.returncode == 2 and "external write" in mcp.stderr
        held += ok
        p.check("AC-14 (4): a direct Linear MCP write is stopped", ok, mcp.stderr[-300:])

        run_ulthar(sb, "bld-1", ["verify", unit_id, "--self"], agent_type=CREATOR)
        advance(sb, unit_id, "quant_verified")
        set_config(sb, caps={"codex": {"check_cmd": STUB_FAIL}})
        cli(sb, "cap", "check", "codex")
        start_agent(sb, "ev-1", EVALUATOR)
        run_ulthar(sb, "ev-1", ["verify", unit_id], agent_type=EVALUATOR)
        run_ulthar(sb, "ev-1", ["review", "record", "--unit", unit_id, "--kind", "verify",
                                "--result", "pass", "--via", "claude"], agent_type=EVALUATOR)
        res = cli(sb, "unit", "advance", unit_id, "owner_review")
        ok = res.returncode != 0 and "CAP-SUBST" in res.stderr
        held += ok
        p.check("AC-14 (3): Codex unavailable -> the substitute evaluation without approval is "
                "refused for acceptance", ok, res.stderr[-400:])

        path = transcript(sb)
        write_jsonl(path, [assistant_line("e2e-a", 130000)], mode="a")  # 65% of 200,000
        call(sb, "Read", {"file_path": str(path)}, transcript_path=str(path))
        edit = write(sb, "bld-1", "src/ui/a.txt", "more\n", CREATOR, tool="Edit")
        agent = call(sb, "Agent", {"description": "UI", "prompt": f"unit {unit_id}"})
        relay = call(sb, "Bash", {"command": "ulthar relay"})
        ok = (edit.returncode == 2 and "relay due" in edit.stderr and agent.returncode == 2
              and relay.returncode == 0)
        held += ok
        p.check("AC-14 (2): usage over the threshold -> the relay is due: writes and launches stop, "
                "`ulthar relay` passes", ok, edit.stderr[-200:] + agent.stderr[-200:] + relay.stderr[-200:])
        p.check(f"AC-14: {held}/5", held == 5)
    finally:
        sb.cleanup()
        stub.close()


def main() -> int:
    p = Probe()
    print("\n== R1-e AC-14: acceptance cases end to end ==")
    check_cases(p)
    return p.result()


if __name__ == "__main__":
    sys.exit(main())
