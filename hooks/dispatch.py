#!/usr/bin/env python3
"""hooks/dispatch.py -- single entry point, one process per hook event.

CONTRACT.md #0 (target layout), #1 (guard interface), #4 (invariants I-1..I-13).
Registered in settings.json as:
    "<your-python>" "~/.claude/hooks/dispatch.py" --event <E>

Imports guard modules and calls them in-process (one process per tool call),
rather than spawning a new process per guard.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.util
import json
import os
import re
import sys
import time
import traceback
from pathlib import Path
from typing import Callable

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


DEPLOY_ORIGIN_FILE = "deployed-from.txt"
"""Bootstrap copy of lib.paths.DEPLOY_ORIGIN_FILE. This module's earliest failure
path is `lib` itself failing to import, so the message builder below must not
depend on `lib` for anything, this name included. Keep the two spellings
identical."""


def _recorded_master_root(deploy_dir: Path) -> str | None:
    """The clone install.py deployed from, or None when this deploy has no record.
    Stdlib only and never raises -- it runs inside a message builder that has to
    work when everything else is broken."""
    try:
        text = (deploy_dir / DEPLOY_ORIGIN_FILE).read_text(encoding="utf-8-sig").strip()
    except Exception:  # noqa: BLE001 -- a message builder must never fail
        return None
    return text or None


def _infra_block_message(
    reason: str, detail: str, event: str, deploy_dir: Path, recovery: str | None = None,
    header: str | None = None,
) -> str:
    """Infrastructure failures (config unreadable, guards/ missing, a routed
    guard unimportable, Ctx construction failing, a bad overlay) BLOCK rather
    than fail open: an unguarded session can delete the owner's untracked
    files, a blocked one cannot. The message must be actionable on its own:
    name the specific failure, the file, and the recovery command for THIS
    event.

    2026-09-05 (Finding B): the master used to be hardcoded as
    `~/.claude/skills/ulthar/hooks` and the redeploy/diagnose commands pointed at
    the DEPLOYED `install.py`. Both are wrong for any clone that is not this
    machine's: the published procedure clones anywhere, and the deployed installer
    takes `~/.claude` as its skill root, so it can neither redeploy nor verify the
    real master. Both now come from the clone install.py recorded at deploy time
    (`<deploy_dir>/deployed-from.txt`), and when there is no record the message
    SAYS the master is unknown instead of naming a path -- a wrong path is what
    made this survive review.

    Defined here, above the `lib` import below, with no dependency on `lib.*`
    (pure stdlib string/Path formatting) -- this must stay callable even when
    `lib` itself failed to import.

    `recovery` overrides the generic redeploy-or-rollback block below with
    cause-specific advice (e.g. cfg['dispatch']['infra_recovery']['overlay']).
    It defaults to None because the two earliest call sites (lib import
    failure, config.json load failure) have no loaded `cfg` yet to pull a
    string from -- this stays the single place infra blocks are WORDED, but
    the wording for a cause whose fix is not "redeploy" is policy the caller
    supplies, not a second parallel function here.

    `header` overrides the generic "DEPLOYMENT failure ... enforcement could
    not start" framing directly below. It defaults to None (the generic
    wording) because that framing is right for every call site except one:
    a guard crashing while an overlay is in effect is NOT a deployment
    defect (enforcement DID start; see the recovery text this same call
    passes), so that one call site supplies its own header instead of
    stretching the generic sentence over a case it misdescribes.

    PE-19 (2026-09-05): this used to point the user at restoring
    `dispatch.ps1` -- the PowerShell masters were deleted the same day this
    note was written, so that rollback target no longer exists anywhere on
    disk, and the message that names a required rollback command must not
    name one that cannot be run. Recovery now names three things that
    actually work from the CURRENT (Python-only) state: fix-and-redeploy,
    `git checkout` the master tree to a known-good commit and redeploy, or
    disable just this event's hook entry in settings.json until it is fixed.

    2026-09-05: that generic recovery is WRONG for a bad overlay -- the
    overlay is not under `hooks/`, is not in git, and redeploying never
    touches it (see the overlay-specific `recovery` text callers pass for
    that cause). Keeping both means the generic block can stay accurate for
    the causes it actually applies to (config.json, a guard module, the
    Ctx build) without also mis-describing the overlay case. NOT tagged as a
    §7 parity exception: no old .ps1 master ever had an overlay to diverge
    from, no exit code changes, and no golden fixture pins it -- a PE id
    here would be undeclared-by-construction (see
    hooks/tests/parity.py::undeclared_source_pe_citations)."""
    if header is None:
        header = (
            "This is a DEPLOYMENT failure, not a rule violation -- enforcement could "
            "not start, so this tool call is blocked rather than silently unguarded.\n"
        )
    master = _recorded_master_root(deploy_dir)
    if master is not None:
        master_root = f'"{master}"'
        installer = f'python "{Path(master) / "hooks" / "install.py"}"'
        unknown_note = ""
    else:
        # Never guess. Naming a path that does not exist is exactly the failure
        # this message shape is supposed to prevent.
        master_root = "<the ULTHAR clone you installed from>"
        installer = f"python {master_root}/hooks/install.py"
        unknown_note = (
            f"This deploy has no record of its master ({deploy_dir / DEPLOY_ORIGIN_FILE} "
            "is missing -- it predates that record, or was hand-copied), so the paths "
            "below cannot be filled in. The master is the ULTHAR clone you ran "
            "`python hooks/install.py` from, wherever you cloned it.\n"
            "\n"
        )
    # NOT the deployed installer: it derives its skill root from its own location
    # (~/.claude), where there is no ULTHAR master to deploy or verify. Running it
    # is worse than useless -- it refuses now, but it used to look like it worked.
    return (
        f"BLOCKED (dispatcher infrastructure): {reason}\n"
        f"  detail: {detail}\n"
        f"{header}"
        "\n"
        f"{unknown_note}"
        "To diagnose:\n"
        f"  {installer} --verify\n"
        "\n"
        f"{recovery if recovery is not None else _generic_recovery(master_root, installer, event)}"
    )


def _generic_recovery(master_root: str, installer: str, event: str) -> str:
    return (
        "To recover (no .ps1 fallback exists -- the PowerShell masters were retired):\n"
        f"  1. Fix the master under {master_root}/hooks and redeploy:\n"
        f"       {installer}\n"
        "  2. Or roll the master tree back to a known-good commit and redeploy:\n"
        f"       git -C {master_root} log --oneline -- config.json hooks/\n"
        f"       git -C {master_root} checkout <good-commit> -- config.json hooks/\n"
        f"       {installer}\n"
        f"  3. Or, until fixed, remove or disable this event's (\"{event}\") hook "
        "entry in ~/.claude/settings.json so tool calls stop being blocked -- "
        "enforcement is off for that event only, until you restore it."
    )


def _resolve_event_from_argv(argv: list[str]) -> str:
    """Best-effort --event extraction without argparse: runs before we can
    assume any import succeeded, so it assumes only that sys.argv is a list
    of strings."""
    for i, a in enumerate(argv):
        if a == "--event" and i + 1 < len(argv):
            return argv[i + 1]
    return "<unknown>"


try:
    from lib import ctx as ctx_mod  # noqa: E402
    from lib import grant as grant_mod  # noqa: E402
    from lib import io as io_mod  # noqa: E402
except Exception as exc:  # noqa: BLE001
    # An uncaught ImportError here would exit the process with code 1, which
    # Claude Code treats as non-blocking -- the tool call would run unguarded.
    # io_mod is exactly what failed to import, so this cannot call
    # io_mod.write_stderr -- writes directly via sys.stderr.buffer instead,
    # matching that function's own UTF-8 encoding without depending on it.
    _event = _resolve_event_from_argv(sys.argv[1:])
    _msg = _infra_block_message(
        "hooks/lib could not be imported", f"{type(exc).__name__}: {exc}", _event, HERE)
    sys.stderr.buffer.write(_msg.encode("utf-8"))
    sys.exit(2)

ALLOW = io_mod.ALLOW


def _resolve_config_path() -> Path:
    """Env override for tests only: ULTHAR_CONFIG (absolute path). Never set in
    production. Otherwise the config sits NEXT TO dispatch.py -- the deployed
    ~/.claude/hooks/config.json (CONTRACT.md #0 'Config resolution')."""
    override = os.environ.get("ULTHAR_CONFIG")
    if override:
        return Path(override)
    return HERE / "config.json"


_FALLBACK_MAX_ERROR_LOG_BYTES = 2097152  # config.json's own logging.max_error_log_bytes
# default. This path fires precisely when config.json could not be LOADED, so
# there is no cfg to read the real value from -- the cap it enforces would
# otherwise be unbounded appends on every single tool call for as long as the
# config stays broken, which is a hot-spin, not a one-off (the exact class this
# whole cap mechanism exists to bound). io_mod is already imported by module
# scope above (an io_mod import failure exits before this function can run).


def _best_effort_config_error_log(phase: str, detail: str) -> None:
    """Used only when config.json or Ctx construction fails, so there is no
    ctx yet to log through. Resolved from the raw environment, wrapped in its
    own try; never raises and never decides the process exit code itself --
    main()'s config_load/build_ctx except blocks BLOCK (exit 2, via
    _infra_block_message), this function only best-effort logs alongside
    that. No stdout is ever written on this path."""
    try:
        home = Path(os.environ.get("USERPROFILE") or os.environ.get("HOME") or Path.home())
        log_path = home / ".claude" / "hooks" / "logs" / "config-errors.jsonl"
        import datetime

        entry = {
            "ts": datetime.datetime.now().astimezone().isoformat(),
            "phase": phase,
            "error": detail,
        }
        io_mod.append_jsonl(log_path, entry, max_bytes=_FALLBACK_MAX_ERROR_LOG_BYTES)
    except Exception:
        pass  # a hook runtime that cannot construct its own context must not raise


def _resolve_tools_pattern(rule: dict, cfg: dict) -> str:
    """`tools` XOR `tools_named` at one site. The sentinel '*' is never
    compiled -- checked by identity in _tool_matches()."""
    if "tools_named" in rule:
        name = rule["tools_named"]
        named = cfg["regex"]["named_alternations"]
        if name not in named:
            raise ctx_mod.ConfigError(f"dangling tools_named reference: {name!r}")
        return named[name]
    return rule["tools"]


def _tool_matches(tool_name: str, tools_spec: str, ctx) -> bool:
    if tools_spec == "*":
        return True
    pat = ctx_mod.compile_pattern(f"^({tools_spec})$", "text", ctx, "dispatch.events[].tools")
    return bool(pat and pat.search(tool_name or ""))


def _is_stdout_emitter(guard_id: str, cfg: dict) -> bool:
    """Declared emitter set (I-9), sourced from config (dispatch.stdout_emitters)
    rather than hardcoded: a second emitter not on this list would silently
    concatenate two payloads into one context blob."""
    return guard_id in cfg.get("dispatch", {}).get("stdout_emitters", [])


class GuardLoadError(Exception):
    """Raised when a guard module cannot be imported or lacks a callable `run`.
    This is an infrastructure failure, not a guard runtime error: dispatch()
    BLOCKS (see _run_guard, _infra_block_message) rather than failing open,
    because a guard that cannot even load means enforcement never started at
    all. Logged distinctly (see _run_guard) so 'the dispatcher cannot load a
    guard' is never indistinguishable from 'the guard ran and allowed'.
    install.py --verify additionally treats an unimportable guard listed in
    dispatch.events as a hard failure (deployment defect), not a runtime
    one."""


def _import_guard(guard_id: str, guards_dir: Path | None = None):
    """Imports hooks/guards/<guard_id>.py. `guards_dir` is a test-only override
    (ULTHAR_GUARDS_DIR) so tests can supply a stub guard without touching guards/.

    Do not move `import importlib.util` into this function: any
    import/assignment anywhere in a function body makes that name local to
    the whole function, so on the non-override path the bare name `importlib`
    would be read before being bound, raising UnboundLocalError -- which
    _run_guard's fail-open catch would silently swallow as ALLOW, making
    every real guard unreachable in production while every test stayed
    green."""
    override_dir = guards_dir or os.environ.get("ULTHAR_GUARDS_DIR")
    try:
        if override_dir:
            mod_path = Path(override_dir) / f"{guard_id}.py"
            spec = importlib.util.spec_from_file_location(f"_ulthar_guard_{guard_id}", mod_path)
            if spec is None or spec.loader is None:
                raise ImportError(f"cannot load spec for {mod_path}")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        else:
            mod = importlib.import_module(f"guards.{guard_id}")
    except Exception as exc:
        raise GuardLoadError(f"{guard_id}: {type(exc).__name__}: {exc}") from exc
    if not callable(getattr(mod, "run", None)):
        raise GuardLoadError(f"{guard_id}: module has no callable run(payload, cfg, ctx)")
    return mod


def _validate_result(result, guard_id: str, ctx) -> io_mod.Result:
    """I-3b: the dispatcher never trusts a guard's return value. Three of the
    four malformed cases below are treated like a raised exception (ALLOW +
    dispatch-errors.jsonl); the fourth -- a reasonless exit 2 -- keeps the
    block with a synthesised reason, logged to config_error_log instead. Do
    not collapse this into one rule."""
    if not isinstance(result, io_mod.Result):
        ctx.log_error(guard_id, RuntimeError(f"bad_result_type: {type(result)!r}"), [])
        return ALLOW
    if result.exit_code not in (0, 2):
        ctx.log_error(guard_id, RuntimeError(f"bad_exit_code: {result.exit_code!r}"), [])
        return ALLOW
    if result.exit_code == 2 and not result.stderr:
        ctx.log_config_error(
            "block_without_reason",
            f"guards.{guard_id}",
            "guard returned exit_code=2 with empty stderr",
            guard_scope=guard_id,
        )
        return io_mod.Result(
            2, "", f"BLOCKED by {guard_id} (no reason supplied -- see config-errors.jsonl)"
        )
    if result.stdout and not _is_stdout_emitter(guard_id, ctx.cfg):
        ctx.log_error(guard_id, RuntimeError("unexpected_stdout"), [])
        return io_mod.Result(result.exit_code, "", result.stderr)
    return result


def _overlay_object_in_frames(exc: BaseException, tainted: frozenset) -> bool:
    """True when an object the PROJECT overlay contributed was bound in some
    frame on this exception's traceback -- i.e. the guard had that object in
    hand when it died. Identity on the LOCAL ITSELF only, containers only.

    Not one level into a local container, though the `entries = cfg[...][...]`
    shape argues for it: that list is the MERGED list, holding base entries and
    overlay entries alike, so any guard binding it and then dying for an
    unrelated live-state reason -- tool_discipline's allowlist file vanishing
    between `exists()` and `read_lines()` -- would be blamed on an overlay that
    contributed a perfectly valid rule. That is the very misattribution this
    round removes, reintroduced one level deeper. Nothing real is lost: every
    shipped guard binds the loop ITEM (`entry`, `case`, `rule`), and
    ctx._collect_container_ids already tags containers nested inside an overlay
    item, so a crash holding only `case["triggers"]` still matches.

    Scalars are excluded because small strings and ints are interned and
    shared; an id() hit on one would be noise, not evidence. That exclusion is
    not a hole: a wrong-typed scalar never merges (lib/ctx.py's merge-time
    validation), so there is nothing for it to miss."""
    tb = exc.__traceback__
    while tb is not None:
        for value in list(tb.tb_frame.f_locals.values()):
            if id(value) in tainted:
                return True
        tb = tb.tb_next
    return False


def _attribute_to_project_overlay(guard_id: str, ctx, exc, event: str):
    """A guard raised. Decide whether the dispatcher may treat that as ALLOW.

    THE RULE, since 2026-09-06 round 4: if ANY overlay file was merged into this
    dispatch's configuration -- user layer, project layer, either -- a guard's
    runtime exception BLOCKS (exit 2), naming the overlay file(s). With no
    overlay merged, it fails open exactly as it always has, because then the
    crash is unambiguously ULTHAR's own bug and bricking every tool call over an
    ULTHAR bug is the worse failure (I-3).

    WHY THE RULE CHANGED, and it is the third time this function has been
    rewritten, so the reasoning matters more than the code. Everything below
    about attribution being un-answerable is still true; what changed is the
    conclusion drawn from it. The previous version asked "did overlay content
    cause this crash?" and used frame evidence to answer. A measured hole:
    `{"command_rules": {"redirect_noise_strips": [["x"]]}}` -- an UNDECLARED list
    path, replaced wholesale by the user layer with no shape check -- made
    tool_discipline raise inside `_build_probes` BEFORE it reached any rule, with
    no overlay object bound on any frame. Not attributable, so: fail open, rc=0,
    empty stderr, every rule in that guard silently gone. The merge-time shape
    checks did not see it either (the list is undeclared, so no schema exists to
    derive), and none of the pins covered `command_rules`.

    So the question is not answerable and this stops asking it. "An overlay is
    present and a guard crashed" is answerable, cheaply and exactly, and it is
    the right condition: a config file is in the cfg, and the one outcome that
    must never follow from a crash is the one that removes enforcement. The
    conservative direction is a BLOCK naming a deletable file.

    THE COST, stated rather than discovered: an ULTHAR bug that only surfaces
    while an overlay happens to be present now blocks tool calls in that
    repository instead of failing open. That is the same lockout a malformed
    overlay already produces today, with the same recovery -- delete the overlay
    file named in the message, from outside Claude Code -- and the message says
    so. Shape validation at merge time (lib/ctx.py) is what keeps this rare: it
    refuses what it can see, so what reaches here is the residue.

    Everything from here down is the earlier reasoning, kept because it is why
    the frame evidence is no longer load-bearing rather than why it was:

    A BACKSTOP AGAINST ACCIDENTAL COLLAPSE, not a trust boundary. A project
    overlay is allowed to switch rules off explicitly (lib/ctx.py:
    _apply_disables); what it must never do is switch them off by accident, by
    shipping content that crashes a guard into the fail-open. This function is
    the last line of that: a shape-valid entry with a semantically wrong VALUE
    blocks, naming the file, instead of silently removing everything that guard
    enforced. It was once the trust boundary and was defeated in three rounds,
    because every version of it answers a question that is only answerable
    heuristically: at crash time, did overlay content cause this crash? The
    refusal now sits at MERGE TIME instead (lib/ctx.py:_merge_list,
    _object_schema, _check_scalar_conforms): content whose shape a guard could
    not read is refused before any guard runs, so a guard crash is once again
    unambiguously ULTHAR's own bug and the fail-open below is correct without
    attributing anything. What this function still buys is the case where a
    guard crashes anyway while holding an overlay object -- a shape-valid entry
    with a semantically wrong VALUE (`target: "bogus"`, whose `probes[target]`
    raises KeyError). Then it BLOCKS naming the overlay file rather than
    silently allowing.

    Why the boundary is not attribution: appending one entry the consuming
    guard cannot read (a destroyer with no `suffix`, a design case with no
    `message`, an int among deploy_refs) makes that guard RAISE, and the
    fail-open below turns the raise into ALLOW -- a malformed append plus fail-open
    equals disable-by-crash -- and deciding after the fact whether the overlay
    was to blame misses whatever execution path nobody enumerated.

    Why merge-time validation is not the required-key table that rots: the
    schema is DERIVED from config.json's own entries at that path (the keys
    every existing entry carries, with their types), so it tracks config.json
    by construction. Only a list that ships empty declares its shape by hand,
    and one that declares nothing refuses appends.

    Why it is not a RE-RUN either (2026-09-05, third-party delta review): the
    previous version answered the question by running the guard a second time
    against the trusted cfg and treating "clean the second time" as proof the
    overlay was at fault. Guards are pure functions of (payload, cfg, ctx) by
    contract, but they READ THE WORLD through ctx -- approval files, markers,
    logs, `git rev-parse`. Two executions separated in time can legitimately
    disagree: a marker file that vanishes between them makes run 1 raise and
    run 2 succeed, and the mechanism then blocked the tool call while blaming a
    project overlay that had nothing to do with it. That is a lockout, and one
    of exactly that shape cost this project a session. Worse, ULTHAR is itself
    one of the actors: tool_discipline's pm-delete-allow ledger is CONSUMED on
    grant (`consume_on_use`), so a run-1 grant guarantees run 2 takes the
    not-granted branch -- the re-run's own premise broken by the re-run's own
    subject.

    So attribution now reads EVIDENCE FROM THE ONE EXECUTION THAT FAILED: was
    an object the project overlay contributed bound in any frame on the
    traceback? Yes -> the guard died holding project overlay content: BLOCK, naming
    the file. No -> ULTHAR's own bug, or an ordinary live-state change: fail
    open exactly as before. No second observation of the world is made, so
    there is nothing for the world to change underneath.

    Residues, stated:
      * a guard that COPIES an overlay item out (`suffix = entry["suffix"]`)
        and only then dies, with no overlay container left on any frame, is not
        attributable and fails open. Same for a guard that iterates the merged
        list without binding the item. No shipped guard reads either way; a new
        one that did would lose the block, not gain a bypass elsewhere -- and
        the alternative (matching on the merged list) blames innocent overlays,
        see _overlay_object_in_frames.
      * a bug in ULTHAR's own code that happens to crash while an overlay item
        is in scope is attributed to the overlay -- BLOCK instead of fail open.
        Conservative direction: loud, names one deletable file, and the recovery
        text is one `del` away, unlike a silently unguarded session.
      * a SCALAR appended at an 'append' path cannot be traced this way at
        all (interning).
    Every one of those was a security hole while the guarantee rested here; the
    same is true of the ones the reviewer added (an entry retained only as an
    object attribute, a traceback reachable only through `__cause__`, a
    comprehension frame). None is a hole now: shape-invalid content never
    merges, so what these gaps fail to attribute is a guard bug, which is
    exactly what fail-open is for. Do not re-promote this function to the
    boundary in order to close them.
    """
    sources = tuple(getattr(ctx, "overlay_sources", ()) or ())
    if not sources:
        # No overlay in this cfg: a guard exception is ULTHAR's own bug and the
        # fail-open is correct, exactly as it always was.
        return None
    named = " / ".join(sources)
    held = _overlay_object_in_frames(exc, ctx.project_overlay_ids)
    detail = (
        f"guard '{guard_id}' raised {type(exc).__name__}: {exc}\n"
        f"  an overlay was merged into this dispatch's configuration ({named}), so this\n"
        f"  crash is NOT treated as 'allow' -- see the recovery note below"
        + ("\n  (the guard died holding content that overlay contributed)" if held else "")
    )
    ctx.log_config_error(
        "overlay_guard_crash",
        f"guards.{guard_id}",
        f"{type(exc).__name__}: {exc} (overlay: {named}; held_overlay_object={held})",
        guard_scope=guard_id,
    )
    recovery = ctx.cfg.get("dispatch", {}).get("infra_recovery", {}).get("overlay")
    return io_mod.Result(2, "", _infra_block_message(
        f"guard '{guard_id}' crashed while an overlay was in effect",
        detail, event, ctx.deploy_dir, recovery=recovery,
        header=(
            "A guard crashed while an overlay was in effect; enforcement started, "
            "the guard did not survive this overlay.\n"
        )))


def _run_guard(guard_id: str, payload: dict, cfg: dict, ctx, event: str) -> io_mod.Result:
    """Imports and calls the guard. A guard's own runtime exception (the
    `mod.run(...)` call raising) is a bug in one rule and fails open, logged
    to dispatch-errors.jsonl (I-3). A failure to load the runtime -- the
    guard module cannot be imported, or its config-declared pattern will not
    compile -- is an infrastructure failure and BLOCKS instead, logged to
    config_error_log and printed as an actionable message (see
    _infra_block_message). The payload itself is never logged, only its
    top-level key names."""
    try:
        mod = _import_guard(guard_id)
    except GuardLoadError as exc:
        ctx.log_config_error("guard_load", f"guards.{guard_id}", str(exc), guard_scope=guard_id)
        msg = _infra_block_message(
            f"guard '{guard_id}' failed to import", str(exc), event, ctx.deploy_dir)
        return io_mod.Result(2, "", msg)
    try:
        result = mod.run(payload, cfg, ctx)
    except ctx_mod.PatternCompileError as exc:
        # Attribution FIRST: an uncompilable pattern that came from a project
        # overlay is not a defect in config.json, and must not be reported with
        # config.json's "fix the master and redeploy" recovery -- redeploying
        # never touches an overlay. Both paths BLOCK either way; what differs is
        # which file the message tells the reader to fix.
        attributed = _attribute_to_project_overlay(guard_id, ctx, exc, event)
        if attributed is not None:
            return attributed
        ctx.log_config_error("pattern_compile", exc.json_path, str(exc), guard_scope=guard_id)
        msg = _infra_block_message(
            f"guard '{guard_id}' has an invalid pattern in config.json ({exc.json_path})",
            str(exc), event, ctx.deploy_dir)
        return io_mod.Result(2, "", msg)
    except Exception as exc:  # noqa: BLE001 -- fail-open is the point (I-3), unchanged
        attributed = _attribute_to_project_overlay(guard_id, ctx, exc, event)
        if attributed is not None:
            return attributed
        ctx.log_error(guard_id, exc, sorted(payload.keys()))
        return ALLOW
    return _validate_result(result, guard_id, ctx)


def _guards_to_run(rule: dict, single_guard: str | None) -> list[str]:
    if single_guard is not None:
        return [g for g in rule["guards"] if g == single_guard]
    return list(rule["guards"])


_OVERLAY_NOTICE_DIRNAME = "overlay-notice"
_OVERLAY_NOTICE_MAX_AGE_SEC = 86400  # one day; see _claim_overlay_notice_once
_OVERLAY_NOTICE_NO_SESSION = "nosession"


def _overlay_notice_marker(ctx, payload: dict, kind: str) -> Path:
    """Where "once per session, per overlay state" is remembered.

    A dispatcher process does not outlive one tool call, so "once" needs
    somewhere on disk. It lives beside THE LOGS (ctx.logs_dir), not under
    ${TEMP} like artifact_hygiene's marker, for one property: the marker is
    unwritable exactly when the config_error_log this notice also writes is
    unwritable, so there is no state in which ULTHAR believes it has already
    told the operator something it could not record.

    The name is a digest of the session id, the overlay path and the disabled
    ids, so a NEW session, a DIFFERENT overlay, or the SAME overlay disabling
    something else is a fresh first encounter rather than a silenced repeat.
    `kind` separates the two surfaces -- the log line fires on any event, the
    operator notice only on a call whose stdout can carry it, so they are not
    the same "once"."""
    session_id = str(payload.get("session_id") or "") or _OVERLAY_NOTICE_NO_SESSION
    key = "\0".join([kind, session_id, str(ctx.project_overlay_path),
                     *getattr(ctx, "project_overlay_disabled", ())])
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    return ctx.logs_dir / _OVERLAY_NOTICE_DIRNAME / f"{digest}.{kind}"


def _claim_overlay_notice_once(marker: Path) -> bool:
    """True when this call is the first encounter (or the first in a day) and
    the claim was recorded -- and true ALSO when the claim could not be
    recorded at all.

    That failure direction is deliberate: an unwritable marker makes the notice
    REPEAT, never disappear. Repeating is a nuisance; disappearing is the
    silent removal this mechanism exists to prevent. It is self-limiting too --
    the marker directory sits beside the log, so a marker that cannot be
    written means the log write is failing as well, and there is no size-capped
    log left to flood.

    The one-day expiry does two jobs: it covers the `_OVERLAY_NOTICE_NO_SESSION`
    fallback (a payload with no session id would otherwise share one marker
    with every future session, which is silence again), and it bounds what
    accumulates in the log directory, at no extra syscall -- the stat that
    reads the mtime is the same one that tests for existence."""
    try:
        if time.time() - marker.stat().st_mtime < _OVERLAY_NOTICE_MAX_AGE_SEC:
            return False
    except OSError:
        pass  # absent or unreadable: a first encounter either way
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_bytes(b"")  # rewritten rather than touched: refreshes the mtime
    except OSError:
        pass  # visible-and-repeating beats invisible; see the docstring
    return True


def _log_overlay_disable_once(ctx, payload: dict) -> None:
    """VISIBILITY SURFACE 1 of 2 -- the durable record, in
    logging.config_error_log, naming the overlay file and every disabled rule
    id. This is what an operator diagnosing "why did that not block?" reads.

    Written on the FIRST DISPATCH that sees the disables, whatever the event
    and whatever the outcome. It used to be gated on `event == "SessionStart"`,
    which meant a session that started outside the repository and later entered
    it -- or one whose overlay appeared after startup -- honoured the disables
    immediately while recording them nowhere, with no later SessionStart
    guaranteed. Once per session per overlay is the cadence; the event is not
    what "once" means."""
    disabled = getattr(ctx, "project_overlay_disabled", ())
    if not disabled:
        return
    if not _claim_overlay_notice_once(_overlay_notice_marker(ctx, payload, "logged")):
        return
    ctx.log_config_error(
        "overlay_disable", "overlays.disable",
        f"project overlay {ctx.project_overlay_path} disables: {', '.join(disabled)}")


def _overlay_disable_notice(ctx, payload: dict) -> str:
    """VISIBILITY SURFACE 2 of 2 -- the text stated in the operator's context,
    once per session per overlay. Returns "" when there is nothing to say or it
    has already been said.

    The CALLER decides whether this dispatch's stdout can carry it and only
    calls this when it can (see dispatch()), so a call that cannot speak does
    not burn the session's one notice. Returned rather than written here so the
    single stdout writer in dispatch() stays single (I-9)."""
    disabled = getattr(ctx, "project_overlay_disabled", ())
    if not disabled:
        return ""
    if not _claim_overlay_notice_once(_overlay_notice_marker(ctx, payload, "notified")):
        return ""
    ids = ", ".join(disabled)
    return (
        f"[ULTHAR] The project overlay {ctx.project_overlay_path} switches off "
        f"{len(disabled)} rule(s) for work in this repository: {ids}. "
        "Project configuration takes precedence over your personal ULTHAR config; "
        "every other rule is still enforced. Delete or edit that file to restore them.\n"
    )


def _notice_stdout(cfg: dict, event: str, notice: str) -> str:
    """The notice, shaped for THIS event's stdout channel.

    On an event where the anchor emits raw text (skill_anchor.
    plain_stdout_events, today SessionStart) stdout IS raw text and the notice
    is raw text. On every other event stdout is read as the JSON envelope
    Claude Code turns into context, so the notice is wrapped in exactly the
    envelope guards/skill_anchor.py builds -- raw text there would be
    transcript-only and would not reach the operator the way the SessionStart
    notice does. The envelope key is read from skill_anchor's own config rather
    than restated, so the two cannot drift."""
    sa = cfg.get("skill_anchor", {})
    if event in sa.get("plain_stdout_events", []):
        return notice
    envelope = {sa.get("envelope_key", "hookSpecificOutput"): {
        "hookEventName": event,
        "additionalContext": notice,
    }}
    return json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))


