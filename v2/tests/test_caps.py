"""R1-e AC-6 (the capability registry, PORT-05) and AC-7 (the evaluator's substitute, PORT-05 /
ROLE-02 / spec 事前決定 6), through the CLI and the production hook entry. The codex health
check is a test command (`caps.codex.check_cmd`); no real Codex is called.

AC-7 drives units to quant_verified the R1-c way; the verify review is recorded `--via`
a capability, and quant_verified -> owner_review then needs codex available at record
time, or the owner's CAP-SUBST approval for that unit and capability (answered through
AskUserQuestion and checked against the transcript as R1-c AC-5 does)."""
from __future__ import annotations

import json
import sys

from helpers import Probe
from r1c_helpers import answer, cli, new_sandbox, runner, to_quant_verified, unit
from r1e_helpers import STUB_FAIL, STUB_OK, set_config


def _caps(sb) -> dict:
    return {r[0]: r[1:] for r in sb.query("SELECT name, healthy, last_check, remaining, detail"
                                          " FROM capabilities")}


def check_registry(p: Probe) -> None:
    sb = new_sandbox()
    try:
        held = 0
        set_config(sb, caps={"codex": {"check_cmd": STUB_OK}})
        ok_run = cli(sb, "cap", "check", "codex")
        row = _caps(sb)["codex"]
        listed = cli(sb, "cap", "list").stdout
        ok = (ok_run.returncode == 0 and row[0] == 1 and row[1] and row[2] == "42"
              and "codex\tavailable" in listed)
        held += ok
        p.check("AC-6: a passing check -> healthy, time and remaining (42) recorded; listed available",
                ok, ok_run.stdout + str(row) + listed)
        set_config(sb, caps={"codex": {"check_cmd": STUB_FAIL}})
        bad = cli(sb, "cap", "check", "codex")
        row = _caps(sb)["codex"]
        listed = cli(sb, "cap", "list").stdout
        degraded = sb.query("SELECT important FROM notifications WHERE kind='capability_degraded'")
        ok = (bad.returncode != 0 and row[0] == 0 and "quota exhausted" in row[3]
              and "codex\tUNAVAILABLE" in listed and "failed" in listed and degraded == [(1,)])
        held += ok
        p.check("AC-6: a failing check -> unavailable with the reason; an important "
                "capability-degraded notification", ok, bad.stdout + str(row) + listed + str(degraded))
        set_config(sb, caps={"codex": {"check_cmd": STUB_OK}})
        cli(sb, "cap", "check", "codex")
        sb.query("UPDATE capabilities SET last_check='2000-01-01T00:00:00.000+00:00' WHERE name='codex'")
        listed = cli(sb, "cap", "list").stdout
        ok = "codex\tUNAVAILABLE" in listed and "older than 24 hours" in listed
        held += ok
        p.check("AC-6: a check older than 24 hours -> unavailable", ok, listed)
        push = cli(sb, "cap", "check", "claude_push")
        listed = cli(sb, "cap", "list").stdout
        ok = push.returncode != 0 and "claude_push\tUNAVAILABLE" in listed and "P-09" in listed
        held += ok
        p.check("AC-6: claude_push is always unavailable (P-09)", ok, push.stdout + listed)
        p.check(f"AC-6: {held}/4", held == 4)
    finally:
        sb.cleanup()


def _review(sb, unit_id: str, reviewer: str, via: str) -> None:
    runner(sb, reviewer, ["verify", unit_id])
    runner(sb, reviewer, ["review", "record", "--unit", unit_id, "--kind", "verify",
                          "--result", "pass", "--via", via])


def _subst(sb, unit_id: str, tag: str, in_transcript: bool = True) -> dict:
    question = json.loads(cli(sb, "unit", "show", unit_id, "--ask", "subst", "--cap", "claude").stdout)
    return answer(sb, question, ["承認する"], tag, in_transcript=in_transcript)


def check_substitute(p: Probe) -> None:
    sb = new_sandbox()
    try:
        allowed = refused = 0
        first = to_quant_verified(sb, builder="bld-a")
        _review(sb, first, "rev-a", "codex")
        res = cli(sb, "unit", "advance", first, "owner_review")
        allowed += res.returncode == 0
        p.check("AC-7: codex available at record time, review --via codex -> owner_review",
                res.returncode == 0, res.stderr[-300:])

        set_config(sb, caps={"codex": {"check_cmd": STUB_FAIL}})
        cli(sb, "cap", "check", "codex")
        second = to_quant_verified(sb, builder="bld-b")
        _review(sb, second, "rev-b", "claude")
        res = cli(sb, "unit", "advance", second, "owner_review")
        ok = res.returncode != 0 and "evaluator_capability_ok" in res.stderr and "CAP-SUBST" in res.stderr
        refused += ok
        p.check("AC-7: codex unavailable, a substitute review without approval -> refused", ok,
                res.stderr[-400:])

        forged = _subst(sb, second, "subst-forged", in_transcript=False)
        res = cli(sb, "unit", "advance", second, "owner_review")
        ok = res.returncode != 0 and unit(sb, second)["state"] == "quant_verified"
        refused += ok
        p.check("AC-7: a forged CAP-SUBST approval (no AskUserQuestion in the transcript) -> refused",
                ok, json.dumps(forged, ensure_ascii=False)[:300] + res.stderr[-300:])

        third = to_quant_verified(sb, builder="bld-c")
        _review(sb, third, "rev-c", "claude")
        _subst(sb, third, "subst-third")
        res = cli(sb, "unit", "advance", second, "owner_review")
        ok = res.returncode != 0 and unit(sb, second)["state"] == "quant_verified"
        refused += ok
        p.check("AC-7: the approval of another unit is not reused -> refused", ok, res.stderr[-300:])

        res = cli(sb, "unit", "advance", third, "owner_review")
        rec = unit(sb, third)
        ok = res.returncode == 0 and rec["state"] == "owner_review" and bool(rec["history"][-1]["tokens"])
        allowed += ok
        p.check("AC-7: a substitute review with the owner's CAP-SUBST approval -> owner_review "
                "(the approval consumed)", ok, res.stderr[-300:])
        p.check(f"AC-7: refused {refused}/3, allowed {allowed}/2", refused == 3 and allowed == 2)
    finally:
        sb.cleanup()


def main() -> int:
    p = Probe()
    print("\n== R1-e AC-6: capability registry ==")
    check_registry(p)
    print("\n== R1-e AC-7: the evaluator's substitute ==")
    check_substitute(p)
    return p.result()


if __name__ == "__main__":
    sys.exit(main())
