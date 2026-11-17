#!/usr/bin/env python3
"""hooks/tests/test_tool_discipline.py -- unit tests for guards/tool_discipline.py.

CONTRACT.md #6 'Fixture inventory' -- the 19 golden cases migrated from
test-discipline-guard.ps1 (tests/fixtures/tool_discipline/golden.json) plus the
Rule-3 minimum->=10 table (B) cases (tests/fixtures/tool_discipline/rule3.json),
including the PE-1 parity exception. Calls guards.tool_discipline.run() DIRECTLY
(no subprocess -- acceptance row 1); a Ctx comes from conftest.SandboxCtx so every
case runs against a throwaway temp home, never the operator's real
~/.claude/state/pm-delete-allow (CONTRACT.md #6 'Environment policy').

Most cases are pure functions of (payload, cfg, ctx) and are run generically.
Two classes need extra machinery this module owns directly (fixtures cannot
express them statically):

  - `needs_chdir`: M-9b/F-18 -- '..' resolves against the HOOK PROCESS's cwd
    (os.getcwd()), never payload["cwd"]. The test chdir()s into a fresh temp dir
    before calling the guard and restores cwd in `finally`.
  - `setup: {"kind": "pm_delete_allow", "paths": [...]}`: the SAME setup schema
    parity.py understands (tests/parity.py SETUP_BUILDERS) -- seeds
    ~/.claude/state/pm-delete-allow with the given entries before the guard
    runs. Pins an earlier harness defect: a prior `{{BAD_PATH}}` templating
    convention was private to this file and invisible to parity.py, which ran
    the literal '{{BAD_PATH}}' text through both sides unrecognised as a path
    token, so Rule 3 never engaged and the fixture "passed" for proving
    nothing. Fixtures now use a RELATIVE path (e.g. "owner-files/x.db") that
    needs no sandbox-path templating: since neither run_old_side() nor
    run_new_side() pass an explicit subprocess `cwd=`, both sides inherit
    parity.py's own process cwd identically, and the pm-delete-allow comparison
    resolves every entry via the SAME `_full_path()`/`dotdot_base=process_cwd`
    mechanism the guard itself uses. The genuine BEFORE/AFTER consume-on-use
    proof (grant once, blocked on an identical second run against the SAME
    still-mutated state) cannot be expressed as a single parity.py fixture --
    it reseeds fresh per side and per case, never chaining two runs -- so it
    lives here as `check_consume_on_use_before_after`, a pure-Python test
    calling the guard directly, twice, against one sandbox.

No pytest dependency; exposes `main() -> int` per run_all.py's discovery contract.
"""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
HOOKS_DIR = TESTS_DIR.parent
sys.path.insert(0, str(HOOKS_DIR))
sys.path.insert(0, str(TESTS_DIR))
import v1_compat  # noqa: E402,F401 -- v2 port: v1 names resolve to v2 code

from conftest import SandboxCtx, load_test_config  # noqa: E402
from guards import tool_discipline  # noqa: E402
from lib import ctx as ctx_mod  # noqa: E402
from lib import io as io_mod  # noqa: E402

FIXTURES_DIR = TESTS_DIR / "fixtures" / "tool_discipline"


class Probe:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0

    def check(self, name: str, cond: bool, detail: str = "") -> None:
        if cond:
            self.passed += 1
            print(f"OK   {name}")
        else:
            self.failed += 1
            print(f"FAIL {name}" + (f"\n       {detail}" if detail else ""))


def load_all_cases() -> list[dict]:
    cases: list[dict] = []
    for f in sorted(FIXTURES_DIR.glob("*.json")):
        data = json.loads(f.read_text(encoding="utf-8"))
        entries = data if isinstance(data, list) else [data]
        cases += [e for e in entries if isinstance(e, dict) and "id" in e]
    return cases


def _assert_common(p: Probe, case: dict, result: io_mod.Result) -> None:
    ok = result.exit_code == case["expect_exit"]
    detail = f"exit={result.exit_code} want={case['expect_exit']} stderr={result.stderr!r}"
    p.check(f"{case['id']}: exit_code", ok, detail)
    contains = case.get("expect_stderr_contains")
    if contains:
        p.check(
            f"{case['id']}: stderr contains {contains!r}", contains in result.stderr, repr(result.stderr)
        )


# ------------------------------------------------------------------ generic (static) cases


def run_generic_case(p: Probe, case: dict) -> None:
    sandbox = SandboxCtx()
    try:
        ctx = sandbox.build(payload=case["payload"])
        result = tool_discipline.run(case["payload"], sandbox.cfg, ctx)
        _assert_common(p, case, result)
    finally:
        sandbox.cleanup()


# ------------------------------------------------------------------ needs_chdir


