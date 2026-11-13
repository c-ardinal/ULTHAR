"""Issues, units and the unit process (ISS-01, GOAL-06/07/09/11, R1-c AC-1/4/6..10), CLI only.

The process definition (states, transitions, precondition names) is
`ulthar/process/unit.yaml`; each precondition's check is the function of that name in
PRECONDITIONS below. A transition is taken only by `ulthar unit advance` (`by: cli`),
`ulthar contract submit` (intake -> contract_draft, `by: submit`) or `ulthar verify
--self` (build -> halted, `by: verify`); each writes the unit's new record (state and
history entry), consumes its tokens and refers to its evidence in ONE operation: a
runtime.db write transaction around one store transaction (as R1-b's ingest does) --
a failure anywhere before the commit leaves nothing written. A token a transition
consumed is also named in that history entry, so it counts as used even if only the
store's half of the operation survived.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
from pathlib import Path

from . import contract as contract_mod
from . import decide
from . import evidence as evidence_mod
from . import store as store_mod
from . import tokens

PROCESS_FILE = Path(__file__).resolve().parent / "process" / "unit.yaml"
HALT_CAUSES = ("implementation", "design", "requirement", "evaluation", "environment")
REVIEW_KINDS = ("contract", "verify")
CONTRACT_STATES = ("intake", "contract_draft", "halted")  # where a new version may be submitted
TOKEN_CHECKS = {"contract_confirm_token", "qual_judgements_passed", "resume_authorized"}
MAX_QUESTIONS = 4  # AskUserQuestion's limit per call


class FlowError(Exception):
    """A refused operation; nothing was written."""


# ---------------------------------------------------------------------------- process

def load_process() -> dict:
    yaml = contract_mod.load_yaml()
    proc = yaml.safe_load(PROCESS_FILE.read_text(encoding="utf-8"))
    states = set(proc.get("states") or [])
    for t in proc.get("transitions") or []:
        unknown = [n for n in t.get("requires") or [] if n not in PRECONDITIONS]
        if t.get("from") not in states or (t.get("to") not in states and t.get("to") != "previous"):
            raise FlowError(f"{PROCESS_FILE.name}: bad transition {t}")
        if unknown:
            raise FlowError(f"{PROCESS_FILE.name}: unknown precondition(s) {unknown} in {t}")
    return proc


def find_transition(proc: dict, unit: dict, to: str, by: str) -> dict:
    cur = unit.get("state")
    for t in proc["transitions"]:
        target = unit.get("halted_from") if t["to"] == "previous" else t["to"]
        if t["from"] == cur and target == to and t.get("by") == by:
            return t
    raise FlowError(f"no transition {cur} -> {to} for `{by}` in process/unit.yaml")


# ---------------------------------------------------------------------------- reading

def read_unit(conn, unit_id: str) -> dict:
    unit = store_mod.get(conn, "unit", unit_id)
    if unit is None:
        raise FlowError(f"unit {unit_id} does not exist")
    return unit


def contract_doc(store: store_mod.Store, ref: dict | None) -> dict:
    """The contract version `ref` points at, after checking the file's sha256."""
    if not ref:
        raise FlowError("the unit has no contract")
    data = (store.dir / ref["path"]).read_bytes()
    if evidence_mod.sha256_bytes(data) != ref["sha"]:
        raise FlowError(f"{ref['path']} differs from the submitted version (sha256 mismatch)")
    return contract_mod.parse(data)


def _current(unit: dict) -> tuple:
    """(sha, version) of the unit's latest submitted contract."""
    cur = unit.get("contract") or {}
    return cur.get("sha"), cur.get("version")


def _spent(unit: dict) -> set[str]:
    return {t for entry in unit.get("history") or [] for t in entry.get("tokens") or []}


def _latest_review(unit: dict, kind: str, sha: str | None) -> dict | None:
    found = [r for r in unit.get("reviews") or [] if r.get("kind") == kind
             and r.get("contract_sha") == sha]
    return found[-1] if found else None


def _evidence(rt, unit: dict, self_check: bool) -> tuple[int, dict, str, str, str] | None:
    """The newest evidence row (id, result, manifest_path, tree_hash, runner) of this
    kind for the unit's confirmed contract."""
    sha = (unit.get("confirmed") or {}).get("sha")
    for row_id, result, path, tree, runner in rt.execute(
            "SELECT id, result, manifest_path, tree_hash, runner_agent_id FROM evidence"
            " WHERE unit_id=? AND contract_sha=? ORDER BY id DESC", (unit["id"], sha)):
        res = json.loads(result or "{}")
        if bool(res.get("self")) == self_check:
            return row_id, res, path, tree, runner
    return None


