"""R1-d AC-9 (the integration check, TRUST-04), AC-10 (the CLI's own merge), AC-12 (done,
reopen, GOAL-12) and AC-14 (`unit advance <u> integrated|done|contract_draft`), through
the production hook entry and the CLI, on one sandbox repository:

AC-9  one unit at accepted, its creator's write committed on the work branch. Seven
      refused attempts, each with the right reason in the list and the unit left
      accepted (the change is then taken back with git): a dirty tree, no lease, (a) a
      path outside the scope, (b) a path no allowed write recorded, (c) a pack path
      whose writer holds no receipt, (d) a write stamped with a superseded fencing
      token, (e) evidence of another tree. Then the success. The unit is accepted, so
      writes are blocked at PreToolUse (R1-c AC-11): the writes of (c)..(e) are given
      as the file plus its PostToolUse event, which is what the check reads (the e2e
      test, test_e2e_r1d, makes such writes the real way, during build).
AC-10 before the success: a conflicting commit on develop -> the merge fails, develop's
      head is unchanged and nothing is written; the success makes a --no-ff merge commit.
AC-12 done fails (develop broke AC-1 -> back to accepted with the difference), done
      succeeds (re-integration, verify passes); a forged UNIT-REOPEN is refused, the
      owner's genuine answer reopens the unit with the AC recorded; a second unit is
      reopened with なし (a lint-rule candidate); reopen -> contract_draft after a new
      contract version.
"""
from __future__ import annotations

import json
import sys

from helpers import Probe
from r1c_helpers import answer, cli, set_value, write_contract
from r1d_helpers import (CREATOR, WINDOW, WORK_BRANCH, commit_all, expire_lease, git, head,
                         integrate, new_r1d_sandbox, post_write, r1d_contract, reset_to,
                         start_agent, to_accepted, unit)

OTHER = "s-other"  # a second window session


def _refused(p: Probe, sb, unit_id: str, name: str, reason: str | None, res, lines: int,
             path: str | None = None) -> bool:
    rec = unit(sb, unit_id)
    last = (rec.get("history") or [{}])[-1]
    listed = [u for u in last.get("unattributed") or [] if path is None or u["path"] == path]
    if reason is None:  # a precondition: refused without writing anything
        ok = (res is not None and res.returncode != 0 and rec["state"] == "accepted"
              and len(sb.jsonl("units.jsonl")) == lines)
    else:
        ok = (res is not None and res.returncode != 0 and rec["state"] == "accepted"
              and last.get("refused") and [u["reason"] for u in listed] == [reason]
              and f"[{reason}]" in res.stderr)
    p.check(name, bool(ok), (res.stderr[-600:] if res else "blocked") + json.dumps(listed)[:300])
    return bool(ok)