def _context_capable_events(cfg: dict) -> set[str]:
    """The events whose stdout actually REACHES the operator, derived from the
    routing table rather than listed: the ones skill_anchor is routed on, since
    that guard's whole purpose is putting text into the session context and it
    would not be wired to an event where its output goes nowhere (today
    PreToolUse and SessionStart).

    This is what stops the notice being burned on an event that cannot carry
    it. `Stop` is routed (artifact_hygiene, dev_process_hygiene), emits no
    stdout of its own, and a Stop hook's JSON has no context channel -- and a
    `cd` into the repository as a turn's last Bash call makes the very next
    dispatch a Stop with the new cwd. Emitting there would consume the
    session's one notice and say nothing, which is the finding again."""
    events = cfg.get("dispatch", {}).get("events", {})
    return {ev for ev, rules in events.items()
            if any("skill_anchor" in (rule.get("guards") or []) for rule in rules)}


def dispatch(event: str, payload: dict, cfg: dict, ctx, single_guard: str | None,
             notice_fn: Callable[[], str] | None = None) -> io_mod.Result:
    """I-9 dispatcher I/O order: rules run in order; within a rule, guards run
    in order; stdout is accumulated here and written once by main(). The
    first exit_code == 2 writes that guard's stderr and terminates. A
    non-empty stderr on exit_code == 0 is deliberately dropped.

    `notice_fn` is the overlay-disable notice, passed as a callable rather than
    a string because calling it CONSUMES the session's one notice -- so it is
    called only at the moment this function has decided the notice can actually
    be emitted (see _emit)."""
    tool_name = str(payload.get("tool_name") or "")
    rules = cfg.get("dispatch", {}).get("events", {}).get(event, [])

    honour_stop = set(cfg.get("dispatch", {}).get("honour_stop_hook_active_for", []))
    stop_field = cfg.get("dispatch", {}).get("stop_hook_active_field", "stop_hook_active")
    stop_active = bool(payload.get(stop_field))

    plain = event in cfg.get("skill_anchor", {}).get("plain_stdout_events", [])
    can_speak = plain or event in _context_capable_events(cfg)

    def _emit(acc: str, blocked: bool) -> str:
        """The single stdout write (I-9), and the only place the overlay notice
        can be attached. Attaching is conditional because at most one guard per
        chain may emit stdout and skill_anchor's non-plain output is JSON:
        prefixing text to it, or writing anything after it, would corrupt it.
        So the notice rides along only when the chain emitted nothing itself,
        or when this event's own stdout shape is plain text -- and only on an
        event whose stdout reaches the operator at all (_context_capable_events).
        On a BLOCK nothing is attached -- a blocked call's stdout is not
        surfaced, and burning the session's one notice there would lose it; the
        log line (surface 1) has already been written by then either way."""
        text = acc
        if notice_fn is not None and not blocked and can_speak and (not acc or plain):
            notice = notice_fn()
            if notice:
                text = notice + acc if plain else _notice_stdout(cfg, event, notice)
        if text:
            io_mod.write_stdout_bytes(text)
        return text

    stdout_acc = ""
    for rule in rules:
        try:
            tools_spec = _resolve_tools_pattern(rule, cfg)
        except ctx_mod.ConfigError as exc:
            ctx.log_config_error("dispatch_route", "dispatch.events", str(exc))
            continue
        if not _tool_matches(tool_name, tools_spec, ctx):
            continue
        for guard_id in _guards_to_run(rule, single_guard):
            # I-12/S-6: the dispatcher owns the stop_hook_active short-circuit,
            # honoured identically in single-guard mode.
            if event == "Stop" and stop_active and guard_id in honour_stop:
                continue
            result = _run_guard(guard_id, payload, cfg, ctx, event)
            if result.stdout:
                stdout_acc += result.stdout
            if result.exit_code == 2:
                emitted = _emit(stdout_acc, blocked=True)
                io_mod.write_stderr(result.stderr)
                return io_mod.Result(2, emitted, result.stderr)
    return io_mod.Result(0, _emit(stdout_acc, blocked=False), "")