def run_chdir_case(p: Probe, case: dict) -> None:
    sandbox = SandboxCtx()
    base = Path(sandbox.root) / "chdir-base"
    base.mkdir(parents=True, exist_ok=True)
    original_cwd = os.getcwd()
    try:
        os.chdir(base)
        ctx = sandbox.build(payload=case["payload"])
        result = tool_discipline.run(case["payload"], sandbox.cfg, ctx)
        _assert_common(p, case, result)
    finally:
        os.chdir(original_cwd)
        sandbox.cleanup()


# ------------------------------------------------------------------ setup: pm_delete_allow


def _seed_allow_file(ctx, entries: list[str]) -> Path:
    rule = _delete_rule(ctx.cfg)
    allow_path = ctx.expand(rule["pm_allowlist"]["file"])
    allow_path.parent.mkdir(parents=True, exist_ok=True)
    io_mod.write_text(allow_path, "".join(f"{e}\n" for e in entries))
    return allow_path


def _delete_rule(cfg: dict) -> dict:
    for rule in cfg["command_rules"]["tool_discipline"]:
        if rule.get("id") == "delete-outside-disposable":
            return rule
    raise AssertionError("delete-outside-disposable rule missing from config")


# The only two placeholders a tool_discipline fixture actually uses --
# ${SUB_TRANSCRIPT} (PE-1). Mirrors parity.py's _placeholder_map naming so a
# fixture reads identically whether it runs through parity.py or here; the
# concrete VALUE differs (no real sandbox root exists in this file's runs) but
# is_main_chain only regex-matches the string, it never touches the
# filesystem, so any value containing/not-containing '/subagents/' is
# equivalent for this test's purposes.
_TEST_PLACEHOLDERS = {
    "MAIN_TRANSCRIPT": "C:/fake/session/main.jsonl",
    "SUB_TRANSCRIPT": "C:/fake/session/subagents/sub1.jsonl",
}


def _resolve_test_placeholders(obj):
    if isinstance(obj, str):
        out = obj
        for name, value in _TEST_PLACEHOLDERS.items():
            out = out.replace("${%s}" % name, value)
        return out
    if isinstance(obj, dict):
        return {k: _resolve_test_placeholders(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_test_placeholders(v) for v in obj]
    return obj


def run_setup_case(p: Probe, case: dict) -> None:
    """Generic handler for any fixture carrying a `setup` block -- today only
    `{"kind": "pm_delete_allow", "paths": [...]}` (U-1's own guard needs
    nothing else). Seeds BEFORE building the Ctx the guard runs against, same
    ordering parity.py's `_seed()` uses."""
    sandbox = SandboxCtx()
    try:
        payload = _resolve_test_placeholders(case["payload"])
        setup = case.get("setup") or {}
        if setup.get("kind") == "pm_delete_allow":
            _seed_allow_file(sandbox.build(payload=payload), setup["paths"])
        elif setup:
            raise AssertionError(f"{case['id']}: unhandled setup kind {setup.get('kind')!r}")
        ctx = sandbox.build(payload=payload)
        result = tool_discipline.run(payload, sandbox.cfg, ctx)
        _assert_common(p, case, result)
    finally:
        sandbox.cleanup()


def check_consume_on_use_before_after(p: Probe) -> None:
    """The genuine BEFORE/AFTER consume-on-use proof that no single parity.py
    fixture can express (see module docstring): seed pm-delete-allow with one
    absolute path, delete it (main chain) -> GRANT, assert the allow file no
    longer contains the entry, then run the IDENTICAL command again against
    the SAME sandbox -> now BLOCKED. A broken consume would silently convert a
    one-shot authorisation into a standing one."""
    sandbox = SandboxCtx()
    try:
        bad_path = str(Path(sandbox.root) / "owner-working-files" / "important-data.db")
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": f"rm -rf {bad_path}"},
        }
        allow_path = _seed_allow_file(sandbox.build(payload=payload), [bad_path])
        before_text = io_mod.read_text(allow_path)

        result1 = tool_discipline.run(payload, sandbox.cfg, sandbox.build(payload=payload))
        p.check(
            "consume-on-use: first (authorised) run GRANTS -> exit 0",
            result1.exit_code == 0,
            str(result1),
        )
        after_text = io_mod.read_text(allow_path)
        p.check(
            "consume-on-use: pm-delete-allow BEFORE state contains the granted path",
            bad_path in before_text,
        )
        p.check(
            "consume-on-use: pm-delete-allow AFTER state no longer contains the consumed path",
            bad_path not in after_text,
            repr(after_text),
        )

        result2 = tool_discipline.run(payload, sandbox.cfg, sandbox.build(payload=payload))
        p.check(
            "consume-on-use: IDENTICAL second run is now BLOCKED (grant was one-shot)",
            result2.exit_code == 2,
            str(result2),
        )
    finally:
        sandbox.cleanup()


# ------------------------------------------------------------------ platform case-sensitivity (done-report item 3)


