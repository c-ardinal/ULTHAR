"""hooks/guards/git_discipline.py -- PreToolUse guard: git syntactic discipline.

Checks blanket staging, working-tree destroyers, hook/signing bypass, force
push, deploy-ref pushes, and protected-branch pushes -- one pure function of
(payload, cfg, ctx).

Phase order is `git_policy.phase_order` and is load-bearing:
    git_command_detect -> blanket_staging -> destroyers -> hook_bypass
    -> is_write_subcommand -> force_push -> deploy_refs -> protected_push
    -> current_branch

`git_command_detect` alone matches the raw command (it runs before any probe
is built); every phase after it matches the quote-stripped `probe` unless its
`match_target` says otherwise; `current_branch` carries `match_target: null`,
meaning it has no regex target at all -- it shells out to `git rev-parse`.

No policy literal lives in this module: every pattern fragment, message,
branch name and ref name comes from `git_policy` in config.json. This module
only sequences the phases and does string substitution (`{why}` / `{ref}` /
`{branch}` via plain str.replace, never str.format).

Every phase that decides to block first consults lib/grant.py for an OWNER GRANT
naming that exact entry (`git_policy.force_push/push-force`,
`git_policy.protected_branches/main`, ...), and a grant makes the entry
`continue` rather than block -- so the entries after it, and every phase after
this one, still run. The match logic itself is unchanged; the consult only asks
whether the owner permitted the decision already reached.
"""
from __future__ import annotations

import re
from pathlib import Path

from ..lib import gitcmd
from ..lib import grant as grant_mod
from ..lib.io import ALLOW, Result, block


def _target_for(phase_name: str, raw: str, probe: str, phase_map: dict) -> str | None:
    """Resolves the per-phase match_target (never a section-level default).
    `null` means "this phase has no regex target" -- callers for
    current_branch never use the return value for regex matching; the lookup
    is still made so an absent phase entry fails loud (KeyError, caught by
    dispatch.py's fail-open wrapper and logged) rather than silently
    defaulting."""
    mt = phase_map[phase_name]
    if mt == "raw":
        return raw
    if mt == "probe_quotes":
        return probe
    return None


def _check_list(entries: list, target: str, cfg: dict, message_key: str,
                list_path: str, payload: dict, ctx) -> Result | None:
    """`enabled: false` skips an entry entirely; missing `enabled` defaults to
    True. Walks entries in array order and blocks on the first match.

    An entry that matches consults lib/grant.py for an owner grant naming THAT
    entry (`<list_path>/<entry id>`); a grant makes this entry `continue` rather
    than block, so the entries after it -- and every phase after this list --
    still run. Match logic itself is unchanged."""
    for entry in entries:
        if not entry.get("enabled", True):
            continue
        pat = gitcmd.subcommand_regex(cfg, entry["suffix"])
        if pat.search(target):
            msg = cfg["git_policy"][message_key].replace("{why}", entry["why"])
            msg = _grant(f"{list_path}/{entry.get('id')}", msg, payload, cfg, ctx)
            if msg is None:
                continue
            return block(msg)
    return None


def _grant(ident: str, message: str, payload: dict, cfg: dict, ctx) -> str | None:
    """None when the owner granted this exact rule for this one call (the caller
    continues); otherwise the message to block with, hint appended."""
    return grant_mod.check(ident, message, payload, cfg, ctx)