def builders(rt, unit_id: str) -> set[str]:
    """agent_ids that wrote the unit's product files (AC-11 allowed events) or ran its
    `verify --self`."""
    out = {r[0] for r in rt.execute(
        "SELECT DISTINCT agent_id FROM events WHERE hook='PreToolUse' AND decision='allow'"
        " AND agent_id IS NOT NULL AND detail LIKE ?", (f'%"{unit_id}"%',))}
    out |= {r[0] for r in rt.execute(
        "SELECT DISTINCT runner_agent_id FROM evidence WHERE unit_id=?"
        " AND json_extract(result, '$.self')=1", (unit_id,)) if r[0]}
    return out


# ---------------------------------------------------------------------------- preconditions

class Ctx:
    """One transition's inputs and effects. Preconditions read; they record what the
    transition consumes (`consume`), sets on the unit (`fields`) and refers to (`refs`)."""

    def __init__(self, store, rt, conn, unit: dict, *, reason=None, cause=None,
                 tree=None, lint=None, halt_reason=None, doc=None):
        self.store, self.rt, self.conn, self.unit = store, rt, conn, unit
        self.reason, self.cause, self.tree, self.lint = reason, cause, tree, lint
        self.doc = doc  # the unit's current (latest submitted) contract, parsed
        self.halt_reason = halt_reason
        self.consume: list[str] = []
        self.fields: dict = {}
        self.refs: list = []
        self._reviewer = None
        self.op_parts: dict = {}  # other runtime.db effects of the operation (apply_op)

    def reviewer(self):
        if self._reviewer is None:
            self._reviewer = _evidence(self.rt, self.unit, False) or ()
        return self._reviewer


def _issue_exists(c: Ctx) -> str | None:
    return None if store_mod.get(c.conn, "issue", c.unit.get("issue_id")) else (
        f"issue {c.unit.get('issue_id')} does not exist")


def _lint_passed(c: Ctx) -> str | None:
    if c.lint is None:
        return "the contract was not linted"
    return None if not c.lint else "lint: " + "; ".join(f"{r} {m}" for r, m in c.lint)


def _contract_review_passed(c: Ctx) -> str | None:
    review = _latest_review(c.unit, "contract", (c.unit.get("contract") or {}).get("sha"))
    if review is None:
        return "no reviewer's contract review for this contract version"
    return None if review["result"] == "pass" else "the reviewer's contract review is fail"


def _contract_confirm_token(c: Ctx) -> str | None:
    cur = c.unit.get("contract") or {}
    good, bad = tokens.valid_owner_tokens(c.rt, c.conn, c.store.project_id, c.unit["id"],
                                          "contract_confirm", _spent(c.unit))
    good = [(t, b) for t, b in good if (b.get("contract_sha"), b.get("version"), b.get("verdict"))
            == (cur.get("sha"), cur.get("version"), "confirm")]
    if not good:
        return ("no unused contract_confirm token for this contract version and sha "
                f"(the owner's 確定 to CONTRACT-CONFIRM){'; ' + '; '.join(bad) if bad else ''}")
    if c.doc is None:
        return "the contract could not be read"
    c.consume.append(good[0][0])
    c.fields["confirmed"] = {"version": cur["version"], "sha": cur["sha"], "path": cur["path"],
                             **contract_mod.summary(c.doc)}
    return None


def _budget_loops_set(c: Ctx) -> str | None:
    loops = (c.unit.get("confirmed") or {}).get("loops")
    return None if isinstance(loops, int) and loops > 0 else "budget.loops is not set"


def _usable_evidence(c: Ctx, row, who: str) -> str | None:
    if not row:
        return f"no {who} verify evidence for the confirmed contract"
    row_id, res, path, _tree, _runner = row
    broken = evidence_mod.intact(res, path)
    if broken:
        return broken
    if not res.get("all_quant_passed"):
        failed = [k for k, v in (res.get("acs") or {}).items() if not v.get("passed")]
        return f"the latest {who} verify did not pass every quantitative AC (failed: {failed})"
    c.refs.append({"evidence_id": row_id, "manifest": res.get("run_id")})
    return None


def _self_verify_all_quant_passed(c: Ctx) -> str | None:
    return _usable_evidence(c, _evidence(c.rt, c.unit, True), "`verify --self`")


