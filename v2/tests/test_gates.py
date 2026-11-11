"""R1-c gates of the unit process:
  AC-8  `verify --self` loops: halted on the loop budget and on a stall (2 runs in a row
        without improvement), not while measurements improve; leaving halted needs the
        cause and the owner's UNIT-RESUME or a newly confirmed contract version (AC-6);
  AC-9  quant_verified -> owner_review: each of its five conditions missing alone is
        refused, all met succeeds;
  AC-10 owner_review -> accepted: no qualitative AC, all passed, one failed, forged.
"""
from __future__ import annotations

import json
import sys

from helpers import Probe
from r1c_helpers import (answer, cli, confirm_contract, contract, edit, new_sandbox, qual_ac,
                         quant_ac, reviewer_pass, runner, set_value, to_build, to_owner_review,
                         to_quant_verified, unit, write_contract)


def _loop(sb, unit_id: str, values: list[str]) -> list[str]:
    states = []
    for v in values:
        set_value(sb, "AC-1", v)
        runner(sb, "bld-1", ["verify", unit_id, "--self"])
        states.append(unit(sb, unit_id)["state"])
    return states


def check_loops(p: Probe) -> None:
    sb = new_sandbox()
    try:
        limit = to_build(sb, contract(budget={"loops": 2}))
        states = _loop(sb, limit, ["10", "9"])
        rec = unit(sb, limit)
        p.check("AC-8: budget.loops=2 reached without passing -> halted (budget) after run 2",
                states == ["build", "halted"] and "budget" in (rec.get("halt_reason") or "")
                and rec["history"][-1]["to"] == "halted" and len(rec["loops"]) == 2,
                f"{states} {rec.get('halt_reason')}")
        stall = to_build(sb, contract())
        states = _loop(sb, stall, ["10", "10", "10"])
        rec = unit(sb, stall)
        p.check("AC-8: the same AC not improving in 2 consecutive runs -> halted (stalled)",
                states == ["build", "build", "halted"] and "stalled" in (rec.get("halt_reason") or ""),
                f"{states} {rec.get('halt_reason')}")
        better = to_build(sb, contract())
        states = _loop(sb, better, ["10", "8", "6", "4", "2"])
        rec = unit(sb, better)
        p.check("AC-8: measurements improving every run (10, 8, 6, 4, then 2 = pass) -> never "
                "halted (false halts 0), each loop recorded with its value",
                states == ["build"] * 5 and [r["values"]["AC-1"] for r in rec["loops"]]
                == [10.0, 8.0, 6.0, 4.0, 2.0], f"{states} {rec.get('loops')}")
        _resume(p, sb, limit, stall)
    finally:
        sb.cleanup()


def _resume(p: Probe, sb, by_owner: str, by_contract: str) -> None:
    set_value(sb, "AC-1", "1")
    r = cli(sb, "unit", "advance", by_owner, "build")
    p.check("AC-8/AC-6: halted -> build refused without the cause classification",
            r.returncode != 0 and "cause" in r.stderr, r.stderr[-300:])
    r = cli(sb, "unit", "advance", by_owner, "build", "--cause", "implementation")
    p.check("AC-6: halted -> build refused with a cause but no owner resume / new contract",
            r.returncode != 0 and "resume_authorized" in r.stderr, r.stderr[-300:])
    question = json.loads(cli(sb, "unit", "show", by_owner, "--ask", "resume").stdout)
    answer(sb, question, ["再開"], "resume")
    r = cli(sb, "unit", "advance", by_owner, "build", "--cause", "implementation")
    rec = unit(sb, by_owner)
    p.check("AC-6: halted -> build (the state before) with the cause and the owner's "
            "UNIT-RESUME; the loop count starts again",
            r.returncode == 0 and rec["state"] == "build" and rec["loops"] == []
            and rec["history"][-1]["cause"] == "implementation", r.stderr[-300:])
    new_doc = contract(acs=[quant_ac(hi=12)])
    cli(sb, "contract", "submit", by_contract, "--file", str(write_contract(sb, new_doc, "v2.yaml")))
    confirm_contract(sb, by_contract)
    r = cli(sb, "unit", "advance", by_contract, "build", "--cause", "requirement")
    rec = unit(sb, by_contract)
    p.check("AC-6: halted -> build through a newly confirmed contract version (v2)",
            r.returncode == 0 and rec["state"] == "build" and rec["confirmed"]["version"] == 2,
            r.stderr[-300:])


