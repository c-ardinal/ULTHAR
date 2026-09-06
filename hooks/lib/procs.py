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

import json
import os
import re
from pathlib import Path
from typing import NamedTuple

import psutil  # THE ONLY non-stdlib import in the runtime (I-13)

from . import ctx as ctx_mod  # compile_text_pattern is the one authority for pattern compilation


class ProcInfo(NamedTuple):
    pid: int
    ppid: int
    name: str
    cmdline: str


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


def kill(pids: list[int], ctx, cfg: dict) -> list[tuple[int, str]]:
    """terminate() the set -> psutil.wait_procs(grace) -> kill() the survivors.
    Per-PID NoSuchProcess/AccessDenied become ('skip <pid> <name>: <reason>') lines,
    matching the masters' try/catch output shape."""
    so = cfg["dev_processes"]["stop_orphans"]
    grace = so["terminate_grace_seconds"]
    skips: list[tuple[int, str]] = []
    procs = []
    for pid in pids:
        try:
            p = psutil.Process(pid)
            p.terminate()
            procs.append(p)
        except psutil.NoSuchProcess:
            skips.append((pid, "skip %d <unknown>: already gone" % pid))
        except psutil.AccessDenied as exc:
            skips.append((pid, "skip %d <unknown>: access denied (%s)" % (pid, exc)))

    gone, alive = psutil.wait_procs(procs, timeout=grace)
    for p in alive:
        try:
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
# session_ledger). L-2 precision, round 2: the guard's Stop-time READ
# (find_session_orphans / find_dev_processes_in_ledger) never enumerates the
# process table at all -- it walks the ledger's own `entries` dict and probes
# only those specific PIDs. The ledger WRITER (update_session_ledger) does
# still enumerate every live process once per update via _process_rows(),
# but harvests only pid/ppid/create_time there, never every process's
# name()/cmdline() -- see that function's docstring. Everything in this
# section is new; find_dev_processes/list_processes/kill/listening_ports
# above are UNCHANGED and still back test_contract.py's direct calls and the
# existing full name+cmdline table-scan semantics.


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
    """L-4: `session_id` reaches a filesystem path unchanged before this fix --
    a crafted value (`..`, `/`, a drive letter) would let a session_id escape
    the ledger directory. Anything outside [A-Za-z0-9._-] becomes '_'; an
    empty/all-unsafe result falls back to 'unknown', matching the previous
    behaviour for a missing session_id."""
    cleaned = _SESSION_ID_UNSAFE.sub("_", session_id or "")
    return cleaned or "unknown"


def _proc_identity_text(proc) -> str:
    """exe() path + cmdline, space-joined and slash-normalised, for
    session_root.candidate_substrings matching (M-1). Best effort: exe() is
    commonly AccessDenied on Windows without elevation and cmdline() can be
    denied too; either or both missing just shrinks the text this proc offers
    to match against, it never raises."""
    parts: list[str] = []
    try:
        parts.append(proc.exe())
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        pass
    try:
        parts.append(" ".join(proc.cmdline()))
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    return " ".join(parts).replace("\\", "/").casefold()


