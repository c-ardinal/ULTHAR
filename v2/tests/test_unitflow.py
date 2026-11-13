"""R1-c unit process:
  AC-1  `issue add` / `unit new` write issues.jsonl / units.jsonl through the store; a unit
        of a missing issue is refused;
  AC-4  `review record` records only with a hook-issued runner token of a subagent;
  AC-5  the CONTRACT-CONFIRM answer issues a contract_confirm token only for 確定 on the
        recorded question with the transcript's tool_use; a forged answer is unusable;
  AC-6  every transition of process/unit.yaml (intake .. accepted, returns, halted) with
        its preconditions, transitions outside the table, injected failures (nothing
        written), and `unit advance` blocked for subagents.
"""
from __future__ import annotations

import json
import sqlite3
import sys

from helpers import Probe
from r1c_helpers import (answer, cli, confirm_contract, contract, make_unit, new_sandbox,
                         runner, to_quant_verified, unit, units_lines, write_contract)


def check_issue_unit(p: Probe) -> None:
    sb = new_sandbox()
    try:
        issue = cli(sb, "issue", "add", "--title", "検索を速くする")
        issue_id = issue.stdout.split("\t")[0]
        new = cli(sb, "unit", "new", "--issue", issue_id)
        unit_id = new.stdout.split("\t")[0]
        issues, units = sb.jsonl("issues.jsonl"), sb.jsonl("units.jsonl")
        made = (issue.returncode == 0 and new.returncode == 0 and len(issues) == 1
                and issues[0]["title"] == "検索を速くする" and len(units) == 1
                and units[0]["state"] == "intake" and units[0]["issue_id"] == issue_id
                and units[0]["unit_id"] == unit_id)
        seqs = [r["seq"] for r in issues + units]
        chained = cli(sb, "store", "verify")
        p.check("AC-1: issue add + unit new wrote issues.jsonl / units.jsonl (state=intake) "
                "2/2, through the store (seq rising, chain verified)",
                made and seqs == sorted(set(seqs)) and chained.returncode == 0,
                f"{issue.stdout}{issue.stderr}{new.stdout}{new.stderr}{chained.stdout}")
        bad = cli(sb, "unit", "new", "--issue", "I-000000000000")
        p.check("AC-1: a unit of a missing issue is refused 1/1 (nothing written)",
                bad.returncode != 0 and "does not exist" in bad.stderr
                and len(sb.jsonl("units.jsonl")) == 1, bad.stderr[-300:])
    finally:
        sb.cleanup()


def check_review_record(p: Probe) -> None:
    sb = new_sandbox()
    try:
        unit_id = make_unit(sb)
        cli(sb, "contract", "submit", unit_id, "--file", str(write_contract(sb, contract())))
        args = ["review", "record", "--unit", unit_id, "--kind", "contract", "--result", "pass"]
        pre, ok = runner(sb, "rev-1", args)
        reviews = unit(sb, unit_id).get("reviews") or []
        p.check("AC-4: the reviewer subagent's record: the hook added --runner-token, the CLI "
                "recorded the reviewer's agent_id 1/1",
                ok is not None and ok.returncode == 0 and len(reviews) == 1
                and reviews[0]["agent_id"] == "rev-1" and "--runner-token" in pre.stdout,
                f"{pre.stdout[-300:]} {ok.stderr[-300:] if ok else ''}")
        token = reviews[0]["token"] if reviews else "-"
        refused = 0
        no_token = cli(sb, *args)
        refused += no_token.returncode != 0 and "no runner token" in no_token.stderr
        reuse = cli(sb, *args, "--runner-token", token)
        refused += reuse.returncode != 0 and "already used" in reuse.stderr
        _, main = runner(sb, None, args)
        refused += main is not None and main.returncode != 0 and "no runner token" in main.stderr
        hand_pre, hand = runner(sb, "rev-1", args, hand_token="0" * 32)
        refused += hand_pre.returncode == 2 and hand is None and "--runner-token" in hand_pre.stderr
        after = unit(sb, unit_id).get("reviews") or []
        p.check(f"AC-4: refused (no token, reused token, main session, hand-written "
                f"--runner-token) {refused}/4, records still 1",
                refused == 4 and len(after) == 1,
                "\n".join(x.stderr[-200:] for x in (no_token, reuse, main or no_token, hand_pre)))
    finally:
        sb.cleanup()


