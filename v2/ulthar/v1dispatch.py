"""The v1 guard pipeline (hooks/dispatch.py at 971632f), ported for the v2 dispatcher.

Behaviour is v1's, function for function: config load -> Ctx -> overlay merge ->
grant validation (PreToolUse only) -> routed guards in order, first exit 2 wins,
infrastructure failures BLOCK, a guard's own runtime exception fails open unless an
overlay is merged (see hooks/dispatch.py for the full rationale of each rule).

Two deliberate differences, both structural:
  * nothing is written to stdout/stderr here -- `run()` returns an Outcome and
    ulthar.hook writes once, after the event row is stored (CORE-02 single writer);
  * the infra-block message names v2 recovery steps instead of v1's install.py.
Not ported: v1's dev-process ledger update (dev_process_hygiene is not one of the
four CORE-06 guards).
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .lib import ctx as ctx_mod
from .lib import grant as grant_mod
from .lib import io as io_mod

ALLOW = io_mod.ALLOW
PACKAGE_DIR = Path(__file__).resolve().parent
GUARDS_DIR_ENV = "ULTHAR_GUARDS_DIR"  # test seam (v1's), used by the AC-4 fault injection


@dataclass
class Outcome:
    result: io_mod.Result
    guard_id: str = "dispatcher"
    guards_run: list[str] = field(default_factory=list)


class InfraBlock(Exception):
    def __init__(self, result: io_mod.Result):
        super().__init__(result.stderr)
        self.result = result


def generic_recovery(event: str) -> str:
    return (
        "To recover:\n"
        f"  1. Fix the file or module named above (package: {PACKAGE_DIR}); "
        "`git -C <your ULTHAR clone> status` shows local edits, and "
        "`git -C <clone> checkout <good-commit> -- v2/` restores a known-good tree.\n"
        "  2. If the runtime database is the cause, check its path ($ULTHAR_RUNTIME_DB or "
        "~/.claude/ulthar/runtime.db) is writable, then run `python -m ulthar db init`.\n"
        f"  3. Or, until fixed, disable this event's (\"{event}\") ulthar hook entry in your "
        "settings.json -- enforcement is off for that event only, until you restore it."
    )


def infra_block_message(reason: str, detail: str, event: str, recovery: str | None = None,
                        header: str | None = None) -> str:
    """Infrastructure failures BLOCK rather than fail open (v1 _infra_block_message):
    name the failure, the detail and the recovery for THIS event."""
    if header is None:
        header = ("This is an ULTHAR infrastructure failure, not a rule violation -- "
                  "enforcement could not run, so this tool call is blocked rather than "
                  "silently unguarded.\n")
    return (f"BLOCKED (ulthar dispatcher infrastructure): {reason}\n"
            f"  detail: {detail}\n{header}\n"
            f"{recovery if recovery is not None else generic_recovery(event)}")


def config_path() -> Path:
    """ULTHAR_CONFIG (tests only) or the config.json shipped beside this module."""
    override = os.environ.get("ULTHAR_CONFIG")
    return Path(override) if override else PACKAGE_DIR / "config.json"


def _resolve_tools_pattern(rule: dict, cfg: dict) -> str:
    if "tools_named" in rule:
        name = rule["tools_named"]
        named = cfg["regex"]["named_alternations"]
        if name not in named:
            raise ctx_mod.ConfigError(f"dangling tools_named reference: {name!r}")
        return named[name]
    return rule["tools"]


def _tool_matches(tool_name: str, tools_spec: str, ctx) -> bool:
    if tools_spec == "*":
        return True
    pat = ctx_mod.compile_pattern(f"^({tools_spec})$", "text", ctx, "dispatch.events[].tools")
    return bool(pat and pat.search(tool_name or ""))


def _is_stdout_emitter(guard_id: str, cfg: dict) -> bool:
    return guard_id in cfg.get("dispatch", {}).get("stdout_emitters", [])


class GuardLoadError(Exception):
    """A guard module cannot be imported or lacks a callable `run`: BLOCK."""


def import_guard(guard_id: str):
    override_dir = os.environ.get(GUARDS_DIR_ENV)
    try:
        if override_dir:
            mod_path = Path(override_dir) / f"{guard_id}.py"
            spec = importlib.util.spec_from_file_location(f"_ulthar_guard_{guard_id}", mod_path)
            if spec is None or spec.loader is None:
                raise ImportError(f"cannot load spec for {mod_path}")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        else:
            mod = importlib.import_module(f"{__package__}.guards.{guard_id}")
    except Exception as exc:
        raise GuardLoadError(f"{guard_id}: {type(exc).__name__}: {exc}") from exc
    if not callable(getattr(mod, "run", None)):
        raise GuardLoadError(f"{guard_id}: module has no callable run(payload, cfg, ctx)")
    return mod


def _validate_result(result, guard_id: str, ctx) -> io_mod.Result:
    """I-3b: never trust a guard's return value (v1 rules, unchanged)."""
    if not isinstance(result, io_mod.Result):
        ctx.log_error(guard_id, RuntimeError(f"bad_result_type: {type(result)!r}"), [])
        return ALLOW
    if result.exit_code not in (0, 2):
        ctx.log_error(guard_id, RuntimeError(f"bad_exit_code: {result.exit_code!r}"), [])
        return ALLOW
    if result.exit_code == 2 and not result.stderr:
        ctx.log_config_error("block_without_reason", f"guards.{guard_id}",
                             "guard returned exit_code=2 with empty stderr", guard_scope=guard_id)
        return io_mod.Result(
            2, "", f"BLOCKED by {guard_id} (no reason supplied -- see config-errors.jsonl)")
    if result.stdout and not _is_stdout_emitter(guard_id, ctx.cfg):
        ctx.log_error(guard_id, RuntimeError("unexpected_stdout"), [])
        return io_mod.Result(result.exit_code, "", result.stderr)
    return result


