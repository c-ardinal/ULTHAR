"""hooks/guards/deploy.py -- PreToolUse guard (Write/Edit/MultiEdit/NotebookEdit).

Blocks direct edits of the DEPLOYED hook tree (paths.deploy_dir): the correct
flow is "edit the master under the skill's hooks/, then `install.py`".

`guarded_extensions` includes ".py" (the deployed hooks are .py) and
`guarded_names` covers "config.json" so a direct edit of the live config is
also caught, plus "deployed-from.txt" -- the deploy dir's trust anchor, which
decides where this very message says the master lives and where skill_anchor
resolves ANCHOR.md from, and which is neither .py nor .ps1 (see
config.json deploy_guard._note_deployed_from). All config-driven, not
hardcoded here.

Recursion covers the whole deployed tree at any depth, logs/ included --
lib.paths.is_under() gives "anywhere under deploy_dir" containment.

Extension/name comparison is case-insensitive on Windows, case-sensitive on
POSIX, matching the rest of this codebase's path-pattern convention.

A blank tool_input.file_path allows (exempt), like skill_anchor.py but unlike
commander.py -- a deliberate, preserved divergence between the three
Write/Edit guards; do not harmonise it away.
"""
from __future__ import annotations

import os
from pathlib import Path

from lib import paths as paths_mod
from lib.io import ALLOW, Result, block


def _guarded_hit(file: str, deploy_dir: Path, cfg: dict, ctx) -> bool:
    dcfg = cfg["deploy_guard"]
    if not dcfg.get("recursive", True):
        return False
    if not paths_mod.is_under(file, str(deploy_dir), ctx):
        return False

    name = Path(file).name
    suffix = Path(file).suffix
    extensions = dcfg.get("guarded_extensions", [])
    names = dcfg.get("guarded_names", [])
    if ctx.is_windows:
        name_key, suffix_key = name.casefold(), suffix.casefold()
        extensions = [e.casefold() for e in extensions]
        names = [n.casefold() for n in names]
    else:
        name_key, suffix_key = name, suffix

    return suffix_key in extensions or name_key in names


def run(payload: dict, cfg: dict, ctx) -> Result:
    dcfg = cfg["deploy_guard"]
    if not dcfg.get("enabled", True):
        return ALLOW

    file = str((payload.get("tool_input") or {}).get("file_path") or "")
    if not file.strip():
        return ALLOW

    deploy_dir = ctx.deploy_dir
    if not _guarded_hit(file, deploy_dir, cfg, ctx):
        return ALLOW

    relative_path = paths_mod.to_posix(os.path.relpath(file, str(deploy_dir)))
    # The master is the clone install.py deployed from, not a fixed location --
    # see deploy_guard._note_message. Unknown stays unknown: the label says so
    # rather than naming a directory that probably does not exist.
    master = paths_mod.deployed_from(deploy_dir)
    master_hooks_dir = (
        paths_mod.to_posix(str(master / "hooks")) if master is not None
        else dcfg["master_unknown_label"]
    )
    message = (
        dcfg["message"]
        .replace("{relative_path}", relative_path)
        .replace("{master_hooks_dir}", master_hooks_dir)
        .replace("{python_exe}", paths_mod.resolve_python_exe(cfg, ctx))
    )
    return block(message)