def check_windows_posix_case_sensitivity(p: Probe) -> None:
    """N-1 semantic regression, demonstrated end-to-end through the guard (not
    just the raw pattern): `/home/x/Dist` must be disposable on Windows and NOT
    on POSIX; `/home/x/dist` disposable on both. Constructs two real lib.ctx.Ctx
    instances differing ONLY in `is_windows` -- this is the exact defect class
    N-1 exists to prevent (an inline `(?i)` on the path pattern would make both
    platforms agree, silently reopening the hole)."""
    from conftest import load_test_config

    cfg = load_test_config()

    def make_ctx(is_windows: bool):
        return ctx_mod.Ctx(
            home=Path("C:/fake-home") if is_windows else Path("/fake-home"),
            state_dir=Path("C:/fake-home/.claude/state") if is_windows else Path("/fake-home/.claude/state"),
            deploy_dir=Path("C:/fake-deploy") if is_windows else Path("/fake-deploy"),
            skills_root=Path("C:/fake-home/.claude/skills") if is_windows else Path("/fake-home/.claude/skills"),
            logs_dir=Path("C:/fake-deploy/logs") if is_windows else Path("/fake-deploy/logs"),
            temp_dir=Path("C:/fake-temp") if is_windows else Path("/fake-temp"),
            cwd=Path("C:/fake-cwd") if is_windows else Path("/fake-cwd"),
            is_windows=is_windows,
            event="PreToolUse",
            cfg=cfg,
        )

    payload_upper = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "rm -rf /home/x/Dist/data.db"},
    }
    payload_lower = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "rm -rf /home/x/dist/data.db"},
    }

    win_upper = tool_discipline.run(payload_upper, cfg, make_ctx(True))
    posix_upper = tool_discipline.run(payload_upper, cfg, make_ctx(False))
    win_lower = tool_discipline.run(payload_lower, cfg, make_ctx(True))
    posix_lower = tool_discipline.run(payload_lower, cfg, make_ctx(False))

    p.check(
        "N-1 Windows: /home/x/Dist IS disposable (case-insensitive) -> ALLOW",
        win_upper.exit_code == 0,
        f"exit={win_upper.exit_code} stderr={win_upper.stderr!r}",
    )
    p.check(
        "N-1 POSIX: /home/x/Dist is NOT disposable (case-sensitive) -> BLOCK",
        posix_upper.exit_code == 2,
        f"exit={posix_upper.exit_code} stderr={posix_upper.stderr!r}",
    )
    p.check(
        "N-1 Windows: /home/x/dist IS disposable -> ALLOW",
        win_lower.exit_code == 0,
        f"exit={win_lower.exit_code} stderr={win_lower.stderr!r}",
    )
    p.check(
        "N-1 POSIX: /home/x/dist IS still disposable (lower-case, both platforms) -> ALLOW",
        posix_lower.exit_code == 0,
        f"exit={posix_lower.exit_code} stderr={posix_lower.stderr!r}",
    )


# ------------------------------------------------------------------ regression checks


def check_dotdot_base_is_consumed(p: Probe) -> None:
    """Pins `dotdot_base` against being hardcoded: it must be READ from config. Proof:
    mutate it to an unrecognised value and assert `ctx.log_config_error` fires
    (spied, not written to disk -- see module docstring's note on the unrelated
    lib/ctx.py tilde-expansion bug this would otherwise trip). Before the fix the
    field was never consulted at all, so this assertion is RED against the old
    code (no call happens) and GREEN against the fix. The command still escapes
    the allowed set either way, so the second assertion (still blocks) holds
    under both -- it is the FIRST assertion that discriminates old vs new."""
    cfg = copy.deepcopy(load_test_config())
    rule = _delete_rule(cfg)
    rule["dotdot_base"] = "bogus_base"  # not "process_cwd" -- an operator typo

    sandbox = SandboxCtx(cfg)
    base = Path(sandbox.root) / "chdir-base"
    base.mkdir(parents=True, exist_ok=True)
    original_cwd = os.getcwd()
    try:
        os.chdir(base)
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "rm -rf dist/../../outside-secret.txt"},
        }
        ctx = sandbox.build(payload=payload)
        calls: list[tuple] = []
        ctx.log_config_error = lambda *a, **k: calls.append((a, k))
        result = tool_discipline.run(payload, cfg, ctx)
    finally:
        os.chdir(original_cwd)
        sandbox.cleanup()

    p.check(
        "dotdot_base: an unrecognised value is REPORTED via log_config_error "
        "(proves the config key is consumed, not decorative)",
        any("dotdot_base" in str(c) for c in calls),
        str(calls),
    )
    p.check(
        "dotdot_base: still falls back to process_cwd and blocks the escape",
        result.exit_code == 2,
        str(result),
    )