def _reviewer_verify_all_quant_passed(c: Ctx) -> str | None:
    return _usable_evidence(c, c.reviewer(), "reviewer's")


def _reviewer_independent(c: Ctx) -> str | None:
    row = c.reviewer()
    if not row:
        return "no reviewer verify evidence"
    if row[4] in builders(c.rt, c.unit["id"]):
        return (f"the reviewer {row[4]} wrote this unit's product or ran its `verify --self`: "
                "the reviewer must be another agent")
    return None


def _verify_review(c: Ctx) -> dict | None:
    return _latest_review(c.unit, "verify", (c.unit.get("confirmed") or {}).get("sha"))


def _verify_review_passed_no_blockers(c: Ctx) -> str | None:
    review = _verify_review(c)
    if review is None:
        return "no reviewer's verify review"
    if review["agent_id"] in builders(c.rt, c.unit["id"]):
        return f"the verify review's agent {review['agent_id']} is one of the unit's builders"
    if review["result"] != "pass" or review.get("blockers"):
        return f"the verify review is {review['result']} with blockers {review.get('blockers')}"
    return None


def _evidence_tree_matches(c: Ctx) -> str | None:
    row = c.reviewer()
    if not row or c.tree is None:
        return "no reviewer verify evidence to compare with the working tree"
    return None if row[3] == c.tree["tree_hash"] else (
        f"the reviewer's evidence is of tree {row[3]}, the working tree is now "
        f"{c.tree['tree_hash']}: verify again")


def _no_unmapped_changes(c: Ctx) -> str | None:
    review = _verify_review(c)
    unmapped = (review or {}).get("unmapped_changes") or []
    return None if review and not unmapped else (
        f"visible changes mapped to no AC: {unmapped} -- handle them in a new contract version"
        if unmapped else "no reviewer's verify review")


def _qual_judgements_passed(c: Ctx) -> str | None:
    confirmed = c.unit.get("confirmed") or {}
    qual = [a["id"] for a in confirmed.get("acs") or [] if a.get("kind") == "qual"]
    if not qual:
        return None
    good, bad = tokens.valid_owner_tokens(c.rt, c.conn, c.store.project_id, c.unit["id"],
                                          "qual_judgement", _spent(c.unit))
    problems = []
    for ac in qual:
        mine = [(t, b) for t, b in good if b.get("ac_id") == ac
                and (b.get("contract_sha"), b.get("version")) == _current(c.unit)
                == (confirmed.get("sha"), confirmed.get("version"))]
        if not mine or mine[0][1].get("verdict") != "pass":
            problems.append(f"{ac}: {'不合格' if mine else 'no owner judgement'}")
        else:
            c.consume.append(mine[0][0])
    return None if not problems else "qualitative ACs not passed by the owner: " + "; ".join(
        problems + bad)


def _reason_given(c: Ctx) -> str | None:
    return None if c.reason and c.reason.strip() else "a return needs --reason"


def _loops_exhausted_or_stalled(c: Ctx) -> str | None:
    return None if c.halt_reason else "neither the loop budget is used up nor a stall found"


def _halt_cause_classified(c: Ctx) -> str | None:
    return None if c.cause in HALT_CAUSES else f"--cause must be one of {HALT_CAUSES}"


def _resume_authorized(c: Ctx) -> str | None:
    good, _ = tokens.valid_owner_tokens(c.rt, c.conn, c.store.project_id, c.unit["id"],
                                        "unit_resume", _spent(c.unit))
    conf = c.unit.get("confirmed") or {}
    good = [(t, b) for t, b in good if b.get("halt_id") == c.unit.get("halt_id")
            and b.get("verdict") == "resume"
            and (b.get("contract_sha"), b.get("version")) == _current(c.unit)
            == (conf.get("sha"), conf.get("version"))]
    if good:
        c.consume.append(good[0][0])
        return None
    cur, conf = c.unit.get("contract") or {}, c.unit.get("confirmed") or {}
    if (cur.get("version") or 0) <= (conf.get("version") or 0):
        return ("no owner UNIT-RESUME token (再開) for this halt, and no new contract version "
                "to confirm")
    why = [w for w in (_lint_passed(c), _contract_review_passed(c), _contract_confirm_token(c)) if w]
    return None if not why else "the new contract version is not confirmed: " + "; ".join(why)