def _overlay_object_in_frames(exc: BaseException, tainted: frozenset) -> bool:
    tb = exc.__traceback__
    while tb is not None:
        for value in list(tb.tb_frame.f_locals.values()):
            if id(value) in tainted:
                return True
        tb = tb.tb_next
    return False


def _attribute_to_project_overlay(guard_id: str, ctx, exc, event: str):
    """With any overlay merged, a guard crash BLOCKS naming the overlay file(s);
    with none, it fails open (v1 round-4 rule)."""
    sources = tuple(getattr(ctx, "overlay_sources", ()) or ())
    if not sources:
        return None
    named = " / ".join(sources)
    held = _overlay_object_in_frames(exc, ctx.project_overlay_ids)
    detail = (
        f"guard '{guard_id}' raised {type(exc).__name__}: {exc}\n"
        f"  an overlay was merged into this dispatch's configuration ({named}), so this\n"
        f"  crash is NOT treated as 'allow' -- see the recovery note below"
        + ("\n  (the guard died holding content that overlay contributed)" if held else "")
    )
    ctx.log_config_error(
        "overlay_guard_crash", f"guards.{guard_id}",
        f"{type(exc).__name__}: {exc} (overlay: {named}; held_overlay_object={held})",
        guard_scope=guard_id)
    recovery = ctx.cfg.get("dispatch", {}).get("infra_recovery", {}).get("overlay")
    return io_mod.Result(2, "", infra_block_message(
        f"guard '{guard_id}' crashed while an overlay was in effect", detail, event,
        recovery=recovery,
        header=("A guard crashed while an overlay was in effect; enforcement started, "
                "the guard did not survive this overlay.\n")))