def check_pm_allowlist_lock_prevents_early_or_lost_consume(p: Probe) -> None:
    """Pins pm-delete-allow atomicity: pre-create the `.lock` file externally,
    simulating another process mid-consumption, and never release it during the
    call. Before the fix there is no lock concept at all, so the old code
    consumes regardless (RED: exit 0, entry silently burned even though
    'another process' was supposedly holding it). After the fix the guard
    exhausts its bounded retry, fails closed (BLOCK), and leaves the ledger
    untouched (GREEN) -- then a normal call succeeds once the lock is released,
    proving this is contention-handling, not a deadlock."""
    sandbox = SandboxCtx()
    try:
        bad_path = str(Path(sandbox.root) / "owner-files" / "important.db")
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": f"rm -rf {bad_path}"},
        }
        ctx = sandbox.build(payload=payload)
        allow_path = _seed_allow_file(ctx, [bad_path])

        lock_path = allow_path.with_name(allow_path.name + ".lock")
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            error_calls: list[tuple] = []
            ctx.log_error = lambda *a, **k: error_calls.append((a, k))
            result = tool_discipline.run(payload, sandbox.cfg, ctx)
            p.check(
                "pm-allowlist: does NOT consume while an external lock is held (fails closed)",
                result.exit_code == 2,
                str(result),
            )
            after = io_mod.read_lines(allow_path)
            p.check(
                "pm-allowlist: entry is still present after lock contention (grant not burned)",
                bad_path in after,
                str(after),
            )
            p.check(
                "pm-allowlist: contention is reported (not a silent failure)",
                len(error_calls) >= 1,
                str(error_calls),
            )
        finally:
            os.close(fd)
            os.remove(str(lock_path))

        result2 = tool_discipline.run(payload, sandbox.cfg, sandbox.build(payload=payload))
        p.check(
            "pm-allowlist: grant succeeds normally once the lock is released (no deadlock)",
            result2.exit_code == 0,
            str(result2),
        )
    finally:
        sandbox.cleanup()


def check_unknown_rule_type_is_logged(p: Probe) -> None:
    """Pins an unrecognised `type` against being silently dropped. Before the fix
    the loop's `else: result = None` branch had no logging at all (RED: no call).
    After the fix `ctx.log_config_error` fires naming the bad json_path (GREEN)."""
    cfg = copy.deepcopy(load_test_config())
    cfg["command_rules"]["tool_discipline"] = [
        {
            "id": "typo-rule",
            "enabled": True,
            "type": "regex_blocc",  # typo: should be regex_block
            "target": "raw",
            "pattern": "(?i)should-never-fire",
            "message": "should never fire",
        }
    ]
    sandbox = SandboxCtx(cfg)
    try:
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "echo hello"},
        }
        ctx = sandbox.build(payload=payload)
        calls: list[tuple] = []
        ctx.log_config_error = lambda *a, **k: calls.append((a, k))
        tool_discipline.run(payload, cfg, ctx)
    finally:
        sandbox.cleanup()

    p.check(
        "tool_discipline: unrecognised rule type is logged via log_config_error",
        any("unrecognised rule type" in str(c) for c in calls),
        str(calls),
    )


# ------------------------------------------------------------------ stale-lock recovery


def _spawn_and_wait_dead_pid() -> int:
    """Returns a PID that is GUARANTEED to have exited (spawn a trivial child,
    wait for it) -- far more reliable than guessing an improbable integer,
    which could coincide with a real running process on the test machine."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    proc.wait(timeout=10)
    return proc.pid


def _write_raw_lock(lock_path: Path, pid: int, age_sec: float, now_ts: float) -> None:
    """Bypasses the guard entirely to plant a lock file as if left behind by
    SOME OTHER process at `now_ts - age_sec` -- exactly what a crashed holder
    would leave on disk."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(f"{pid}\n{now_ts - age_sec}\n", encoding="utf-8")