PRECONDITIONS = {
    "issue_exists": _issue_exists,
    "lint_passed": _lint_passed,
    "contract_review_passed": _contract_review_passed,
    "contract_confirm_token": _contract_confirm_token,
    "budget_loops_set": _budget_loops_set,
    "self_verify_all_quant_passed": _self_verify_all_quant_passed,
    "reviewer_verify_all_quant_passed": _reviewer_verify_all_quant_passed,
    "reviewer_independent": _reviewer_independent,
    "verify_review_passed_no_blockers": _verify_review_passed_no_blockers,
    "evidence_tree_matches": _evidence_tree_matches,
    "no_unmapped_changes": _no_unmapped_changes,
    "qual_judgements_passed": _qual_judgements_passed,
    "reason_given": _reason_given,
    "loops_exhausted_or_stalled": _loops_exhausted_or_stalled,
    "halt_cause_classified": _halt_cause_classified,
    "resume_authorized": _resume_authorized,
}


# ---------------------------------------------------------------------------- applying

def atomic(store: store_mod.Store, rt: sqlite3.Connection, writer: dict, body):
    """body(tx) inside a runtime.db write transaction around one store transaction.
    The store's commit is the commit point: a raise before it writes nothing. The unit
    record it commits carries the operation's runtime.db effects (`op`), so a failure of
    the runtime.db COMMIT or of the store's finalize after it is completed by the next
    open of the unit (`recover`): all applied, never half."""
    rt.execute("BEGIN IMMEDIATE")
    try:
        with store.transaction(writer, rt) as tx:
            result = body(tx)
        rt.execute("COMMIT")
    except BaseException:
        try:
            rt.execute("ROLLBACK")
        finally:
            store.release()
        raise
    store.finalize(rt)
    return result


def apply_op(rt, project_id: str, unit_id: str, op: dict | None, strict: bool) -> None:
    """A unit record's runtime.db effects: tokens consumed, evidence rows, invalidated
    questions and tokens. `strict` (the operation itself): a token already consumed is a
    refusal; otherwise (recovery) every step is idempotent."""
    if not op:
        return
    now = store_mod.now_iso()
    for token_id in op.get("consume") or []:
        cur = rt.execute("UPDATE tokens SET consumed_at=? WHERE token_id=? AND consumed_at IS NULL",
                         (now, token_id))
        if strict and cur.rowcount != 1:
            raise FlowError(f"token {token_id[:8]} was used concurrently")
    for ev in op.get("evidence") or []:
        if not rt.execute("SELECT 1 FROM evidence WHERE unit_id=? AND"
                          " json_extract(result, '$.run_id')=?",
                          (unit_id, ev["result"]["run_id"])).fetchone():
            rt.execute("INSERT INTO evidence (unit_id, manifest_path, tree_hash, contract_sha,"
                       " runner_agent_id, result) VALUES (?,?,?,?,?,?)",
                       (unit_id, ev["manifest_path"], ev["tree_hash"], ev["contract_sha"],
                        ev["runner_agent_id"], json.dumps(ev["result"], ensure_ascii=False)))
    if op.get("invalidate_below"):
        tokens.invalidate_for_new_contract(rt, project_id, unit_id, op["invalidate_below"])


def recover(store: store_mod.Store, rt: sqlite3.Connection, unit_id: str) -> dict:
    """Opens the unit -- the store settles an unfinished transaction of its own -- and
    re-applies the latest record's runtime.db effects: a no-op unless an earlier
    operation stopped between the store's commit and the runtime.db commit. Returns
    the unit."""
    with store.reader(rt) as conn:
        unit = read_unit(conn, unit_id)
    rt.execute("BEGIN IMMEDIATE")
    try:
        apply_op(rt, store.project_id, unit_id, unit.get("op"), strict=False)
        rt.execute("COMMIT")
    except BaseException:
        rt.execute("ROLLBACK")
        raise
    return unit


def _new_op(**parts) -> dict:
    return {"id": os.urandom(6).hex(), **{k: v for k, v in parts.items() if v}}


def _contract_intact(ctx) -> str | None:
    """R1-c AC-7: every transition re-reads the confirmed contract file; its sha256 must
    still be the confirmed sha, which is the sha the evidence is selected by."""
    conf = ctx.unit.get("confirmed")
    if not conf:
        return None
    try:
        data = (ctx.store.dir / conf["path"]).read_bytes()
    except OSError as exc:
        return f"the confirmed contract {conf['path']} cannot be read ({exc})"
    if evidence_mod.sha256_bytes(data) != conf["sha"]:
        return (f"the confirmed contract {conf['path']} changed since it was confirmed "
                "(its sha256 is no longer the confirmed / evidence contract sha)")
    return None