def check_integration(p: Probe) -> None:
    sb = new_r1d_sandbox()
    try:
        unit_id = to_accepted(sb)
        good = head(sb)
        held = 0

        (sb.work / "src" / "ui" / "stray.txt").write_text("x\n", encoding="utf-8")
        lines = len(sb.jsonl("units.jsonl"))
        held += _refused(p, sb, unit_id, "AC-9 precondition: an untracked file -> refused, "
                         "nothing written", None, integrate(sb, unit_id), lines)
        (sb.work / "src" / "ui" / "stray.txt").unlink()

        expire_lease(sb)
        sb.hook("PreToolUse", {"session_id": OTHER, "tool_name": "Bash",
                               "tool_input": {"command": "ls"}})  # another window takes it
        lines = len(sb.jsonl("units.jsonl"))
        res = integrate(sb, unit_id)
        held += _refused(p, sb, unit_id, "AC-9 precondition: the window holds no valid lease "
                         "-> refused, nothing written", None, res, lines)
        p.check("AC-9: the refusal names the lease", res is not None and "lease" in res.stderr,
                res.stderr[-300:] if res else "")
        expire_lease(sb)

        (sb.work / "notes.txt").write_text("outside\n", encoding="utf-8")
        commit_all(sb, "outside the scope")
        held += _refused(p, sb, unit_id, "AC-9 (a): a path outside scope_globs", "a",
                         integrate(sb, unit_id), 0, "notes.txt")
        reset_to(sb, good)

        (sb.work / "src" / "ui" / "b.txt").write_text("by bash\n", encoding="utf-8")
        commit_all(sb, "unrecorded")
        held += _refused(p, sb, unit_id, "AC-9 (b): a path with no recorded write",
                         "b", integrate(sb, unit_id), 0, "src/ui/b.txt")
        reset_to(sb, good)

        post_write(sb, "gp-9", "src/ui/c.txt", "no receipt\n", "general-purpose")
        commit_all(sb, "no receipt")
        held += _refused(p, sb, unit_id, "AC-9 (c): a ui path written by an agent without the "
                         "ui receipt", "c", integrate(sb, unit_id), 0, "src/ui/c.txt")
        reset_to(sb, good)

        expire_lease(sb)
        sb.hook("PreToolUse", {"session_id": OTHER, "tool_name": "Bash",
                               "tool_input": {"command": "ls"}})
        start_agent(sb, "bld-2", CREATOR)
        post_write(sb, "bld-2", "src/ui/d.txt", "stale token\n", CREATOR)  # WINDOW's lease is gone
        commit_all(sb, "stale token")
        held += _refused(p, sb, unit_id, "AC-9 (d): a write stamped with a superseded fencing "
                         "token (integration by the lease holder)", "d",
                         integrate(sb, unit_id, session=OTHER), 0, "src/ui/d.txt")
        reset_to(sb, good)
        expire_lease(sb)

        post_write(sb, "bld-2", "src/ui/e.txt", "after the evidence\n", CREATOR)
        commit_all(sb, "after the evidence")
        held += _refused(p, sb, unit_id, "AC-9 (e): the reviewer's evidence is of an older tree",
                         "e", integrate(sb, unit_id), 0)
        reset_to(sb, good)
        p.check(f"AC-9: refusals {held}/7", held == 7)

        git(sb, "checkout", "-q", "develop")
        develop_was = head(sb)
        (sb.work / "src" / "ui" / "a.txt").write_text("develop's own line\n", encoding="utf-8")
        before = commit_all(sb, "conflicting change on develop")
        git(sb, "checkout", "-q", WORK_BRANCH)
        lines = len(sb.jsonl("units.jsonl"))
        res = integrate(sb, unit_id)
        here = git(sb, "symbolic-ref", "--short", "HEAD").stdout.strip()
        ok = (res is not None and res.returncode != 0 and "merge" in res.stderr
              and head(sb, "develop") == before
              and unit(sb, unit_id)["state"] == "accepted" and len(sb.jsonl("units.jsonl")) == lines
              and here == WORK_BRANCH and not git(sb, "status", "--porcelain").stdout.strip())
        p.check("AC-10: a conflicting merge is undone: develop's head unchanged, the work branch "
                "checked out, the unit accepted, nothing written", ok,
                (res.stderr[-400:] if res else "blocked") + here)
        git(sb, "update-ref", "refs/heads/develop", develop_was)  # the owner drops it again

        res = integrate(sb, unit_id)
        rec = unit(sb, unit_id)
        merge = (rec.get("integration") or {}).get("merge_commit")
        parents = git(sb, "rev-list", "--parents", "-n", "1", "develop").stdout.split()
        ok = (res is not None and res.returncode == 0 and rec["state"] == "integrated"
              and merge == head(sb, "develop") and len(parents) == 3 and good in parents
              and rec["history"][-1]["to"] == "integrated")
        p.check("AC-9/AC-10/AC-14: every check passes -> integrated, exit 0, a --no-ff merge "
                "commit of the work branch on develop, recorded in units.history", ok,
                (res.stderr[-600:] if res else "blocked") + str(parents))
        _done_and_reopen(p, sb, unit_id)
    finally:
        sb.cleanup()