def check_stale_lock_reclaimed_when_owner_dead(p: Probe) -> None:
    """Pins stale-lock reclaim: a lock recorded by a CONFIRMED-DEAD pid, well past
    the staleness age, must be reclaimed -- the grant proceeds and
    the reclaim is logged. Before the fix there is no owner/age concept at
    all: ANY pre-existing lock file (regardless of content or age) exhausts
    the bounded retry and fails closed (RED: exit 2, no reclaim log). After
    the fix the dead owner is detected, the lock is reclaimed, and the
    otherwise-valid grant succeeds (GREEN: exit 0, reclaim logged)."""
    sandbox = SandboxCtx()
    try:
        bad_path = str(Path(sandbox.root) / "owner-files" / "dead-owner.db")
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": f"rm -rf {bad_path}"},
        }
        ctx = sandbox.build(payload=payload)
        allow_path = _seed_allow_file(ctx, [bad_path])
        lock_path = allow_path.with_name(allow_path.name + ".lock")

        dead_pid = _spawn_and_wait_dead_pid()
        now_ts = ctx.now().timestamp()
        _write_raw_lock(lock_path, dead_pid, age_sec=3600, now_ts=now_ts)  # 1h old, way past the floor

        log_calls: list[tuple] = []
        ctx.log_error = lambda *a, **k: log_calls.append((a, k))

        result = tool_discipline.run(payload, sandbox.cfg, ctx)

        p.check(
            "stale lock (dead owner): reclaimed, grant proceeds -> exit 0",
            result.exit_code == 0,
            str(result),
        )
        p.check(
            "stale lock (dead owner): the reclaim is logged, not silent",
            any("pm_allowlist_lock_reclaimed" in str(c) for c in log_calls),
            str(log_calls),
        )
        p.check(
            "stale lock (dead owner): the lock file itself is gone afterwards",
            not lock_path.exists(),
        )
    finally:
        sandbox.cleanup()


def check_stale_but_owner_alive_not_reclaimed(p: Probe) -> None:
    """Pins the MUST-NOT counterpart: a lock old enough to look abandoned but
    whose recorded pid is this test process itself (definitely alive) must
    NEVER be reclaimed, no matter how
    old. Both before and after the fix the call is blocked (the lock is never
    released during this test), so exit code alone does not discriminate --
    the discriminator is the MESSAGE: before the fix there is no
    contention-specific text at all (generic 'not allowed' only); after the
    fix the contention suffix explains what happened and what to do."""
    sandbox = SandboxCtx()
    try:
        bad_path = str(Path(sandbox.root) / "owner-files" / "alive-owner.db")
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": f"rm -rf {bad_path}"},
        }
        ctx = sandbox.build(payload=payload)
        allow_path = _seed_allow_file(ctx, [bad_path])
        lock_path = allow_path.with_name(allow_path.name + ".lock")

        alive_pid = os.getpid()  # this very test process -- unambiguously alive
        now_ts = ctx.now().timestamp()
        _write_raw_lock(lock_path, alive_pid, age_sec=3600, now_ts=now_ts)  # old, but NOT dead

        try:
            result = tool_discipline.run(payload, sandbox.cfg, ctx)
            p.check(
                "old lock, LIVE owner: still refused (never reclaim a live holder)",
                result.exit_code == 2,
                str(result),
            )
            p.check(
                "old lock, LIVE owner: refusal message explains contention and says retry",
                "retry the same command" in result.stderr,
                repr(result.stderr),
            )
            p.check(
                "old lock, LIVE owner: the lock file is left untouched (not reclaimed)",
                lock_path.exists(),
            )
        finally:
            lock_path.unlink(missing_ok=True)  # ours to clean up -- the guard correctly left it alone
    finally:
        sandbox.cleanup()


# ------------------------------------------------------------------ config thresholds


def check_stale_age_zero_still_never_reclaims_live_owner(p: Probe) -> None:
    """Pins the ordering: the liveness check must remain mandatory regardless of
    the configured age, so lowering the value can never let a live holder be
    robbed. `thresholds.allowlist_lock_stale_age_sec` is mutated to 0, the MOST
    permissive value possible (every lock is instantly 'old enough'), and the
    lock's recorded owner is this test process itself (definitely alive). If
    the age gate alone controlled reclaim, age=0 would reclaim on first
    contention; the liveness check must still refuse. A brand-new (age~0)
    lock is used deliberately -- proving the mandatory gate holds even in the
    single case an age-only implementation would be MOST tempted to reclaim."""
    cfg = copy.deepcopy(load_test_config())
    cfg["thresholds"]["allowlist_lock_stale_age_sec"] = 0
    sandbox = SandboxCtx(cfg)
    try:
        bad_path = str(Path(sandbox.root) / "owner-files" / "zero-age-alive.db")
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": f"rm -rf {bad_path}"},
        }
        ctx = sandbox.build(payload=payload)
        allow_path = _seed_allow_file(ctx, [bad_path])
        lock_path = allow_path.with_name(allow_path.name + ".lock")

        alive_pid = os.getpid()
        now_ts = ctx.now().timestamp()
        _write_raw_lock(lock_path, alive_pid, age_sec=0, now_ts=now_ts)  # freshly "aged" 0

        try:
            result = tool_discipline.run(payload, cfg, ctx)
            p.check(
                "stale_age_sec=0 + LIVE owner: still refused -- age alone never reclaims",
                result.exit_code == 2,
                str(result),
            )
            p.check(
                "stale_age_sec=0 + LIVE owner: lock left untouched",
                lock_path.exists(),
            )
        finally:
            lock_path.unlink(missing_ok=True)
    finally:
        sandbox.cleanup()