def take(tx, ctx: Ctx, t: dict, to: str, actor: dict, extra: dict | None = None) -> dict:
    """Checks every precondition of `t` and writes the transition; FlowError otherwise."""
    failed = [f"confirmed_contract_intact: {why}"] if (why := _contract_intact(ctx)) else []
    failed += [f"{name}: {why}" for name in t.get("requires") or []
               if (why := PRECONDITIONS[name](ctx))]
    if failed:
        raise FlowError(f"{t['from']} -> {to} refused:\n  " + "\n  ".join(failed))
    op = _new_op(consume=list(ctx.consume), **ctx.op_parts)
    apply_op(ctx.rt, ctx.store.project_id, ctx.unit["id"], op, strict=True)
    unit = ctx.unit
    entry = {"from": unit["state"], "to": to, "actor": actor, "ts": store_mod.now_iso(),
             "reason": ctx.reason or ctx.halt_reason, "evidence_ref": ctx.refs,
             "tokens": ctx.consume}
    if ctx.cause:
        entry["cause"] = ctx.cause
    fields = dict(ctx.fields, state=to, history=(unit.get("history") or []) + [entry], op=op,
                  **(extra or {}))
    if to == "build":
        fields["loops"] = []
    if to == "halted":
        fields.update(halted_from=unit["state"], halt_id=os.urandom(6).hex(),
                      halt_reason=ctx.halt_reason)
    elif unit["state"] == "halted":
        fields.update(halted_from=None)
    return tx.revise("unit", unit["id"], **fields)


def _writer(actor: dict, what: str) -> dict:
    return {"cli": what, **actor}


def advance(store: store_mod.Store, rt: sqlite3.Connection, unit_id: str, to: str, *,
            reason: str | None, cause: str | None, actor: dict) -> dict:
    proc = load_process()
    unit = recover(store, rt, unit_id)
    t = find_transition(proc, unit, to, "cli")
    needs = set(t.get("requires") or [])
    for session_id in tokens.owner_sessions(rt, unit_id) if needs & TOKEN_CHECKS else ():
        decide._ingest_session(store, rt, session_id)
    tree = evidence_mod.tree_state(store) if "evidence_tree_matches" in needs else None
    lint, doc = None, None
    if needs & {"lint_passed", "resume_authorized"} and unit.get("contract"):
        doc = contract_doc(store, unit["contract"])
        lint = contract_mod.lint(doc, store.repo or store.root)

    def body(tx):
        fresh = read_unit(tx.conn, unit_id)
        if fresh["seq"] != unit["seq"]:
            raise FlowError(f"unit {unit_id} changed during the operation; run it again")
        ctx = Ctx(store, rt, tx.conn, fresh, reason=reason, cause=cause, tree=tree, lint=lint,
                  doc=doc)
        return take(tx, ctx, t, to, actor)
    return atomic(store, rt, _writer(actor, f"unit advance {to}"), body)


# ---------------------------------------------------------------------------- issues and units

def add_issue(store: store_mod.Store, title: str, actor: dict) -> dict:
    if not title.strip():
        raise FlowError("--title must not be empty")
    with store.transaction(_writer(actor, "issue add")) as tx:
        return tx.add("issue", {"title": title, "status": "open", "priority": None,
                                "labels": [], "links": [], "mirrors": {}})


def new_unit(store: store_mod.Store, issue_id: str, actor: dict) -> dict:
    with store.transaction(_writer(actor, "unit new")) as tx:
        if tx.current_one("issue", issue_id) is None:
            raise FlowError(f"issue {issue_id} does not exist: no unit created")
        uid = "U-" + os.urandom(6).hex()
        return tx.add("unit", {"id": uid, "unit_id": uid, "issue_id": issue_id,
                               "state": "intake", "contract_ref": None, "history": [],
                               "contract": None, "confirmed": None, "reviews": [], "loops": []})


