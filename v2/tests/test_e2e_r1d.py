"""R1-d AC-13 (design §10, R1-d acceptance cases) end to end, through the production hook
entry (`__main__.py hook`) and the CLI, on a reproduced session: the owner asks for a
UI change; the window drafts contracts; creators and an evaluator work as subagents.
  (1) "no-ui self-declaration": a contract that declares no pack while its scope holds UI
      paths stops at the lint (LINT-H), and that unit's creator's Edit of a UI path is
      blocked by the pack rule (AC-5);
  (2) "no skill named": an Agent launch without subagent_type is rewritten to the pack
      creator, and a general-purpose subagent's Edit of a UI path is blocked (no receipt);
  (3) a shell write the hook cannot read (`p=src; p=$p/ui/g.tsx; echo x > "$p"`),
      committed -> the integration stops: unattributed, reason (b);
  (4) a regular Edit (post-hash recorded), then an undeclared command overwrites the file,
      committed -> the integration stops: unattributed, reason (b);
  (5) session A's lease lapses, session B takes it, then A's creator writes a file,
      committed -> the integration (by the lease holder B) stops: reason (d).
      Since R1-e (AC-1) that Edit is already blocked at write time; the file then reaches
      the tree by a route the PreToolUse does not see (only its PostToolUse is recorded,
      as test_integrate's (d) case does), and the integration still stops it with (d).
"""
from __future__ import annotations

import json
import sys

from helpers import Probe
from r1c_helpers import cli, contract, make_unit, write_contract
from r1d_helpers import (CREATOR, EVALUATOR, WINDOW, advance, bash, commit_all, confirm,
                         expire_lease, integrate, new_r1d_sandbox, payload, post_write,
                         run_ulthar, start_agent, to_build, unit, write)

OTHER = "e2e-window-b"


def _listed(sb, unit_id: str) -> dict:
    last = unit(sb, unit_id)["history"][-1]
    return {u["path"]: u["reason"] for u in last.get("unattributed") or []} if last.get("refused") else {}


def _write_time_cases(p: Probe, sb, unit_id: str) -> int:
    held = 0
    noui = make_unit(sb)
    cli(sb, "contract", "submit", noui, "--file",
        str(write_contract(sb, contract(packs=[], scope_globs=["src/**"]), "noui.yaml")))
    lint = cli(sb, "contract", "lint", noui)
    confirm(sb, noui)
    adv = cli(sb, "unit", "advance", noui, "contract_confirmed")
    start_agent(sb, "noui-1", "general-purpose")
    edit = write(sb, "noui-1", "src/ui/x.tsx", "x\n", "general-purpose", tool="Edit")
    ok = (lint.returncode != 0 and "LINT-H" in lint.stdout and adv.returncode != 0
          and "lint_passed" in adv.stderr and edit.returncode == 2 and "ulthar skill pack" in edit.stderr)
    held += ok
    p.check("AC-13 (1): packs [] with a UI scope stops at LINT-H (not confirmable), and that "
            "unit's creator's Edit of a UI path is blocked by the pack rule", ok,
            lint.stdout[-200:] + adv.stderr[-200:] + edit.stderr[-300:])

    launch = sb.hook("PreToolUse", payload("Agent", {"description": "UI", "prompt":
                                                     f"unit {unit_id}: ボタンを減らす"}))
    updated = (json.loads(launch.stdout)["hookSpecificOutput"].get("updatedInput") or {}
               if launch.returncode == 0 and launch.stdout.strip() else {})
    start_agent(sb, "gp-2", "general-purpose")
    edit = write(sb, "gp-2", "src/ui/y.tsx", "y\n", "general-purpose", tool="Edit")
    ok = (updated.get("subagent_type") == CREATOR and edit.returncode == 2
          and "no receipt" in edit.stderr)
    held += ok
    p.check("AC-13 (2): a launch without subagent_type becomes ulthar-creator@ui, and a "
            "general-purpose subagent's Edit of a UI path is blocked (no receipt)", ok,
            launch.stdout[-200:] + edit.stderr[-300:])
    return held


