"""hooks/guards/brief_gate.py -- PreToolUse guard for Agent/SendMessage briefs.

Detects whether the tool call carries a "brief" at all, classifies it into a
kind (forwarder / committer / reviewer / creator), then walks that kind's
`rules[]` in array order, blocking on the first rule that fails.

This module contains no case names, no gate numbers as branch conditions, and
no UI vocabulary: `require_pattern` / `design_cases` / `approval_handshake` are
the only three rule types it knows about (a closed set -- adding a fourth type
is a code change). Adding or removing an entry from `design_cases.cases[]`, or
from any kind's `rules[]`, is a pure config edit.

The approval-handshake rule is logic, not data: extract the referenced file
name, resolve it under `dir`, block if absent; else block if the file's age is
under `min_age_sec` and it does not contain `ok_marker` as a raw, unanchored
substring (so e.g. 'NOT-OWNER-OK' satisfies it -- deliberate, do not tighten).
Placeholder substitution is plain `str.replace`, never `str.format` (a message
containing a literal `{` must never raise). The three placeholders are not
uniform: `{approval_dir}` is the raw config value, `{approval_path}` is the
expanded absolute path with native separators, `{approval_name}` is the
basename only.

Compact-header support (2026-09-05): a `require_pattern` rule's `requires`
pattern may be satisfied either by the brief text itself, or -- generically,
with no case names or gate numbers involved -- by the content of the SAME
approval file gate 6 resolves for this kind, if that kind carries an
approval_handshake rule and the file exists. This lets one compact reference
(`BRIEF:<file>.md | ...` or the long-form `APPROVAL:<file>.md`) stand in for
several restated markers: the reference is resolved once, generically, by
JSON path only (`reference_pattern`, `dir`), never by rule id. Resolution
failure of any kind (no approval rule on this kind, no reference in the text,
file missing, file unreadable) yields no fallback text, never an exception --
a fallback that could raise would let the dispatcher's per-guard fail-open
turn a bad reference into a silent ALLOW.
"""
from __future__ import annotations

from lib import ctx as ctx_mod
from lib import io as io_mod

ALLOW = io_mod.ALLOW

_RULE_TYPE_REQUIRE_PATTERN = "require_pattern"
_RULE_TYPE_DESIGN_CASES = "design_cases"
_RULE_TYPE_APPROVAL_HANDSHAKE = "approval_handshake"


def run(payload: dict, cfg: dict, ctx) -> io_mod.Result:
    brief_rules = cfg["brief_rules"]
    detect_cfg = brief_rules["detect"]

    text = _extract_text(payload, detect_cfg.get("text_fields", []))
    if not _is_brief(text, detect_cfg, ctx):
        return ALLOW

    kind = _classify_kind(text, brief_rules.get("kinds", []), ctx)
    if kind is None:
        return ALLOW

    for rule in kind.get("rules", []):
        if not rule.get("enabled", True):
            continue
        result = _eval_rule(rule, text, kind, cfg, ctx)
        if result is not None:
            return result
    return ALLOW


# ------------------------------------------------------------------ detect + classify


def _extract_text(payload: dict, text_fields: list[str]) -> str:
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    parts = []
    for field in text_fields:
        value = tool_input.get(field)
        if isinstance(value, str) and value:
            parts.append(value)
    return "\n".join(parts)


def _is_brief(text: str, detect_cfg: dict, ctx) -> bool:
    if len(text) < detect_cfg.get("min_length", 0):
        return False
    pattern = ctx_mod.compile_pattern(
        detect_cfg["brief_pattern"], "text", ctx, "brief_rules.detect.brief_pattern"
    )
    return bool(pattern and pattern.search(text))


def _classify_kind(text: str, kinds: list[dict], ctx) -> dict | None:
    """First kind (in array order) whose `match` matches classifies the brief.
    The sentinel '*' (checked by identity, never compiled) always matches, so a
    kind carrying it acts as the fallback purely by array position."""
    for kind in kinds:
        if not kind.get("enabled", True):
            continue
        match = kind["match"]
        if match == "*":
            return kind
        pattern = ctx_mod.compile_pattern(match, "text", ctx, "brief_rules.kinds[].match")
        if pattern and pattern.search(text):
            return kind
    return None


# ------------------------------------------------------------------ rule dispatch


def _eval_rule(rule: dict, text: str, kind: dict, cfg: dict, ctx) -> io_mod.Result | None:
    rtype = rule["type"]
    if rtype == _RULE_TYPE_REQUIRE_PATTERN:
        return _eval_require_pattern(rule, text, kind, ctx)
    if rtype == _RULE_TYPE_DESIGN_CASES:
        return _eval_design_cases(text, kind.get("id"), cfg, ctx)
    if rtype == _RULE_TYPE_APPROVAL_HANDSHAKE:
        return _eval_approval_handshake(rule, text, ctx)
    # Closed set: an unrecognised type is a config defect, not a reason to
    # crash the guard -- skip the rule rather than raise.
    return None