def submit(store: store_mod.Store, rt: sqlite3.Connection, unit_id: str, data: bytes,
           actor: dict) -> dict:
    """AC-2: a new contract version (never overwriting one); intake -> contract_draft."""
    contract_mod.parse(data)  # refuses before anything is written
    proc = load_process()
    sha = evidence_mod.sha256_bytes(data)
    created: list[Path] = []

    def body(tx):
        unit = read_unit(tx.conn, unit_id)
        if unit["state"] not in CONTRACT_STATES:
            raise FlowError(f"unit {unit_id} is {unit['state']}: a contract version is submitted "
                            f"only in {CONTRACT_STATES} (return to contract_draft first)")
        version = ((unit.get("contract") or {}).get("version") or 0) + 1
        rel = f"contracts/{unit_id}/v{version}.yaml"
        path = store.dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "xb") as fh:  # a version is never overwritten
            created.append(path)
            fh.write(data)
        ref = {"version": version, "sha": sha, "path": rel}
        if unit["state"] == "intake":
            ctx = Ctx(store, rt, tx.conn, unit)
            ctx.op_parts["invalidate_below"] = version
            t = find_transition(proc, unit, "contract_draft", "submit")
            return take(tx, ctx, t, "contract_draft", actor, extra={"contract": ref,
                                                                   "contract_ref": rel})
        op = _new_op(invalidate_below=version)  # AC-10: older questions and tokens void
        apply_op(rt, store.project_id, unit_id, op, strict=True)
        return tx.revise("unit", unit_id, contract=ref, contract_ref=rel, op=op)
    recover(store, rt, unit_id)
    try:
        return atomic(store, rt, _writer(actor, "contract submit"), body)
    except BaseException:
        for path in created:
            if not _published(store, unit_id, path, sha):
                path.unlink(missing_ok=True)
        raise


def _published(store: store_mod.Store, unit_id: str, path: Path, sha: str) -> bool:
    """Whether a committed unit record may point at this contract file. False only when
    the store provably holds none (the failure came before its commit); when the store
    cannot be read, the file is kept -- an orphan file is harmless, a referenced one
    deleted is not (a failed runtime.db COMMIT or finalize comes after the commit)."""
    rel = path.relative_to(store.dir).as_posix()
    try:
        with store.reader() as conn:
            return conn is not None and conn.execute(
                "SELECT 1 FROM records WHERE kind='unit' AND id=?"
                " AND json_extract(json, '$.contract.path')=? AND json_extract(json, '$.contract.sha')=?",
                (unit_id, rel, sha)).fetchone() is not None
    except Exception:  # noqa: BLE001 -- unsure: keep the file
        return True


# ---------------------------------------------------------------------------- review record

def spend_runner(rt: sqlite3.Connection, token: str | None, command: str, unit_id: str,
                 project_id: str) -> dict:
    """Checks and consumes the runner token in its own runtime.db transaction."""
    rt.execute("BEGIN IMMEDIATE")
    try:
        bound = tokens.consume_runner(rt, token, command, unit_id, project_id)
        rt.execute("COMMIT")
    except BaseException:
        rt.execute("ROLLBACK")
        raise
    return bound


def record_review(store: store_mod.Store, rt: sqlite3.Connection, unit_id: str, kind: str,
                  result: str, blockers: list[str], unmapped: list[str],
                  token: str | None) -> dict:
    """AC-4: the runner token is spent first (a failure later never leaves it usable),
    then the review is written."""
    bound = spend_runner(rt, token, "review record", unit_id, store.project_id)

    def body(tx):
        unit = read_unit(tx.conn, unit_id)
        ref = unit.get("contract") if kind == "contract" else unit.get("confirmed")
        if not ref:
            raise FlowError(f"unit {unit_id} has no {'submitted' if kind == 'contract' else 'confirmed'}"
                            " contract to review")
        review = {"kind": kind, "result": result, "blockers": blockers,
                  "unmapped_changes": unmapped, "agent_id": bound["agent_id"],
                  "contract_sha": ref["sha"], "ts": store_mod.now_iso(), "token": token}
        return tx.revise("unit", unit_id, reviews=(unit.get("reviews") or []) + [review], op=None)
    recover(store, rt, unit_id)
    return atomic(store, rt, {"cli": "review record", "runner_token": token}, body)


# ---------------------------------------------------------------------------- verify

def _distance(ac: dict, value, passed: bool) -> float:
    """How far a measurement is from passing (0 = passing); for the stall rule."""
    if passed:
        return 0.0
    th = ac.get("threshold") or {}
    if th.get("lo") is None and th.get("hi") is None:
        return 1.0
    if value is None:
        return math.inf
    if th.get("lo") is not None and value <= th["lo"]:
        return th["lo"] - value
    if th.get("hi") is not None and value >= th["hi"]:
        return value - th["hi"]
    return 0.0


