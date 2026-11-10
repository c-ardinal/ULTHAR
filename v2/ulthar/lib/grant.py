"""hooks/lib/grant.py -- owner grants: a one-shot, main-chain-only exemption from
ONE named rule, addressed exactly the way an overlay `disable` is.

A grant is ONE EMPTY FILE the OWNER creates. Its path under `grants.dir` IS the
rule identifier:

    <grants.dir>/<dotted config path>/<rule id>
    ~/.claude/state/ulthar-grants/git_policy.force_push/push-force

Same addressing as `disable` (lib/ctx.py:_collect_disables), two lifetimes:
`disable` is standing and scoped to a repository; a grant is scoped to this
machine and is CONSUMED the first time it is used.

WHY A DIRECTORY OF EMPTY FILES AND NOT A LEDGER FILE. The delete rule's
pm-delete-allow is a line-oriented ledger, and consuming one line means
read-decide-rewrite, which needs the exclusive lock, the stale-lock reclaim and
the TOCTOU-safe claim marker that guards/tool_discipline.py carries ~200 lines
of. One file per grant needs none of that read-modify-write: the whole of
consumption is "decide who gets this file". So pm-delete-allow's code is left
untouched rather than migrated, and this module stays small.

WHAT DECIDES THE WINNER, and why it is NOT `os.remove`. The obvious consume is
`os.remove(grant)` -- "exactly one racer can delete a file". MEASURED ON
WINDOWS, THAT IS FALSE, and not marginally: two threads racing os.remove() on
the same path, 300 trials (tests/test_grants.py::check_consume_primitive_is_
exactly_one_winner), BOTH reported success in 144 of them (48%); only 156 saw
the loser get FileNotFoundError or PermissionError. Deleting on Windows sets a
disposition on a still-open directory entry, so a second delete arriving inside
that window succeeds too -- exactly the same class of Windows surprise
tool_discipline._reclaim_stale_lock's docstring records for `os.replace`. Had
this shipped, two concurrent tool calls would BOTH have been granted by one
grant file, roughly half the time they raced -- the double-spend the ledger's
lock exists to prevent, reintroduced by the mechanism that was supposed to
avoid needing one.

The winner is therefore decided by the one primitive this codebase has measured
as exactly-one-winner (300/300 in the same harness, and 300/300 where
_reclaim_stale_lock measured it): `os.open(claim, O_CREAT | O_EXCL)` on a marker
beside the grant. The claim winner re-checks the grant still exists (another
racer may have completed a whole consume in between), removes it, and removes
its own marker. `os.remove` still does the deleting; it just no longer decides
anything.

THE SELF-GRANT PROBLEM, which this module only half solves. A grant file the
AGENT can create is the agent permitting itself, not the user permitting it.
The structural half lives elsewhere and is named in config as `grants.protected`:
guards/grant_files.py blocks Write/Edit of every authority file, and
command_rules.tool_discipline/authority-file-in-shell blocks the shell paths to
the same files. What is NOT closed: nothing here authenticates an author, a
shell command that assembles the path from pieces evades a regex over the
command text, and an owner can always be asked to run something they do not
understand. See docs/DESIGN.md.

Consulted by a guard at the exact moment it would block, via `check()`, which
returns None (granted -- the caller continues its own rule loop) or the message
to block with, hint appended. The consult must stay INSIDE the guard's loop:
overriding a block from the dispatcher instead would skip every rule the guard
had not reached yet.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from . import ctx as ctx_mod
from . import io as io_mod
from . import paths as paths_mod

_SECTION = "grants"

# ponytail: fixed bounded retry around the winner's own os.remove of the grant
# -- absorbs the transient Windows sharing violation from a losing racer's
# still-closing exists()/stat() handle on the same path. Same shape, and the
# same reason, as tool_discipline._RECLAIM_REMOVE_RETRY_*: a handle-release
# wait, not a policy knob, so not read from config.
_REMOVE_RETRY_ATTEMPTS = 5
_REMOVE_RETRY_SLEEP_SEC = 0.005


class GrantError(Exception):
    """A grant file that cannot be honoured: a malformed identifier, one naming
    a rule that does not exist, or one naming a rule in `never_exempt`. Raised
    by validate() and turned into a BLOCK by dispatch.py -- the same treatment a
    `disable` that matches nothing gets (lib/ctx.py:_apply_disables), and for
    the same reason: a grant that silently did nothing would leave the owner
    believing a rule is exempted when it is not."""


def _gcfg(cfg: dict) -> dict:
    section = cfg.get(_SECTION)
    return section if isinstance(section, dict) else {}


def _enabled(gcfg: dict) -> bool:
    return bool(gcfg.get("enabled", False))


def _dir(gcfg: dict, ctx) -> Path | None:
    raw = gcfg.get("dir")
    return ctx.expand(raw) if raw else None


def path_for(ident: str, cfg: dict, ctx) -> Path | None:
    """The file whose existence IS the grant named by `ident`. None when grants
    are configured off. Segment-wise join (never a raw string concat) with '..'
    refused: identifiers come from config and from validate()'s own directory
    walk today, but a path-building helper that can be walked out of its own
    directory is a defect regardless of who calls it."""
    base = _dir(_gcfg(cfg), ctx)
    if base is None:
        return None
    parts = [p for p in paths_mod.to_posix(ident).split("/") if p and p != "."]
    if any(p == ".." for p in parts) or not parts:
        return None
    return base.joinpath(*parts)


def _log(ctx, gcfg: dict, outcome: str, ident: str, permitted: str = "") -> None:
    """One line per grant EVENT, not just per consumption: `consumed`,
    `ignored_subagent` (a grant existed and deliberately did not apply) and
    `race_lost` are all recorded, so an owner who wrote a grant and saw nothing
    happen can tell which of the three it was -- or, when there is no line at
    all and the file is still on disk, that the rule simply never fired.
    Wrapped so a logging failure never changes a guard's decision."""
    target = gcfg.get("log")
    if not target:
        return
    entry = {
        "ts": ctx.now().isoformat(),
        "event": ctx.event,
        "outcome": outcome,
        "rule": ident,
        "permitted": permitted,
    }
    try:
        io_mod.append_jsonl(
            paths_mod.expand(target, ctx), entry, max_bytes=gcfg.get("max_log_bytes")
        )
    except Exception:  # noqa: BLE001 -- an audit write must never raise into a guard
        pass