def _eval_require_pattern(rule: dict, text: str, kind: dict, ctx) -> io_mod.Result | None:
    pattern = ctx_mod.compile_pattern(
        rule["requires"], "text", ctx, "brief_rules.kinds[].rules[].requires"
    )
    if pattern and pattern.search(text):
        return None
    reference_text = _resolve_reference_text(kind, text, ctx)
    if pattern and reference_text and pattern.search(reference_text):
        return None
    return io_mod.block(rule["message"])


def _resolve_reference_text(kind: dict, text: str, ctx) -> str | None:
    """Generic fallback source for `require_pattern` rules (compact-header
    support, see module docstring): if this KIND carries an approval_handshake
    rule and the brief text resolves, through THAT rule's own
    `reference_pattern`/`dir`, to an existing file, that file's content is a
    second place a require_pattern may be satisfied from. Found by rule TYPE
    only -- never by rule id or kind id -- so this stays a structural fallback,
    not a special case for any one gate. Any resolution failure (no such rule
    on this kind, no reference match, missing file, unreadable file) returns
    None rather than raising: a raised exception here would reach the
    dispatcher's per-guard fail-open and turn a bad reference into ALLOW."""
    approval_rule = next(
        (r for r in kind.get("rules", []) if r.get("type") == _RULE_TYPE_APPROVAL_HANDSHAKE),
        None,
    )
    if approval_rule is None:
        return None
    try:
        reference_pattern = ctx_mod.compile_pattern(
            approval_rule["reference_pattern"], "text", ctx,
            "brief_rules.kinds[].rules[].reference_pattern",
        )
        match = reference_pattern.search(text) if reference_pattern else None
        if not match:
            return None
        approval_path = ctx.expand(approval_rule["dir"]) / match.group(1)
        if not approval_path.exists():
            return None
        return io_mod.read_text(approval_path)
    except Exception:
        return None


def _eval_design_cases(text: str, kind_id: str | None, cfg: dict, ctx) -> io_mod.Result | None:
    """Delegates to the top-level `design_cases.cases[]` list. Walks it in array
    order; a case only evaluates when enabled and (no `applies_to`, or `kind_id`
    is in it) -- `applies_to` narrows, it never extends. Blocks on the first case
    whose triggers fire without satisfaction."""
    cases = cfg.get("design_cases", {}).get("cases", [])
    for case in cases:
        if not case.get("enabled", True):
            continue
        applies_to = case.get("applies_to")
        if applies_to is not None and kind_id not in applies_to:
            continue
        triggers = ctx_mod.compile_patterns(
            case.get("triggers", []), "text", ctx, "design_cases.cases[].triggers[]"
        )
        if not any(p.search(text) for p in triggers):
            continue
        satisfied_by = ctx_mod.compile_patterns(
            case.get("satisfied_by", []), "text", ctx, "design_cases.cases[].satisfied_by[]"
        )
        if any(p.search(text) for p in satisfied_by):
            continue
        return io_mod.block(case["message"])
    return None


def _eval_approval_handshake(rule: dict, text: str, ctx) -> io_mod.Result | None:
    approval_dir_raw = rule["dir"]

    reference_pattern = ctx_mod.compile_pattern(
        rule["reference_pattern"], "text", ctx, "brief_rules.kinds[].rules[].reference_pattern"
    )
    match = reference_pattern.search(text) if reference_pattern else None
    if not match:
        message = rule["message_missing_reference"].replace("{approval_dir}", approval_dir_raw)
        return io_mod.block(message)

    approval_name = match.group(1)
    approval_dir_path = ctx.expand(approval_dir_raw)
    approval_path = approval_dir_path / approval_name

    if not approval_path.exists():
        message = (
            rule["message_file_not_found"]
            .replace("{approval_dir}", approval_dir_raw)
            .replace("{approval_path}", str(approval_path))
            .replace("{approval_name}", approval_name)
        )
        return io_mod.block(message)

    min_age_sec = rule["min_age_sec"]
    age_sec = ctx.now().timestamp() - approval_path.stat().st_mtime
    ok_marker = rule["ok_marker"]

    # Raw, case-insensitive substring over the whole file, not a line match,
    # not anchored -- deliberate, do not tighten.
    content = io_mod.read_text(approval_path)
    has_marker = ok_marker.casefold() in content.casefold()

    if age_sec < min_age_sec and not has_marker:
        message = (
            rule["message_window_not_elapsed"]
            .replace("{approval_dir}", approval_dir_raw)
            .replace("{approval_path}", str(approval_path))
            .replace("{approval_name}", approval_name)
            .replace("{age_sec}", str(int(age_sec)))
            .replace("{min_age_sec}", str(min_age_sec))
            .replace("{ok_marker}", ok_marker)
        )
        return io_mod.block(message)

    return None