def halt_reason(doc: dict, loops: list[dict], limit: int) -> str | None:
    """AC-8: (a) the loop budget used up without every quantitative AC passing; (b) an
    AC whose measurement did not improve in 2 consecutive runs."""
    last = loops[-1]
    if last["all_quant_passed"]:
        return None
    reasons = []
    if len(loops) >= limit:
        reasons.append(f"budget: {len(loops)}/{limit} loops used without every quantitative AC "
                       "passing")
    if len(loops) >= 3:
        stalled = []
        for ac in doc["acs"]:
            if ac["kind"] != "quant" or last["passed"].get(ac["id"]):
                continue
            d = [_distance(ac, run["values"].get(ac["id"]), run["passed"].get(ac["id"], False))
                 for run in loops[-3:]]
            if d[1] >= d[0] and d[2] >= d[1]:
                stalled.append(f"{ac['id']} ({', '.join(str(r['values'].get(ac['id'])) for r in loops[-3:])})")
        if stalled:
            reasons.append("stalled: no improvement in 2 consecutive runs for " + ", ".join(stalled))
    return "; ".join(reasons) or None


def verify(store: store_mod.Store, rt: sqlite3.Connection, unit_id: str, self_check: bool,
           token: str | None) -> tuple[dict, dict]:
    """AC-7/AC-8: runs the confirmed contract's verify_cmd of every AC. Returns (the
    manifest, the unit's record after the run)."""
    bound = spend_runner(rt, token, "verify", unit_id, store.project_id)
    unit = recover(store, rt, unit_id)
    want = "build" if self_check else "quant_verified"
    if unit["state"] != want:
        raise FlowError(f"unit {unit_id} is {unit['state']}: `verify{' --self' if self_check else ''}`"
                        f" runs in {want}")
    doc = contract_doc(store, unit.get("confirmed"))
    proc = load_process() if self_check else None
    manifest, path, manifest_sha = evidence_mod.run(store, unit, doc, bound["agent_id"], self_check)
    acs = {r["id"]: {"passed": r["passed"], "value": r["value"], "kind": r["kind"]}
           for r in manifest["acs"]}

    def body(tx):
        fresh = read_unit(tx.conn, unit_id)
        if fresh["state"] != want or (fresh.get("confirmed") or {}).get("sha") != manifest["contract_sha"]:
            raise FlowError(f"unit {unit_id} changed while verifying; evidence not recorded")
        result = {"self": self_check, "all_quant_passed": manifest["all_quant_passed"],
                  "acs": acs, "manifest_sha256": manifest_sha, "run_id": manifest["run_id"]}
        if not self_check:  # runtime.db only: its own transaction is the whole operation
            apply_op(rt, store.project_id, unit_id,
                     {"evidence": [_evidence_row(path, manifest, result)]}, strict=True)
            return fresh
        loops = (fresh.get("loops") or []) + [{
            "n": len(fresh.get("loops") or []) + 1, "run_id": manifest["run_id"],
            "values": {k: v["value"] for k, v in acs.items() if v["kind"] == "quant"},
            "passed": {k: v["passed"] for k, v in acs.items() if v["kind"] == "quant"},
            "all_quant_passed": manifest["all_quant_passed"]}]
        result["loop"] = len(loops)
        row = _evidence_row(path, manifest, result)
        why = halt_reason(doc, loops, fresh["confirmed"]["loops"])
        if why is None:
            op = _new_op(evidence=[row])
            apply_op(rt, store.project_id, unit_id, op, strict=True)
            return tx.revise("unit", unit_id, loops=loops, op=op)
        ctx = Ctx(store, rt, tx.conn, fresh, halt_reason=why)
        ctx.op_parts["evidence"] = [row]
        ctx.refs.append({"run_id": manifest["run_id"]})
        return take(tx, ctx, find_transition(proc, fresh, "halted", "verify"), "halted",
                    {"cli": "verify --self", "agent_id": bound["agent_id"]}, extra={"loops": loops})
    rec = atomic(store, rt, {"cli": "verify", "agent_id": bound["agent_id"]}, body)
    return manifest, rec


def _evidence_row(path: Path, manifest: dict, result: dict) -> dict:
    return {"manifest_path": str(path), "tree_hash": manifest["tree_hash"],
            "contract_sha": manifest["contract_sha"],
            "runner_agent_id": manifest["runner_agent_id"], "result": result}


# ---------------------------------------------------------------------------- owner questions