def main(argv: list[str]) -> int:
    io_mod.configure_streams()  # UTF-8 everywhere, before anything else (I-4)

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--event", required=True)
    parser.add_argument("--guard", default=None)
    args, _unknown = parser.parse_known_args(argv[1:])

    payload = io_mod.read_stdin_json()
    if payload is None:
        # A corrupted/unreadable stdin payload short-circuits to ALLOW before
        # any guard runs (fail-open, logged, I-3) -- no ctx exists yet, so use
        # the same best-effort path used when Ctx itself cannot be constructed.
        _best_effort_config_error_log("stdin_parse", "stdin was empty, unreadable, or not valid JSON")
        return 0
    event = str(payload.get("hook_event_name") or "").strip() or args.event

    config_path = _resolve_config_path()
    deploy_dir = config_path.parent
    try:
        cfg = ctx_mod.load_config(config_path)
    except Exception as exc:
        # config.json failing to load is a failure to load the runtime
        # itself, not a per-rule bug -- BLOCK rather than approve a
        # destructive tool call because enforcement could not even start.
        # Still best-effort logged (no ctx exists yet to log through).
        _best_effort_config_error_log("config_load", f"{type(exc).__name__}: {exc}")
        io_mod.write_stderr(_infra_block_message(
            f"config.json could not be loaded ({config_path})",
            f"{type(exc).__name__}: {exc}", event, deploy_dir))
        return 2

    try:
        ctx = ctx_mod.build_ctx(payload, cfg, event, deploy_dir)
    except Exception as exc:
        _best_effort_config_error_log("build_ctx", f"{type(exc).__name__}: {exc}")
        io_mod.write_stderr(_infra_block_message(
            "the dispatcher context could not be constructed",
            f"{type(exc).__name__}: {exc}", event, deploy_dir))
        return 2

    try:
        # Config-generalisation round: user/project overlays (see config.json "overlays").
        # A bad overlay (malformed JSON, a structurally broken rule entry, a
        # `disable` naming a rule that does not exist, or a project overlay
        # touching a path outside overlays.project_allowed_paths) is a
        # config-load failure exactly like a broken config.json -- BLOCK
        # rather than silently proceeding on the base config alone, which would
        # make a broken overlay indistinguishable from "no rules there".
        # merge_overlays additionally records, on ctx, which objects the
        # project layer contributed -- read only on the guard-exception
        # path, see _attribute_to_project_overlay.
        cfg = ctx_mod.merge_overlays(cfg, ctx)
        ctx.cfg = cfg
    except Exception as exc:
        ctx.log_config_error("overlay_merge", "overlays", f"{type(exc).__name__}: {exc}")
        # 2026-09-05: overlay recovery is NOT "redeploy" (see _infra_block_message's
        # `recovery` param) -- the text lives in config.json like every other
        # guard-facing message template, sourced from the cfg that's already
        # loaded at this point (unlike the two earlier failure sites above).
        overlay_recovery = cfg.get("dispatch", {}).get("infra_recovery", {}).get("overlay")
        io_mod.write_stderr(_infra_block_message(
            "a user/project overlay could not be applied",
            f"{type(exc).__name__}: {exc}", event, deploy_dir, recovery=overlay_recovery))
        return 2

    try:
        # Owner grants (config.json "grants"): one file per grant, named for the
        # rule it exempts. Validated ONCE per dispatch, before any guard runs, so
        # a grant naming a rule that does not exist -- or one naming a rule in
        # grants.never_exempt -- fails LOUDLY on the next tool call instead of
        # sitting on disk looking like a permission that was ignored. Same
        # treatment, and the same reason, as a `disable` that matches nothing.
        # Costs one is_dir() when no grant directory exists, which is the normal
        # state.
        #
        # PreToolUse ONLY, deliberately -- unlike the overlay merge, which must run
        # on every event because it decides what the config IS. Grants only apply
        # where guards block tool calls, and blocking on Stop would be a LOOP:
        # exit 2 -> the model continues -> Stop fires -> exit 2, with the model
        # unable to clear the cause, because every write route to a grant file is
        # now blocked for every agent. Loudness is not lost: the next tool call is
        # a PreToolUse.
        if event == "PreToolUse":
            grant_mod.validate(cfg, ctx)
    except Exception as exc:  # noqa: BLE001 -- any failure here must name the file
        ctx.log_config_error("grant_validate", "grants", f"{type(exc).__name__}: {exc}")
        grant_recovery = cfg.get("dispatch", {}).get("infra_recovery", {}).get("grants")
        io_mod.write_stderr(_infra_block_message(
            "an owner grant could not be honoured",
            f"{type(exc).__name__}: {exc}", event, deploy_dir, recovery=grant_recovery))
        return 2

    # Surface 1 (the durable log line) fires here, before any guard runs and
    # whatever this dispatch goes on to decide -- a disable that took effect
    # must leave a record even on a call that blocks or that cannot speak.
    # Surface 2 (the operator notice) is left to dispatch(), which is the only
    # place that knows whether this call's stdout can carry it.
    _log_overlay_disable_once(ctx, payload)
    _update_dev_process_ledger(event, payload, cfg, ctx)
    result = dispatch(event, payload, cfg, ctx, args.guard,
                      notice_fn=lambda: _overlay_disable_notice(ctx, payload))
    return result.exit_code


