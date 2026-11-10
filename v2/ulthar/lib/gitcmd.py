"""hooks/lib/gitcmd.py -- git command-line parsing shared by git_discipline and pr_state.

CONTRACT.md #2 (lib/gitcmd.py). `git_policy.global_option_prefix` (PE-4) is the
UNION of the `-C|-c|--git-dir|--work-tree|--exec-path` prefixes both guards must
recognise -- a narrower prefix set here would let `git --exec-path=/x push origin
<merged-branch>` skip the PR-state check.

All regex patterns this module compiles come from cfg -- it holds no policy literal.
Every `re.compile()` call goes through `lib.ctx.compile_text_pattern`, the one
authority for `kind: "text"` (regex.kinds.text -- always IGNORECASE): a pattern
relying on its own inline `(?i)` instead is forbidden by N-1, and `git`/`gh`
subcommand matching must not become case-sensitive on some config strings and not
others.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import ctx as ctx_mod


def is_git_command(cmd: str, cfg: dict) -> bool:
    pat = ctx_mod.compile_text_pattern(cfg["git_policy"]["git_command_detect"],
                                        "git_policy.git_command_detect")
    return bool(pat and pat.search(cmd))


_QUOTE_STRIP_CACHE: dict[int, tuple[int, list[tuple[re.Pattern, str]]]] = {}


def strip_quotes(cmd: str, cfg: dict) -> str:
    """Quoted strings -> "" / ''. Used to build the quote-stripped `probe_quotes`
    target so a message inside quotes (e.g. `git commit -m "drop the -a flag"`)
    cannot be mistaken for the subcommand it names.

    The patterns (`command_rules.quote_strip_patterns`) must each REQUIRE a
    closing quote (`"[^"]*"`, not `"[^"]*"?`): an unterminated quote (e.g.
    `git commit -m "x -a`) must be left alone so `-a` stays visible and the
    blanket-staging rule still BLOCKS -- a pattern with an optional closing
    quote would swallow the rest of the line, including `-a`, and silently
    allow what should be blocked."""
    # ponytail: cache keyed on id(cfg), single entry -- correct only because one
    # process handles exactly one cfg for its whole lifetime; key on something
    # cfg-content-derived (e.g. a version field) if ever embedded long-lived or
    # reused across configs in one process.
    cached = _QUOTE_STRIP_CACHE.get(id(cfg))
    if cached is None or cached[0] != id(cfg):
        pairs = [(ctx_mod.compile_text_pattern(pattern, "command_rules.quote_strip_patterns[][0]"), repl)
                  for pattern, repl in cfg["command_rules"]["quote_strip_patterns"]]
        cached = (id(cfg), pairs)
        _QUOTE_STRIP_CACHE[id(cfg)] = cached
    for pattern, repl in cached[1]:
        cmd = pattern.sub(repl, cmd)
    return cmd


_subcommand_regex_cache: dict[tuple[int, str], re.Pattern] = {}


def subcommand_regex(cfg: dict, suffix: str) -> re.Pattern:
    """prefix + suffix, cached by (id(cfg), suffix) for the lifetime of one dispatch."""
    # ponytail: id(cfg) can be reused after a dict is garbage-collected, in
    # principle returning a stale pattern for an unrelated later config. Safe
    # under this runtime's actual lifecycle (one process per dispatch, one cfg
    # object) but not under a long-lived embedding or pytest-xdist-style reuse
    # -- fix would need a reference-holding key (e.g. `(cfg.get('version'),
    # id(cfg))`) plus keeping cfg alive.
    key = (id(cfg), suffix)
    cached = _subcommand_regex_cache.get(key)
    if cached is not None:
        return cached
    prefix = cfg["git_policy"]["global_option_prefix"]
    pat = ctx_mod.compile_text_pattern(prefix + suffix, "git_policy.global_option_prefix+suffix")
    _subcommand_regex_cache[key] = pat
    return pat


_DIR_OPTION_CACHE: dict[int, re.Pattern] = {}


def repo_dir(cmd: str, cfg: dict, fallback: Path) -> Path:
    """Honours `git -C <dir>` (quoted or bare)."""
    # ponytail: same id(cfg)-keyed cache tradeoff as subcommand_regex() above.
    pat = _DIR_OPTION_CACHE.get(id(cfg))
    if pat is None:
        pat = ctx_mod.compile_text_pattern(cfg["git_policy"]["pr_state"]["dir_option"],
                                            "git_policy.pr_state.dir_option")
        _DIR_OPTION_CACHE[id(cfg)] = pat
    m = pat.search(cmd) if pat else None
    if not m:
        return fallback
    value = m.group(3) or m.group(4)
    if not value:
        return fallback
    return Path(value)


def push_target_branch(cmd: str, cfg: dict) -> str | None:
    """The FIRST refspec after the remote -- tokens[refspec_token_index], index 1 --
    NOT the last. Preserved as-is for parity (F-14)."""
    pr = cfg["git_policy"]["pr_state"]
    push_split = ctx_mod.compile_text_pattern(pr["push_split_pattern"], "git_policy.pr_state.push_split_pattern")
    parts = push_split.split(cmd, maxsplit=1)
    if len(parts) < 2:
        return None
    after_push = parts[1]

    segment_split = ctx_mod.compile_text_pattern(pr["segment_split_pattern"],
                                                  "git_policy.pr_state.segment_split_pattern")
    seg = segment_split.split(after_push, maxsplit=1)[0]

    token_split = ctx_mod.compile_text_pattern(pr["token_split_pattern"], "git_policy.pr_state.token_split_pattern")
    tokens = [t for t in token_split.split(seg) if t]

    reject = ctx_mod.compile_text_pattern(pr["token_reject_pattern"], "git_policy.pr_state.token_reject_pattern")
    tokens = [t for t in tokens if not reject.search(t)]

    idx = pr["refspec_token_index"]
    if idx >= len(tokens):
        return None
    token = tokens[idx]
    if ":" in token:
        token = token.split(":", 1)[1]
    strip = ctx_mod.compile_text_pattern(pr["refs_heads_strip_pattern"],
                                          "git_policy.pr_state.refs_heads_strip_pattern")
    return strip.sub("", token)


def current_branch(dir: Path, ctx) -> str | None:
    """Via ctx.run_cmd with the LOCAL timeout budget."""
    result = ctx.run_cmd(["git", "rev-parse", "--abbrev-ref", "HEAD"], "local", cwd=dir)
    if result.rc != 0 or result.timed_out:
        return None
    branch = result.out.strip()
    return branch or None
