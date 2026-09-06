"""hooks/guards/skill_anchor.py -- context anchor for long sessions.

Re-hands the active skill list's ANCHOR.md content to the model at the moment
it acts (Agent dispatch / SendMessage resume, SessionStart) and, more
narrowly, before a main-chain Write/Edit of a deliverable path.

PostCompact is NOT one of these moments (PE-20, 2026-09-05): the harness never
shows a PostCompact hook's stdout to the model (debug log only) and the event
carries no decision fields at all -- there is no output channel of any shape
for it. config.json's dispatch.events has no PostCompact entry, so this guard
is never invoked for that event; the anchor is re-delivered after compaction
via SessionStart(source=compact) instead, which already works.

THE ONE HARD RULE FOR THIS GUARD (never violate): it always returns exit_code
0. An anchor that blocks work is worse than one that is forgotten -- there is
no `block()` call anywhere in this module, and there must never be one.

Anchor lookup per active-skills entry: `skills_root/<name>/ANCHOR.md`, falling
back -- for ULTHAR's own entry only -- to the clone install.py recorded in
`<deploy_dir>/deployed-from.txt` (lib.paths.deployed_from). See _anchor_path.
This guard stays silent when nothing resolves, because it must never block and
has no channel a human reads; the loud, actionable report ("armed / not armed /
no ANCHOR.md, and what to create") is install.py's, printed on every deploy and
every --verify.

Two stdout shapes, chosen by ctx.event:
  - skill_anchor.plain_stdout_events (today: ["SessionStart"]) -> raw anchor text.
  - every other event -> the compact JSON envelope
    {"hookSpecificOutput": {"hookEventName": <event>, "additionalContext": <body>}}
    with key order and compactness preserved (json.dumps(..., ensure_ascii=False,
    separators=(",", ":")) so raw UTF-8 bytes are emitted, not \\uXXXX escapes).

Write/Edit gating: only applies when ctx.event == "PreToolUse" and the tool
matches skill_anchor.write_edit_tools_named. Within that branch:
  - main-chain-only is lib.ctx.is_main_chain (the two-signal agent_id +
    transcript-path test) -- cheaper to get wrong than a block, never one.
  - a blank file_path exits this branch early (unlike commander.py -- a
    deliberate divergence between guards, not harmonised).
  - "outside cwd" uses lib.paths.is_under(), not a string prefix check.
  - exempt_patterns (same list as commander's, paths.exempt_patterns.patterns)
    skip the anchor for coordination/scratch paths.
None of these early exits are reachable outside the Write/Edit branch --
SessionStart and Agent|SendMessage dispatch have no file_path at all and fall
straight through to anchor assembly.
"""
from __future__ import annotations

import json

from lib import paths as paths_mod
from lib.ctx import compile_pattern, compile_patterns, is_main_chain
from lib.io import ALLOW, Result, read_lines, read_text


def _resolve_list_path(cfg: dict, ctx):
    for template in cfg["skill_anchor"]["list_lookup_order"]:
        candidate = ctx.expand(template)
        if candidate.exists():
            return candidate
    return None


def _read_names(list_path, acfg: dict) -> list[str]:
    prefix = acfg["comment_prefix"]
    return [line for line in read_lines(list_path) if not line.startswith(prefix)]


def _write_edit_should_skip(payload: dict, cfg: dict, ctx) -> bool:
    """True when the Write/Edit-specific gate says "no anchor" -- not main
    chain, blank path, outside cwd, or an exempt (coordination/scratch) path.
    Only called when the event/tool combination has already been confirmed to
    be a guarded Write/Edit."""
    acfg = cfg["skill_anchor"]

    if acfg.get("write_edit_main_chain_only", True) and not is_main_chain(payload):
        return True  # two-signal main-chain test

    file = str((payload.get("tool_input") or {}).get("file_path") or "")
    if not file.strip():
        return True  # exits 0 on blank path -- unlike commander.py

    if acfg.get("outside_cwd_is_exempt", True) and not paths_mod.is_under(file, str(ctx.cwd), ctx):
        return True  # component-wise containment, not a string prefix check

    if acfg.get("write_edit_respect_exempt_patterns", True):
        exempt_patterns = compile_patterns(
            cfg["paths"]["exempt_patterns"]["patterns"], "path", ctx, "paths.exempt_patterns.patterns[]"
        )
        if paths_mod.matches_any(file, exempt_patterns, ctx):
            return True

    return False