def check_missing_or_invalid_thresholds_use_safe_defaults(p: Probe) -> None:
    """Pins the fallback for a missing or non-numeric threshold: it must never
    silently become zero retries or a zero stale age. Decision: fall back to the
    historical default (20 / 0.01 / 30) and report via log_config_error -- never
    to 0/None. Proof, per case: key absent entirely; a string where a number is
    expected; a bool (which Python's isinstance(True, int) would otherwise let
    slip through as 1); a negative number; and NaN, +Infinity and -Infinity,
    which `json.loads` accepts as a stdlib JSON extension and which a naive
    `value < 0` check does not catch (NaN comparisons are always False;
    +Infinity is not negative) -- all cases must behave IDENTICALLY to the real
    default (a legitimately pre-authorised, uncontended grant still succeeds)
    and all must log the rejection."""
    from conftest import load_test_config

    cases = [
        ("missing-key", lambda th: th.pop("allowlist_lock_retry_attempts", None)),
        ("non-numeric-string", lambda th: th.__setitem__("allowlist_lock_retry_attempts", "lots")),
        ("bool-instead-of-int", lambda th: th.__setitem__("allowlist_lock_retry_attempts", True)),
        ("negative", lambda th: th.__setitem__("allowlist_lock_retry_attempts", -1)),
        # json.loads accepts NaN/Infinity/-Infinity as a stdlib extension, and a
        # naive `value < 0` check lets all three slip through (any comparison
        # against NaN is False; +Infinity is not negative) -- see
        # check_nan_retry_attempts_does_not_hang for the concrete infinite-loop
        # consequence under real contention.
        ("nan", lambda th: th.__setitem__("allowlist_lock_retry_attempts", float("nan"))),
        ("positive-infinity", lambda th: th.__setitem__("allowlist_lock_retry_attempts", float("inf"))),
        ("negative-infinity", lambda th: th.__setitem__("allowlist_lock_retry_attempts", float("-inf"))),
    ]
    for name, mutate in cases:
        cfg = copy.deepcopy(load_test_config())
        mutate(cfg["thresholds"])
        sandbox = SandboxCtx(cfg)
        try:
            bad_path = str(Path(sandbox.root) / "owner-files" / f"threshold-{name}.db")
            payload = {
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": f"rm -rf {bad_path}"},
            }
            ctx = sandbox.build(payload=payload)
            _seed_allow_file(ctx, [bad_path])
            calls: list[tuple] = []
            ctx.log_config_error = lambda *a, **k: calls.append((a, k))

            result = tool_discipline.run(payload, cfg, ctx)
            p.check(
                f"threshold {name}: an ordinary uncontended grant still succeeds (safe default used)",
                result.exit_code == 0,
                str(result),
            )
            p.check(
                f"threshold {name}: the bad value is reported, not silently absorbed",
                any("allowlist_lock_retry_attempts" in str(c) for c in calls),
                str(calls),
            )
        finally:
            sandbox.cleanup()


# ------------------------------------------------------------------ hot-spin / TOCTOU


def check_unowned_lock_is_never_stale(p: Probe) -> None:
    """Pins a hot-spin incident (measured live at 25.03s / 21612 log lines when a
    lock was treated as reclaimable on mtime alone): a lock file with NO readable
    owner record -- exactly the window between another process's os.open() and
    its _write_lock_owner() -- must NEVER be reclaimed, regardless of how old its
    mtime is. Plants an EMPTY lock file (created, never written to -- exactly
    what a lock looks like mid-acquisition) with an artificially ancient mtime;
    the call must fail closed (contention), not reclaim it.

    Also closes the gap in check_stale_age_zero_still_never_reclaims_live_owner:
    that test only exercises the case where an owner record HAS been written.
    This is the missing unowned-lock case."""
    sandbox = SandboxCtx()
    try:
        bad_path = str(Path(sandbox.root) / "owner-files" / "unowned-lock.db")
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": f"rm -rf {bad_path}"},
        }
        ctx = sandbox.build(payload=payload)
        allow_path = _seed_allow_file(ctx, [bad_path])
        lock_path = allow_path.with_name(allow_path.name + ".lock")

        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)  # created, but NEVER written to -- no owner record
        ancient = ctx.now().timestamp() - 3600
        os.utime(lock_path, (ancient, ancient))

        log_calls: list[tuple] = []
        ctx.log_error = lambda *a, **k: log_calls.append((a, k))

        start = time.perf_counter()
        result = tool_discipline.run(payload, sandbox.cfg, ctx)
        elapsed = time.perf_counter() - start

        try:
            p.check(
                "unowned lock: never reclaimed -- fails closed (exit 2) regardless of mtime age",
                result.exit_code == 2,
                str(result),
            )
            p.check(
                f"unowned lock: fails closed QUICKLY, bounded by the retry budget (took {elapsed:.3f}s)",
                elapsed < 5.0,
                f"elapsed={elapsed:.3f}s",
            )
            p.check(
                "unowned lock: no 'reclaimed' log line",
                not any("pm_allowlist_lock_reclaimed" in str(c) for c in log_calls),
                str(log_calls),
            )
            p.check(
                "unowned lock: left on disk untouched (never removed)",
                lock_path.exists(),
            )
        finally:
            lock_path.unlink(missing_ok=True)
    finally:
        sandbox.cleanup()