def invoke_guard(guard_id: str, mod, payload: dict, cfg: dict, ctx, event: str) -> io_mod.Result:
    """Calls one loaded guard with v1's exception and result rules."""
    try:
        result = mod.run(payload, cfg, ctx)
    except ctx_mod.PatternCompileError as exc:
        attributed = _attribute_to_project_overlay(guard_id, ctx, exc, event)
        if attributed is not None:
            return attributed
        ctx.log_config_error("pattern_compile", exc.json_path, str(exc), guard_scope=guard_id)
        return io_mod.Result(2, "", infra_block_message(
            f"guard '{guard_id}' has an invalid pattern in config.json ({exc.json_path})",
            str(exc), event))
    except Exception as exc:  # noqa: BLE001 -- fail-open is the point (I-3)
        attributed = _attribute_to_project_overlay(guard_id, ctx, exc, event)
        if attributed is not None:
            return attributed
        ctx.log_error(guard_id, exc, sorted(payload.keys()))
        return ALLOW
    return _validate_result(result, guard_id, ctx)


def run_guard(guard_id: str, payload: dict, cfg: dict, ctx, event: str) -> io_mod.Result:
    try:
        mod = import_guard(guard_id)
    except GuardLoadError as exc:
        ctx.log_config_error("guard_load", f"guards.{guard_id}", str(exc), guard_scope=guard_id)
        return io_mod.Result(2, "", infra_block_message(
            f"guard '{guard_id}' failed to import", str(exc), event))
    return invoke_guard(guard_id, mod, payload, cfg, ctx, event)


_OVERLAY_NOTICE_DIRNAME = "overlay-notice"
_OVERLAY_NOTICE_MAX_AGE_SEC = 86400
_OVERLAY_NOTICE_NO_SESSION = "nosession"


def _overlay_notice_marker(ctx, payload: dict, kind: str) -> Path:
    session_id = str(payload.get("session_id") or "") or _OVERLAY_NOTICE_NO_SESSION
    key = "\0".join([kind, session_id, str(ctx.project_overlay_path),
                     *getattr(ctx, "project_overlay_disabled", ())])
    import hashlib  # noqa: PLC0415 -- lazy: only an overlay with disables gets here

    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    return ctx.logs_dir / _OVERLAY_NOTICE_DIRNAME / f"{digest}.{kind}"


def _claim_overlay_notice_once(marker: Path) -> bool:
    """First encounter (or first in a day) -> True; an unwritable marker repeats."""
    try:
        if time.time() - marker.stat().st_mtime < _OVERLAY_NOTICE_MAX_AGE_SEC:
            return False
    except OSError:
        pass
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_bytes(b"")
    except OSError:
        pass
    return True


def _log_overlay_disable_once(ctx, payload: dict) -> None:
    disabled = getattr(ctx, "project_overlay_disabled", ())
    if not disabled:
        return
    if not _claim_overlay_notice_once(_overlay_notice_marker(ctx, payload, "logged")):
        return
    ctx.log_config_error(
        "overlay_disable", "overlays.disable",
        f"project overlay {ctx.project_overlay_path} disables: {', '.join(disabled)}")


def _overlay_disable_notice(ctx, payload: dict) -> str:
    disabled = getattr(ctx, "project_overlay_disabled", ())
    if not disabled:
        return ""
    if not _claim_overlay_notice_once(_overlay_notice_marker(ctx, payload, "notified")):
        return ""
    return (
        f"[ULTHAR] The project overlay {ctx.project_overlay_path} switches off "
        f"{len(disabled)} rule(s) for work in this repository: {', '.join(disabled)}. "
        "Project configuration takes precedence over your personal ULTHAR config; "
        "every other rule is still enforced. Delete or edit that file to restore them.\n"
    )


def _notice_stdout(cfg: dict, event: str, notice: str) -> str:
    sa = cfg.get("skill_anchor", {})
    if event in sa.get("plain_stdout_events", []):
        return notice
    envelope = {sa.get("envelope_key", "hookSpecificOutput"): {
        "hookEventName": event, "additionalContext": notice}}
    return json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))


def _context_capable_events(cfg: dict) -> set[str]:
    events = cfg.get("dispatch", {}).get("events", {})
    return {ev for ev, rules in events.items()
            if any("skill_anchor" in (rule.get("guards") or []) for rule in rules)}