def _tokens(sb, kind: str) -> list:
    return sb.query("SELECT token_id, bound, consumed_at FROM tokens WHERE kind=?", (kind,))


def check_confirm_token(p: Probe) -> None:
    sb = new_sandbox()
    try:
        unit_id = make_unit(sb)
        cli(sb, "contract", "submit", unit_id, "--file", str(write_contract(sb, contract())))
        detail = confirm_contract(sb, unit_id)
        rows = _tokens(sb, "contract_confirm")
        bound = json.loads(rows[0][1]) if rows else {}
        issued = (len(rows) == 1 and bound.get("unit_id") == unit_id
                  and bound.get("contract_sha") == unit(sb, unit_id)["contract"]["sha"]
                  and bound["question_event_id"] < bound["answer_event_id"])
        p.check("AC-5: 確定 -> one contract_confirm token bound to (project, unit, sha, question "
                "and answer events) 1/1", issued, f"{detail} {rows}")
        not_issued = 0
        question = json.loads(cli(sb, "contract", "show", unit_id).stdout)
        answer(sb, question, ["修正"], "revise")
        not_issued += len(_tokens(sb, "contract_confirm")) == 1
        stale = json.loads(cli(sb, "contract", "show", unit_id).stdout)
        doc2 = contract(non_goals=["配色は変えない", "文言は変えない"])
        cli(sb, "contract", "submit", unit_id, "--file", str(write_contract(sb, doc2, "c2.yaml")))
        d = answer(sb, stale, ["確定"], "stale")
        not_issued += len(_tokens(sb, "contract_confirm")) == 1 and "not issued" in d.get(
            "unit_question", "")
        fresh = json.loads(cli(sb, "contract", "show", unit_id).stdout)
        d = answer(sb, fresh, ["確定"], "fake", in_transcript=False)
        not_issued += len(_tokens(sb, "contract_confirm")) == 1 and "transcript" in d.get(
            "unit_question", "")
        p.check(f"AC-5: no token for 修正, a stale sha, a PostToolUse with no transcript "
                f"record: {not_issued}/3", not_issued == 3, str(_tokens(sb, "contract_confirm")))
        runner(sb, "rev-1", ["review", "record", "--unit", unit_id, "--kind", "contract",
                             "--result", "pass"])
        fresh = json.loads(cli(sb, "contract", "show", unit_id).stdout)
        qid = sb.query("SELECT qid FROM questions ORDER BY rowid DESC LIMIT 1")[0][0]
        answer(sb, fresh, ["確定"], "lie", transcript_answers=["修正"])
        status = sb.query("SELECT status FROM questions WHERE qid=?", (qid,))[0][0]
        before = units_lines(sb)
        adv = cli(sb, "unit", "advance", unit_id, "contract_confirmed")
        p.check("AC-5: a PostToolUse claiming 確定 while the transcript says 修正 -> refused at "
                "the transition, nothing written; the question stays `asked` (refused 4/4)",
                adv.returncode != 0 and "not in the session transcript" in adv.stderr
                and units_lines(sb) == before and status == "asked", adv.stderr[-400:] + status)
        answer(sb, fresh, ["確定"], "genuine")
        adv = cli(sb, "unit", "advance", unit_id, "contract_confirmed")
        p.check("AC-5: ... the owner's genuine 確定 to the same question afterwards -> the "
                "transition succeeds 1/1 (the forged one did not block it)",
                adv.returncode == 0 and unit(sb, unit_id)["state"] == "contract_confirmed",
                adv.stderr[-400:])
    finally:
        sb.cleanup()


def _advance(sb, unit_id: str, to: str, *extra: str):
    return cli(sb, "unit", "advance", unit_id, to, *extra)


