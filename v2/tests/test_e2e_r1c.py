"""R1-c AC-12 (design §10, R1-c acceptance cases) end to end, through the production hook
entry (`__main__.py hook`) and the CLI, on a reproduced session: the owner asks for a
change, the window drafts a contract, a builder and a reviewer work as subagents.
  (1) moving to contract_confirmed without the owner's confirm token is refused;
  (2) a contract with a vague word stops at the lint (and cannot be confirmed);
  (3) evidence a builder wrote is not accepted;
  (4) a product write before the contract is confirmed is blocked.
"""
from __future__ import annotations

import json
import sys

from helpers import Probe
from r1c_helpers import (SESSION, answer, cli, confirm_contract, contract, edit, make_unit,
                         new_sandbox, quant_ac, runner, set_value, write_contract)


def check_cases(p: Probe) -> None:
    sb = new_sandbox()
    try:
        held = 0
        prompt = sb.hook("UserPromptSubmit", {"session_id": SESSION, "prompt": "ボタンを減らして",
                                              "prompt_id": "e2e-1"})
        unit_id = make_unit(sb)
        vague = contract(acs=[quant_ac(statement="ボタンをなるべく減らす")])
        cli(sb, "contract", "submit", unit_id, "--file", str(write_contract(sb, vague)))
        lint = cli(sb, "contract", "lint", unit_id)
        confirm_contract(sb, unit_id)
        adv = cli(sb, "unit", "advance", unit_id, "contract_confirmed")
        ok = (lint.returncode != 0 and "LINT-A" in lint.stdout and "なるべく" in lint.stdout
              and adv.returncode != 0 and "lint_passed" in adv.stderr)
        held += ok
        p.check("AC-12 (2): the vague contract stops at the lint, and is not confirmed even "
                "with the owner's 確定", ok and prompt.returncode == 0, lint.stdout + adv.stderr[-300:])

        write = edit(sb, "bld-1", "src/app.txt", tool="Write")
        ok = write.returncode == 2 and "unit scope" in write.stderr
        held += ok
        p.check("AC-12 (4): a builder's product write while the contract is a draft is blocked",
                ok, write.stderr[-300:])

        clean = contract()
        cli(sb, "contract", "submit", unit_id, "--file", str(write_contract(sb, clean, "v2.yaml")))
        runner(sb, "rev-1", ["review", "record", "--unit", unit_id, "--kind", "contract",
                             "--result", "pass"])
        adv = cli(sb, "unit", "advance", unit_id, "contract_confirmed")
        ok = adv.returncode != 0 and "contract_confirm_token" in adv.stderr
        held += ok
        p.check("AC-12 (1): lint and review pass, but no owner confirm token for v2 -> "
                "contract_confirmed refused", ok, adv.stderr[-300:])

        question = json.loads(cli(sb, "contract", "show", unit_id).stdout)
        answer(sb, question, ["確定"], "e2e")
        for to in ("contract_confirmed", "build"):
            cli(sb, "unit", "advance", unit_id, to)
        set_value(sb, "AC-1", "10")
        runner(sb, "bld-1", ["verify", unit_id, "--self"])
        evidence_dir = sb.kr / ".ulthar" / "evidence" / unit_id
        fake = {"unit_id": unit_id, "all_quant_passed": True, "self": True,
                "acs": [{"id": "AC-1", "passed": True, "value": 1}]}
        hook_write = sb.hook("PreToolUse", {
            "session_id": SESSION, "agent_id": "bld-1", "agent_type": "ulthar-creator",
            "tool_name": "Write", "tool_input": {"file_path": str(evidence_dir / "mine.json"),
                                                 "content": json.dumps(fake)}})
        (evidence_dir / "mine.json").write_text(json.dumps(fake), encoding="utf-8")
        adv = cli(sb, "unit", "advance", unit_id, "quant_verified")
        ok = (hook_write.returncode == 2 and adv.returncode != 0
              and "self_verify_all_quant_passed" in adv.stderr)
        held += ok
        p.check("AC-12 (3): the builder's own evidence file is blocked at the hook and, placed "
                "anyway, not accepted", ok, hook_write.stderr[-200:] + adv.stderr[-300:])
        p.check(f"AC-12: {held}/4", held == 4)
    finally:
        sb.cleanup()


def main() -> int:
    p = Probe()
    print("\n== AC-12: R1-c acceptance cases end to end ==")
    check_cases(p)
    return p.result()


if __name__ == "__main__":
    sys.exit(main())
