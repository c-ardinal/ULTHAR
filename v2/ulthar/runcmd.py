"""`ulthar run <unit> <name>` -- a declared file-changing command (TRUST-07 second half,
R1-d AC-7), CLI only.

Only `write_cmds[<name>]` of the unit's confirmed contract runs (no command from the
command line), started windowless with its process tree contained
(ulthar.worker.run_contained, as `ulthar verify`), by the subagent the runner token
names (R1-c AC-4: the hook issues it to a subagent's `ulthar run` only). The working
tree is snapshotted before and after (every file git sees -- tracked, and untracked
ones .gitignore does not ignore -- through a temporary index, the knowledge store
excepted); every path whose blob changed, deletions included, is recorded with its
post-hash (none for a deleted path) in one runtime.db `events` row: hook `cli`, tool
`ulthar run`, the runner's agent_id and its session's fencing token (ulthar.lease).
The row is `allow` -- an allowed write event TRUST-04 (b) accepts -- only when every
changed path is inside the confirmed scope_globs and, where a pack's path_globs match
it, the runner holds that pack's receipt (AC-5's rule); otherwise the run is recorded as
failed (`deny`), so its changes stay unattributed at integration.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path, PurePosixPath

from . import contract as contract_mod
from . import evidence as evidence_mod
from . import flow
from . import lease as lease_mod
from . import packs as packs_mod
from . import product
from . import shellwords
from . import stages
from . import store as store_mod
from . import worker

RUN_TIMEOUT_SEC = 600
GUARD_ID = "ulthar_run"


def snapshot(store: store_mod.Store) -> dict[str, str]:
    """Repository path -> blob of every file git sees in the working tree."""
    repo = store.repo
    git_dir = Path(evidence_mod.git(repo, "rev-parse", "--absolute-git-dir").strip())
    spec = ["--", ".", *evidence_mod._exclude(store, repo)]
    with tempfile.TemporaryDirectory(prefix="ulthar-run-") as tmp:
        index = Path(tmp) / "index"
        if (git_dir / "index").is_file():
            shutil.copyfile(git_dir / "index", index)
        env = dict(os.environ, GIT_INDEX_FILE=str(index))
        evidence_mod.git(repo, "add", "-A", *spec, env=env)
        out = evidence_mod.git(repo, "ls-files", "-s", "-z", *spec, env=env)
    blobs = {}
    for entry in filter(None, out.split("\0")):
        meta, path = entry.split("\t", 1)
        blobs[path] = meta.split()[1]
    return blobs


def _problems(changed: dict, unit: dict, packs: list[dict], have: set[str]) -> list[str]:
    scope = [product.fold(g) for g in (unit.get("confirmed") or {}).get("scope_globs") or []]
    out = []
    for rel in sorted(changed):
        folded = product.fold(rel)
        if not any(PurePosixPath(folded).full_match(g) for g in scope):
            out.append(f"{rel}: outside the confirmed scope_globs")
            continue
        need = packs_mod.needed([folded], False, packs)
        if need - have:
            out.append(f"{rel}: needs the receipt of pack {sorted(need - have)}")
    return out


def run(store: store_mod.Store, rt, unit_id: str, name: str, token: str | None) -> dict:
    """Runs the declared command; returns the recorded event's summary."""
    bound = flow.spend_runner(rt, token, "run", unit_id, store.project_id)
    unit = flow.recover(store, rt, unit_id)
    if unit["state"] not in product.SCOPE_STATES:
        raise flow.FlowError(f"unit {unit_id} is {unit['state']}: `ulthar run` runs in "
                             f"{product.SCOPE_STATES}")
    cmds = flow.contract_doc(store, unit.get("confirmed")).get("write_cmds") or {}
    if name not in cmds:
        raise flow.FlowError(f"`{name}` is not one of the confirmed contract's write_cmds "
                             f"{sorted(cmds)}: nothing was run")
    argv = shellwords.tokenize(cmds[name], None)
    exe = contract_mod.resolve_command(argv[0] if argv else "", store.repo)
    if exe is None:
        raise flow.FlowError(f"write_cmds.{name}: `{argv[0] if argv else ''}` does not resolve")
    packs = stages.project_packs(store)
    before = snapshot(store)
    try:
        rc, out, err = worker.run_contained([exe, *argv[1:]], b"", RUN_TIMEOUT_SEC, str(store.repo),
                                            dict(os.environ), what="write_cmd")
        error = None
    except (OSError, RuntimeError) as exc:
        rc, out, err, error = None, b"", b"", f"{type(exc).__name__}: {exc}"
    after = snapshot(store)
    changed = {p: after.get(p) for p in set(before) | set(after) if before.get(p) != after.get(p)}
    agent = bound["agent_id"]
    problems = _problems(changed, unit, packs, packs_mod.receipts_of(rt, agent))
    detail = {"unit_id": unit_id, "name": name, "command": cmds[name], "exit_code": rc,
              "error": error, "problems": problems, "runner_token": token,
              "stdout_sha256": evidence_mod.sha256_bytes(out),
              "stderr_sha256": evidence_mod.sha256_bytes(err)}
    post = {str(store.repo / rel): blob for rel, blob in sorted(changed.items())}
    rt.execute("BEGIN IMMEDIATE")
    try:
        fence = lease_mod.token_of(rt, store.project_id, str(bound.get("session_id")))
        cur = rt.execute(
            "INSERT INTO events (ts, session_id, agent_id, agent_type, hook, tool, decision,"
            " guard_id, fence_token, paths, post_hashes, detail) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (store_mod.now_iso(), str(bound.get("session_id") or "(none)"), agent, None, "cli",
             "ulthar run", "deny" if problems else "allow", GUARD_ID, fence,
             json.dumps(sorted(post)) if post else None, json.dumps(post),
             json.dumps(detail, ensure_ascii=False)))
        rt.execute("COMMIT")
    except BaseException:
        rt.execute("ROLLBACK")
        raise
    return {"event_id": cur.lastrowid, "decision": "deny" if problems else "allow",
            "changed": changed, "problems": problems, "exit_code": rc, "error": error,
            "fence_token": fence}
