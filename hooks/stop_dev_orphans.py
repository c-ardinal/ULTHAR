#!/usr/bin/env python3
"""hooks/stop_dev_orphans.py -- manual CLI, kills orphaned dev-tool process chains.

Companion to guards/dev_process_hygiene.py (Stop hook). NOT a guard: it is never
listed in `dispatch.events` and is invoked directly by the operator (or by the
model, per the hygiene guard's own message) --

    python stop_dev_orphans.py                                  # orphans only
    python stop_dev_orphans.py --all --root C:\\path\\to\\project

--all ALSO kills NON-orphaned dev-server chains under --root (or every matching
chain if --root is omitted). SAFETY: do not use --all while another server must
survive -- it is not scoped to "this session's" processes, only to name+pattern
(+root) match.

One of only two modules in the runtime permitted to import psutil (the other is
guards/dev_process_hygiene.py) -- the guard-purity AST scan only scans
`guards/*.py`, and this file is deliberately outside that directory. As a CLI
it may print, call sys.exit and read argv.

`_find_targets` calls `find_dev_processes_in_ledger(root=None)` and applies
its own case-insensitive root substring filter locally instead of passing
`root=` through: process command lines must always match case-insensitively,
but find_dev_processes_in_ledger()'s own root_match is case-sensitive (same
composition this module has always used; only the candidate SOURCE changed).

ulthar-enforcement-round1: this CLI now scopes to its OWN session's ledger(s)
too, the same as guards/dev_process_hygiene.py, per PM decision 4 ("同じ台帳を
使い、他セッションのプロセスを殺さないこと"). It has no session_id (it is run
directly, not via a hook payload), so it resolves its own session root the
same ancestor-walk way the guard's ledger writer does, then finds every
ledger file recorded under that (root_pid, root_create_time) pair
(lib.procs.find_ledgers_by_root) -- never a full-table scan, and NEVER
another session's ledger. Round 2 (M-2): MULTIPLE session_ids (the main
chain plus any sub-agents) legitimately share one root, each with its own
ledger file, so this merges entries across every matching ledger rather than
using only the first one found -- a dev server a sub-agent started must
still be found by a plain `python stop_dev_orphans.py`. When no root can be
resolved, or no ledger matches it, _find_targets returns an empty list
rather than falling back to a wider scan: an unscoped fallback here would be
exactly the cross-session risk this round exists to close.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from lib import ctx as ctx_mod  # noqa: E402
from lib import procs as procs_mod  # noqa: E402

HELP_EPILOG = (
    "SAFETY: --all also kills dev-server chains that are NOT orphaned -- i.e. still\n"
    "serving something. Do not use --all while another server (e.g. one backing a\n"
    "still-running background audit agent) must survive; scope it with --root when\n"
    "you only mean to stop the chains under one project tree."
)


def _resolve_config_path() -> Path:
    """Same resolution rule as dispatch.py (CONTRACT.md #0): ULTHAR_CONFIG for tests
    only, otherwise the config next to this script (the deployed
    ~/.claude/hooks/config.json)."""
    import os

    override = os.environ.get("ULTHAR_CONFIG")
    if override:
        return Path(override)
    return HERE / "config.json"


def _session_ledger_entries(cfg: dict, ctx) -> dict:
    """This CLI's own session's ledger entries, merged across every ledger
    that shares this process's resolved (root_pid, root_create_time) -- M-2:
    a sub-agent's ledger is a SEPARATE file (different session_id) but the
    same root, and must not be invisible to a plain `stop_dev_orphans.py`
    run. {} when no session root, or no matching ledger, could be found --
    never another session's, and never a full-table fallback (see module
    docstring). Later ledgers win on a PID collision (exceedingly unlikely:
    would require two of THIS session's own sub-processes to share a pid,
    which only happens on OS pid reuse across two of them)."""
    root_pid, _err = procs_mod.resolve_session_root(cfg, ctx)
    if root_pid is None:
        return {}
    root_create_time = procs_mod.process_create_time(root_pid)
    if root_create_time is None:
        return {}
    merged: dict = {}
    for ledger in procs_mod.find_ledgers_by_root(cfg, ctx, root_pid, root_create_time):
        merged.update(ledger.get("entries", {}))
    return merged


def _find_targets(cfg: dict, ctx, all_flag: bool, root: str) -> list:
    """orphans ALWAYS qualify; --all additionally pulls in non-orphaned chains
    matching --root (or every chain, if --root is empty). Candidates are
    sourced from THIS session's own ledger only."""
    entries = _session_ledger_entries(cfg, ctx)
    orphans = procs_mod.find_dev_processes_in_ledger(cfg, ctx, entries, orphans_only=True)
    if not all_flag:
        return orphans
    orphan_pids = {p.pid for p in orphans}
    # root=None disables find_dev_processes_in_ledger()'s own root check (its
    # `root is None or ...` short-circuits true), returning every name+pattern
    # match regardless of orphan-ness or root; filtered case-insensitively below.
    candidates = procs_mod.find_dev_processes_in_ledger(cfg, ctx, entries, orphans_only=False, root=None)
    if not root:
        return candidates
    needle = root.casefold()
    return [p for p in candidates if p.pid in orphan_pids or needle in p.cmdline.casefold()]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="stop_dev_orphans.py",
        description="Kill orphaned dev-tool process chains (parent already dead).",
        epilog=HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--all", action="store_true", dest="all_flag",
        help="ALSO kill non-orphaned chains under --root (or all matching chains "
             "if --root is omitted). " + HELP_EPILOG,
    )
    parser.add_argument(
        "--root", default="",
        help="Restrict --all to chains whose command line contains this substring.",
    )
    args = parser.parse_args(argv)

    config_path = _resolve_config_path()
    cfg = ctx_mod.load_config(config_path)
    deploy_dir = config_path.parent
    ctx = ctx_mod.build_ctx({}, cfg, "CLI", deploy_dir)

    so = cfg["dev_processes"]["stop_orphans"]
    ports_str = cfg["dev_processes"]["ports_join"].join(str(p) for p in cfg["dev_processes"]["ports"])

    targets = _find_targets(cfg, ctx, args.all_flag, args.root)
    if not targets:
        print(so["no_targets_message"])
        return 0

    pids = [p.pid for p in targets]
    name_by_pid = {p.pid: p.name for p in targets}
    skips = procs_mod.kill(pids, ctx, cfg)
    skip_by_pid = dict(skips)
    for pid in pids:
        if pid in skip_by_pid:
            print(skip_by_pid[pid])
        else:
            print(f"killed {pid} {name_by_pid[pid]}")

    time.sleep(so["settle_seconds"])

    ports, err = procs_mod.listening_ports(cfg, ctx)
    if err is not None:
        # AccessDenied etc.: never report "ports free" when the check itself
        # couldn't run -- a false all-clear would be worse than no output.
        print(so["port_check_degraded_message"].replace("{ports}", ports_str).replace("{error}", err))
    elif ports:
        print(so["still_listening_message"])
        for p in ports:
            print(f"  {p}")
    else:
        print(so["ports_free_message"].replace("{ports}", ports_str))

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