def _is_guarded_write_edit(payload: dict, cfg: dict, ctx) -> bool:
    if ctx.event != "PreToolUse":
        return False
    acfg = cfg["skill_anchor"]
    named = cfg["regex"]["named_alternations"]
    tools_spec = named[acfg["write_edit_tools_named"]]
    pattern = compile_pattern(f"^({tools_spec})$", "text", ctx, "skill_anchor.write_edit_tools_named")
    tool = str(payload.get("tool_name") or "")
    return bool(pattern and pattern.search(tool))


def _off_disabled(names: list[str], acfg: dict) -> bool:
    if not acfg.get("off_token_requires_single_entry", True):
        return False
    if len(names) != 1:
        return False
    return names[0].casefold() == acfg["off_token"].casefold()


def _anchor_path(name: str, acfg: dict, ctx):
    """Where `name`'s ANCHOR.md is, or None.

    Primary lookup is unchanged: `skills_root/<name>/ANCHOR.md`.

    Fallback, and ONLY for ULTHAR's own entry (`skill_anchor.self_skill_name`):
    the clone install.py recorded in the deploy dir. Without it the anchor was
    reachable only from a clone that happened to sit at skills_root/ulthar, which
    the published install procedure never creates -- a user who created the marker
    exactly as documented got silence, with nothing anywhere saying why. ULTHAR
    knows where its OWN master is (install.py wrote it down); it knows nothing
    about any other skill, so no other name gets a fallback.

    The comparison is on the NAME, casefolded -- never on the clone's directory
    name. GitHub's default clone directory for this repo is `ULTHAR` and the skill
    name is `ulthar`; on a case-sensitive filesystem those differ, so a
    directory-name match would fail exactly where it is most needed. The recorded
    path is used verbatim, whatever the directory is called."""
    path = ctx.skills_root / name / acfg["anchor_file_name"]
    if path.exists():
        return path
    self_name = acfg.get("self_skill_name") or ""
    if not self_name or name.casefold() != self_name.casefold():
        return None
    master = paths_mod.deployed_from(ctx.deploy_dir)
    if master is None:
        return None
    fallback = master / acfg["anchor_file_name"]
    return fallback if fallback.exists() else None


def _collect_anchor_parts(names: list[str], cfg: dict, ctx) -> list[str]:
    acfg = cfg["skill_anchor"]
    reject = compile_pattern(acfg["name_reject_pattern"], "path", ctx, "skill_anchor.name_reject_pattern")
    parts: list[str] = []
    for name in names:
        if reject and reject.search(name):
            continue  # the list is data; a name is a directory name only
        anchor_path = _anchor_path(name, acfg, ctx)
        if anchor_path is None:
            continue
        try:
            text = read_text(anchor_path).strip()
        except Exception:
            continue
        if text:
            parts.append(text)
    return parts


def run(payload: dict, cfg: dict, ctx) -> Result:
    acfg = cfg["skill_anchor"]
    if not acfg.get("enabled", True):
        return ALLOW

    list_path = _resolve_list_path(cfg, ctx)
    if list_path is None:
        return ALLOW

    names = _read_names(list_path, acfg)
    if not names:
        return ALLOW
    if _off_disabled(names, acfg):
        return ALLOW

    if _is_guarded_write_edit(payload, cfg, ctx) and _write_edit_should_skip(payload, cfg, ctx):
        return ALLOW

    parts = _collect_anchor_parts(names, cfg, ctx)
    if not parts:
        return ALLOW

    body = acfg["join_separator"].join(parts)
    max_chars = acfg["max_chars"]
    if len(body) > max_chars:
        body = body[:max_chars] + acfg["truncation_suffix"]

    if ctx.event in acfg.get("plain_stdout_events", []):
        return Result(0, body, "")

    envelope = {
        acfg["envelope_key"]: {
            "hookEventName": ctx.event,
            "additionalContext": body,
        }
    }
    stdout = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    return Result(0, stdout, "")