def _permitted(payload: dict, gcfg: dict) -> str:
    """WHAT the grant let through, for the audit line: the tool plus the command
    (or the file path). Truncated to grants.permitted_truncate so one
    pathological command cannot fill the capped log by itself."""
    tool = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    detail = str(
        tool_input.get("command")
        or tool_input.get("file_path")
        or tool_input.get("notebook_path")
        or ""
    )
    limit = gcfg.get("permitted_truncate")
    if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0:
        detail = detail[:limit]
    return f"{tool}: {detail}" if tool else detail


def _hint(template_key: str, ident: str, path: Path | None, gcfg: dict, ctx) -> str:
    template = gcfg.get(template_key, "")
    if not template or path is None:
        return ""
    log_path = gcfg.get("log", "")
    return (
        template.replace("{grant_path}", str(path))
        .replace("{ident}", ident)
        .replace("{log}", str(paths_mod.expand(log_path, ctx)) if log_path else "")
    )


def check(ident: str, message: str, payload: dict, cfg: dict, ctx) -> str | None:
    """The ONE call a guard makes at a block site.

    Returns None when the call is granted -- the caller must then CONTINUE its
    own rule evaluation, not return ALLOW, so the rules it had not reached yet
    still run. Otherwise returns the message to block with, with the hint that
    tells the owner how to permit this appended (the agent cannot act on that
    hint; that is the point).

    Consumption happens HERE, before the rest of the dispatch chain has run, so
    a later guard blocking the same tool call burns the grant on a command that
    never executed. That is the identical, deliberate trade tool_discipline's
    pm_allowlist already makes and for the identical reason: the failure is
    always 'a grant is lost' (the owner re-authorises), never 'an extra call is
    authorised'."""
    gcfg = _gcfg(cfg)
    if not _enabled(gcfg):
        return message
    path = path_for(ident, cfg, ctx)
    if path is None:
        return message
    if ident in set(gcfg.get("never_exempt", [])):
        # Unreachable in practice -- validate() refuses such a file at dispatch
        # time before any guard runs -- and kept anyway: this is the branch that
        # must not depend on validation having been wired in.
        return message
    if not path.exists():
        return message + _hint("hint", ident, path, gcfg, ctx)
    if gcfg.get("main_chain_only", True) and not ctx_mod.is_main_chain(payload):
        _log(ctx, gcfg, "ignored_subagent", ident, _permitted(payload, gcfg))
        return message + _hint("hint_subagent", ident, path, gcfg, ctx)
    outcome = _consume(path, gcfg, ctx)
    if outcome != "consumed":
        # Could not take it -> must not grant. Fails CLOSED in every branch: a
        # lost race, a claim we could not take, an unremovable grant. The cost
        # of failing closed is one re-authorisation; the cost of failing open is
        # a second call the owner never permitted.
        _log(ctx, gcfg, outcome, ident)
        return message + _hint("hint", ident, path, gcfg, ctx)
    _log(ctx, gcfg, "consumed", ident, _permitted(payload, gcfg))
    return None