_LEDGER_UPDATE_TOOLS = {"bash", "powershell"}


def _update_dev_process_ledger(event: str, payload: dict, cfg: dict, ctx) -> None:
    """ulthar-enforcement-round1: keeps <state_dir>/session-procs/<session_id>.json
    current so guards/dev_process_hygiene.py's Stop-time read is scoped to this
    session's own descendants instead of a full process-table scan. Called on
    every Stop (where the guard is about to read it) and on PreToolUse for
    Bash/PowerShell only (where a dev server actually gets spawned) -- never
    on every tool call, which would pay psutil's C-extension import cost on
    the hottest path (row 9) for tool calls that can never start one.

    lib.procs (and therefore psutil) is imported LAZILY here, not at module
    level -- every OTHER event (Write/Edit/Agent/SendMessage/...) must still
    never load it. This is dispatcher-owned I/O, not a guard's: guards may not
    write ledger/session state outside their three named exceptions (CONTRACT
    section 1), and this runs before any guard even in the chain.

    Best-effort and silent on ordinary failure (a session root that resolves
    fine this dispatch and fails the next is not a defect, just a process that
    exited) -- update_session_ledger itself logs the one failure PM decision 1
    calls out (an unresolvable session root) once per session via a marker
    file, so nothing here needs to log a second time."""
    if not cfg.get("dev_processes", {}).get("enabled", True):
        return
    if event == "PreToolUse":
        tool = str(payload.get("tool_name") or "").casefold()
        if tool not in _LEDGER_UPDATE_TOOLS:
            return
    elif event != "Stop":
        return
    session_id = str(payload.get("session_id") or "")
    if not session_id:
        return
    try:
        from lib import procs as procs_mod  # noqa: PLC0415 -- lazy: keep psutil off every other event
        procs_mod.update_session_ledger(cfg, ctx, session_id)
    except Exception as exc:  # noqa: BLE001 -- best-effort, never blocks a tool call over this
        ctx.log_error("dev_process_ledger", exc, sorted(payload.keys()))


if __name__ == "__main__":
    sys.exit(main(sys.argv))
