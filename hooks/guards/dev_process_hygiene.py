"""hooks/guards/dev_process_hygiene.py -- Stop guard.

Flags orphaned dev-tool process chains (dev servers, wrangler/vite/
concurrently, e2e servers, `tail -f` watchers, draw.io export) whose parent
process is already dead -- SCOPED TO THIS SESSION (ulthar-enforcement-round1):
a PID never recorded in <state_dir>/session-procs/<session_id>.json is never
reported, however it looks on the live machine. That ledger is written
elsewhere (dispatch.py, on PreToolUse Bash/PowerShell and on Stop) -- this
guard only READS it, per the guard-purity rule that filesystem side effects
outside the three named exceptions are not this file's job.

`stop_hook_active` is not handled here -- the dispatcher owns that
short-circuit (this guard's id is in `dispatch.honour_stop_hook_active_for`).
Without a per-session marker of its own, this guard's only loop guard is that
dispatcher-level flag; forgetting it would produce a silent infinite Stop loop.

One of only two modules in the runtime permitted to import psutil (the other
is stop_dev_orphans.py, a CLI). `lib.procs` is imported at module level here
deliberately -- dispatch.py only imports this module for a `Stop` event, so
psutil never loads on the PreToolUse hot path THROUGH THIS GUARD (dispatch.py
itself now also imports lib.procs directly, on PreToolUse Bash/PowerShell, to
keep the session ledger current -- a deliberate, documented deviation from the
former "psutil is Stop-only" latency argument, traded for session-scoping;
see lib/procs.py's session-ledger section docstring).

KNOWN RESIDUE (round 2, H-1, accepted and documented rather than closed --
see docs/DESIGN.md): a short-lived WRAPPER that is BOTH spawned AND has
already exited entirely BETWEEN two hook invocations (no PreToolUse/Stop
dispatch ever overlapped its lifetime) is never recorded in the ledger at
all, so a long-lived WORKER it left behind cannot be linked back to this
session either -- lib.procs.update_session_ledger's "widening" (any live pid
whose ppid is an ALREADY-ledgered pid) only helps once the wrapper itself
was captured at least once. Closing this fully would need continuous
monitoring between hook calls (polling or an OS process-creation
subscription), which this hook-driven design does not do.
"""
from __future__ import annotations

from lib import io as io_mod
from lib import paths as paths_mod
from lib import procs as procs_mod


def run(payload: dict, cfg: dict, ctx) -> io_mod.Result:
    dp = cfg["dev_processes"]
    if not dp.get("enabled", True):
        return io_mod.ALLOW

    session_id = str(payload.get("session_id") or "")
    if not session_id:
        # No session id to scope a ledger by -- nothing can be safely reported
        # (a ledger keyed by an empty/unknown id would be shared across every
        # such payload), so this is inert rather than falling back to a
        # full-table scan.
        return io_mod.ALLOW

    try:
        orphans = procs_mod.find_session_orphans(cfg, ctx, session_id)
    except procs_mod.LedgerError as exc:
        # "台帳が無い／壊れている -> 不活性で rc=0、config_error_log に1行":
        # a MISSING ledger is find_session_orphans returning [] silently (the
        # ordinary case, never logged); this branch is only the "壊れている"
        # half -- a ledger file that exists and cannot be trusted.
        ctx.log_config_error("session_ledger", f"session-procs/{session_id}.json", str(exc))
        return io_mod.ALLOW
    if not orphans:
        return io_mod.ALLOW

    max_listed = dp["max_listed"]
    truncate_at = dp["cmdline_truncate"]

    lines: list[str] = []
    for proc in orphans[:max_listed]:
        cmdline = proc.cmdline
        if len(cmdline) > truncate_at:
            cmdline = cmdline[:truncate_at] + "..."
        line = (
            dp["hygiene_line_format"]
            .replace("{pid}", str(proc.pid))
            .replace("{name}", proc.name)
            .replace("{cmdline}", cmdline)
        )
        lines.append(line)

    more = ""
    if len(orphans) > max_listed:
        more = dp["hygiene_more_format"].replace("{n}", str(len(orphans) - max_listed))

    ports_str = dp["ports_join"].join(str(p) for p in dp["ports"])
    python_exe = paths_mod.resolve_python_exe(cfg, ctx)
    deploy_dir = str(ctx.deploy_dir)

    message = dp["hygiene_message"]
    message = message.replace("{count}", str(len(orphans)))
    message = message.replace("{lines}", "\n".join(lines))
    message = message.replace("{more}", more)
    message = message.replace("{ports}", ports_str)
    message = message.replace("{python_exe}", python_exe)
    message = message.replace("{deploy_dir}", deploy_dir)

    return io_mod.block(message)