def run(payload: dict, cfg: dict, ctx) -> Result:
    tool_input = payload.get("tool_input") or {}
    cmd = str(tool_input.get("command") or "")
    if not cmd.strip():
        return ALLOW

    if not gitcmd.is_git_command(cmd, cfg):
        return ALLOW

    probe = gitcmd.strip_quotes(cmd, cfg)

    gp = cfg["git_policy"]
    phase_map = {p["phase"]: p["match_target"] for p in gp["phase_order"]}

    # --- blanket staging (git add -A / git add .) ---
    target = _target_for("blanket_staging", cmd, probe, phase_map)
    result = _check_list(gp["blanket_staging"], target, cfg, "blanket_staging_message",
                         "git_policy.blanket_staging", payload, ctx)
    if result is not None:
        return result

    # --- working-tree destroyers (reset --hard, clean -f, checkout .) ---
    target = _target_for("destroyers", cmd, probe, phase_map)
    result = _check_list(gp["destroyers"], target, cfg, "destroyers_message",
                         "git_policy.destroyers", payload, ctx)
    if result is not None:
        return result

    # --- hook/signing bypass (--no-verify, --no-gpg-sign). Sits ahead of
    # is_write_subcommand for the same reason blanket_staging/destroyers do: it is
    # an operation-TYPE check, not a destination-specific one. ---
    target = _target_for("hook_bypass", cmd, probe, phase_map)
    result = _check_list(gp["hook_bypass"], target, cfg, "hook_bypass_message",
                         "git_policy.hook_bypass", payload, ctx)
    if result is not None:
        return result

    # --- is_write_subcommand: early ALLOW gate. A command that is not
    # commit/push/merge/rebase returns here without git ever being run. ---
    target = _target_for("is_write_subcommand", cmd, probe, phase_map)
    write_pat = gitcmd.subcommand_regex(cfg, gp["write_subcommand_suffix"])
    if not write_pat.search(target):
        return ALLOW

    # --- force push (--force / --force-with-lease / -f). Checked before
    # deploy_refs/protected_push so "you are force-pushing at all" (operation-type)
    # is reported ahead of "to this specific destination" (destination-specific). ---
    target = _target_for("force_push", cmd, probe, phase_map)
    result = _check_list(gp["force_push"], target, cfg, "force_push_message",
                         "git_policy.force_push", payload, ctx)
    if result is not None:
        return result

    # --- deploy-triggering preview branches ---
    target = _target_for("deploy_refs", cmd, probe, phase_map)
    for ref in gp["deploy_refs"]:
        suffix = gp["push_target_suffix_template"].replace("{ref}", re.escape(ref))
        pat = gitcmd.subcommand_regex(cfg, suffix)
        if pat.search(target):
            msg = gp["deploy_ref_message"].replace("{ref}", ref)
            # Scalar list: the entry IS its own identifier (lib/ctx.py:_entry_id),
            # exactly as an overlay `disable` addresses it.
            msg = _grant(f"git_policy.deploy_refs/{ref}", msg, payload, cfg, ctx)
            if msg is None:
                continue
            return block(msg)

    # --- protected branch, explicit form: push directly naming it ---
    target = _target_for("protected_push", cmd, probe, phase_map)
    for branch_name in gp["protected_branches"]:
        suffix = gp["protected_push_target_suffix_template"].replace(
            "{ref}", re.escape(branch_name)
        )
        pat = gitcmd.subcommand_regex(cfg, suffix)
        if pat.search(target):
            msg = gp["protected_push_message"].replace("{ref}", branch_name)
            msg = _grant(f"git_policy.protected_branches/{branch_name}", msg, payload, cfg, ctx)
            if msg is None:
                continue
            return block(msg)

    # --- protected branch, implicit form: resolve the current branch and block
    # writes made while standing on it. match_target: null -- this phase has no
    # regex target at all; it shells out to `git rev-parse` instead. Repo dir is
    # the payload's cwd directly -- this phase never consults a `-C <dir>` inside
    # the command text (only pr_state does that, via gitcmd.repo_dir). ---
    _target_for("current_branch", cmd, probe, phase_map)  # validates config presence
    cwd_raw = payload.get("cwd")
    dir_ = Path(cwd_raw) if cwd_raw else ctx.cwd
    if not dir_.exists():
        return ALLOW
    branch = gitcmd.current_branch(dir_, ctx)
    if not branch:
        return ALLOW
    matched = next((b for b in gp["protected_branches"] if b.casefold() == branch.casefold()), None)
    if matched is not None:
        msg = gp["protected_branch_message"].replace("{branch}", branch)
        # Addressed by the CONFIG entry's own spelling, not by what git reported:
        # one identifier for both protected-branch phases, matching how a
        # `disable` would name it.
        msg = _grant(f"git_policy.protected_branches/{matched}", msg, payload, cfg, ctx)
        if msg is not None:
            return block(msg)

    return ALLOW
