"""hooks/guards/dev_process_hygiene.py -- Stop guard.

Flags orphaned dev-tool process chains (dev servers, wrangler/vite/
concurrently, e2e servers, `tail -f` watchers, draw.io export) whose parent
process is already dead.

`stop_hook_active` is not handled here -- the dispatcher owns that
short-circuit (this guard's id is in `dispatch.honour_stop_hook_active_for`).
Without a per-session marker of its own, this guard's only loop guard is that
dispatcher-level flag; forgetting it would produce a silent infinite Stop loop.

One of only two modules in the runtime permitted to import psutil (the other
is stop_dev_orphans.py, a CLI). `lib.procs` is imported at module level here
deliberately -- dispatch.py only imports this module for a `Stop` event, so
psutil never loads on the PreToolUse hot path.
"""
from __future__ import annotations

from lib import io as io_mod
from lib import paths as paths_mod
from lib import procs as procs_mod


def run(payload: dict, cfg: dict, ctx) -> io_mod.Result:
    dp = cfg["dev_processes"]
    if not dp.get("enabled", True):
        return io_mod.ALLOW

    orphans = procs_mod.find_dev_processes(cfg, ctx, orphans_only=True)
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