def _gate_case(sb, case: str) -> tuple[str, object]:
    set_value(sb, "AC-1", "1")
    unit_id = to_quant_verified(sb, builder=f"bld-{case}")
    reviewer = f"bld-{case}" if case == "b" else "rev-1"
    if case == "a":
        set_value(sb, "AC-1", "10")
    runner(sb, reviewer, ["verify", unit_id])
    review = {"c": ["--result", "fail", "--blocker", "保存ボタンが押せない"],
              "e": ["--result", "pass", "--unmapped-change", "ヘッダーの色が変わった"]}.get(
        case, ["--result", "pass"])
    runner(sb, "rev-1", ["review", "record", "--unit", unit_id, "--kind", "verify", *review])
    if case == "d":
        (sb.work / "src" / "app.txt").write_text("changed after the review\n", encoding="utf-8")
    return unit_id, cli(sb, "unit", "advance", unit_id, "owner_review")


def check_reviewer_gate(p: Probe) -> None:
    expect = {"a": "reviewer_verify_all_quant_passed", "b": "reviewer_independent",
              "c": "verify_review_passed_no_blockers", "d": "evidence_tree_matches",
              "e": "no_unmapped_changes"}
    sb = new_sandbox()
    try:
        refused = 0
        for case, name in expect.items():
            _, r = _gate_case(sb, case)
            named = [n for n in expect.values() if n in r.stderr]
            ok = r.returncode != 0 and named == [name]
            refused += ok
            p.check(f"AC-9 ({case}) missing alone -> refused naming only {name}", ok,
                    r.stderr[-500:])
        unit_id, r = _gate_case(sb, "ok")
        p.check(f"AC-9: refused 5 conditions {refused}/5", refused == 5)
        p.check("AC-9: all five met -> quant_verified -> owner_review 1/1",
                r.returncode == 0 and unit(sb, unit_id)["state"] == "owner_review", r.stderr[-400:])
    finally:
        sb.cleanup()


def check_qualitative(p: Probe) -> None:
    sb = new_sandbox()
    try:
        expected = 0
        none = to_owner_review(sb)
        r = cli(sb, "unit", "advance", none, "accepted")
        ok = r.returncode == 0 and unit(sb, none)["state"] == "accepted"
        expected += ok
        p.check("AC-10: no qualitative AC -> owner_review -> accepted at once", ok, r.stderr[-300:])
        doc = contract(acs=[quant_ac("AC-1"), quant_ac("AC-2"), quant_ac("AC-3"), qual_ac()])
        for ac in ("AC-2", "AC-3"):
            set_value(sb, ac, "1")
        cases = {"pass": (["合格"], None), "fail": (["不合格"], None), "forged": (["合格"], ["不合格"])}
        for case, (said, real) in cases.items():
            unit_id = to_owner_review(sb, doc, builder=f"bld-q{case}")
            question = json.loads(cli(sb, "unit", "show", unit_id, "--ask", "qual").stdout)
            answer(sb, question, said, f"qual-{case}", transcript_answers=real)
            r = cli(sb, "unit", "advance", unit_id, "accepted")
            state = unit(sb, unit_id)["state"]
            if case == "pass":
                ok = r.returncode == 0 and state == "accepted"
                label = "all qualitative ACs 合格 (owner's tokens) -> accepted"
            elif case == "fail":
                back = cli(sb, "unit", "advance", unit_id, "build", "--reason", "AC-Q 不合格")
                ok = (r.returncode != 0 and "AC-Q: 不合格" in r.stderr and back.returncode == 0
                      and unit(sb, unit_id)["state"] == "build")
                label = "one 不合格 -> accepted refused; owner_review -> build allowed"
            else:
                ok = (r.returncode != 0 and "not in the session transcript" in r.stderr
                      and state == "owner_review")
                label = "a PostToolUse claiming 合格 while the transcript says 不合格 -> refused"
            expected += ok
            p.check(f"AC-10: {label}", ok, r.stderr[-400:])
        p.check(f"AC-10: as expected {expected}/4", expected == 4)
    finally:
        sb.cleanup()


def main() -> int:
    p = Probe()
    print("\n== AC-8: self loop, budget and stall ==")
    check_loops(p)
    print("\n== AC-9: reviewer gate ==")
    check_reviewer_gate(p)
    print("\n== AC-10: qualitative judgement ==")
    check_qualitative(p)
    return p.result()


if __name__ == "__main__":
    sys.exit(main())