def dispatch(event: str, payload: dict, cfg: dict, ctx, single_guard: str | None,
             notice_fn: Callable[[], str] | None = None) -> Outcome:
    """I-9: rules in order, guards in order, first exit 2 wins; stdout accumulated
    and returned (written once by the caller)."""
    tool_name = str(payload.get("tool_name") or "")
    dcfg = cfg.get("dispatch", {})
    rules = dcfg.get("events", {}).get(event, [])
    honour_stop = set(dcfg.get("honour_stop_hook_active_for", []))
    stop_active = bool(payload.get(dcfg.get("stop_hook_active_field", "stop_hook_active")))
    plain = event in cfg.get("skill_anchor", {}).get("plain_stdout_events", [])
    can_speak = plain or event in _context_capable_events(cfg)

    def _emit(acc: str, blocked: bool) -> str:
        if notice_fn is not None and not blocked and can_speak and (not acc or plain):
            notice = notice_fn()
            if notice:
                return notice + acc if plain else _notice_stdout(cfg, event, notice)
        return acc

    stdout_acc = ""
    ran: list[str] = []
    for rule in rules:
        try:
            tools_spec = _resolve_tools_pattern(rule, cfg)
        except ctx_mod.ConfigError as exc:
            ctx.log_config_error("dispatch_route", "dispatch.events", str(exc))
            continue
        if not _tool_matches(tool_name, tools_spec, ctx):
            continue
        for guard_id in rule["guards"]:
            if single_guard is not None and guard_id != single_guard:
                continue
            if event == "Stop" and stop_active and guard_id in honour_stop:
                continue
            ran.append(guard_id)
            result = run_guard(guard_id, payload, cfg, ctx, event)
            stdout_acc += result.stdout or ""
            if result.exit_code == 2:
                return Outcome(io_mod.Result(2, _emit(stdout_acc, True), result.stderr),
                               guard_id, ran)
    return Outcome(io_mod.Result(0, _emit(stdout_acc, False), ""), "dispatcher", ran)


def prepare(event: str, payload: dict) -> tuple[dict, object]:
    """config -> Ctx -> overlays -> grants. Raises InfraBlock (exit 2) on failure."""
    path = config_path()
    deploy_dir = path.parent
    try:
        cfg = ctx_mod.load_config(path)
    except Exception as exc:
        raise InfraBlock(io_mod.Result(2, "", infra_block_message(
            f"config.json could not be loaded ({path})", f"{type(exc).__name__}: {exc}",
            event))) from exc
    try:
        ctx = ctx_mod.build_ctx(payload, cfg, event, deploy_dir)
    except Exception as exc:
        raise InfraBlock(io_mod.Result(2, "", infra_block_message(
            "the dispatcher context could not be constructed",
            f"{type(exc).__name__}: {exc}", event))) from exc
    recoveries = cfg.get("dispatch", {}).get("infra_recovery", {})
    try:
        cfg = ctx_mod.merge_overlays(cfg, ctx)
        ctx.cfg = cfg
    except Exception as exc:
        ctx.log_config_error("overlay_merge", "overlays", f"{type(exc).__name__}: {exc}")
        raise InfraBlock(io_mod.Result(2, "", infra_block_message(
            "a user/project overlay could not be applied", f"{type(exc).__name__}: {exc}",
            event, recovery=recoveries.get("overlay")))) from exc
    try:
        if event == "PreToolUse":
            grant_mod.validate(cfg, ctx)
    except Exception as exc:  # noqa: BLE001 -- any failure here must name the file
        ctx.log_config_error("grant_validate", "grants", f"{type(exc).__name__}: {exc}")
        raise InfraBlock(io_mod.Result(2, "", infra_block_message(
            "an owner grant could not be honoured", f"{type(exc).__name__}: {exc}", event,
            recovery=recoveries.get("grants")))) from exc
    return cfg, ctx


def run(event: str, payload: dict, single_guard: str | None = None) -> Outcome:
    try:
        cfg, ctx = prepare(event, payload)
    except InfraBlock as block:
        return Outcome(block.result, "dispatcher")
    _log_overlay_disable_once(ctx, payload)
    return dispatch(event, payload, cfg, ctx, single_guard,
                    notice_fn=lambda: _overlay_disable_notice(ctx, payload))