def contract_question(store: store_mod.Store, rt: sqlite3.Connection, unit_id: str) -> dict:
    """AC-5: the AskUserQuestion input with the whole current contract, recorded."""
    with store.reader(rt) as conn:
        unit = read_unit(conn, unit_id)
    ref = unit.get("contract")
    if not ref:
        raise FlowError(f"unit {unit_id} has no contract")
    text = (store.dir / ref["path"]).read_bytes()
    if evidence_mod.sha256_bytes(text) != ref["sha"]:
        raise FlowError(f"{ref['path']} differs from the submitted version (sha256 mismatch)")
    question = (f"CONTRACT-CONFIRM:{unit_id} v{ref['version']} sha:{ref['sha'][:12]}\n\n"
                f"単位 {unit_id} のゴール契約 v{ref['version']} の全文です。確定しますか？\n\n"
                + text.decode("utf-8"))
    payload = tokens.ask_input([(question, "契約の確定", [
        (tokens.CONFIRM, "この契約で確定する（製品への書き込みは対象範囲内だけ許可される）"),
        (tokens.REVISE, "確定しない。修正内容を自由記述かメモで書く")])])
    tokens.record_question(rt, store.project_id, unit_id, "contract_confirm", payload,
                           sha=ref["sha"], version=ref["version"])
    return payload


def qual_question(store: store_mod.Store, rt: sqlite3.Connection, unit_id: str) -> dict:
    """AC-10: every qualitative AC in one AskUserQuestion (合格 / 不合格 each), recorded."""
    with store.reader(rt) as conn:
        unit = read_unit(conn, unit_id)
    if unit["state"] != "owner_review":
        raise FlowError(f"unit {unit_id} is {unit['state']}, not owner_review")
    doc = contract_doc(store, unit.get("confirmed"))
    qual = [ac for ac in doc["acs"] if ac["kind"] == "qual"]
    if not qual:
        raise FlowError("the contract has no qualitative AC: `ulthar unit advance "
                        f"{unit_id} accepted` needs no judgement")
    if len(qual) > MAX_QUESTIONS:
        raise FlowError(f"{len(qual)} qualitative ACs: one AskUserQuestion holds at most "
                        f"{MAX_QUESTIONS}")
    sha, version = _bound_contract(unit)
    asks = []
    for ac in qual:
        rubric = ac.get("rubric") or {}
        body = "\n".join(f"{k}: {json.dumps(rubric.get(k), ensure_ascii=False)}" for k in contract_mod.RUBRIC_KEYS)
        asks.append((f"QUAL-JUDGE:{unit_id} sha:{sha[:12]} {ac['id']}: {ac['statement']}\n{body}",
                     "定性の判定", [(tokens.PASS, f"{ac['id']} は合格"),
                                   (tokens.FAIL, f"{ac['id']} は不合格（build に戻す）")]))
    payload = tokens.ask_input(asks)
    tokens.record_question(rt, store.project_id, unit_id, "qual_judgement", payload, sha=sha,
                           version=version, acs=[ac["id"] for ac in qual])
    return payload


def resume_question(store: store_mod.Store, rt: sqlite3.Connection, unit_id: str) -> dict:
    with store.reader(rt) as conn:
        unit = read_unit(conn, unit_id)
    if unit["state"] != "halted":
        raise FlowError(f"unit {unit_id} is {unit['state']}, not halted")
    sha, version = _bound_contract(unit)
    question = (f"UNIT-RESUME:{unit_id} sha:{sha[:12]}\n\n単位 {unit_id} は "
                f"{unit.get('halted_from')} で停止しました（理由: {unit.get('halt_reason')}）。"
                "この契約のまま再開しますか？")
    payload = tokens.ask_input([(question, "単位の再開", [
        (tokens.RESUME, f"{unit.get('halted_from')} に戻して再開する"),
        (tokens.NO_RESUME, "停止したままにする（新しい契約版で扱う）")])])
    tokens.record_question(rt, store.project_id, unit_id, "unit_resume", payload, sha=sha,
                           version=version, halt_id=unit.get("halt_id"))
    return payload


def _bound_contract(unit: dict) -> tuple[str, int]:
    """(sha, version) a QUAL-JUDGE / UNIT-RESUME question and its tokens are bound to: the
    confirmed contract, which must also be the latest submitted one (AC-10)."""
    conf = unit.get("confirmed") or {}
    if not conf or _current(unit) != (conf.get("sha"), conf.get("version")):
        raise FlowError(f"unit {unit['id']}: a newer contract version (v"
                        f"{(unit.get('contract') or {}).get('version')}) awaits confirmation; "
                        "confirm it (CONTRACT-CONFIRM) instead")
    return conf["sha"], conf["version"]
