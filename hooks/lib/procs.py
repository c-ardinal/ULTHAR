"""hooks/lib/procs.py -- dev-process discovery, ONE code path over psutil.

CONTRACT.md #2 (lib/procs.py), I-13, PE-6, PE-12. psutil is the SOLE non-stdlib
import in the runtime. Historically reachable ONLY from
guards/dev_process_hygiene.py (Stop) and stop_dev_orphans.py (CLI) -- see
I-13.2 -- with nothing on the PreToolUse path importing it.

ulthar-enforcement-round1 (session-scoping) DELIBERATELY WIDENS THAT: dispatch.py
now also imports this module directly (lazily, inside a function -- never at
its own module level) on PreToolUse for Bash/PowerShell tool calls, to keep
each session's process ledger current at the one place new descendants
actually appear. Every OTHER event, and every OTHER tool on PreToolUse, still
never touches it -- the guard-purity AST scan (test_contract.py check1b) only
ever asserted psutil stays out of guards/*.py OTHER than
dev_process_hygiene.py, which remains true; it never asserted psutil off
dispatch.py itself. See the session-ledger section below for the trade this
buys (session-scoped orphan detection, which a live-only ancestry walk from
the root cannot provide once a descendant's immediate parent has died).

No fallback, deliberately: a fallback means two behaviours to test and a guard that
can quietly change semantics. A missing psutil is made loud at deploy/verify time
(install.py), not silently degraded here.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import NamedTuple

import psutil  # THE ONLY non-stdlib import in the runtime (I-13)

from . import ctx as ctx_mod  # compile_text_pattern is the one authority for pattern compilation
from . import io as io_mod  # round-4 send-back item 6: reuses the append-lock primitive for the
                             # ledger's read-modify-replace and the sweep's re-stat-under-lock


class ProcInfo(NamedTuple):
    pid: int
    ppid: int
    name: str
    cmdline: str
    # round-4 send-back item 4: kill() now re-verifies (pid, create_time)
    # identity immediately before each signal, so a ledger-sourced ProcInfo
    # (find_dev_processes_in_ledger) carries create_time through to the CLI.
    # Defaulted to None (not required) so every existing construction of this
    # NamedTuple elsewhere (test_contract.py's direct instantiation, the
    # legacy full-table find_dev_processes/list_processes pair, which never
    # had create_time to offer) keeps working unchanged.
    create_time: float | None = None


def list_processes(ctx, cfg: dict) -> list[ProcInfo]:
    """psutil.process_iter(cfg['dev_processes']['discovery']['attrs']). cmdline is a
    LIST from psutil and is joined with cmdline_join (' ') so the regex sees the same
    flat string the masters matched. Per-process NoSuchProcess / AccessDenied /
    ZombieProcess are caught and that process is SKIPPED (iter_error_policy) -- the
    table is a moving target and one unreadable system process must not abort the
    sweep."""
    disc = cfg["dev_processes"]["discovery"]
    attrs = disc["attrs"]
    join = disc["cmdline_join"]
    out: list[ProcInfo] = []
    for proc in psutil.process_iter(attrs):
        try:
            info = proc.info
            cmdline_list = info.get("cmdline") or []
            cmdline = join.join(cmdline_list)
            out.append(
                ProcInfo(
                    pid=info.get("pid") or 0,
                    ppid=info.get("ppid") or 0,
                    name=info.get("name") or "",
                    cmdline=cmdline,
                )
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return out


def _normalise_name(name: str, strip_exe: bool = True) -> str:
    """Strip a TRAILING '.exe' only (draw.io.exe -> draw.io, not draw) when
    `strip_exe` is true (dev_processes.strip_exe_suffix_when_matching),
    casefold either way."""
    if strip_exe and name.casefold().endswith(".exe"):
        name = name[:-4]
    return name.casefold()


def find_dev_processes(
    cfg: dict,
    ctx,
    orphans_only: bool = True,
    root: str | None = None,
) -> list[ProcInfo]:
    """name (trailing '.exe' stripped, casefolded) in cfg['dev_processes']['names']
    AND joined cmdline matches cfg['dev_processes']['pattern']
    AND (orphan: ppid not in the live pid set) OR (not orphans_only and
         (root is None or root in cmdline)).

    Orphan-ness and the root filter are OR'd, not branched: under
    `orphans_only=False` (--all mode) a genuine orphan must still match even when
    it sits outside `root` -- branching instead (root test only under --all)
    would silently miss a dangling process just because it started elsewhere,
    defeating --all's purpose of widening the match set."""
    dp = cfg["dev_processes"]
    names = {n.casefold() for n in dp["names"]}
    pattern = ctx_mod.compile_text_pattern(dp["pattern"], "dev_processes.pattern")  # B3 sweep
    strip_exe = dp.get("strip_exe_suffix_when_matching", True)

    all_procs = list_processes(ctx, cfg)
    live_pids = {p.pid for p in all_procs}

    out: list[ProcInfo] = []
    for proc in all_procs:
        if _normalise_name(proc.name, strip_exe) not in names:
            continue
        if not pattern.search(proc.cmdline):
            continue
        is_orphan = proc.ppid not in live_pids
        # `root` is matched case-insensitively only on Windows (kind:"path" rules,
        # N-1): unconditional case-insensitivity would wrongly match a `/Repo`
        # root filter against a `/repo` cmdline on Linux.
        if root is None:
            root_match = not orphans_only
        elif ctx.is_windows:
            root_match = (not orphans_only) and (root.casefold() in proc.cmdline.casefold())
        else:
            root_match = (not orphans_only) and (root in proc.cmdline)
        if is_orphan or root_match:
            out.append(proc)
    return out


def kill(targets: list[tuple[int, float]], ctx, cfg: dict) -> list[tuple[int, str]]:
    """terminate() the set -> psutil.wait_procs(grace) -> kill() the survivors.
    Per-PID NoSuchProcess/AccessDenied become ('skip <pid> <name>: <reason>') lines,
    matching the masters' try/catch output shape.

    round-4 send-back item 4: `targets` is (pid, create_time) pairs -- the
    LEDGER's recorded identity, not a bare pid list -- and the identity is
    RE-VERIFIED immediately before EACH signal (both terminate() and the
    follow-up kill() on a grace-period survivor). Time passes between when
    stop_dev_orphans.py computed its candidate list and when this function
    actually acts; if the OS reused that pid for an unrelated process in the
    meantime, psutil.Process(pid) would happily construct a handle to that
    NEW process, and .terminate()/.kill() would kill the WRONG thing. psutil's
    own internal reuse guard protects a Process object against reuse that
    happens AFTER construction, but not against reuse that already happened
    BEFORE it -- which is exactly the window here -- so the create_time
    comparison against the caller-supplied ledger value is this function's
    own job, not something to rely on psutil for."""
    so = cfg["dev_processes"]["stop_orphans"]
    grace = so["terminate_grace_seconds"]
    skips: list[tuple[int, str]] = []
    procs: list = []
    expected_create_time: dict[int, float] = {}
    for pid, create_time in targets:
        expected_create_time[pid] = create_time
        try:
            p = psutil.Process(pid)
            if abs(p.create_time() - create_time) > 1e-6:
                skips.append((pid, "skip %d <unknown>: pid reused by a different "
                                    "process since it was recorded, not killed" % pid))
                continue
            p.terminate()
            procs.append(p)
        except psutil.NoSuchProcess:
            skips.append((pid, "skip %d <unknown>: already gone" % pid))
        except psutil.AccessDenied as exc:
            skips.append((pid, "skip %d <unknown>: access denied (%s)" % (pid, exc)))

    gone, alive = psutil.wait_procs(procs, timeout=grace)
    for p in alive:
        try:
            if abs(p.create_time() - expected_create_time.get(p.pid, -1.0)) > 1e-6:
                skips.append((p.pid, "skip %d <unknown>: pid reused during the grace "
                                       "period, not killed" % p.pid))
                continue
            p.kill()
        except psutil.NoSuchProcess:
            skips.append((p.pid, "skip %d <unknown>: already gone" % p.pid))
        except psutil.AccessDenied as exc:
            skips.append((p.pid, "skip %d <unknown>: access denied (%s)" % (p.pid, exc)))

    return skips