def check_transitions(p: Probe) -> None:
    sb = new_sandbox()
    try:
        unit_id = make_unit(sb)
        sub = cli(sb, "contract", "submit", unit_id, "--file", str(write_contract(sb, contract())))
        p.check("AC-6: intake -> contract_draft (contract submit)",
                sub.returncode == 0 and unit(sb, unit_id)["state"] == "contract_draft", sub.stderr)
        r = _advance(sb, unit_id, "contract_confirmed")
        p.check("AC-6: contract_draft -> contract_confirmed refused without review and token",
                r.returncode != 0 and "contract_review_passed" in r.stderr
                and "contract_confirm_token" in r.stderr, r.stderr[-400:])
        runner(sb, "rev-1", ["review", "record", "--unit", unit_id, "--kind", "contract",
                             "--result", "pass"])
        r = _advance(sb, unit_id, "contract_confirmed")
        p.check("AC-6:   ... refused with the review but no contract_confirm token",
                r.returncode != 0 and "contract_confirm_token" in r.stderr
                and "contract_review_passed" not in r.stderr, r.stderr[-400:])
        question = json.loads(cli(sb, "contract", "show", unit_id).stdout)
        answer(sb, question, ["確定"], "ok")
        r = _advance(sb, unit_id, "build")
        p.check("AC-6: a transition outside the table (contract_draft -> build) is refused",
                r.returncode != 0 and "no transition" in r.stderr, r.stderr[-300:])
        r = _advance(sb, unit_id, "contract_confirmed")
        state = unit(sb, unit_id)
        p.check("AC-6: contract_draft -> contract_confirmed with lint, review and token; the "
                "token consumed and named in the history; scope written for the hook",
                r.returncode == 0 and state["state"] == "contract_confirmed"
                and state["confirmed"]["scope_globs"] == ["src/**"]
                and state["history"][-1]["tokens"]
                and all(t[2] for t in _tokens(sb, "contract_confirm")), r.stderr[-300:])
        r = _advance(sb, unit_id, "build")
        p.check("AC-6: contract_confirmed -> build (budget.loops set)",
                r.returncode == 0 and unit(sb, unit_id)["state"] == "build", r.stderr[-300:])
        r = _advance(sb, unit_id, "quant_verified")
        p.check("AC-6: build -> quant_verified refused without a passing `verify --self`",
                r.returncode != 0 and "self_verify_all_quant_passed" in r.stderr, r.stderr[-300:])
        r = _advance(sb, unit_id, "halted")
        p.check("AC-6: build -> halted by `unit advance` is refused (verify --self only)",
                r.returncode != 0 and "no transition" in r.stderr, r.stderr[-300:])
        runner(sb, "bld-1", ["verify", unit_id, "--self"])
        r = _advance(sb, unit_id, "quant_verified")
        p.check("AC-6: build -> quant_verified after a passing `verify --self` (evidence named)",
                r.returncode == 0 and unit(sb, unit_id)["history"][-1]["evidence_ref"],
                r.stderr[-300:])
        r = _advance(sb, unit_id, "build")
        p.check("AC-6: return quant_verified -> build refused without --reason",
                r.returncode != 0 and "reason_given" in r.stderr, r.stderr[-300:])
        r = _advance(sb, unit_id, "build", "--reason", "評価役の指摘で作り直す")
        p.check("AC-6: return quant_verified -> build with a reason",
                r.returncode == 0 and unit(sb, unit_id)["history"][-1]["reason"], r.stderr[-300:])
        runner(sb, "bld-1", ["verify", unit_id, "--self"])
        _advance(sb, unit_id, "quant_verified")
        r = _advance(sb, unit_id, "contract_draft", "--reason", "AC が検証できない")
        p.check("AC-6: return quant_verified -> contract_draft with a reason",
                r.returncode == 0 and unit(sb, unit_id)["state"] == "contract_draft", r.stderr[-300:])
    finally:
        sb.cleanup()


