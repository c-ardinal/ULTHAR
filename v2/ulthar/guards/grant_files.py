"""hooks/guards/grant_files.py -- PreToolUse guard (Write/Edit/MultiEdit/NotebookEdit).

Blocks every agent -- subagent AND commander -- from writing the files that carry
the OWNER's permission over ULTHAR's own rules: the grant directory
(`grants.dir`), the delete rule's pm-delete-allow ledger, and the user and
project overlay files, each named by REFERENCE to the one place its path already
lives (see grants.protected / lib/grant.py:protected_targets).

WHY THIS GUARD EXISTS. The owner's requirement is that a rule be waivable "when
the USER has permitted it". A permission file the AGENT can write establishes
nothing -- the agent is permitting itself, which is exactly what happened
repeatedly with pm-delete-allow: it sits under ~/.claude/state, which
paths.exempt_patterns exempts, so the commander could Write it and consume it in
the same turn. On a single-user machine the author of a file cannot be
authenticated, so the closure has to be structural: these files are simply not
writable through this tool. This guard is the Write/Edit half;
command_rules.tool_discipline/authority-file-in-shell is the shell half.

NO main-chain check, deliberately: unlike a grant (which never applies to a
subagent), this protection applies to EVERY chain. The commander is the actor
this exists to stop.

Containment, not equality: lib.paths.is_under() answers both questions at once
-- a path equal to a protected FILE is "under" it, and any path inside the grant
DIRECTORY is too -- so a new grant three directories deep is covered without
enumerating anything. Case handling is lib.paths' usual (case-insensitive on
Windows, exact on POSIX).

Reads `notebook_path` as well as `file_path`: NotebookEdit supplies the former,
and a guard that only read `file_path` would leave a whole tool able to write
these files (the divergence commander.py's F-16 note records for the other three
Write/Edit guards).

This guard has no rule list and therefore no rule identifier, so no grant can
name it -- it is unaddressable by construction, which is the property that stops
the grant mechanism closing over itself.
"""
from __future__ import annotations

from pathlib import Path

from ..lib import grant as grant_mod
from ..lib import paths as paths_mod
from ..lib.io import ALLOW, Result, block


def run(payload: dict, cfg: dict, ctx) -> Result:
    gcfg = cfg.get("grants") or {}
    if not gcfg.get("enabled", False):
        return ALLOW

    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    file = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
    if not file.strip():
        return ALLOW
    # A RELATIVE tool path is resolved against the payload's cwd before comparing:
    # every protected target is absolute, and comparing an absolute target against
    # `\.claude/ulthar-overlay.json` would simply miss -- which is the whole
    # protection gone for the one spelling an agent standing in the repo would
    # naturally use. ctx.cwd IS the payload's cwd (lib/ctx.py:build_ctx).
    candidate = file if Path(file).is_absolute() else str(ctx.cwd / file)

    for entry, target in grant_mod.protected_targets(cfg, ctx):
        if paths_mod.is_under(candidate, str(target), ctx):
            message = (
                gcfg["protected_message"]
                .replace("{file}", file)
                .replace("{why}", str(entry.get("why", entry.get("id", ""))))
            )
            return block(message)
    return ALLOW
