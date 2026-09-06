"""hooks/lib/procs.py -- dev-process discovery, ONE code path over psutil.

CONTRACT.md #2 (lib/procs.py), I-13, PE-6, PE-12. psutil is the SOLE non-stdlib
import in the runtime, and it is reachable ONLY from guards/dev_process_hygiene.py
(Stop) and stop_dev_orphans.py (CLI) -- see I-13.2. Nothing on the PreToolUse path
may import this module.

No fallback, deliberately: a fallback means two behaviours to test and a guard that
can quietly change semantics. A missing psutil is made loud at deploy/verify time
(install.py), not silently degraded here.
"""
from __future__ import annotations

import re
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