def check_small_refusals(p: Probe) -> None:
    sb = new_sandbox()
    try:
        doc = contract(budget={"loops": None})
        unit_id = make_unit(sb)
        cli(sb, "contract", "submit", unit_id, "--file", str(write_contract(sb, doc)))
        confirm_contract(sb, unit_id)
        _advance(sb, unit_id, "contract_confirmed")
        r = _advance(sb, unit_id, "build")
        p.check("AC-6: contract_confirmed -> build refused while budget.loops is unset",
                r.returncode != 0 and "budget_loops_set" in r.stderr, r.stderr[-300:])
        lint_bad = make_unit(sb)
        cli(sb, "contract", "submit", lint_bad, "--file",
            str(write_contract(sb, contract(non_goals=[]), "c-lint.yaml")))
        confirm_contract(sb, lint_bad)
        r = _advance(sb, lint_bad, "contract_confirmed")
        p.check("AC-6: contract_draft -> contract_confirmed refused when the lint fails",
                r.returncode != 0 and "lint_passed" in r.stderr and "LINT-E" in r.stderr,
                r.stderr[-300:])
        with sb.inproc(), sb.store().transaction({"hook": "test-setup"}) as tx:
            orphan = tx.add("unit", {"id": "U-0000000000aa", "unit_id": "U-0000000000aa",
                                     "issue_id": "I-missing", "state": "intake",
                                     "contract_ref": None, "history": []})
        r = cli(sb, "contract", "submit", orphan["id"], "--file", str(write_contract(sb, contract())))
        versions = list((sb.kr / ".ulthar" / "contracts" / orphan["id"]).glob("*.yaml"))
        p.check("AC-6: intake -> contract_draft refused when the issue does not exist "
                "(no contract version left behind)",
                r.returncode != 0 and "issue_exists" in r.stderr and not versions,
                r.stderr[-300:] + str(versions))
    finally:
        sb.cleanup()


def check_owner_review_return(p: Probe) -> None:
    sb = new_sandbox()
    try:
        unit_id = to_quant_verified(sb)
        runner(sb, "rev-1", ["verify", unit_id])
        runner(sb, "rev-1", ["review", "record", "--unit", unit_id, "--kind", "verify",
                             "--result", "pass"])
        r = _advance(sb, unit_id, "owner_review")
        p.check("AC-6: quant_verified -> owner_review (AC-9 conditions met)",
                r.returncode == 0 and unit(sb, unit_id)["state"] == "owner_review", r.stderr[-400:])
        r = _advance(sb, unit_id, "build")
        p.check("AC-6: return owner_review -> build refused without --reason",
                r.returncode != 0 and "reason_given" in r.stderr, r.stderr[-300:])
        r = _advance(sb, unit_id, "build", "--reason", "定性 AC が不合格")
        p.check("AC-6: return owner_review -> build with a reason",
                r.returncode == 0 and unit(sb, unit_id)["state"] == "build", r.stderr[-300:])
    finally:
        sb.cleanup()


class _FailCommit:
    """A runtime.db connection whose next COMMIT fails (the injected crash point)."""

    def __init__(self, conn):
        self._conn, self.armed = conn, False  # armed by flow.atomic (see _inject)

    def execute(self, sql, *args):
        if self.armed and sql.strip().upper() == "COMMIT":
            self.armed = False
            raise sqlite3.OperationalError("injected: runtime.db COMMIT failed")
        return self._conn.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _inject(sb, unit_id: str, to: str, name: str, wrap=None, submit: bytes | None = None) -> str:
    from ulthar import db as db_mod  # noqa: PLC0415
    from ulthar import flow  # noqa: PLC0415

    real_atomic = flow.atomic

    def arming_atomic(store, rt, writer, body):
        rt.armed = True  # the transition's own runtime.db COMMIT, not an earlier one
        return real_atomic(store, rt, writer, body)

    with sb.inproc():
        conn = db_mod.connect(sb.db)
        rt = wrap(conn) if wrap else conn
        if wrap:
            flow.atomic = arming_atomic
        try:
            if submit is not None:
                flow.submit(sb.store(), rt, unit_id, submit, actor={"test": name})
            else:
                flow.advance(sb.store(), rt, unit_id, to, reason=None, cause=None,
                             actor={"test": name})
            return "no error"
        except Exception as exc:  # noqa: BLE001 -- the injected failure
            return str(exc)
        finally:
            flow.atomic = real_atomic
            conn.close()