def _consume(path: Path, gcfg: dict, ctx) -> str:
    """Takes the grant, or says why it could not. "consumed" is the only success.

    The claim marker -- not the removal -- decides the winner (see the module
    docstring's measurement). After winning it the grant is re-checked: a racer
    that lost the claim, waited, and came back could otherwise find the marker
    gone and claim a grant that has already been spent."""
    claim_path = path.with_name(path.name + str(gcfg.get("claim_suffix", ".claim")))
    if not _take_claim(claim_path, gcfg, ctx):
        return "race_lost"
    try:
        if not path.exists():
            return "race_lost"  # consumed by whoever held this claim before us
        for attempt in range(_REMOVE_RETRY_ATTEMPTS):
            try:
                os.remove(str(path))
                return "consumed"
            except FileNotFoundError:
                return "race_lost"
            except OSError:
                if attempt + 1 < _REMOVE_RETRY_ATTEMPTS:
                    time.sleep(_REMOVE_RETRY_SLEEP_SEC)
        return "consume_failed"
    finally:
        try:
            os.remove(str(claim_path))
        except OSError:
            pass  # best-effort: a marker left behind fails CLOSED (it only ever
            # denies a future grant at this one identifier until it is removed),
            # never open. `claim_stale_sec` is what stops that being permanent.


