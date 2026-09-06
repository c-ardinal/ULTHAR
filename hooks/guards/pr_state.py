"""hooks/guards/pr_state.py -- PreToolUse guard: block a push to a branch whose PR
is already merged/closed with no OPEN pull request.

`pr_state` matches the raw command in every phase -- it never builds a
quote-stripped probe the way `git_discipline` does. That asymmetry between the
two guards is real, is preserved, and is not to be harmonised.

Two subprocess calls, each with an explicit `kind`: the branch-resolution
fallback (`git rev-parse`) is "local" (5s budget); `gh pr list` is "remote"
(20s budget). Both fail open (ALLOW) on any non-zero exit, timeout or parse
error -- a slow/hung `gh` must never silently block every push; it must
silently ALLOW instead.
"""
from __future__ import annotations

import json as json_mod
from pathlib import Path

from lib import gitcmd
from lib.ctx import compile_pattern
from lib.io import ALLOW, Result, block


def run(payload: dict, cfg: dict, ctx) -> Result:
    gp = cfg["git_policy"]
    pr = gp["pr_state"]
    if not pr.get("enabled", True):
        return ALLOW

    tool_input = payload.get("tool_input") or {}
    cmd = str(tool_input.get("command") or "")
    if not cmd.strip():
        return ALLOW

    # Every place below that scans command text resolves its target through
    # this config value instead of using `cmd` directly. Today only "raw" is
    # implemented (pr_state never builds a quote-stripped probe); an
    # unrecognised value is a config defect and raises, which dispatch.py's
    # per-guard fail-open turns into ALLOW + one dispatch-errors.jsonl line.
    match_target = pr.get("match_target")
    if match_target == "raw":
        target = cmd
    else:
        raise ValueError(
            f"git_policy.pr_state.match_target={match_target!r} is not implemented "
            "(only 'raw' is supported today)"
        )

    # push_detect_suffix is composed with the shared git_policy.global_option_prefix
    # so a leading global option like `git --exec-path=/x push origin <branch>`
    # cannot bypass detection.
    push_pat = gitcmd.subcommand_regex(cfg, pr["push_detect_suffix"])
    if not push_pat.search(target):
        return ALLOW

    # Deleting a remote branch is not "a push of changes" -- never consult PR state.
    delete_pat = compile_pattern(
        pr["branch_delete_push"], "text", ctx, "git_policy.pr_state.branch_delete_push"
    )
    if delete_pat is not None and delete_pat.search(target):
        return ALLOW

    fallback_dir = Path(payload.get("cwd") or ctx.cwd)
    dir_ = gitcmd.repo_dir(target, cfg, fallback_dir)

    branch = gitcmd.push_target_branch(target, cfg)
    if not branch:
        branch = gitcmd.current_branch(dir_, ctx)

    if not branch or branch.casefold() == "head":
        return ALLOW

    sha_pat = compile_pattern(
        pr["sha_ref_pattern"], "text", ctx, "git_policy.pr_state.sha_ref_pattern"
    )
    if sha_pat is not None and sha_pat.match(branch):
        return ALLOW

    # Fail open on any gh outcome that is not a clean, parseable success --
    # non-zero rc, timeout, or empty stdout. pr_state is not one of the guards
    # permitted filesystem side effects, so it does not call ctx.log_error itself.
    argv = [_sub(tok, branch, pr["limit"]) for tok in pr["gh_argv"]]
    result = ctx.run_cmd(argv, "remote", cwd=dir_)
    if result.timed_out or result.rc != 0 or not result.out.strip():
        return ALLOW

    try:
        parsed = json_mod.loads(result.out)
    except (json_mod.JSONDecodeError, ValueError):
        return ALLOW
    if not isinstance(parsed, list):
        return ALLOW

    prs = [entry for entry in parsed if isinstance(entry, dict) and entry.get("number")]
    if not prs:
        return ALLOW

    open_state = str(pr["open_state"]).casefold()
    if any(str(entry.get("state", "")).casefold() == open_state for entry in prs):
        return ALLOW

    line_format = pr["pr_line_format"]
    lines = [
        line_format.replace("{number}", str(entry.get("number")))
        .replace("{state}", str(entry.get("state")))
        .replace("{url}", str(entry.get("url")))
        for entry in prs
    ]
    pr_list = "\n".join(lines)
    message = pr["message"].replace("{branch}", branch).replace("{pr_list}", pr_list)
    return block(message)


def _sub(token: str, branch: str, limit) -> str:
    return token.replace("{branch}", branch).replace("{limit}", str(limit))