def listening_ports(cfg: dict, ctx) -> tuple[list[int], str | None]:
    """psutil.net_connections(kind='inet'), status == 'LISTEN', laddr.port in ports.
    Returns (ports_found, error). On AccessDenied returns ([], reason) and the
    caller prints the DEGRADED message -- never 'ports free'."""
    ports = set(cfg["dev_processes"]["ports"])
    try:
        conns = psutil.net_connections(kind="inet")
    except psutil.AccessDenied as exc:
        return [], str(exc)
    found = []
    for c in conns:
        if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port in ports:
            found.append(c.laddr.port)
    return sorted(set(found)), None


# ============================================================ session ledger
# ulthar-enforcement-round1: scope dev-process hygiene to THIS session's own
# descendants (config.json dev_processes.session_root / dev_processes.
# session_ledger). The guard's Stop-time READ (find_session_orphans /
# find_dev_processes_in_ledger) never enumerates the process table at all --
# it walks the ledger's own `entries` dict and probes only those specific
# PIDs. The ledger WRITER (update_session_ledger) enumerates via _ppid_map()
# (one bulk OS query, zero per-process Process() construction) and harvests
# name()/cmdline() only for pids it does not already know unchanged. Every
# find_dev_processes/list_processes/kill/listening_ports function above is
# either UNCHANGED (find_dev_processes/list_processes/listening_ports, still
# backing test_contract.py's direct calls) or changed only in its identity
# contract (kill(), round 4 item 4).


class LedgerError(Exception):
    """A session ledger file exists but is not readable as the shape this
    module writes (corrupt JSON, wrong top-level type, an entry missing a
    required key). Distinct from "no ledger file" (the ordinary, silent,
    nothing-recorded-yet case, which is None, never an exception) -- callers
    log ONLY this case to config_error_log (dev_process_hygiene.run) and treat
    it as inert (ALLOW), per PM decision: "台帳が無い／壊れている -> 不活性で
    rc=0、config_error_log に1行" read as "壊れている" (a file that exists and
    cannot be trusted), not "無い" (nothing there yet, which is not an error)."""


_LEDGER_ENTRY_KEYS = {"pid", "ppid", "name", "cmdline", "create_time", "first_seen"}
_SESSION_ID_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def _sanitize_session_id(session_id: str) -> str:
    """round-4 item 5: INJECTIVE sanitisation. round 3's char-substitution
    alone (`[^A-Za-z0-9._-]` -> '_') was NOT injective: "a/b" and "a_b" both
    sanitise to "a_b" and would silently share one ledger file -- two
    genuinely different sessions merging their process lists into one. Fixed
    by appending a short hash of the RAW (pre-substitution) id: two different
    raw ids collide in their hash suffix only with negligible probability,
    even when their substituted PREFIX is identical, so the combined name is
    injective in practice. (The brief's other offered option, percent-encoding
    every unsafe byte, is exactly as injective and was not chosen only because
    it can make a pathological id's filename far longer; the hash suffix stays
    at a fixed 8 hex chars regardless of input length.) An empty/missing
    session_id still maps to a fixed 'unknown' name (its hash is of the empty
    string, which is fine: there is exactly one "no session id" case, and it
    always deserves the SAME ledger, not a fresh one per hook call)."""
    raw = session_id or ""
    cleaned = _SESSION_ID_UNSAFE.sub("_", raw) or "unknown"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    return f"{cleaned}-{digest}"


def _proc_identity_text(proc) -> tuple[str, bool]:
    """(text, complete) -- exe() path + cmdline, space-joined and
    slash-normalised, for session_root.candidate_substrings matching.
    `complete` is True only when BOTH exe() and cmdline() were actually
    readable; either one denied (AccessDenied is the common case: exe() on
    Windows without elevation, or cmdline() on a protected process) drops
    that field from `text` AND flips `complete` False.

    round-7 send-back HIGH (partial identity): round 6 dropped an unreadable
    field silently and returned only the shrunken text, so a caller doing a
    plain substring test against it could not tell "genuinely no match" from
    "no match in the PART we could read" -- a foreign root identifiable ONLY
    by its exe() path (e.g. a cmdline that reveals nothing, common for a
    compiled binary launched with no arguments) would fail the substring
    test on the readable cmdline alone and be confirmed `False` (not a
    boundary), exactly the false-negative the round-6 HIGH fix was meant to
    close. `complete` lets _matches_candidate_substrings tell the two cases
    apart: `False` may only be confirmed from a COMPLETE text; a `True`
    match, in contrast, is trustworthy even from partial text (a substring
    that DID appear is a real finding regardless of what else was missing)."""
    parts: list[str] = []
    complete = True
    try:
        parts.append(proc.exe())
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        complete = False
    try:
        parts.append(" ".join(proc.cmdline()))
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        complete = False
    return " ".join(parts).replace("\\", "/").casefold(), complete


def _matches_candidate_substrings(proc, cfg: dict) -> bool | None:
    """round-4 item 1: THE boundary predicate, and (together with
    candidate_names as ROOT RESOLUTION's own fallback, never used for the
    descendant boundary) the more precise half of root discrimination.
    Substring-only, deliberately: a bare process NAME ('node', 'claude') is
    too generic to ever mean "stop descending here" -- a perfectly ordinary
    Vite dev server is a 'node' process, and treating every 'node' as a
    session boundary would stop the descendant walk from ever reaching the
    `workerd` process Vite spawns underneath it. Only a positive exe-path/
    cmdline match (session_root.candidate_substrings) is specific enough to
    say "this is another Claude Code host", never a name alone.

    round-7 send-back HIGH: THREE-VALUED. `True` (a substring matched --
    trustworthy even from PARTIAL text, a match is a match regardless of
    what else was unreadable), `False` (confirmed no match, but ONLY when
    the identity text was COMPLETE -- every field the predicate reads was
    actually readable), or `None` ("unknown" -- no match found, but the text
    was incomplete, so a match in the UNREADABLE part cannot be ruled out).
    Callers (the descendant boundary decision, is_root_boundary) already
    treat `None` the same as `True` for "never descend" -- see
    _harvest_from_proc / _descendant_pids."""
    sr = cfg["dev_processes"]["session_root"]
    substrings = [s.casefold() for s in sr.get("candidate_substrings", [])]
    text, complete = _proc_identity_text(proc)
    if any(s in text for s in substrings):
        return True
    return False if complete else None


