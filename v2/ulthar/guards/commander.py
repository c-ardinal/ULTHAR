"""hooks/guards/commander.py -- PreToolUse guard (Write/Edit/MultiEdit/NotebookEdit).

Enforces the one hard rule of the ulthar skill: while commander-mode is
active, the main chain delegates and reviews but never authors deliverables.

Activation is read from commander.marker_lookup_order (first existing path
wins). The mode is the marker file's first line only, stripped and casefolded:
  - matches commander.modes.inert ("off")     -> fully inert, exit before any logging
  - matches commander.modes.block ("enforce") -> block mode
  - anything else (including no marker content, or the literal "observe")
    -> commander.modes[commander.default_mode_when_unrecognised] ("observe").

Main-vs-subagent is lib.ctx.is_main_chain -- the two-signal (agent_id +
transcript path) test. "Outside the project tree" is decided with
lib.paths.is_under() (component-wise, normalised), not a plain string prefix
check, which would wrongly treat C:\\repo-backup\\x as inside C:\\repo.

A blank tool_input.file_path (as NotebookEdit supplies -- it sends
notebook_path instead) matches no exempt pattern, and the outside-cwd
exemption is skipped precisely because the path is blank. So a blank path
stays non-exempt and, under enforce mode on the main chain, blocks -- exactly
like every other guarded Write/Edit call. This is a deliberate divergence from
deploy.py/skill_anchor.py (which exempt a blank path outright); do not
harmonise it away.

Every decision (block or allow) reached after the marker resolves to a
non-inert mode is appended to commander.decision_log, including the log-write
itself failing silently (best-effort) -- a logging failure must never turn a
real block into an accidental allow.
"""
from __future__ import annotations

from ..lib import paths as paths_mod
from ..lib.ctx import compile_patterns, is_main_chain
from ..lib.io import ALLOW, Result, append_jsonl_drop_oldest, block, read_text


def _read_first_line(path) -> str:
    text = read_text(path)
    for line in text.splitlines():
        return line
    return ""


def _resolve_marker(cfg: dict, ctx) -> object | None:
    for template in cfg["commander"]["marker_lookup_order"]:
        candidate = ctx.expand(template)
        if candidate.exists():
            return candidate
    return None


def run(payload: dict, cfg: dict, ctx) -> Result:
    ccfg = cfg["commander"]
    if not ccfg.get("enabled", True):
        return ALLOW

    marker = _resolve_marker(cfg, ctx)
    if marker is None:
        # No marker anywhere in the lookup order -> fully inert, before any logging.
        return ALLOW

    modes = ccfg["modes"]
    raw_mode = _read_first_line(marker).strip().casefold()
    inert_token = modes["inert"].casefold()
    block_token = modes["block"].casefold()

    if raw_mode == inert_token:
        return ALLOW  # "off": inert, no log entry

    if raw_mode == block_token:
        mode = modes["block"]
    else:
        mode = modes[ccfg["default_mode_when_unrecognised"]]  # resolves to "observe"

    tool = str(payload.get("tool_name") or "")
    file = str((payload.get("tool_input") or {}).get("file_path") or "")
    cwd = ctx.cwd

    agent_id = str(payload.get("agent_id") or "")
    agent_type = str(payload.get("agent_type") or "")
    transcript = str(payload.get("transcript_path") or "")
    is_main = is_main_chain(payload)

    exempt_patterns = compile_patterns(
        cfg["paths"]["exempt_patterns"]["patterns"], "path", ctx, "paths.exempt_patterns.patterns[]"
    )
    exempt = paths_mod.matches_any(file, exempt_patterns, ctx)
    if not exempt and file.strip():
        # The outside-cwd exemption is evaluated only when file is non-blank.
        if ccfg.get("outside_cwd_is_exempt", True) and not paths_mod.is_under(file, str(cwd), ctx):
            exempt = True

    blocking = is_main and not exempt and mode == modes["block"]

    entry = {
        "ts": ctx.now().isoformat(),
        "mode": mode,
        "tool": tool,
        "file": file,
        "agent_id": agent_id,
        "agent_type": agent_type,
        "transcript": transcript,
        "is_main": is_main,
        "exempt": exempt,
        "decision": "block" if blocking else "allow",
    }
    try:
        append_jsonl_drop_oldest(
            ctx.expand(ccfg["decision_log"]), entry,
            max_bytes=ccfg.get("max_decision_log_bytes"),
        )
    except Exception:
        pass  # best-effort: a logging failure must never block the decision path

    if not blocking:
        return ALLOW

    message = (
        ccfg["message"]
        .replace("{tool}", tool)
        .replace("{file}", file)
        .replace("{cwd}", str(cwd))
    )
    return block(message)