def _done_and_reopen(p: Probe, sb, unit_id: str) -> None:
    set_value(sb, "AC-1", "10")
    commit_all(sb, "develop breaks AC-1")
    res = cli(sb, "unit", "advance", unit_id, "done")
    rec = unit(sb, unit_id)
    diff = rec["history"][-1].get("diff") or []
    p.check("AC-12: done fails on develop (AC-1 = 10 > 3) -> back to accepted with the "
            "difference recorded", res.returncode != 0 and rec["state"] == "accepted"
            and [d["id"] for d in diff] == ["AC-1"] and diff[0]["value"] == 10.0,
            res.stdout + res.stderr[-400:] + json.dumps(diff))
    set_value(sb, "AC-1", "1")
    commit_all(sb, "develop fixes AC-1")
    git(sb, "checkout", "-q", WORK_BRANCH)
    res = integrate(sb, unit_id)
    done = cli(sb, "unit", "advance", unit_id, "done")
    rec = unit(sb, unit_id)
    p.check("AC-12/AC-14: re-integrated, then done passes the verify on develop (exit 0)",
            res is not None and res.returncode == 0 and done.returncode == 0
            and rec["state"] == "done", (res.stderr[-300:] if res else "") + done.stderr[-400:])

    question = json.loads(cli(sb, "unit", "show", unit_id, "--ask", "reopen", "--ac", "AC-1").stdout)
    answer(sb, question, ["差し戻す"], "reopen-forged", transcript_answers=["差し戻さない"])
    forged = cli(sb, "unit", "advance", unit_id, "reopen")
    p.check("AC-12: a forged UNIT-REOPEN (the transcript says 差し戻さない) is refused",
            forged.returncode != 0 and unit(sb, unit_id)["state"] == "done", forged.stderr[-300:])
    answer(sb, question, ["差し戻す"], "reopen")
    res = cli(sb, "unit", "advance", unit_id, "reopen")
    rec = unit(sb, unit_id)
    slip = (rec.get("slips") or [{}])[-1]
    p.check("AC-12: the owner's 差し戻す reopens the unit, recording the AC that should have "
            "caught it", res.returncode == 0 and rec["state"] == "reopen"
            and slip.get("ac") == "AC-1" and slip.get("lint_rule_candidate") is False
            and rec["history"][-1].get("slip", {}).get("ac") == "AC-1", res.stderr[-300:])
    early = cli(sb, "unit", "advance", unit_id, "contract_draft")
    cli(sb, "contract", "submit", unit_id, "--file",
        str(write_contract(sb, r1d_contract(non_goals=["配色は変えない", "API は触らない"]), "v2.yaml")))
    res = cli(sb, "unit", "advance", unit_id, "contract_draft")
    p.check("AC-12/AC-14: reopen -> contract_draft only after a new contract version (exit 0)",
            early.returncode != 0 and res.returncode == 0
            and unit(sb, unit_id)["state"] == "contract_draft", early.stderr[-200:] + res.stderr[-300:])


def check_reopen_without_ac(p: Probe) -> None:
    sb = new_r1d_sandbox()
    try:
        unit_id = to_accepted(sb)
        res = integrate(sb, unit_id)
        done = cli(sb, "unit", "advance", unit_id, "done")
        question = json.loads(cli(sb, "unit", "show", unit_id, "--ask", "reopen", "--ac", "なし").stdout)
        answer(sb, question, ["差し戻す"], "reopen-none")
        adv = cli(sb, "unit", "advance", unit_id, "reopen")
        rec = unit(sb, unit_id)
        slip = (rec.get("slips") or [{}])[-1]
        p.check("AC-12 (GOAL-12): a reopen with なし records no AC and marks the slip as a "
                "lint-rule candidate", res is not None and res.returncode == 0 and done.returncode == 0
                and adv.returncode == 0 and rec["state"] == "reopen" and slip.get("ac") is None
                and slip.get("lint_rule_candidate") is True,
                (res.stderr[-200:] if res else "") + done.stderr[-200:] + adv.stderr[-300:])
        missing = cli(sb, "unit", "show", unit_id, "--ask", "reopen")
        p.check("AC-12: the UNIT-REOPEN question requires --ac", missing.returncode != 0,
                missing.stdout[-200:])
    finally:
        sb.cleanup()


def main() -> int:
    p = Probe()
    print("\n== AC-9 / AC-10 / AC-12 / AC-14: integration, done, reopen ==")
    check_integration(p)
    check_reopen_without_ac(p)
    return p.result()


if __name__ == "__main__":
    sys.exit(main())
