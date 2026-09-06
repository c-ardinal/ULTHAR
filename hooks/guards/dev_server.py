"""hooks/guards/dev_server.py -- PreToolUse guard: e.g. `npm run dev:heavy` must carry
an enlarged Node heap in the SAME command (a heavy build can OOM on Node's default heap).

Config-driven: block if `pattern` matches and `requires` does not. No policy
literal (pattern/message) lives here.

`target` selects which probe of the command the rule is tested against --
`raw` | `probe_redirect` | `probe_quotes`, the same three-way enum
`tool_discipline` honours. Guards must not import each other, so the small
probe-building step is duplicated here rather than imported from
`guards.tool_discipline`.
"""
from __future__ import annotations

from lib import ctx as ctx_mod
from lib import io as io_mod

_RULE_KEY = "dev_server"


def run(payload: dict, cfg: dict, ctx) -> io_mod.Result:
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    cmd = str(tool_input.get("command") or "")
    if not cmd.strip():
        return io_mod.ALLOW

    probes = _build_probes(cmd, cfg, ctx)

    for idx, rule in enumerate(cfg["command_rules"][_RULE_KEY]):
        if not rule.get("enabled", True):
            continue
        if rule["type"] != "regex_require":
            # Unknown rule type: a one-character typo in `type` must not
            # silently remove a check with no trace.
            ctx.log_config_error(
                "unknown_rule_type",
                f"command_rules.{_RULE_KEY}[{idx}].type",
                f"unrecognised rule type {rule['type']!r}",
                guard_scope=_RULE_KEY,
            )
            continue
        result = _eval_regex_require(rule, probes, ctx)
        if result is not None:
            return result
    return io_mod.ALLOW


def _build_probes(cmd: str, cfg: dict, ctx) -> dict:
    """`raw` unmodified; `probe_redirect` with harmless redirects stripped;
    `probe_quotes` with quoted strings blanked -- same three targets
    `tool_discipline` exposes (CONTRACT.md #3.6), built from the same shared
    config lists so both guards treat a given command identically."""
    redirect = cmd
    for pattern, repl in cfg["command_rules"]["redirect_noise_strips"]:
        compiled = ctx_mod.compile_pattern(
            pattern, "text", ctx, "command_rules.redirect_noise_strips[]"
        )
        if compiled is not None:
            redirect = compiled.sub(repl, redirect)

    quotes = cmd
    for pattern, repl in cfg["command_rules"]["quote_strip_patterns"]:
        compiled = ctx_mod.compile_pattern(
            pattern, "text", ctx, "command_rules.quote_strip_patterns[]"
        )
        if compiled is not None:
            quotes = compiled.sub(repl, quotes)

    return {"raw": cmd, "probe_redirect": redirect, "probe_quotes": quotes}


def _eval_regex_require(rule: dict, probes: dict, ctx) -> io_mod.Result | None:
    target = probes[rule.get("target", "raw")]

    pattern = ctx_mod.compile_pattern(
        rule["pattern"], "text", ctx, f"command_rules.{_RULE_KEY}[].pattern"
    )
    if pattern is None or not pattern.search(target):
        return None

    requires = ctx_mod.compile_pattern(
        rule["requires"], "text", ctx, f"command_rules.{_RULE_KEY}[].requires"
    )
    if requires is not None and requires.search(target):
        return None  # requirement satisfied

    return io_mod.block(rule["message"])
