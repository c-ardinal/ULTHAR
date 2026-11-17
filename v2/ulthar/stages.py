"""R1-d's part of the unit process, CLI only: the preconditions of accepted ->
integrated -> done, done -> reopen -> contract_draft (process/unit.yaml; ulthar.flow
looks them up by name), the UNIT-REOPEN question, and the pack rules the contract
commands apply (SKL-02 / SKL-06, R1-d AC-6):
  * `ulthar contract submit` adds the default_acs of the contract's packs to the
    version it saves (an id that is already there refuses the submission);
  * `ulthar review record --kind verify` is taken only from an agent_id holding the
    receipt of `ulthar-evaluator@<pack>` for one of the unit's packs.
GOAL-12: a reopen records the AC that should have caught the slip -- or none, which
makes the slip a candidate for a new lint rule (the unit's `slips`).
"""
from __future__ import annotations

from . import contract as contract_mod
from . import flow
from . import packs as packs_mod
from . import store as store_mod
from . import tokens

NO_AC = "なし"


def packs_or_error(store: store_mod.Store):
    """The project's packs, or the text of the error reading them (for the lint)."""
    try:
        return packs_mod.for_store(store)
    except packs_mod.PackError as exc:
        return str(exc)


def project_packs(store: store_mod.Store) -> list[dict]:
    try:
        return packs_mod.for_store(store)
    except packs_mod.PackError as exc:
        raise flow.FlowError(str(exc)) from exc


# ---------------------------------------------------------------------------- AC-6

def with_default_acs(store: store_mod.Store, data: bytes) -> bytes:
    """The contract to save: `data` plus its packs' default_acs (unchanged bytes when
    the packs add none). FlowError for an unknown pack or an AC id already present."""
    doc = contract_mod.parse(data)
    wanted = doc.get("packs") or []
    if not wanted:
        return data
    defined = {p["name"]: p for p in project_packs(store)}
    unknown = [p for p in wanted if p not in defined]
    if unknown:
        raise flow.FlowError(f"packs {unknown} are not defined in .claude/ulthar.json")
    extra = [ac for p in wanted for ac in defined[p]["default_acs"]]
    if not extra:
        return data
    ids = [ac["id"] for ac in doc["acs"]] + [ac["id"] for ac in extra]
    clash = sorted({i for i in ids if ids.count(i) > 1})
    if clash:
        raise flow.FlowError(f"the packs' default ACs reuse AC ids {clash}: rename the "
                             "contract's ACs (SKL-06)")
    yaml = contract_mod.load_yaml()
    doc["acs"] = doc["acs"] + [dict(ac) for ac in extra]
    return yaml.safe_dump(doc, allow_unicode=True, sort_keys=False).encode("utf-8")


def check_evaluator(rt, project_id: str, unit: dict, bound: dict) -> None:
    """A verify review only from the unit's pack evaluator (SKL-06), whose receipt is of
    this project and of the session the runner token was issued in."""
    wanted = packs_mod.unit_packs(unit)
    if not wanted:
        return
    agent_id = bound["agent_id"]
    held = packs_mod.receipts_of(rt, project_id, bound.get("session_id"), agent_id, role="evaluator")
    if not held & set(wanted):
        raise flow.FlowError(
            f"the verify review of unit {unit['id']} is taken only from a verified "
            f"{' / '.join(packs_mod.agent_type('evaluator', p) for p in wanted)}; agent "
            f"{agent_id} holds evaluator receipts for {sorted(held) or 'no pack'} (SKL-06)")


# ---------------------------------------------------------------------------- preconditions

def _report(c) -> dict:
    return getattr(c, "integration", None) or {}


def _lease_held(c) -> str | None:
    return _report(c).get("lease", "the integration check did not run")


def _worktree_clean(c) -> str | None:
    return _report(c).get("clean", "the integration check did not run")