def _take_claim(claim_path: Path, gcfg: dict, ctx) -> bool:
    """O_CREAT|O_EXCL, the exactly-one-winner primitive, with ONE stale reclaim.

    A marker outlives its creator only if that process died between creating it
    and removing it -- microseconds. Without a reclaim that residue would make
    one identifier permanently ungrantable and, worse, SILENTLY so (validate()
    must skip markers: a marker legitimately exists for microseconds during
    another process's consume, so raising on one would turn an ordinary race
    into a session-wide block). `claim_stale_sec` bounds that: a marker older
    than a consume can possibly take is removed and the claim retried once.
    Robbing a live holder would need a consume that ran past that bound, which
    is one os.remove; the worst case if it ever happened is a double grant --
    i.e. exactly the behaviour the naive os.remove consume had ALL the time."""
    for attempt in (0, 1):
        try:
            fd = os.open(str(claim_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if attempt == 1 or not _claim_is_stale(claim_path, gcfg, ctx):
                return False
            try:
                os.remove(str(claim_path))
            except OSError:
                return False
            continue
        except OSError:
            return False
        os.close(fd)
        return True
    return False


def _claim_is_stale(claim_path: Path, gcfg: dict, ctx) -> bool:
    limit = gcfg.get("claim_stale_sec")
    if not isinstance(limit, (int, float)) or isinstance(limit, bool) or limit <= 0:
        return False
    try:
        age = time.time() - claim_path.stat().st_mtime
    except OSError:
        return False  # it vanished; the retry will settle it without a reclaim
    return age > limit


# ---------------------------------------------------------------- validation


def validate(cfg: dict, ctx) -> None:
    """Raises GrantError for any file in the grant directory that is not a
    grant this configuration can honour. Called once per dispatch, before any
    guard runs (dispatch.py), so a typo fails LOUDLY on the next tool call
    instead of sitting there looking like a permission that was ignored.

    Costs one is_dir() on the common path (no grant directory at all); the walk
    only happens once the owner has created one."""
    gcfg = _gcfg(cfg)
    if not _enabled(gcfg):
        return
    base = _dir(gcfg, ctx)
    if base is None or not base.is_dir():
        return
    never = set(gcfg.get("never_exempt", []))
    claim_suffix = str(gcfg.get("claim_suffix", ".claim"))
    for entry in sorted(base.rglob("*")):
        if not entry.is_file():
            continue
        if claim_suffix and entry.name.endswith(claim_suffix):
            # A claim marker legitimately exists for the microseconds another
            # process is consuming a grant. Raising on one would turn an
            # ordinary race into a block for every session -- see _take_claim.
            continue
        ident = paths_mod.to_posix(os.path.relpath(str(entry), str(base)))
        path, sep, rule_id = ident.partition("/")
        if not sep or not path or not rule_id:
            raise GrantError(
                f"grant file {entry} is not <dotted config path>/<rule id> -- a grant "
                f'names one rule, e.g. "git_policy.force_push/push-force" (a directory '
                f"named for the config path, holding a file named for the rule id)"
            )
        if ident in never:
            raise GrantError(
                f"grant file {entry} names {ident!r}, which is in grants.never_exempt "
                f"and cannot be granted: {_never_exempt_reason(ident, cfg)}"
            )
        ids = ctx_mod.rule_ids_at(cfg, path)
        if ids is None:
            raise GrantError(
                f"grant file {entry} names {path!r}, which is not a list of rules in "
                f"config.json"
            )
        if rule_id not in ids:
            raise GrantError(
                f"grant file {entry} matches no rule. {path!r} holds: "
                f"{', '.join(repr(i) for i in ids) or '(nothing)'}"
            )


def _never_exempt_reason(ident: str, cfg: dict) -> str:
    """The `_note_never_exempt` prose is the reason, and it lives in config
    beside the list rather than being restated here."""
    return _gcfg(cfg).get("_note_never_exempt", "see grants.never_exempt in config.json")


# ---------------------------------------------------------------- protected paths


def resolve_ref(cfg: dict, ref: str) -> list:
    """Resolves a dotted config reference to every value it names, using the
    same `key[]` element step config.json's own regex.pattern_fields paths use.
    `command_rules.tool_discipline[].pm_allowlist.file` yields the one entry
    that has a pm_allowlist. Missing keys yield nothing rather than raising:
    a reference to a path a fork removed must degrade to 'nothing to protect
    there', which the caller reports, not to a crash inside a guard."""
    nodes: list = [cfg]
    for segment in ref.split("."):
        is_list = segment.endswith("[]")
        key = segment[:-2] if is_list else segment
        nxt: list = []
        for node in nodes:
            if not isinstance(node, dict) or key not in node:
                continue
            value = node[key]
            if is_list:
                if isinstance(value, list):
                    nxt.extend(value)
            else:
                nxt.append(value)
        nodes = nxt
    return nodes


def protected_targets(cfg: dict, ctx) -> list[tuple[dict, Path]]:
    """[(protected entry, expanded path)] for guards/grant_files.py. Every
    target is named by REFERENCE to the single place that path already lives
    (grants.dir, overlays.user_path, the delete rule's pm_allowlist.file), so
    moving one of those moves its protection with it instead of leaving this
    list pointed at a path nothing uses any more."""
    out: list[tuple[dict, Path]] = []
    for entry in _gcfg(cfg).get("protected", []):
        if not isinstance(entry, dict):
            continue
        for value in resolve_ref(cfg, str(entry.get("ref", ""))):
            if isinstance(value, str) and value.strip():
                out.append((entry, ctx.expand(value)))
    return out