def check_reclaim_bounded_when_removal_always_fails(p: Probe) -> None:
    """Pins bounded termination when reclaim removal keeps failing: even when a
    lock IS correctly judged stale (dead owner, past the age floor) but its removal
    keeps failing every single time (simulating a persistent Windows sharing
    violation from a holder that never actually lets go), the guard must
    still terminate in BOUNDED time with a BOUNDED number of log lines --
    never the unbounded hot-spin measured in the incident (25.03s, 21612
    reclaim log lines, ~865 iterations/second). Both `os.replace` (this
    module's reclaim primitive) and `os.remove` are monkeypatched to always
    raise, forcing every reclaim attempt to fail regardless of which
    primitive is in use."""
    sandbox = SandboxCtx()
    try:
        bad_path = str(Path(sandbox.root) / "owner-files" / "never-removable.db")
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": f"rm -rf {bad_path}"},
        }
        ctx = sandbox.build(payload=payload)
        allow_path = _seed_allow_file(ctx, [bad_path])
        lock_path = allow_path.with_name(allow_path.name + ".lock")

        dead_pid = _spawn_and_wait_dead_pid()
        now_ts = ctx.now().timestamp()
        _write_raw_lock(lock_path, dead_pid, age_sec=3600, now_ts=now_ts)

        log_calls: list[tuple] = []
        ctx.log_error = lambda *a, **k: log_calls.append((a, k))

        original_replace, original_remove = os.replace, os.remove

        def _always_fail(*_a, **_k):
            raise OSError("simulated: removal can never actually succeed")

        os.replace = _always_fail
        os.remove = _always_fail
        try:
            start = time.perf_counter()
            result = tool_discipline.run(payload, sandbox.cfg, ctx)
            elapsed = time.perf_counter() - start
        finally:
            os.replace = original_replace
            os.remove = original_remove
            lock_path.unlink(missing_ok=True)

        p.check(
            "unreclaimable stale lock: fails closed (exit 2), never grants",
            result.exit_code == 2,
            str(result),
        )
        p.check(
            f"unreclaimable stale lock: BOUNDED elapsed time (took {elapsed:.3f}s; incident was 25.03s)",
            elapsed < 5.0,
            f"elapsed={elapsed:.3f}s",
        )
        reclaimed_logs = [c for c in log_calls if "pm_allowlist_lock_reclaimed" in str(c)]
        p.check(
            "unreclaimable stale lock: NEVER logs 'reclaimed' when removal always failed",
            not reclaimed_logs,
            str(reclaimed_logs),
        )
        p.check(
            f"unreclaimable stale lock: BOUNDED log volume ({len(log_calls)} lines; incident was 21612)",
            len(log_calls) <= 25,
            f"{len(log_calls)} log calls",
        )
    finally:
        sandbox.cleanup()


def check_reclaim_is_toctou_safe(p: Probe) -> None:
    """Pins TOCTOU-safety of reclaim: two contenders racing to reclaim the
    SAME stale (dead-owner) lock at the same instant must not both succeed --
    the pre-fix delete-by-path let A delete-and-recreate, then B delete A's
    FRESH lock and create its own, putting both 'inside' the critical section
    on the one mechanism guarding an irreversible ledger. Forces a real race
    with a threading.Barrier (not just sequential calls, which would trivially
    pass without ever exercising the concurrent window) and requires exactly
    ONE winner -- `os.replace`'s atomicity on a shared source path is what
    guarantees this, not any application-level locking (there IS no lock on
    the lock)."""
    sandbox = SandboxCtx()
    try:
        lock_path = Path(sandbox.root) / "toctou-test.lock"
        dead_pid = _spawn_and_wait_dead_pid()
        ctx = sandbox.build()
        now_ts = ctx.now().timestamp()
        _write_raw_lock(lock_path, dead_pid, age_sec=3600, now_ts=now_ts)

        barrier = threading.Barrier(2)
        results: dict[str, bool] = {}

        def worker(name: str) -> None:
            barrier.wait(timeout=5)
            results[name] = tool_discipline._reclaim_stale_lock(lock_path, ctx)

        t1 = threading.Thread(target=worker, args=("A",))
        t2 = threading.Thread(target=worker, args=("B",))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        wins = sum(1 for v in results.values() if v is True)
        p.check(
            "TOCTOU: exactly ONE concurrent reclaimer wins the race (never both, never zero)",
            wins == 1,
            str(results),
        )
    finally:
        sandbox.cleanup()