def _provenance_verified(c) -> str | None:
    rep = _report(c)
    if "unattributed" not in rep:
        return "the integration check did not run"
    return None if not rep["unattributed"] else f"{len(rep['unattributed'])} unattributed change(s)"


def _integration_verify_passed(c) -> str | None:
    ran = getattr(c, "verify", None)
    if ran is None:
        return "the verify on the integration branch did not run"
    return None if ran["all_quant_passed"] else "not every quantitative AC passed there"


def _integration_verify_failed(c) -> str | None:
    ran = getattr(c, "verify", None)
    return None if ran is not None and not ran["all_quant_passed"] else "the verify passed"


def _reopen_authorized(c) -> str | None:
    conf = c.unit.get("confirmed") or {}
    good, bad = tokens.valid_owner_tokens(c.rt, c.conn, c.store.project_id, c.unit["id"],
                                          "unit_reopen", flow._spent(c.unit))
    good = [(t, b) for t, b in good if b.get("verdict") == "reopen"
            and (b.get("contract_sha"), b.get("version")) == (conf.get("sha"), conf.get("version"))]
    if not good:
        return ("no owner UNIT-REOPEN token (差し戻す) for this unit and contract"
                + ("; " + "; ".join(bad) if bad else ""))
    token_id, bound = good[0]
    c.consume.append(token_id)
    ac = bound.get("ac")
    slip = {"ac": None if ac == NO_AC else ac, "lint_rule_candidate": ac == NO_AC,
            "ts": store_mod.now_iso(), "token": token_id}
    c.entry["slip"] = slip
    c.fields["slips"] = (c.unit.get("slips") or []) + [slip]
    c.fields["reopened_at_version"] = (c.unit.get("contract") or {}).get("version")
    return None


def _contract_resubmitted(c) -> str | None:
    now = (c.unit.get("contract") or {}).get("version") or 0
    at = c.unit.get("reopened_at_version") or 0
    return None if now > at else (f"no contract version after the reopen (still v{now}): "
                                  "`ulthar contract submit` a new one first")


PRECONDITIONS = {
    "lease_held": _lease_held,
    "worktree_clean": _worktree_clean,
    "provenance_verified": _provenance_verified,
    "integration_verify_passed": _integration_verify_passed,
    "integration_verify_failed": _integration_verify_failed,
    "reopen_authorized": _reopen_authorized,
    "contract_resubmitted": _contract_resubmitted,
}


# ---------------------------------------------------------------------------- UNIT-REOPEN

def reopen_question(store: store_mod.Store, rt, unit_id: str, ac: str | None) -> dict:
    """AC-12: the AskUserQuestion input of the owner's UNIT-REOPEN, recorded. `ac`: the
    AC that should have caught the problem, or なし (required)."""
    with store.reader(rt) as conn:
        unit = flow.read_unit(conn, unit_id)
    if unit["state"] != "done":
        raise flow.FlowError(f"unit {unit_id} is {unit['state']}, not done")
    conf = unit.get("confirmed") or {}
    ids = [a["id"] for a in conf.get("acs") or []]
    if ac not in ids + [NO_AC]:
        raise flow.FlowError(f"--ac is required: the AC that should have caught it ({ids}) or "
                             f"{NO_AC}")
    question = (f"UNIT-REOPEN:{unit_id} sha:{conf['sha'][:12]} ac:{ac}\n\n単位 {unit_id} は done "
                f"です。差し戻しますか？ 捉えるべきだった AC: {ac}"
                + ("（該当する AC がない＝lint 規則の追加候補として記録します）" if ac == NO_AC else ""))
    payload = tokens.ask_input([(question, "単位の差し戻し", [
        (tokens.REOPEN, "差し戻す（reopen に移し、新しい契約版で直す）"),
        (tokens.NO_REOPEN, "差し戻さない（done のまま）")])])
    tokens.record_question(rt, store.project_id, unit_id, "unit_reopen", payload,
                           sha=conf["sha"], version=conf["version"], ac=ac)
    return payload