def check_cases(p: Probe) -> None:
    sb = new_r1d_sandbox()
    try:
        sb.hook("UserPromptSubmit", {"session_id": WINDOW, "prompt": "設定画面のボタンを減らして",
                                     "prompt_id": "e2e-r1d-1"})
        unit_id = to_build(sb)
        held = _write_time_cases(p, sb, unit_id)

        start_agent(sb, "bld-1", CREATOR)
        write(sb, "bld-1", "src/ui/a.txt", "ui v1\n", CREATOR, tool="Edit")
        write(sb, "bld-1", "src/ui/f.txt", "edited\n", CREATOR, tool="Edit")
        gen = bash(sb, "bld-1", "python tools/gen.py src/ui/f.txt", CREATOR)
        (sb.work / "src" / "ui" / "f.txt").write_text("generated\n", encoding="utf-8")
        expire_lease(sb)
        sb.hook("PreToolUse", {"session_id": OTHER, "tool_name": "Bash", "tool_input": {"command": "ls"}})
        stale = write(sb, "bld-1", "src/ui/d.txt", "after the lapse\n", CREATOR, tool="Edit")
        post_write(sb, "bld-1", "src/ui/d.txt", "after the lapse\n", CREATOR)
        expire_lease(sb)  # B's lease lapses as well: the window takes it back and goes on
        run_ulthar(sb, "bld-1", ["verify", unit_id, "--self"], agent_type=CREATOR)
        advance(sb, unit_id, "quant_verified")
        start_agent(sb, "ev-1", EVALUATOR)
        run_ulthar(sb, "ev-1", ["verify", unit_id], agent_type=EVALUATOR)
        run_ulthar(sb, "ev-1", ["review", "record", "--unit", unit_id, "--kind", "verify",
                                "--result", "pass", "--via", "codex"], agent_type=EVALUATOR)
        advance(sb, unit_id, "owner_review")
        advance(sb, unit_id, "accepted")
        commit_all(sb, "the unit's work")
        expire_lease(sb)
        sb.hook("PreToolUse", {"session_id": OTHER, "tool_name": "Bash", "tool_input": {"command": "ls"}})
        res = integrate(sb, unit_id, session=OTHER)
        listed = _listed(sb, unit_id)
        ok = (gen.returncode == 0 and res is not None and res.returncode != 0
              and listed.get("src/ui/f.txt") == "b" and "src/ui/a.txt" not in listed)
        held += ok
        p.check("AC-13 (4): an Edit then an undeclared overwrite (allowed at write time), "
                "committed -> unattributed (b)", ok, (res.stderr[-500:] if res else "") + str(listed))
        ok = (stale.returncode == 2 and "lease" in stale.stderr
              and listed.get("src/ui/d.txt") == "d")
        held += ok
        p.check("AC-13 (5): a write after the lease lapsed and B took it (blocked at write time "
                "since R1-e AC-1; written anyway), committed -> unattributed (d)", ok,
                str(listed) + stale.stderr[-200:])

        shell = bash(sb, "bld-1", 'p=src; p=$p/ui/g.tsx; echo x > "$p"', CREATOR)
        (sb.work / "src" / "ui" / "g.tsx").write_text("x\n", encoding="utf-8")
        commit_all(sb, "a shell write")
        res = integrate(sb, unit_id, session=OTHER)
        listed = _listed(sb, unit_id)
        ok = (shell.returncode == 0 and res is not None and res.returncode != 0
              and listed.get("src/ui/g.tsx") == "b" and unit(sb, unit_id)["state"] == "accepted")
        held += ok
        p.check("AC-13 (3): a shell write the hook cannot read (allowed at write time), "
                "committed -> unattributed (b); the unit stays accepted", ok,
                shell.stderr[-200:] + (res.stderr[-300:] if res else "") + str(listed))
        p.check(f"AC-13: {held}/5", held == 5)
    finally:
        sb.cleanup()


def main() -> int:
    p = Probe()
    print("\n== R1-d AC-13: acceptance cases end to end ==")
    check_cases(p)
    return p.result()


if __name__ == "__main__":
    sys.exit(main())
