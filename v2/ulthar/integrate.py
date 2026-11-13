"""Integration (TRUST-04, TRUST-05; R1-d AC-9, AC-10, AC-12), CLI only.

`ulthar unit advance <u> integrated` -- the window's command (its runner token, issued
by the hook to the main session only, names the window's session):
  preconditions: the window holds the project's valid lease; the working tree has no
  change and no untracked file (the knowledge store excepted);
  changed paths: `git diff --name-status --no-renames` from the reviewer evidence's base
  commit to the work branch's tip, together with the same from the merge base with the
  integration branch (so a commit made before the evidence is never skipped; renames
  count on both sides);
  per path: (a) inside the confirmed scope_globs; (b) its blob at the tip (none when
  deleted) is the post-hash of the last allowed write event of that path; (c) that
  event's agent_id holds the receipt of the pack the path needs; (d) that event's
  fencing token was the token its session held when it was recorded (lease history);
  and (e) the latest reviewer evidence's tree hash is `<tip>^{tree}`.
Any failure is an "unattributed change" (path, reason a..e, the last event id), printed
and written to units.history as a refused attempt; the unit stays accepted and there
is no override (spec 事前決定 4): the builder rewrites the path the regular way.
On success the CLI itself merges: `git checkout <integration>`, `git merge --no-ff -F
<file> <work branch>`; a failed merge (a conflict) is aborted, the work branch checked
out again and the integration branch left at its old commit, nothing written. Then the
unit record (integration commit, history) is written as one operation (ulthar.flow
atomic); if that fails the integration branch is set back too.

`ulthar unit advance <u> done`: on the integration branch, at or past the integration
commit, with a clean tree, the CLI runs the confirmed verify commands again; all
quantitative ACs passing -> done, else the unit goes back to accepted with the
difference recorded (the failing ACs with this run's and the reviewer's values).
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path, PurePosixPath

from . import evidence as evidence_mod
from . import flow
from . import lease as lease_mod
from . import packs as packs_mod
from . import product
from . import stages
from . import store as store_mod
from .evidence import EvidenceError

DEFAULT_INTEGRATION = "develop"
REASONS = {"a": "outside the confirmed scope_globs",
           "b": "the blob at the tip is not the post-hash of the last allowed write",
           "c": "the writer holds no receipt of the pack the path needs",
           "d": "the write's fencing token was not its session's token at the time",
           "e": "the latest reviewer evidence is not of the tip's tree"}


def integration_branch(store: store_mod.Store) -> str:
    branch = store.config.get("integration_branch") or DEFAULT_INTEGRATION
    if not isinstance(branch, str):
        raise flow.FlowError(".claude/ulthar.json: integration_branch must be a branch name")
    return branch


def _git(repo: Path, *args: str) -> str:
    try:
        return evidence_mod.git(repo, *args)
    except EvidenceError as exc:
        raise flow.FlowError(str(exc)) from exc


def _dirty(store: store_mod.Store, repo: Path) -> list[str]:
    spec = ["--", ".", *evidence_mod._exclude(store, repo)]
    out = _git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all", *spec)
    return [e for e in out.split("\0") if e]


def _changed(repo: Path, base: str, tip: str) -> list[str]:
    out = _git(repo, "diff", "--name-status", "--no-renames", "-z", base, tip, "--")
    parts = [p for p in out.split("\0") if p]
    return parts[1::2]


def _tip_blobs(repo: Path, tip: str, paths: list[str]) -> dict[str, str]:
    if not paths:
        return {}
    out = _git(repo, "ls-tree", "-z", tip, "--", *paths)
    blobs = {}
    for entry in filter(None, out.split("\0")):
        meta, path = entry.split("\t", 1)
        blobs[path] = meta.split()[2]
    return blobs


def _last_writes(rt, repo: Path) -> dict[str, tuple]:
    """Folded repository path -> (event id, ts, session, agent, token, post-hash) of the
    last allowed event that recorded a post-hash for it (PostToolUse, `ulthar run`)."""
    roots = product.forms(str(repo))
    memo: dict = {}
    last: dict[str, tuple] = {}
    for eid, ts, session, agent, token, raw in rt.execute(
            "SELECT id, ts, session_id, agent_id, fence_token, post_hashes FROM events WHERE"
            " post_hashes IS NOT NULL AND decision='allow' ORDER BY id"):
        for key, value in json.loads(raw).items():
            for form in product.forms(key, memo):
                for root in roots:
                    if form.startswith(root + "/"):
                        last[form[len(root) + 1:]] = (eid, ts, session, agent, token, value)
    return last


def _path_problem(rel: str, blob, write, scope: list[str], packs: list[dict], rt) -> tuple:
    """(reason letter, detail) of the first condition a..d the path fails, or (None, None)."""
    folded = product.fold(rel)
    if not any(PurePosixPath(folded).full_match(g) for g in scope):
        return "a", f"scope_globs {scope}"
    if write is None:
        return "b", "no allowed write of this path was recorded"
    eid, ts, session, agent, token, value = write
    if value != blob:
        return "b", f"tip blob {blob}, last recorded post-hash {value} (event {eid})"
    need = packs_mod.needed([folded], False, packs)
    if need and not (agent and need <= packs_mod.receipts_of(rt, agent)):
        return "c", f"needs {sorted(need)}; event {eid} was written by {agent or 'the window'}"
    why = lease_mod.invalid_because(rt, token, session, ts)
    if why:
        return "d", why
    return None, None


def check(store: store_mod.Store, rt, unit: dict, session_id: str) -> dict:
    """The integration report: {lease, clean (None = fine), unattributed[], tip, base,
    work_branch, branch}. FlowError when it cannot be computed."""
    repo = store.repo
    if repo is None or not (repo / ".git").exists():
        raise flow.FlowError("integration needs the project's git repository")
    held = lease_mod.holder(rt, store.project_id)
    report: dict = {"lease": None if held and held[0] == session_id else (
        f"the window session {session_id} holds no valid lease (holder: "
        f"{held[0] if held else 'none'}): any hook call of the window renews it"),
        "clean": None}
    dirty = _dirty(store, repo)
    if dirty:
        report["clean"] = f"the working tree has changes or untracked files: {dirty[:10]}"
    branch = integration_branch(store)
    work = _git(repo, "symbolic-ref", "--short", "-q", "HEAD").strip()
    if not work or work.casefold() == branch.casefold():
        raise flow.FlowError(f"run the integration from the unit's work branch (now: {work or 'detached'})")
    tip = _git(repo, "rev-parse", "--verify", "HEAD").strip()
    _git(repo, "rev-parse", "--verify", f"refs/heads/{branch}")
    row = flow._evidence(rt, unit, False)
    if not row:
        raise flow.FlowError("no reviewer verify evidence for the confirmed contract")
    broken = evidence_mod.intact(row[1], row[2])
    if broken:
        raise flow.FlowError(broken)
    base = json.loads(Path(row[2]).read_text(encoding="utf-8")).get("base_commit")
    merge_base = _git(repo, "merge-base", branch, tip).strip()
    paths = sorted(set((_changed(repo, base, tip) if base else []) + _changed(repo, merge_base, tip)))
    blobs = _tip_blobs(repo, tip, paths)
    writes = _last_writes(rt, repo)
    scope = [product.fold(g) for g in (unit.get("confirmed") or {}).get("scope_globs") or []]
    packs = stages.project_packs(store)
    unattributed = []
    for rel in paths:
        write = writes.get(product.fold(rel))
        reason, why = _path_problem(rel, blobs.get(rel), write, scope, packs, rt)
        if reason:
            unattributed.append({"path": rel, "reason": reason, "why": f"{REASONS[reason]}: {why}",
                                 "event_id": write[0] if write else None})
    tree = _git(repo, "rev-parse", f"{tip}^{{tree}}").strip()
    if row[3] != tree:
        unattributed.append({"path": f"{tip[:12]}^{{tree}}", "reason": "e", "event_id": None,
                             "why": f"{REASONS['e']}: evidence {row[0]} is of tree {row[3]}, the "
                                    f"tip's tree is {tree}"})
    report.update(unattributed=unattributed, tip=tip, base=base, merge_base=merge_base,
                  work_branch=work, branch=branch, evidence_id=row[0], paths=paths)
    return report


def _merge(repo: Path, report: dict, unit_id: str) -> tuple[str, str]:
    """(old integration head, integration commit); FlowError with the branch restored."""
    branch, work = report["branch"], report["work_branch"]
    old = _git(repo, "rev-parse", "--verify", f"refs/heads/{branch}").strip()
    with tempfile.TemporaryDirectory(prefix="ulthar-merge-") as tmp:
        msg = Path(tmp) / "message.txt"
        msg.write_text(f"Merge branch '{work}' into {branch} (ulthar unit {unit_id})\n\n"
                       f"Integrated by `ulthar unit advance {unit_id} integrated` after the "
                       f"TRUST-04 check of {len(report['paths'])} changed path(s).\n",
                       encoding="utf-8")
        try:
            _git(repo, "checkout", "-q", branch)
            _git(repo, "merge", "--no-ff", "-F", str(msg), work)
        except flow.FlowError as exc:
            _undo(repo, branch, work, old)
            raise flow.FlowError(f"the merge into {branch} failed; {branch} is left at "
                                 f"{old[:12]} and {work} checked out again: {exc}") from exc
    return old, _git(repo, "rev-parse", "--verify", "HEAD").strip()


def _undo(repo: Path, branch: str, work: str, old: str) -> None:
    for args in (["merge", "--abort"], ["checkout", "-q", work]):
        try:
            evidence_mod.git(repo, *args)
        except EvidenceError:
            pass
    if evidence_mod.git(repo, "rev-parse", "--verify", f"refs/heads/{branch}").strip() != old:
        evidence_mod.git(repo, "update-ref", f"refs/heads/{branch}", old)


def _record_refusal(store, rt, unit_id: str, unit: dict, report: dict, actor: dict) -> None:
    entry = {"from": unit["state"], "to": "integrated", "refused": True, "actor": actor,
             "ts": store_mod.now_iso(), "reason": "unattributed changes (TRUST-04)",
             "unattributed": report["unattributed"], "tip": report["tip"],
             "base": report["base"], "evidence_ref": [{"evidence_id": report["evidence_id"]}]}

    def body(tx):
        fresh = flow.read_unit(tx.conn, unit_id)
        if fresh["seq"] != unit["seq"]:
            raise flow.FlowError(f"unit {unit_id} changed during the operation; run it again")
        return tx.revise("unit", unit_id, history=(fresh.get("history") or []) + [entry], op=None)
    flow.atomic(store, rt, {"cli": "unit advance integrated", **actor}, body)


def unattributed_text(items: list[dict]) -> str:
    return "\n  ".join(f"[{u['reason']}] {u['path']} (event {u['event_id']}): {u['why']}"
                       for u in items)


def advance_integrated(store, rt, unit_id: str, actor: dict, token: str | None) -> dict:
    proc = flow.load_process()
    bound = flow.spend_runner(rt, token, "unit advance", unit_id, store.project_id)
    actor = dict(actor, session_id=bound.get("session_id"))
    unit = flow.recover(store, rt, unit_id)
    t = flow.find_transition(proc, unit, "integrated", "cli")
    report = check(store, rt, unit, str(bound.get("session_id")))
    pre = [w for w in (report["lease"], report["clean"]) if w]
    if pre:
        raise flow.FlowError("accepted -> integrated refused:\n  " + "\n  ".join(pre))
    if report["unattributed"]:
        _record_refusal(store, rt, unit_id, unit, report, actor)
        raise flow.FlowError("accepted -> integrated refused: unattributed changes (TRUST-04; no "
                             "override -- have the builder rewrite them the regular way, then run "
                             "the integration again):\n  " + unattributed_text(report["unattributed"]))
    repo = store.repo
    if _dirty(store, repo) or _git(repo, "rev-parse", "HEAD").strip() != report["tip"]:
        raise flow.FlowError("the working tree changed during the check; run it again")
    old, merged = _merge(repo, report, unit_id)
    info = {"branch": report["branch"], "work_branch": report["work_branch"], "tip": report["tip"],
            "base": report["base"], "merge_base": report["merge_base"], "merge_commit": merged,
            "previous_head": old, "paths": report["paths"]}

    def body(tx):
        fresh = flow.read_unit(tx.conn, unit_id)
        if fresh["seq"] != unit["seq"]:
            raise flow.FlowError(f"unit {unit_id} changed during the operation; run it again")
        ctx = flow.Ctx(store, rt, tx.conn, fresh)
        ctx.integration = report
        ctx.refs.append({"evidence_id": report["evidence_id"], "merge_commit": merged})
        return flow.take(tx, ctx, t, "integrated", actor, extra={"integration": info})
    try:
        return flow.atomic(store, rt, flow._writer(actor, "unit advance integrated"), body)
    except BaseException:
        if not _committed(store, unit_id, merged):
            _undo(repo, report["branch"], report["work_branch"], old)
        raise


def _committed(store, unit_id: str, merged: str) -> bool:
    """Whether a committed unit record names this integration commit (then the merge
    stays: recovery completes the rest); unsure counts as committed."""
    try:
        with store.reader() as conn:
            unit = store_mod.get(conn, "unit", unit_id) or {}
        return (unit.get("integration") or {}).get("merge_commit") == merged
    except Exception:  # noqa: BLE001 -- unsure: keep the merge
        return True


# ---------------------------------------------------------------------------- done

def advance_done(store, rt, unit_id: str, actor: dict) -> tuple[dict, dict]:
    """(unit record, manifest): done when the verify on the integration branch passes,
    back to accepted (with the difference) when it does not."""
    proc = flow.load_process()
    unit = flow.recover(store, rt, unit_id)
    t_done = flow.find_transition(proc, unit, "done", "cli")
    t_back = flow.find_transition(proc, unit, "accepted", "done")
    info, repo = unit.get("integration") or {}, store.repo
    here = _git(repo, "symbolic-ref", "--short", "-q", "HEAD").strip()
    if here != info.get("branch"):
        raise flow.FlowError(f"check out the integration branch {info.get('branch')} first (now: {here})")
    try:
        evidence_mod.git(repo, "merge-base", "--is-ancestor", info["merge_commit"], "HEAD")
    except EvidenceError as exc:
        raise flow.FlowError(f"{here} does not contain the integration commit "
                             f"{info['merge_commit'][:12]}") from exc
    dirty = _dirty(store, repo)
    if dirty:
        raise flow.FlowError(f"the working tree has changes or untracked files: {dirty[:10]}")
    doc = flow.contract_doc(store, unit.get("confirmed"))
    manifest, path, manifest_sha = evidence_mod.run(store, unit, doc, None, False)
    acs = {r["id"]: {"passed": r["passed"], "value": r["value"], "kind": r["kind"]}
           for r in manifest["acs"]}
    result = {"self": False, "stage": "done", "all_quant_passed": manifest["all_quant_passed"],
              "acs": acs, "manifest_sha256": manifest_sha, "run_id": manifest["run_id"]}
    reviewer = flow._evidence(rt, unit, False)
    before = (reviewer[1].get("acs") or {}) if reviewer else {}
    diff = [{"id": k, "value": v["value"], "reviewer_value": (before.get(k) or {}).get("value")}
            for k, v in acs.items() if v["kind"] == "quant" and not v["passed"]]

    def body(tx):
        fresh = flow.read_unit(tx.conn, unit_id)
        if fresh["seq"] != unit["seq"]:
            raise flow.FlowError(f"unit {unit_id} changed while verifying; nothing recorded")
        ctx = flow.Ctx(store, rt, tx.conn, fresh)
        ctx.verify = manifest
        ctx.op_parts["evidence"] = [flow._evidence_row(path, manifest, result)]
        ctx.refs.append({"run_id": manifest["run_id"]})
        if manifest["all_quant_passed"]:
            return flow.take(tx, ctx, t_done, "done", actor)
        ctx.reason = "the verify on the integration branch failed: " + json.dumps(diff, ensure_ascii=False)
        ctx.entry["diff"] = diff
        return flow.take(tx, ctx, t_back, "accepted", {"cli": "unit advance done", **actor})
    rec = flow.atomic(store, rt, flow._writer(actor, "unit advance done"), body)
    return rec, manifest