def _is_session_root_candidate(proc, cfg: dict) -> bool:
    """M-1: prefers an exe-path/cmdline discriminator (session_root.
    candidate_substrings) over a bare process NAME -- a name collides too
    easily (11 of 13 real `claude.exe` processes measured on a review machine
    were the Claude DESKTOP app, not the CLI; `node` matches VS Code's own
    extension-host process, which sits in the same ancestor chain as the
    actual session host). candidate_names (the original, name-only rule) is
    the FALLBACK, tried only when no substring matched -- so a deployment
    that has not customised the substrings still resolves a root exactly as
    before. Used BOTH for the ancestor climb (resolve_session_root) and as
    the descent boundary in the ledger's descendant walk (a descendant that
    is itself a session root belongs to ANOTHER session)."""
    sr = cfg["dev_processes"]["session_root"]
    substrings = [s.casefold() for s in sr.get("candidate_substrings", [])]
    text = _proc_identity_text(proc)
    if any(s in text for s in substrings):
        return True
    try:
        name = _normalise_name(proc.name(), strip_exe=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False
    return name in {n.casefold() for n in sr["candidate_names"]}


def resolve_session_root(cfg: dict, ctx) -> tuple[int | None, str | None]:
    """Walks psutil ancestry from THIS hook process upward looking for the
    session's root (the Claude Code host -- _is_session_root_candidate).
    Falls back to the nearest non-shell ancestor (session_root.shell_names)
    when no candidate is ever seen, per PM decision. Returns (pid, None) on
    success, or (None, reason) when neither a candidate nor a fallback could
    be found within max_ancestor_hops -- the caller (update_session_ledger)
    treats that as inert and logs it ONCE."""
    sr = cfg["dev_processes"]["session_root"]
    shells = {n.casefold() for n in sr["shell_names"]}
    max_hops = sr["max_ancestor_hops"]
    try:
        parent = psutil.Process(os.getpid()).parent()
    except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
        return None, f"cannot inspect the hook process's own parent: {exc}"

    fallback_pid: int | None = None
    hops = 0
    while parent is not None and hops < max_hops:
        try:
            pid = parent.pid
            is_candidate = _is_session_root_candidate(parent, cfg)
            name = _normalise_name(parent.name(), strip_exe=True)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            break
        if is_candidate:
            return pid, None
        if name not in shells and fallback_pid is None:
            fallback_pid = pid
        try:
            parent = parent.parent()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            parent = None
        hops += 1
    if fallback_pid is not None:
        return fallback_pid, None
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

    M-2: returns ALL matches, not just the first -- multiple session_ids
    (main chain + sub-agents) legitimately resolve to the SAME root (they
    share one Claude Code host), so each keeps its own ledger file under a
    different session_id but they all describe descendants of the same root
    process. The CLI merges their entries so a dev server one sub-agent
    started is still found by `python stop_dev_orphans.py` run for the whole
    session. `root_create_time` disambiguates a reused root_pid across two
    DIFFERENT actual host processes over the machine's lifetime (a stale
    ledger from a long-dead session whose PID got recycled must never match).
    A ledger that fails to parse is skipped (best-effort discovery, not the
    guard's fail-loud read)."""
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
    """H-2R: the platform's BULK pid->ppid map (psutil._psplatform.ppid_map()),
    which is what psutil.Process.children(recursive=True) already uses
    internally to run in 23-28ms -- ONE OS-level bulk query, zero per-process
    Process object construction. process_iter() is not a cheaper substitute
    for this: it CONSTRUCTS a Process object per pid (measured 12-16s cold on
    the reviewer's machine, since every hook dispatch is a fresh interpreter
    with no warm handle cache) -- the cost is the construction, not which
    attributes are read off it afterward. This is why round 2's _process_rows
    (deleted this round) was wrong: it called process_iter() to get a CHEAP
    field triple, but process_iter() is never cheap regardless of which
    fields are requested.

    Private psutil API, used ONLY here and wrapped: returns None when
    unavailable (a psutil version/platform that does not expose it), in
    which case update_session_ledger falls back to descendants-only, no
    widening (see its docstring) -- documented, not silent."""
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


def _harvest_from_proc(proc, ppid: int, cfg: dict, now: float) -> dict | None:
    """ONE psutil.Process object, read for BOTH the ledger entry (name,
    cmdline, create_time) AND the M-1 boundary check
    (_is_session_root_candidate) -- H-2R: the expensive part is constructing
    the Process object, not the attribute reads, so a caller that already
    has one must never construct a second for the same pid."""
    try:
        name = proc.name()
        cmdline = " ".join(proc.cmdline())
        create_time = proc.create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return None
    is_boundary = _is_session_root_candidate(proc, cfg)
    return {
        "pid": proc.pid, "ppid": ppid, "name": name, "cmdline": cmdline,
        "create_time": create_time, "first_seen": now, "is_root_boundary": is_boundary,
    }


def _harvest_pid(pid: int, ppid: int, cfg: dict, now: float) -> dict | None:
    """_harvest_from_proc for a bare pid (no Process object yet) -- exactly
    ONE psutil.Process(pid) construction."""
    try:
        proc = psutil.Process(pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None
    return _harvest_from_proc(proc, ppid, cfg, now)


def _needs_harvest(pid: int, ppid: int, entries: dict) -> bool:
    """H-2: an already-ledgered pid whose recorded ppid still matches is
    treated as the SAME process instance and skipped -- no Process()
    construction at all. ppid (not create_time) is the cheap signal here,
    because _ppid_map() does not carry create_time; a pid reused by an
    unrelated process is, in practice, spawned by a different parent, so a
    changed ppid is what triggers a real re-harvest (which then refreshes
    create_time too). Documented trade, not airtight against a pid reused
    with a coincidentally identical ppid -- the alternative (fetching
    create_time for every already-known pid to double-check) is exactly the
    per-pid Process() cost H-2R exists to remove."""
    prior = entries.get(str(pid))
    return prior is None or prior.get("ppid") != ppid


def _descendant_pids(
    root_pid: int, ppid_map: dict[int, int], entries: dict, cfg: dict, now: float,
) -> tuple[set[int], dict[int, dict]]:
    """Structural BFS over the ppid_map -- ZERO psutil.Process() construction
    for a pid this ledger already knows about with an unchanged ppid
    (_needs_harvest). For a NEW or ppid-changed pid, resolves identity AND
    the M-1 boundary check with EXACTLY ONE Process() construction
    (_harvest_pid), inline, so the caller never re-harvests the same pid.
    Returns (descendant_pids, {pid: freshly-harvested entry}).

    M-1: descent STOPS at a pid resolved as a boundary (another session's own
    root) -- its children are never visited by THIS BFS, so they can never
    enter `descendant_pids` this way. H-4 closes the remaining route (a
    boundary's child entering via WIDENING instead) in update_session_ledger."""
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
        if _needs_harvest(pid, ppid, entries):
            entry = _harvest_pid(pid, ppid, cfg, now)
            if entry is None:
                continue  # vanished between the ppid_map snapshot and Process()
            harvested[pid] = entry
            is_boundary = entry["is_root_boundary"]
        else:
            is_boundary = bool(entries[str(pid)].get("is_root_boundary", False))
        if is_boundary:
            continue  # boundary: another session's root, do not descend further
        frontier.extend(children_of.get(pid, []))
    return out, harvested


def _boundary_tainted_pids(entries: dict) -> set[int]:
    """H-4: every ledger pid that IS a recorded root-boundary, or is a
    descendant of one BY THE LEDGER'S OWN RECORDED ppid CHAIN -- the closure
    that must never be used as a widening PARENT. Round-2's widening used
    `existing_pids` (every ledgered pid, boundaries included) as the eligible
    parent set: a boundary gets its own ledger entry (by design -- it is
    still a real process this session's root has as a descendant), and on
    the VERY NEXT update its children were pulled in by widening because the
    boundary's own pid was "a pid already in the ledger", the exact
    protection M-1 exists to provide. This closure is computed fresh each
    update from CURRENT entries, so a boundary discovered THIS update taints
    its own children from the FOLLOWING update onward (this update's own BFS
    already refused to descend into it, so nothing below it exists in
    `entries` yet to taint)."""
    by_pid = {e["pid"]: e for e in entries.values()}
    tainted: set[int] = {pid for pid, e in by_pid.items() if e.get("is_root_boundary")}
    changed = True
    while changed:
        changed = False
        for pid, e in by_pid.items():
            if pid not in tainted and e.get("ppid") in tainted:
                tainted.add(pid)
                changed = True
    return tainted


def update_session_ledger(cfg: dict, ctx, session_id: str) -> None:
    """Best-effort: resolves this session's root, enumerates its LIVE
    descendants, and merges any new ones into the on-disk ledger.

    H-1 (widening): in addition to root's live descendants, ALSO records any
    live process whose ppid is a PID ALREADY IN THIS LEDGER (alive or dead at
    capture time) AND NOT boundary-tainted (H-4, _boundary_tainted_pids) --
    this is what catches a worker whose short-lived wrapper was recorded once
    and has since died: the worker is no longer reachable by walking root's
    live children (the wrapper, the link in that chain, is gone), but its
    ppid still points at a pid this ledger already knows. Measured gap this
    closes: a self-spawned `python -m http.server` orphan went unreported
    because its `bash -c` wrapper had already exited by the time of the
    snapshot that would have caught it as root's descendant. When a WIDENED
    pid itself turns out to be a session-root candidate (another session
    reached through widening rather than BFS descent), it is recorded with
    `is_root_boundary: True` like any other boundary -- _boundary_tainted_pids
    then excludes it (and anything below it) as a widening parent from the
    NEXT update onward. RESIDUE, accepted and documented (docs/DESIGN.md,
    this guard's own docstring): a wrapper that is BOTH spawned and has
    ALREADY EXITED between two hook invocations -- so it never appeared as
    root's descendant in any snapshot, and the worker's ppid was therefore
    never a ledger PID either -- is still invisible.

    H-2R (cost): descendant discovery and widening are both driven by
    _ppid_map() (one bulk OS query, zero Process() construction) rather than
    process_iter() (which constructs a Process PER PID and was measured
    costing 12-16s cold -- see _ppid_map's docstring). A psutil.Process()
    object is constructed at most ONCE per NEW-or-changed candidate pid
    (_harvest_pid/_harvest_from_proc), shared between the identity harvest
    and the M-1 boundary check; an already-known, ppid-unchanged pid costs
    nothing. FALLBACK, documented rather than silent: when _ppid_map() is
    unavailable (an unsupported psutil version/platform), this falls back to
    psutil.Process(root).children(recursive=True) for DESCENDANTS ONLY, with
    NO WIDENING -- there is no cheap way to ask "which live pid's ppid is a
    ledger pid" without either the bulk map or a process_iter()-shaped full
    scan, and the latter is exactly what H-2R forbids.

    Called from dispatch.py's main(), never from a guard (guards may not
    perform filesystem side effects outside their three named exceptions;
    this is dispatcher/lib-owned I/O, the same class as Ctx.log_error). Silent
    on every failure except one: a session root that cannot be resolved at
    all is logged ONCE per session (a `.rootfail` marker suppresses repeats)
    via ctx.log_config_error, per PM decision 1 ("取れなかった場合の挙動:
    不活性 + config_error_log 1行") -- every other failure (root process died
    between resolution and enumeration, a corrupt on-disk ledger this write
    replaces cleanly) is routine and unlogged: a missed snapshot gets another
    chance next dispatch, as long as the descendant is still reachable then."""
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

    path = session_ledger_path(cfg, ctx, session_id)
    try:
        existing_ledger = _load_ledger_file(path)
    except LedgerError:
        existing_ledger = None  # this write replaces a corrupt ledger cleanly
    entries: dict = dict((existing_ledger or {}).get("entries") or {})

    now = ctx.now().timestamp()
    ppid_map = _ppid_map()

    if ppid_map is None:
        # FALLBACK (documented, not silent): descendants only, no widening.
        try:
            root_proc = psutil.Process(root_pid)
            root_create_time = root_proc.create_time()
            descendants = root_proc.children(recursive=True)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return  # root died between resolution and this enumeration -- next dispatch retries
        for proc in descendants:
            try:
                ppid = proc.ppid()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            if not _needs_harvest(proc.pid, ppid, entries):
                continue
            entry = _harvest_from_proc(proc, ppid, cfg, now)
            if entry is not None:
                entries[str(proc.pid)] = entry
    else:
        if root_pid not in ppid_map:
            return  # root died between resolution and this enumeration -- next dispatch retries
        try:
            root_create_time = psutil.Process(root_pid).create_time()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return

        descendant_pids, harvested = _descendant_pids(root_pid, ppid_map, entries, cfg, now)
        for pid, entry in harvested.items():
            entries[str(pid)] = entry

        # H-1/H-4: widening. Eligible PARENTS are ledger pids that are not
        # boundary-tainted (H-4) -- computed AFTER this update's own BFS
        # harvest has landed, so a boundary discovered just now is already
        # excluded before widening runs.
        tainted = _boundary_tainted_pids(entries)
        eligible_parents = {e["pid"] for e in entries.values()} - tainted
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

    sl = cfg["dev_processes"]["session_ledger"]
    _prune_ledger(entries, sl["max_entries"], sl["ttl_sec"], now)

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump({"root_pid": root_pid, "root_create_time": root_create_time, "entries": entries}, fh)
    os.replace(str(tmp), str(path))  # atomic swap: a crash mid-write leaves the OLD ledger intact

    _sweep_stale_ledger_files(cfg, ctx, sl["ttl_sec"], now)


def _sweep_stale_ledger_files(cfg: dict, ctx, ttl_sec: float, now: float) -> None:
    """M-3: deletes ledger/.rootfail files whose mtime is past `ttl_sec` on
    every update -- a session whose process exited long ago, and which never
    ran another Stop/PreToolUse-Bash dispatch to clean up after itself,
    otherwise leaves its ledger (and, if it failed to resolve a root even
    once, its .rootfail marker) on disk forever. Best-effort and silent: a
    file this process cannot stat() or remove (a race with another writer) is
    left for the next sweep, never logged -- this is routine housekeeping,
    not a defect. Runs AFTER this session's own write, so its own fresh
    ledger is never swept in the same pass."""
    ledger_dir = _ledger_dir(cfg, ctx)
    if not ledger_dir.is_dir():
        return
    for path in list(ledger_dir.glob("*.json")) + list(ledger_dir.glob("*.rootfail")):
        try:
            if now - path.stat().st_mtime > ttl_sec:
                path.unlink()
        except OSError:
            pass


def _prune_ledger(entries: dict, max_entries: int, ttl_sec: float, now: float) -> None:
    """L-3: an entry whose process is STILL ALIVE is never TTL-evicted -- a
    long-lived dev server started early in a long session must not fall out
    of the ledger just because `first_seen` is old; only a DEAD entry past
    TTL is dropped. Once still over `max_entries` after that, eviction is
    NEWEST-first: the entries this update JUST learned about are dropped
    before the ones tracked longest, since the oldest are the ones most
    likely to be the actual long-lived server this ledger exists to catch."""
    for key in [k for k, e in list(entries.items())
                if now - e.get("first_seen", now) > ttl_sec]:
        alive, _ppid = _entry_still_alive(entries[key])
        if not alive:
            del entries[key]
    if len(entries) > max_entries:
        newest_first = sorted(entries.items(), key=lambda kv: kv[1].get("first_seen", 0), reverse=True)
        for key, _ in newest_first[: len(entries) - max_entries]:
            del entries[key]


def _entry_still_alive(entry: dict) -> tuple[bool, int | None]:
    """(alive, live_ppid) for one ledger entry, checked by TARGETED
    psutil.Process(pid)/create_time comparison -- never a table scan. A pid
    whose create_time no longer matches was reused by an unrelated process and
    is treated as gone, the same as NoSuchProcess."""
    try:
        proc = psutil.Process(entry["pid"])
        if abs(proc.create_time() - entry["create_time"]) > 1e-6:
            return False, None
        return True, proc.ppid()
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return False, None


def find_dev_processes_in_ledger(
    cfg: dict, ctx, entries: dict, orphans_only: bool = True, root: str | None = None,
) -> list[ProcInfo]:
    """The name/pattern/orphan/root logic of find_dev_processes, sourced from a
    session ledger's `entries` dict instead of list_processes()'s full-table
    scan of every process's name+cmdline. A PID not present in `entries` can
    never appear in the result -- the ledger IS the candidate set, not an
    additional filter on top of one."""
    dp = cfg["dev_processes"]
    names = {n.casefold() for n in dp["names"]}
    pattern = ctx_mod.compile_text_pattern(dp["pattern"], "dev_processes.pattern")
    strip_exe = dp.get("strip_exe_suffix_when_matching", True)

    out: list[ProcInfo] = []
    for entry in entries.values():
        name = entry["name"]
        cmdline = entry["cmdline"]
        if _normalise_name(name, strip_exe) not in names:
            continue
        # L-5: symmetric with the `root is None` handling below -- `pattern
        # is None` is the compile_text_pattern sentinel ('*', match
        # everything), not an error; the guard clause form mirrors root's.
        if pattern is None:
            pass
        elif not pattern.search(cmdline):
            continue
        alive, live_ppid = _entry_still_alive(entry)
        if not alive:
            continue
        is_orphan = live_ppid is None or not psutil.pid_exists(live_ppid)
        if root is None:
            root_match = not orphans_only
        elif ctx.is_windows:
            root_match = (not orphans_only) and (root.casefold() in cmdline.casefold())
        else:
            root_match = (not orphans_only) and (root in cmdline)
        if is_orphan or root_match:
            out.append(ProcInfo(pid=entry["pid"], ppid=live_ppid or 0, name=name, cmdline=cmdline))
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