def _matches_candidate_name(proc, cfg: dict) -> bool:
    """The ROOT-RESOLUTION-ONLY name fallback (session_root.candidate_names),
    tried only after every ancestor has been checked for a substring match
    and none matched anywhere in the chain (round-4 item 1). NEVER used as
    the descendant boundary predicate -- see _matches_candidate_substrings."""
    sr = cfg["dev_processes"]["session_root"]
    try:
        name = _normalise_name(proc.name(), strip_exe=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False
    return name in {n.casefold() for n in sr["candidate_names"]}


def resolve_session_root(cfg: dict, ctx) -> tuple[int | None, str | None]:
    """Walks psutil ancestry from THIS hook process upward looking for the
    session's root (the Claude Code host).

    round-4 item 1 (M-1 correction): a bare process NAME collides too easily
    to trust as soon as it's seen (measured: 11 of 13 real `claude.exe`
    processes on a review machine were the Claude DESKTOP app, not the CLI;
    `node` matches VS Code's own extension-host process, which sits in the
    SAME ancestor chain as the actual session host, often CLOSER to the hook
    process than the real host is). So the WHOLE ancestor chain (up to
    max_ancestor_hops) is collected FIRST, then checked in two full passes:
    (1) every ancestor for a session_root.candidate_substrings match -- the
    NEAREST one found wins; only if (1) finds nothing anywhere in the chain,
    (2) every ancestor for a session_root.candidate_names match (the
    original, name-only rule, kept as the fallback for a deployment that has
    not customised the substrings) -- again nearest wins. If NEITHER pass
    finds anything, the nearest non-shell ancestor is the last-resort
    fallback (per PM decision: 'とれない場合はフックの直近の非シェル祖先を根と
    する'). Returns (pid, None) on success, or (None, reason) when nothing at
    all could be found within max_ancestor_hops -- the caller
    (update_session_ledger) treats that as inert and logs it ONCE."""
    sr = cfg["dev_processes"]["session_root"]
    shells = {n.casefold() for n in sr["shell_names"]}
    max_hops = sr["max_ancestor_hops"]
    try:
        parent = psutil.Process(os.getpid()).parent()
    except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
        return None, f"cannot inspect the hook process's own parent: {exc}"

    # (pid, is_shell, matches_substring, matches_name), nearest ancestor first.
    ancestors: list[tuple[int, bool, bool, bool]] = []
    hops = 0
    while parent is not None and hops < max_hops:
        try:
            pid = parent.pid
            matches_sub = _matches_candidate_substrings(parent, cfg)
            name = _normalise_name(parent.name(), strip_exe=True)
            matches_name = name in {n.casefold() for n in sr["candidate_names"]}
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            break
        ancestors.append((pid, name in shells, matches_sub, matches_name))
        try:
            parent = parent.parent()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            parent = None
        hops += 1

    for pid, _is_shell, matches_sub, _matches_name in ancestors:
        if matches_sub:
            return pid, None
    for pid, _is_shell, _matches_sub, matches_name in ancestors:
        if matches_name:
            return pid, None
    for pid, is_shell, _matches_sub, _matches_name in ancestors:
        if not is_shell:
            return pid, None
    return None, (
        "no session root found within max_ancestor_hops ancestors of the hook "
        "process (no ancestor matched session_root.candidate_substrings or "
        "candidate_names, and every ancestor climbed looked like a shell in "
        "session_root.shell_names)"
    )


def process_create_time(pid: int) -> float | None:
    """One targeted psutil.Process(pid).create_time() lookup, or None if the
    pid is gone/denied. Used by stop_dev_orphans.py to get ITS OWN resolved
    root's create_time for find_ledgers_by_root's (root_pid, root_create_time)
    match -- a single call, not a table scan."""
    try:
        return psutil.Process(pid).create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


def _root_still_alive(root_pid: int, root_create_time: float) -> bool | None:
    """Three-valued (round-6 send-back MEDIUM 2): True (confirmed the SAME
    process, still alive), False (confirmed gone -- NoSuchProcess, or a live
    pid whose create_time no longer matches, i.e. reused), or None
    ("unknown" -- AccessDenied, alive but unreadable). Used by the sweep to
    decide whether a ledger's root has died. Previously AccessDenied
    collapsed into False ("dead"), which let the sweep delete a ledger whose
    root was ACTUALLY STILL ALIVE, just unreadable -- callers must treat
    None the safe direction for their context (the sweep never deletes on
    None, only on a confirmed False)."""
    try:
        proc = psutil.Process(root_pid)
    except psutil.NoSuchProcess:
        return False
    try:
        return abs(proc.create_time() - root_create_time) < 1e-6
    except psutil.AccessDenied:
        return None


def _ledger_dir(cfg: dict, ctx) -> Path:
    return ctx.state_dir / cfg["dev_processes"]["session_ledger"]["subdir"]


def session_ledger_path(cfg: dict, ctx, session_id: str) -> Path:
    return _ledger_dir(cfg, ctx) / f"{_sanitize_session_id(session_id)}.json"


def _load_ledger_file(path: Path) -> dict | None:
    """None when absent (ordinary). Raises LedgerError when present but not a
    readable ledger of the expected shape."""
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise LedgerError(f"{path}: {type(exc).__name__}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("entries"), dict):
        raise LedgerError(f"{path}: not a ledger object (missing/invalid 'entries')")
    for pid_str, entry in data["entries"].items():
        if not isinstance(entry, dict) or not _LEDGER_ENTRY_KEYS <= set(entry):
            raise LedgerError(f"{path}: entry {pid_str!r} missing one of {sorted(_LEDGER_ENTRY_KEYS)}")
    return data


def read_session_ledger(cfg: dict, ctx, session_id: str) -> dict | None:
    """Public read used by guards/dev_process_hygiene.py. See _load_ledger_file
    for the None-vs-LedgerError contract."""
    return _load_ledger_file(session_ledger_path(cfg, ctx, session_id))


def find_ledgers_by_root(cfg: dict, ctx, root_pid: int, root_create_time: float) -> list[dict]:
    """Used by stop_dev_orphans.py, which has no session_id (it is invoked
    directly, not via a hook payload): scans every ledger file for ones whose
    recorded (root_pid, root_create_time) match THIS process's own resolved
    session root, so the CLI scopes to its own session exactly the way the
    guard does, without ever touching a full process-table scan.

    Returns ALL matches, not just the first -- multiple session_ids (main
    chain + sub-agents) legitimately resolve to the SAME root (they share one
    Claude Code host), so each keeps its own ledger file under a different
    session_id but they all describe descendants of the same root process.
    The CLI merges their entries so a dev server one sub-agent started is
    still found by `python stop_dev_orphans.py` run for the whole session.
    `root_create_time` disambiguates a reused root_pid across two DIFFERENT
    actual host processes over the machine's lifetime (a stale ledger from a
    long-dead session whose PID got recycled must never match). A ledger that
    fails to parse is skipped (best-effort discovery, not the guard's
    fail-loud read)."""
    ledger_dir = _ledger_dir(cfg, ctx)
    if not ledger_dir.is_dir():
        return []
    out = []
    for path in sorted(ledger_dir.glob("*.json")):
        try:
            data = _load_ledger_file(path)
        except LedgerError:
            continue
        if data is None:
            continue
        if data.get("root_pid") != root_pid:
            continue
        if abs(data.get("root_create_time", -1.0) - root_create_time) > 1e-6:
            continue
        out.append(data)
    return out


def _root_fail_marker(cfg: dict, ctx, session_id: str) -> Path:
    return _ledger_dir(cfg, ctx) / f"{_sanitize_session_id(session_id)}.rootfail"


def _ppid_map() -> dict[int, int] | None:
    """The platform's BULK pid->ppid map (psutil._psplatform.ppid_map()),
    which is what psutil.Process.children(recursive=True) already uses
    internally to run in 23-28ms -- ONE OS-level bulk query, zero per-process
    Process object construction. process_iter() is not a cheaper substitute
    for this: it CONSTRUCTS a Process object per pid (measured 12-16s cold,
    reproduced directly on this machine too -- every hook dispatch is a fresh
    interpreter with no warm handle cache) -- the cost is the construction,
    not which attributes are read off it afterward.

    Private psutil API, used ONLY here and wrapped: returns None when
    unavailable (a psutil version/platform that does not expose it), in
    which case update_session_ledger falls back to reconstructing the
    descendant subtree from children(recursive=True) itself (see its
    docstring) -- documented, not silent."""
    try:
        from psutil import _psplatform  # noqa: PLC0415 -- private, isolated to this one call site
        return dict(_psplatform.ppid_map())
    except Exception:
        return None


def _children_of(ppid_map: dict[int, int]) -> dict[int, list[int]]:
    children: dict[int, list[int]] = {}
    for pid, ppid in ppid_map.items():
        children.setdefault(ppid, []).append(pid)
    return children


def _harvest_from_proc(proc, ppid: int, cfg: dict, now: float) -> dict:
    """ONE psutil.Process object, read for BOTH the ledger entry (name,
    cmdline, create_time) AND the M-1 boundary check
    (_matches_candidate_substrings) -- the expensive part is constructing the
    Process object, not the attribute reads, so a caller that already has one
    must never construct a second for the same pid.

    round-4 item 8 / round-6 send-back HIGH (inaccessible intermediary):
    NoSuchProcess (truly gone -- no children can exist either) still raises
    to the two thin wrappers below, but AccessDenied/ZombieProcess (ALIVE,
    just unreadable -- a real node the ppid map already proved has children)
    is recorded as a PLACEHOLDER entry (name/cmdline/create_time all None)
    instead of being dropped: an inaccessible node must never hide the whole
    subtree beneath it FROM THE LEDGER. Its `is_root_boundary` is `None`
    ("unknown"), NOT `False` -- round 4 recorded `False` here and every
    caller then treated it as "confirmed not a boundary", so an unreadable
    FOREIGN root's entire subtree was walked straight through, exactly
    backwards from the owner's absolute-isolation requirement. `None` is
    read by every boundary check as "never descend / never trust as a
    widening parent while this identity is unconfirmed" (see
    _descendant_pids, _fallback_descendants, _widening_eligible_parents) --
    the SAME treatment as a confirmed boundary for traversal purposes, but
    distinguishable from one for widening once the entry is later confirmed
    DEAD (an unknown pid that has since disappeared reverts to the ordinary
    dead-parent widening rule, since there is nothing left to hide).
    Documented residue: an unreadable wrapper's own children are missed
    until the wrapper itself dies (at which point normal H-1 widening
    applies) -- the safe direction under "never another session's", traded
    against completeness. _LEDGER_ENTRY_KEYS only requires the keys be
    PRESENT, not non-None, so this placeholder shape is still a valid ledger
    entry. Every reader of `name`/`cmdline`/`create_time`/`is_root_boundary`
    downstream treats None defensively."""
    try:
        name = proc.name()
        cmdline = " ".join(proc.cmdline())
        create_time = proc.create_time()
    except psutil.NoSuchProcess:
        raise
    except (psutil.AccessDenied, psutil.ZombieProcess):
        return {
            "pid": proc.pid, "ppid": ppid, "name": None, "cmdline": None,
            "create_time": None, "first_seen": now, "is_root_boundary": None,
        }
    is_boundary = _matches_candidate_substrings(proc, cfg)
    return {
        "pid": proc.pid, "ppid": ppid, "name": name, "cmdline": cmdline,
        "create_time": create_time, "first_seen": now, "is_root_boundary": is_boundary,
    }


def _harvest_pid(pid: int, ppid: int, cfg: dict, now: float) -> dict | None:
    """_harvest_from_proc for a bare pid (no Process object yet) -- exactly
    ONE psutil.Process(pid) construction. None only when the pid is
    genuinely gone (NoSuchProcess) -- see _harvest_from_proc for the
    inaccessible-but-alive placeholder case, which is never None."""
    try:
        proc = psutil.Process(pid)
        return _harvest_from_proc(proc, ppid, cfg, now)
    except psutil.NoSuchProcess:
        return None


def _needs_harvest(pid: int, ppid: int, entries: dict) -> bool:
    """A pid this ledger has NO entry for, whose recorded ppid no longer
    matches, or whose cached entry is a STALE PLACEHOLDER, needs a fresh
    harvest. ppid (not create_time) is the cheap signal for a pid we are
    seeing for the FIRST time (or reparented) this update -- _ppid_map()
    does not carry create_time. This is deliberately NOT the only identity
    check any more: item 3 adds a create_time re-verification for a known,
    non-boundary pid we are about to DESCEND INTO (see _descendant_pids),
    which this cheap ppid-only check cannot catch (a pid reused with a
    coincidentally identical ppid).

    round-7 send-back MEDIUM (stale placeholder): a cached entry whose
    `is_root_boundary` is `None` ("unknown" -- was unreadable last time), OR
    whose `name`/`cmdline`/`create_time` is `None` (the inaccessible-
    intermediary placeholder shape), is ALSO treated as needing a fresh
    harvest EVERY update, LEAF entries included -- not only internal nodes
    about to be descended into. Access permissions can change (a process
    finishes an elevation-dropping startup sequence, or simply becomes
    readable once whatever transient condition denied it clears), and an
    unknown/incomplete verdict must never freeze forever: as soon as the
    identity becomes readable, it is reclassified (boundary or not, and
    matched against dev_processes.names/pattern like any other entry)."""
    prior = entries.get(str(pid))
    if prior is None or prior.get("ppid") != ppid:
        return True
    if prior.get("is_root_boundary") is None:
        return True
    return prior.get("name") is None or prior.get("cmdline") is None or prior.get("create_time") is None


def _descendant_pids(
    root_pid: int, ppid_map: dict[int, int], entries: dict, cfg: dict, now: float,
) -> tuple[set[int], dict[int, dict]]:
    """Structural BFS over the ppid_map. For a LEAF pid (nothing in
    `children_of` for it) this ledger already knows about with an unchanged
    ppid, no Process() construction happens at all. For a NEW or
    ppid-changed pid, resolves identity AND the M-1 boundary check with
    exactly ONE Process() construction (_harvest_pid), inline, so the caller
    never re-harvests the same pid. Returns (descendant_pids, {pid:
    freshly-harvested entry}).

    round-4 item 3 / round-6 MEDIUM 1 (known-PID re-verification, both
    directions): before trusting a CACHED verdict for any INTERNAL node
    (one that has live children in `ppid_map` right now -- about to be
    descended into, or not, depending on that verdict), its create_time is
    re-verified against the ledger with ONE psutil.Process() call --
    regardless of whether the cached `is_root_boundary` is True, False, or
    None. round 4 only re-verified non-boundary nodes, which left a STALE
    True (a confirmed boundary whose pid the OS later reused for an
    ORDINARY process) pruning our own legitimate subtree forever, with no
    way back in -- the opposite direction from a stale False/None letting a
    foreign subtree through. Both directions now go through the same check.
    "One Process() per INTERNAL node actually traversed, not per pid
    overall": a leaf pays nothing, and a pid already re-harvested this
    update (ppid changed) skips the extra call too (already fresh). A
    mismatch (or an AccessDenied that prevents comparing at all) forces an
    immediate re-harvest and re-classification before any descent decision
    is made.

    round-6 HIGH (unknown boundary): `is_boundary` is never coerced to
    `bool` any more -- `None` ("unknown", an unreadable-but-alive node, see
    _harvest_from_proc) is carried through exactly like `True` for the
    descent decision below (`is not False`): an identity that cannot be
    confirmed safe must never be descended through, the same as a confirmed
    boundary. Only a CONFIRMED `False` allows descent."""
    children_of = _children_of(ppid_map)
    out: set[int] = set()
    harvested: dict[int, dict] = {}
    frontier = list(children_of.get(root_pid, []))
    while frontier:
        pid = frontier.pop()
        if pid in out:
            continue
        out.add(pid)
        ppid = ppid_map.get(pid)
        prior = entries.get(str(pid))
        # round-7 MEDIUM: _needs_harvest also forces a fresh harvest for a
        # STALE PLACEHOLDER (is_root_boundary None, or any of
        # name/cmdline/create_time None) every update, leaves included --
        # not only when re-verifying an internal node below.
        needs_fresh = _needs_harvest(pid, ppid, entries)
        is_boundary: bool | None = False
        if not needs_fresh and pid in children_of:
            # MEDIUM 1: an internal node -- re-verify BEFORE trusting either
            # the cached True/False/None value, whichever it is.
            prior_ct = prior.get("create_time")
            try:
                real_ct = psutil.Process(pid).create_time()
            except psutil.NoSuchProcess:
                continue  # gone -- its (former) children are unreachable this update too
            except psutil.AccessDenied:
                real_ct = None  # can't compare -- forces a re-harvest below
            if prior_ct is None or real_ct is None or abs(real_ct - prior_ct) > 1e-6:
                needs_fresh = True
        if needs_fresh:
            entry = _harvest_pid(pid, ppid, cfg, now)
            if entry is None:
                continue  # gone (NoSuchProcess) between the ppid_map snapshot and Process()
            harvested[pid] = entry
            is_boundary = entry["is_root_boundary"]
        else:
            is_boundary = prior.get("is_root_boundary", False)
        if is_boundary is not False:
            continue  # confirmed boundary (True) OR unknown (None): never descend
        frontier.extend(children_of.get(pid, []))
    return out, harvested


def _boundary_tainted_pids(entries: dict) -> set[int]:
    """Every ledger pid that IS a recorded root-boundary, or is a descendant
    of one BY THE LEDGER'S OWN RECORDED ppid CHAIN -- the closure that must
    never be used as a widening PARENT. Using every ledgered pid (boundaries
    included) as the eligible parent set let a boundary's children leak in
    one update after being flagged, and grandchildren the update after that.
    This closure is computed fresh each update from CURRENT entries, so a
    boundary discovered THIS update taints its own children from the
    FOLLOWING update onward (this update's own BFS already refused to
    descend into it, so nothing below it exists in `entries` yet to taint).

    round-6: only a CONFIRMED boundary (`is_root_boundary is True`) taints
    PERMANENTLY here, dead or alive -- `is_root_boundary is None` ("unknown",
    an unreadable-but-alive node) is deliberately NOT included in this
    static closure: whether it should still taint depends on whether the
    underlying process is still alive, which only a live query can answer,
    and that liveness-dependent exclusion is `_widening_eligible_parents`'s
    job, not this one's (Python truthiness already does the right thing
    here without special-casing: `None` is falsy, so it is naturally
    excluded from this set comprehension)."""
    by_pid = {e["pid"]: e for e in entries.values()}
    tainted: set[int] = {pid for pid, e in by_pid.items() if e.get("is_root_boundary") is True}
    changed = True
    while changed:
        changed = False
        for pid, e in by_pid.items():
            if pid not in tainted and e.get("ppid") in tainted:
                tainted.add(pid)
                changed = True
    return tainted


def _fallback_descendants(root_pid: int, cfg: dict, entries: dict, now: float) -> dict[int, dict]:
    """round-4 item 2: the _ppid_map()-unavailable fallback. Previously this
    called psutil.Process(root).children(recursive=True) and recorded every
    result with NO boundary awareness at all -- a real leak in fallback mode,
    since children(recursive=True) knows nothing about session boundaries.

    Fixed by reconstructing a LOCAL ppid map from the very Process objects
    children(recursive=True) already returned (each one's own .ppid() is
    free -- no extra Process() construction), then running the SAME
    BFS-with-boundary-pruning algorithm _descendant_pids uses, just scoped to
    this smaller, already-fetched subtree instead of the whole machine. NO
    WIDENING here (documented, unchanged from before): there is no cheap way
    to ask "which live pid's ppid is a ledger pid" without either the bulk
    map or a process_iter()-shaped full scan, and the latter is exactly what
    this fallback exists to avoid paying.

    round-6 LOW (double degradation, documented rather than closed): this
    fallback ALREADY runs on a degraded path (no _ppid_map(), no widening);
    an unreadable (AccessDenied) node ENCOUNTERED WITHIN it is a second,
    independent degradation stacked on top of the first -- same
    is_root_boundary=None / never-descend treatment as the main path (see
    _harvest_from_proc), but here it ALSO means that unreadable node's
    subtree is invisible for the rest of THIS session's life if _ppid_map()
    stays unavailable (no widening exists in fallback mode to ever link its
    children back in, even after it dies -- unlike the main path, where a
    dead unknown-boundary reverts to the ordinary H-1 widening rule). Two
    independent reasons for reduced visibility compounding is a real, if
    narrow, residue: it requires BOTH an unsupported psutil version/platform
    AND a permission-denied node in the same session."""
    try:
        descendants = psutil.Process(root_pid).children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return {}

    local_rows: dict[int, tuple[int, object]] = {}
    for proc in descendants:
        try:
            local_rows[proc.pid] = (proc.ppid(), proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    local_ppid_map = {pid: ppid for pid, (ppid, _proc) in local_rows.items()}
    children_of = _children_of(local_ppid_map)

    harvested: dict[int, dict] = {}
    seen: set[int] = set()
    frontier = list(children_of.get(root_pid, []))
    while frontier:
        pid = frontier.pop()
        if pid in seen or pid not in local_rows:
            continue
        seen.add(pid)
        ppid, proc = local_rows[pid]
        prior = entries.get(str(pid))
        # round-7 MEDIUM: _needs_harvest also forces a fresh harvest for a
        # stale placeholder every update, leaves included -- see
        # _descendant_pids's identical comment.
        needs_fresh = _needs_harvest(pid, ppid, entries)
        is_boundary: bool | None = False
        if not needs_fresh and pid in children_of:
            # round-6 MEDIUM 1: re-verify EVERY internal node regardless of
            # its cached boundary value (True/False/None), same as the main
            # (_ppid_map) path -- see _descendant_pids.
            prior_ct = prior.get("create_time")
            try:
                real_ct = proc.create_time()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                real_ct = None
            if prior_ct is None or real_ct is None or abs(real_ct - prior_ct) > 1e-6:
                needs_fresh = True
        if needs_fresh:
            try:
                entry = _harvest_from_proc(proc, ppid, cfg, now)
            except psutil.NoSuchProcess:
                continue  # vanished between children() and now -- skip this pid
            harvested[pid] = entry
            is_boundary = entry["is_root_boundary"]
        else:
            is_boundary = prior.get("is_root_boundary", False)
        if is_boundary is not False:
            # round-6 HIGH: confirmed boundary (True) OR unknown (None) --
            # never descend into (or record beyond) either.
            continue
        frontier.extend(children_of.get(pid, []))
    return harvested


def _widening_eligible_parents(entries: dict, tainted: set[int], ppid_map: dict[int, int]) -> set[int]:
    """round-5 send-back MEDIUM: a ledger pid is an eligible WIDENING parent
    only if (a) no LIVE process currently has that pid -- the ordinary H-1
    case, a dead wrapper whose ppid a worker still points at, which must stay
    eligible exactly as before -- or (b) a live process DOES have that pid
    AND its create_time matches the one this ledger recorded for it, i.e. it
    is genuinely still the SAME process, not one the OS handed that pid to
    afterwards.

    Previously ANY non-boundary ledger pid was eligible regardless of
    current liveness or identity: once the OS reused a dead wrapper's pid
    for an unrelated FOREIGN process, that foreign process's own children
    (which happen to share the reused pid as their ppid) were widened in as
    if they belonged to this session -- measured. Never documented as an
    accepted residue, because the owner's requirement is "never another
    session's": this closes it in code.

    Bounded cost: `psutil.Process()` is constructed only for a ledger pid
    that BOTH is not boundary-tainted AND actually appears as a ppid value
    in `ppid_map` (i.e. has at least one live child right now) -- a ledger
    pid with no live children this update costs nothing here, and a pid with
    live children pays exactly one Process() construction, the same
    "per node actually relevant, not per ledger entry" shape item 3 already
    established for the descendant walk.

    round-6 HIGH: `tainted` (from _boundary_tainted_pids) only ever excludes
    a CONFIRMED (`True`) boundary permanently. An `is_root_boundary is None`
    ("unknown") entry that turns out to be DEAD here falls through to case
    (a) above and stays eligible -- explicitly decided: once it is
    confirmed gone there is nothing left to hide, so the ordinary H-1
    dead-parent rule applies. But while it is CONFIRMED ALIVE (same
    create_time), it must NOT be trusted as a widening parent merely because
    it passed the identity check -- its own boundary status is still
    unknown, and treating an unconfirmed identity as safe is exactly the
    absolute-isolation requirement this round exists to close. Only an entry
    that is alive, identity-confirmed, AND recorded `is_root_boundary is
    False` stays eligible."""
    ledger_pids = {e["pid"] for e in entries.values()} - tainted
    by_pid = {e["pid"]: e for e in entries.values()}
    live_parent_candidates = ledger_pids & set(ppid_map.values())
    eligible = set(ledger_pids)
    for pid in live_parent_candidates:
        try:
            live_create_time = psutil.Process(pid).create_time()
        except psutil.NoSuchProcess:
            continue  # (a) dead -- stays eligible, this is the ordinary H-1 case
        except psutil.AccessDenied:
            eligible.discard(pid)  # can't verify identity -- fail-safe: exclude
            continue
        recorded = by_pid[pid].get("create_time")
        if recorded is None or abs(live_create_time - recorded) > 1e-6:
            eligible.discard(pid)  # (b) FAILED: a live process holds this pid now, but it is not the one we ledgered
            continue
        if by_pid[pid].get("is_root_boundary") is not False:
            eligible.discard(pid)  # alive + identity-confirmed, but boundary status still unknown -- exclude
    return eligible


def update_session_ledger(cfg: dict, ctx, session_id: str) -> None:
    """Best-effort: resolves this session's root, enumerates its LIVE
    descendants, and merges any new ones into the on-disk ledger.

    Called from dispatch.py's main(), never from a guard (guards may not
    perform filesystem side effects outside their three named exceptions;
    this is dispatcher/lib-owned I/O, the same class as Ctx.log_error).

    ORDER OF OPERATIONS (item, LOW #2): housekeeping (the stale-file sweep)
    runs FIRST, before root resolution -- previously the sweep ran at the
    very END, so a session whose root resolution keeps FAILING (returns
    early after writing/rechecking its .rootfail marker) never reached the
    sweep at all, and neither did anyone else's stale files during that
    whole time. Running it first means it always runs, regardless of how
    this dispatch's own root resolution turns out.

    Silent on every failure except one: a session root that cannot be
    resolved at all is logged ONCE per session (a `.rootfail` marker
    suppresses repeats) via ctx.log_config_error, per PM decision 1
    ("取れなかった場合の挙動: 不活性 + config_error_log 1行") -- every other
    failure (root process died between resolution and enumeration, a corrupt
    on-disk ledger this write replaces cleanly) is routine and unlogged: a
    missed snapshot gets another chance next dispatch, as long as the
    descendant is still reachable then.

    round-4 item 5 (identity discard): if an EXISTING ledger's own recorded
    (root_pid, root_create_time) does not match what THIS dispatch just
    resolved, its entries are DISCARDED (this write starts from empty) rather
    than merged -- a mismatch means the ledger on disk describes a different
    actual process than the one this session_id resolves to right now (root
    pid reuse, or an internal inconsistency), and merging onto it would
    silently relabel a stranger's descendants as this session's own. Never
    happens on the ordinary path (the same session_id keeps resolving to the
    same root for its whole lifetime), so this costs nothing normally.

    round-4 item 6 (lost update): the whole load-merge-write sequence for
    THIS session's own ledger file is wrapped in the SAME append-lock
    primitive lib/io.py's log writers use (io_mod._acquire_append_lock /
    io_mod._lock_path) -- a concurrent writer of the SAME file (two dispatches
    for the same session racing) can otherwise silently lose one's update
    (classic read-modify-write race). A lock that cannot be acquired within
    its bounded retry budget is treated as ordinary contention: this update is
    skipped, the next dispatch tries again -- consistent with every other
    best-effort failure mode here."""
    sl = cfg["dev_processes"]["session_ledger"]
    now = ctx.now().timestamp()
    _sweep_stale_ledger_files(cfg, ctx, sl["ttl_sec"], now)  # LOW #2: housekeeping BEFORE resolution

    root_pid, err = resolve_session_root(cfg, ctx)
    if root_pid is None:
        marker = _root_fail_marker(cfg, ctx, session_id)
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(marker), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except OSError:
            return  # already logged once for this session
        os.close(fd)
        ctx.log_config_error("session_root", "dev_processes.session_root", err or "unknown")
        return

    try:
        root_create_time = psutil.Process(root_pid).create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return  # root died between resolution and now -- next dispatch retries

    path = session_ledger_path(cfg, ctx, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)  # MUST exist before the lock file can be created
    lock_path = io_mod._lock_path(path)
    fd = io_mod._acquire_append_lock(lock_path)
    if fd is None:
        return  # contended beyond the bounded retry budget -- next dispatch retries
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        try:
            existing_ledger = _load_ledger_file(path)
        except LedgerError:
            existing_ledger = None  # this write replaces a corrupt ledger cleanly
        if existing_ledger is not None and (
            existing_ledger.get("root_pid") != root_pid
            or abs(existing_ledger.get("root_create_time", -1.0) - root_create_time) > 1e-6
        ):
            existing_ledger = None  # item 5: identity mismatch -- discard, never relabel
        entries: dict = dict((existing_ledger or {}).get("entries") or {})

        ppid_map = _ppid_map()
        if ppid_map is None:
            entries.update({str(pid): e for pid, e in _fallback_descendants(root_pid, cfg, entries, now).items()})
        else:
            if root_pid not in ppid_map:
                return  # root died between resolution and this enumeration -- next dispatch retries
            descendant_pids, harvested = _descendant_pids(root_pid, ppid_map, entries, cfg, now)
            for pid, entry in harvested.items():
                entries[str(pid)] = entry

            # H-1/H-4 widening. Eligible PARENTS are ledger pids that are not
            # boundary-tainted -- computed AFTER this update's own BFS
            # harvest has landed, so a boundary discovered just now is
            # already excluded before widening runs.
            tainted = _boundary_tainted_pids(entries)
            eligible_parents = _widening_eligible_parents(entries, tainted, ppid_map)
            for pid, ppid in ppid_map.items():
                if ppid not in eligible_parents:
                    continue
                if pid in descendant_pids or pid in harvested:
                    continue  # already resolved via BFS this update
                if not _needs_harvest(pid, ppid, entries):
                    continue
                entry = _harvest_pid(pid, ppid, cfg, now)
                if entry is not None:
                    entries[str(pid)] = entry

        _prune_ledger(entries, sl["max_entries"], sl["ttl_sec"], now)

        path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump({"root_pid": root_pid, "root_create_time": root_create_time, "entries": entries}, fh)
        os.replace(str(tmp), str(path))  # atomic swap: a crash mid-write leaves the OLD ledger intact
    finally:
        # LOW #3: a crash between the tmp write and os.replace() must not
        # leave a stray `.tmp` file behind forever -- best-effort cleanup,
        # and the sweep (below) also globs `*.tmp` as a second line of
        # defence for one that slips past this (e.g. this very process
        # killed between the try body and this finally).
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        os.close(fd)
        try:
            os.remove(lock_path)
        except OSError:
            pass


def _sweep_stale_ledger_files(cfg: dict, ctx, ttl_sec: float, now: float) -> None:
    """Deletes ledger/.rootfail/.tmp files that are stale, on every update
    (before this session's own root resolution -- LOW #2). Best-effort and
    silent: a file this process cannot stat() or remove is left for the next
    sweep, never logged -- this is routine housekeeping, not a defect.

    round-4 item 7: a `.json` ledger is stale ONLY when its recorded ROOT is
    actually dead (pid gone, or create_time no longer matches --
    _root_still_alive) -- NOT merely because its mtime is old. A long-running
    session that dispatches infrequently (idle longer than ttl_sec between
    tool calls) must never lose its ledger just because nothing touched it
    for a while; only a session whose root process has genuinely exited is
    swept. `.rootfail` and `.tmp` files carry no root to check and stay on
    the original mtime-past-ttl rule (a `.rootfail` marker is inherently tied
    to a session that could never even resolve a root, so there is nothing
    else to verify; a stray `.tmp` is a crash artifact with no owner to ask).

    round-4 item 6 (sweep race): mtime is checked TWICE -- once cheaply
    up front (skips anything clearly fresh, no lock needed), and again AFTER
    taking that specific file's own append lock (the SAME lock
    update_session_ledger takes to write it), immediately before unlinking.
    A concurrent writer refreshing the file between the two checks means the
    second stat sees the new mtime and the sweep backs off -- the lock makes
    "decide it's stale" and "the owner is mid-write" mutually exclusive.

    round-5 LOW: `*.append.lock` files (the lock files THIS sweep and
    update_session_ledger both take on a `.json` ledger) are themselves
    swept -- a holder that crashed between os.open() and os.remove() leaves
    one behind forever otherwise. These are NOT treated like the `.json`/
    `.rootfail`/`.tmp` candidates above (taking a *second*, `.append.lock`-
    suffixed lock ON a lock file makes no sense): lib/io.py already owns the
    correct staleness test and exactly-one-winner reclaim for its own lock
    files (`_append_lock_is_stale` / `_append_lock_reclaim`, the same ones
    `_acquire_append_lock` itself uses when IT finds a lock stale), so this
    delegates to those rather than reimplementing lock semantics here."""
    ledger_dir = _ledger_dir(cfg, ctx)
    if not ledger_dir.is_dir():
        return

    for lock_file in ledger_dir.glob("*.append.lock"):
        try:
            if io_mod._append_lock_is_stale(lock_file):
                io_mod._append_lock_reclaim(lock_file)
        except OSError:
            pass

    candidates = list(ledger_dir.glob("*.json")) + list(ledger_dir.glob("*.rootfail")) + list(ledger_dir.glob("*.tmp"))
    for path in candidates:
        try:
            if now - path.stat().st_mtime <= ttl_sec:
                continue  # clearly fresh -- no lock needed for this cheap pre-check
        except OSError:
            continue

        if path.suffix == ".json":
            try:
                data = _load_ledger_file(path)
            except LedgerError:
                data = None  # a corrupt-AND-stale ledger has nothing to preserve either way
            if data is not None:
                root_pid = data.get("root_pid")
                root_create_time = data.get("root_create_time")
                if root_pid is not None and root_create_time is not None \
                        and _root_still_alive(root_pid, root_create_time) is not False:
                    # round-6 MEDIUM 2: True (alive) OR None (unknown -- can't
                    # confirm it's dead) both preserve the ledger; only a
                    # CONFIRMED False (gone/reused) is eligible for deletion.
                    continue

        lock_path = io_mod._lock_path(path)
        fd = io_mod._acquire_append_lock(lock_path)
        if fd is None:
            continue  # contended -- leave it for the next sweep, never block
        try:
            if now - path.stat().st_mtime > ttl_sec:  # RE-STAT under the lock (item 6)
                path.unlink()
        except OSError:
            pass
        finally:
            os.close(fd)
            try:
                os.remove(lock_path)
            except OSError:
                pass


def _prune_ledger(entries: dict, max_entries: int, ttl_sec: float, now: float) -> None:
    """TTL: an entry whose process is STILL ALIVE is never evicted -- a
    long-lived dev server started early in a long session must not fall out
    of the ledger just because `first_seen` is old; only a DEAD entry past
    TTL is dropped.

    round-4 item 7 (max_entries correction): when still over `max_entries`
    after the TTL pass, DEAD entries are evicted first (oldest dead first),
    and an ALIVE entry is NEVER evicted for being merely over the cap -- a
    ledger holding only genuinely alive processes may temporarily sit above
    max_entries rather than lose track of a real, running server (documented
    residue: the cap is a soft target once everything in it is alive, not a
    hard limit)."""
    for key in [k for k, e in list(entries.items())
                if now - e.get("first_seen", now) > ttl_sec]:
        alive, _ppid = _entry_still_alive(entries[key])
        if alive is False:  # round-6 MEDIUM 2: None (unknown) is preserved, same as True
            del entries[key]
    if len(entries) > max_entries:
        classified = []
        for key, e in entries.items():
            alive, _ppid = _entry_still_alive(e)
            classified.append((key, e, alive))
        dead_oldest_first = sorted(
            ((key, e) for key, e, alive in classified if alive is False),
            key=lambda ke: ke[1].get("first_seen", 0),
        )
        overflow = len(entries) - max_entries
        for key, _e in dead_oldest_first:
            if overflow <= 0:
                break
            del entries[key]
            overflow -= 1
        # Never evict an alive entry: if overflow remains here, every
        # remaining entry is alive and the ledger stays over max_entries.


def _entry_still_alive(entry: dict) -> tuple[bool | None, int | None]:
    """(alive, live_ppid), THREE-VALUED alive (round-6 MEDIUM 2): True
    (confirmed the same process, still running), False (confirmed gone --
    NoSuchProcess/ZombieProcess, or a live pid whose create_time no longer
    matches, i.e. reused), or None ("unknown" -- AccessDenied, alive but
    unreadable). Checked by TARGETED psutil.Process(pid)/create_time
    comparison -- never a table scan.

    Previously AccessDenied collapsed into False ("dead"), which let
    _prune_ledger TTL-evict, and find_dev_processes_in_ledger silently skip,
    an entry that was actually STILL ALIVE, just unreadable. Callers must
    treat None the safe direction: _prune_ledger never evicts on None (same
    protection as True), find_dev_processes_in_ledger never reports an
    orphan on None (same as False there -- an unconfirmed identity must
    never be reported OR killed, which is the stricter of the two safe
    directions for that caller). `create_time` may be None (the
    inaccessible-intermediary placeholder, which never had one to record) --
    in that case the create_time check is skipped and only pid-existence is
    used, a documented, narrower guarantee for that one entry shape.

    round-4 LOW #4 (parent identity, documented rather than fixed): the
    returned `live_ppid` is a bare pid, not itself identity-checked against
    ANY recorded parent create_time -- so a parent pid reused by an unrelated
    process between this entry's capture and this call could make an actually
    dead parent look "alive" (false negative on orphan detection). Storing
    and comparing the parent's own create_time would close this, at the cost
    of one MORE psutil.Process() construction per harvested entry (the
    parent's, in addition to the entry's own) -- directly working against
    H-2R's whole point of minimising Process() construction on the hook path.
    Deliberately left open and documented rather than paid for: the residual
    risk is a narrow double-reuse race (BOTH the entry's own pid AND its
    parent's pid would need to be reused, in the right order, inside one
    dispatch interval) that a real orphan-detection guard treats as an
    acceptable false negative, not a security boundary."""
    try:
        proc = psutil.Process(entry["pid"])
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return False, None
    try:
        if entry.get("create_time") is not None and abs(proc.create_time() - entry["create_time"]) > 1e-6:
            return False, None
    except psutil.AccessDenied:
        return None, None
    try:
        return True, proc.ppid()
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return True, None  # confirmed alive; its own ppid became unreadable an instant later


def find_dev_processes_in_ledger(
    cfg: dict, ctx, entries: dict, orphans_only: bool = True, root: str | None = None,
) -> list[ProcInfo]:
    """The name/pattern/orphan/root logic of find_dev_processes, sourced from a
    session ledger's `entries` dict instead of list_processes()'s full-table
    scan of every process's name+cmdline. A PID not present in `entries` can
    never appear in the result -- the ledger IS the candidate set, not an
    additional filter on top of one.

    item 8: an entry with `name`/`cmdline` of None (the inaccessible-
    intermediary placeholder) can never be classified as "dev-shaped" --
    there is nothing to match dev_processes.names/pattern against -- so it is
    skipped here, same as a name/pattern mismatch. It still counts as a
    ledger member for BFS/widening purposes upstream; it simply never
    surfaces as an orphan report by itself."""
    dp = cfg["dev_processes"]
    names = {n.casefold() for n in dp["names"]}
    pattern = ctx_mod.compile_text_pattern(dp["pattern"], "dev_processes.pattern")
    strip_exe = dp.get("strip_exe_suffix_when_matching", True)

    out: list[ProcInfo] = []
    for entry in entries.values():
        name = entry["name"]
        cmdline = entry["cmdline"]
        if name is None or cmdline is None:
            continue
        if _normalise_name(name, strip_exe) not in names:
            continue
        # Symmetric with the `root is None` handling below -- `pattern is
        # None` is the compile_text_pattern sentinel ('*', match everything),
        # not an error; the guard clause form mirrors root's.
        if pattern is None:
            pass
        elif not pattern.search(cmdline):
            continue
        alive, live_ppid = _entry_still_alive(entry)
        if alive is not True:
            # round-6 MEDIUM 2: an UNKNOWN identity (AccessDenied) is never
            # reported as an orphan -- kill() downstream would act on a
            # process we cannot actually confirm is the one this ledger
            # means, which is the stricter safe direction for this caller
            # (unlike _prune_ledger, which PRESERVES on unknown).
            continue
        is_orphan = live_ppid is None or not psutil.pid_exists(live_ppid)
        if root is None:
            root_match = not orphans_only
        elif ctx.is_windows:
            root_match = (not orphans_only) and (root.casefold() in cmdline.casefold())
        else:
            root_match = (not orphans_only) and (root in cmdline)
        if is_orphan or root_match:
            out.append(ProcInfo(
                pid=entry["pid"], ppid=live_ppid or 0, name=name, cmdline=cmdline,
                create_time=entry.get("create_time"),
            ))
    return out


def find_session_orphans(cfg: dict, ctx, session_id: str) -> list[ProcInfo]:
    """guards/dev_process_hygiene.py's ONLY entry point. Raises LedgerError
    when the ledger exists but cannot be trusted (the guard logs that one case
    to config_error_log and returns ALLOW); returns [] -- never raises -- when
    there is simply no ledger yet, which is the common case for a session that
    has run no Bash/PowerShell tool calls."""
    ledger = read_session_ledger(cfg, ctx, session_id)
    if ledger is None:
        return []
    return find_dev_processes_in_ledger(cfg, ctx, ledger.get("entries", {}), orphans_only=True)