def check_atomic(p: Probe) -> None:
    """AC-6: failures injected at the 3 points -- before the store commit, at the
    runtime.db COMMIT, at the store's finalize -- leave, once the unit is opened again,
    either everything applied (record, history, token consumed) or nothing; never half."""
    from ulthar import flow  # noqa: PLC0415
    from ulthar import integrity  # noqa: PLC0415
    from ulthar import store as store_mod  # noqa: PLC0415

    sb = new_sandbox()
    try:
        unit_id = make_unit(sb)
        cli(sb, "contract", "submit", unit_id, "--file", str(write_contract(sb, contract())))
        confirm_contract(sb, unit_id)
        before_lines = units_lines(sb)
        real_apply, real_manifest = flow.apply_op, integrity.write_manifest

        def after_consume(rt, project_id, uid, op, strict):
            real_apply(rt, project_id, uid, op, strict)
            if strict:
                raise RuntimeError("injected: crash after the token was consumed")

        def no_manifest(*a, **k):
            raise OSError("injected: the store's write-ahead manifest cannot be written")

        errors = {}
        flow.apply_op = after_consume
        try:
            errors["inside the operation"] = _inject(sb, unit_id, "contract_confirmed", "a1")
        finally:
            flow.apply_op = real_apply
        integrity.write_manifest = no_manifest
        try:
            errors["at the store commit"] = _inject(sb, unit_id, "contract_confirmed", "a2")
        finally:
            integrity.write_manifest = real_manifest
        state = unit(sb, unit_id)  # the next open
        unconsumed = [t for t in _tokens(sb, "contract_confirm") if t[2] is None]
        p.check("AC-6: failures before the store commit (inside the operation, at the store "
                "commit) -> after the next open nothing is applied: no record, token unused",
                all("injected" in v for v in errors.values())
                and units_lines(sb) == before_lines and state["state"] == "contract_draft"
                and len(unconsumed) == 1 and cli(sb, "store", "verify").returncode == 0,
                f"{errors} lines {units_lines(sb)}/{before_lines} {state['state']}")

        err = _inject(sb, unit_id, "contract_confirmed", "b", wrap=_FailCommit)
        half = (units_lines(sb) == before_lines + 1  # the store committed, runtime.db did not
                and len([t for t in _tokens(sb, "contract_confirm") if t[2] is None]) == 1)
        reopened = unit(sb, unit_id)  # `unit show`: the next open recovers
        token = reopened["history"][-1].get("tokens") or ["-"]
        consumed = sb.query("SELECT consumed_at FROM tokens WHERE token_id=?", (token[0],))
        p.check("AC-6: failure at the runtime.db COMMIT -> after the next open everything is "
                "applied (state, history, the token consumed), store chain intact",
                "injected" in err and half and reopened["state"] == "contract_confirmed"
                and units_lines(sb) == before_lines + 1 and consumed and consumed[0][0]
                and cli(sb, "store", "verify").returncode == 0
                and not (sb.kr / ".ulthar" / "txn.json").exists(),
                f"{err} {reopened['state']} {consumed}")

        real_finalize = store_mod.Store.finalize

        def no_finalize(self, rt=None):
            raise OSError("injected: finalize failed")

        store_mod.Store.finalize = no_finalize
        try:
            err = _inject(sb, unit_id, "build", "c")
        finally:
            store_mod.Store.finalize = real_finalize
        reopened = unit(sb, unit_id)
        p.check("AC-6: failure at the store's finalize -> after the next open the transition is "
                "applied whole (state build, chain verified, no manifest left)",
                "injected" in err and reopened["state"] == "build"
                and units_lines(sb) == before_lines + 2
                and cli(sb, "store", "verify").returncode == 0
                and not (sb.kr / ".ulthar" / "txn.json").exists(), f"{err} {reopened['state']}")
    finally:
        sb.cleanup()