# ------------------------------------------------------------------ NaN thresholds


def check_nan_retry_attempts_does_not_hang(p: Probe) -> None:
    """Pins the same failure shape as the hot-spin above, triggered via config
    instead of lock ownership: `thresholds.allowlist_lock_retry_attempts: NaN`
    used to slip past
    validation (`value < 0` is False for NaN -- any comparison against NaN is
    False), and `attempts >= nan` is ALSO always False, so the retry loop
    never terminated on its own: an infinite spin whose eventual resolution
    (the dispatcher's own timeout) Claude Code's hook runtime treats as ALLOW
    -- a bounded fail-closed path turning into an unbounded hang that ends in
    approval, the same shape as the hot-spin above. Genuinely contends the lock (this
    test itself holds an open fd on it, never releasing it during the call)
    so the fix is proven under real contention, not just via the validation
    function in isolation, and the test bounds ITSELF with a hard wall-clock
    assertion rather than trusting the guard not to hang forever."""
    cfg = copy.deepcopy(load_test_config())
    cfg["thresholds"]["allowlist_lock_retry_attempts"] = float("nan")
    sandbox = SandboxCtx(cfg)
    try:
        bad_path = str(Path(sandbox.root) / "owner-files" / "nan-retry.db")
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": f"rm -rf {bad_path}"},
        }
        ctx = sandbox.build(payload=payload)
        allow_path = _seed_allow_file(ctx, [bad_path])
        lock_path = allow_path.with_name(allow_path.name + ".lock")
        # Genuine external contention -- held open for the whole call, never
        # released -- exactly the scenario `attempts >= retry_attempts` exists
        # to bound.
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            start = time.perf_counter()
            result = tool_discipline.run(payload, cfg, ctx)
            elapsed = time.perf_counter() - start
            p.check(
                "NaN retry_attempts: still terminates and fails closed (exit 2), not an infinite spin",
                result.exit_code == 2,
                str(result),
            )
            p.check(
                f"NaN retry_attempts: BOUNDED elapsed time (took {elapsed:.3f}s)",
                elapsed < 5.0,
                f"elapsed={elapsed:.3f}s",
            )
        finally:
            os.close(fd)
            lock_path.unlink(missing_ok=True)
    finally:
        sandbox.cleanup()


def main() -> int:
    p = Probe()
    cases = load_all_cases()

    golden = [c for c in cases if c["id"].startswith("golden-")]
    rule3 = [c for c in cases if c["id"].startswith("r3-")]
    p.check(f"fixtures: 19 golden cases loaded ({len(golden)} found)", len(golden) == 19)
    p.check(f"fixtures: Rule-3 minimum >= 10 cases loaded ({len(rule3)} found)", len(rule3) >= 10)

    for case in cases:
        if case.get("needs_chdir"):
            run_chdir_case(p, case)
        elif case.get("setup"):
            run_setup_case(p, case)
        else:
            run_generic_case(p, case)

    print("\n== N-1 Windows/POSIX case-sensitivity (done-report item 3) ==")
    check_windows_posix_case_sensitivity(p)

    print("\n== round-2 send-back regressions ==")
    check_dotdot_base_is_consumed(p)
    check_pm_allowlist_lock_prevents_early_or_lost_consume(p)
    check_unknown_rule_type_is_logged(p)

    print("\n== round-3 send-back: real before/after consume-on-use proof ==")
    check_consume_on_use_before_after(p)

    print("\n== round-4 send-back: stale allowlist-lock recovery ==")
    check_stale_lock_reclaimed_when_owner_dead(p)
    check_stale_but_owner_alive_not_reclaimed(p)

    print("\n== round-6 send-back: config-driven lock thresholds ==")
    check_stale_age_zero_still_never_reclaims_live_owner(p)
    check_missing_or_invalid_thresholds_use_safe_defaults(p)

    print("\n== round-7 send-back: D-1 hot-spin / D-2 TOCTOU ==")
    check_unowned_lock_is_never_stale(p)
    check_reclaim_bounded_when_removal_always_fails(p)
    check_reclaim_is_toctou_safe(p)

    print("\n== round-8 send-back: D-3 NaN/Infinity thresholds ==")
    check_nan_retry_attempts_does_not_hang(p)

    # Defensive boundary: blank command never crashes and always allows.
    sandbox = SandboxCtx()
    try:
        ctx = sandbox.build()
        result = tool_discipline.run({"tool_input": {"command": "   "}}, sandbox.cfg, ctx)
        p.check("blank command -> ALLOW (no crash)", result.exit_code == 0)
    finally:
        sandbox.cleanup()

    print(f"\nRESULT: {p.passed} passed, {p.failed} failed")
    return 1 if p.failed else 0


if __name__ == "__main__":
    sys.exit(main())