def check_submit_atomic(p: Probe) -> None:
    """AC-6 for `contract submit`: before the store commit -> no unit revision and no
    contract file; at the runtime.db COMMIT or at finalize (after the store commit) ->
    after the next open, the whole revision, the invalidated questions and the exact
    contract bytes are there."""
    from ulthar import integrity  # noqa: PLC0415
    from ulthar import store as store_mod  # noqa: PLC0415

    sb = new_sandbox()
    try:
        unit_id = make_unit(sb)
        cli(sb, "contract", "submit", unit_id, "--file", str(write_contract(sb, contract())))
        folder = sb.kr / ".ulthar" / "contracts" / unit_id

        def asked(version: int) -> list:
            cli(sb, "contract", "show", unit_id)  # an open question for this version
            return sb.query("SELECT qid FROM questions WHERE unit_id=? AND"
                            " json_extract(payload, '$.version')=?", (unit_id, version))

        def invalidated(qids) -> bool:
            return all(sb.query("SELECT json_extract(payload, '$.invalidated') FROM questions"
                                " WHERE qid=?", q)[0][0] for q in qids)

        v1_questions = asked(1)
        lines = units_lines(sb)
        data = {v: write_contract(sb, contract(non_goals=[f"版 {v} の外"]), f"s{v}.yaml").read_bytes()
                for v in (2, 3)}

        def no_manifest(*a, **k):
            raise OSError("injected: the store's write-ahead manifest cannot be written")

        integrity.write_manifest, real_manifest = no_manifest, integrity.write_manifest
        try:
            err = _inject(sb, unit_id, "", "s1", submit=data[2])
        finally:
            integrity.write_manifest = real_manifest
        rec = unit(sb, unit_id)
        p.check("AC-6 submit: failure before the store commit -> no unit revision, no contract "
                "file, the old questions untouched",
                "injected" in err and units_lines(sb) == lines and rec["contract"]["version"] == 1
                and not (folder / "v2.yaml").exists() and not invalidated(v1_questions),
                f"{err} {rec['contract']}")

        err = _inject(sb, unit_id, "", "s2", wrap=_FailCommit, submit=data[2])
        rec = unit(sb, unit_id)  # the next open recovers
        p.check("AC-6 submit: failure at the runtime.db COMMIT -> after the next open the "
                "revision (v2), the invalidations and the exact contract bytes are there",
                "injected" in err and rec["contract"]["version"] == 2
                and (folder / "v2.yaml").read_bytes() == data[2] and invalidated(v1_questions)
                and cli(sb, "contract", "lint", unit_id).returncode == 0,
                f"{err} {rec['contract']} v2 exists={(folder / 'v2.yaml').exists()}")

        v2_questions = asked(2)
        real_finalize = store_mod.Store.finalize

        def no_finalize(self, rt=None):
            raise OSError("injected: finalize failed")

        store_mod.Store.finalize = no_finalize
        try:
            err = _inject(sb, unit_id, "", "s3", submit=data[3])
        finally:
            store_mod.Store.finalize = real_finalize
        rec = unit(sb, unit_id)
        p.check("AC-6 submit: failure at finalize -> after the next open the revision (v3), "
                "the invalidations and the exact contract bytes are there, chain verified",
                "injected" in err and rec["contract"]["version"] == 3
                and (folder / "v3.yaml").read_bytes() == data[3] and invalidated(v2_questions)
                and cli(sb, "store", "verify").returncode == 0
                and not (sb.kr / ".ulthar" / "txn.json").exists(),
                f"{err} {rec['contract']} v3 exists={(folder / 'v3.yaml').exists()}")
    finally:
        sb.cleanup()


def check_agent_blocked(p: Probe) -> None:
    sb = new_sandbox()
    try:
        unit_id = make_unit(sb)
        pre = sb.hook("PreToolUse", {"session_id": "r1c", "agent_id": "bld-1",
                                     "agent_type": "ulthar-creator", "tool_name": "Bash",
                                     "tool_input": {"command": f"ulthar unit advance {unit_id} build"}})
        main = sb.hook("PreToolUse", {"session_id": "r1c", "tool_name": "Bash",
                                      "tool_input": {"command": f"ulthar unit advance {unit_id} build"}})
        p.check("AC-6: `ulthar unit advance` from a subagent is blocked; the window's is not",
                pre.returncode == 2 and "unit advance" in pre.stderr and main.returncode == 0,
                pre.stderr[-300:] + main.stderr[-300:])
    finally:
        sb.cleanup()


def main() -> int:
    p = Probe()
    print("\n== AC-1: issues and units ==")
    check_issue_unit(p)
    print("\n== AC-4: reviewer records ==")
    check_review_record(p)
    print("\n== AC-5: contract confirm token ==")
    check_confirm_token(p)
    print("\n== AC-6: state machine ==")
    check_transitions(p)
    check_small_refusals(p)
    check_owner_review_return(p)
    check_atomic(p)
    check_submit_atomic(p)
    check_agent_blocked(p)
    return p.result()


if __name__ == "__main__":
    sys.exit(main())
