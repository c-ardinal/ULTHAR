"""hooks/guards/tool_discipline.py -- PreToolUse guard for shell anti-patterns.

Four rules, config-driven and in order:
  Rule 1 (cd-compound)               -- command_rules.tool_discipline[0]  regex_block
  Rule 2 (shell-file-creation)        -- command_rules.tool_discipline[1]  regex_block_any
  Rule 3 (delete-outside-disposable)  -- command_rules.tool_discipline[2]  delete_allowlist
  Rule 4 (authority-file-in-shell)    -- command_rules.tool_discipline[3]  regex_block

Rule 4 is the shell half of the self-grant closure (guards/grant_files.py is the
Write/Edit half): it blocks every shell route to the files that carry the
OWNER's permission, which the shell-file-creation rule does not cover (`python
-c "open(...)"`, `sed -i`, `cp`). It is in grants.never_exempt -- a grant that
switched it off would authorise writing grants.

A rule that blocks consults lib/grant.py for an owner grant naming THAT rule
(`command_rules.tool_discipline/<id>`); a grant turns the block into a
`continue`, so every rule after it still runs. See lib/grant.py.

No policy literal lives in this module -- every pattern, message and threshold
is read from `cfg`. Adding/removing a regex_block / regex_block_any rule is a
pure config edit.

Rule 3 is the highest-risk logic in this guard: it gates deletion and protects
the owner's untracked files.
  - Main-chain detection for the PM allowlist uses the stricter two-signal
    `lib.ctx.is_main_chain`, not an agent_id-only check.
  - A `..`-bearing token is resolved against the base directory named by
    `dotdot_base` (today only `"process_cwd"` is defined -> the hook
    process's own cwd via `os.getcwd()`, never the payload's `cwd`). An
    unrecognised value is reported via `ctx.log_config_error` and falls back
    to `process_cwd` rather than silently ignoring the field or crashing.
  - `allowed_path_pattern` is a `kind:"path"` pattern -- IGNORECASE only on
    Windows, never a blanket case-fold.
  - pm-delete-allow entries are consumed (rewritten without the granted ones)
    only when every bad path in the command is listed -- partial grants never
    happen. Consumption is a single locked, atomic critical section: the
    allow file is re-read fresh while holding an exclusive lock, so two
    racing tool calls can never lose one grant to the other's stale write,
    and the rewrite itself is write-temp + os.replace so a crash mid-write
    can never leave a truncated ledger.

DELIBERATE design decision, not an open question: a PM grant is consumed as
soon as this guard decides to allow the deletion, before the dispatcher has
run the later guards in the PreToolUse chain (git_discipline, pr_state,
dev_server) for the SAME tool call. If one of those later blocks the call, the
grant has already been burned on a command that never executed. Left as-is
because it fails safe -- the outcome is always "a grant is lost" (costs a PM
re-authorisation), never "an extra deletion gets authorised" (would cost the
owner a file). A real fix means deferring the commit of consumption until the
whole dispatch chain returns ALLOW, which only the dispatcher can do -- a
single guard has no visibility into its siblings; out of this guard's scope.

The pm-delete-allow lock (`O_CREAT|O_EXCL`) records its owner (pid + timestamp)
so a lock left behind by a process that died mid-critical-section is
reclaimable -- but only after a bounded age AND only once the recorded pid is
confirmed dead (`_pid_is_alive`). A live holder that is merely slow is never
robbed of its lock, no matter how old it gets. Every reclaim is logged
(`ctx.log_error`, `pm_allowlist_lock_reclaimed`) -- a lock that silently
reappears would be exactly as confusing as one that never clears.

An unowned lock (the window between another process's `os.open()` and its
`_write_lock_owner()`) must NEVER be judged stale from file mtime alone -- no
pid means death can never be confirmed. Getting this wrong turns a bounded
fail-closed retry into an unbounded hot-spin that only the dispatcher's own
timeout would end, which Claude Code's hook runtime then resolves to ALLOW --
exactly the infrastructure-failure class this runtime's guard/config-load
failures are designed to block, not allow (see
`check_unowned_lock_is_never_stale` /
`check_stale_age_zero_still_never_reclaims_live_owner`). Reclaim is
TOCTOU-safe: mutual exclusion between racing reclaimers is decided by an
`os.open(claim_path, O_CREAT|O_EXCL)` claim marker (the same primitive that
guards lock acquisition itself), NOT by relying on `os.replace`/rename
atomicity -- measured empirically, Windows does not guarantee that only one
of two threads racing a rename of the same source path succeeds (see
`_reclaim_stale_lock`'s docstring for the measured failure rate of the
rename-race approach this replaced). A "reclaimed" log line is emitted only
when this call actually removed the lock it judged stale.
"""
from __future__ import annotations

import math
import os
import time
from pathlib import Path

from lib import ctx as ctx_mod
from lib import grant as grant_mod
from lib import io as io_mod
from lib import paths as paths_mod

_RULE_KEY = "tool_discipline"

# Closed enum of mechanism identifiers `dotdot_base` may name -- adding a
# second value is a code change (a new resolution strategy to implement),
# never a config edit.
_KNOWN_DOTDOT_BASES = {"process_cwd"}

# ponytail: allowlist lock retry is a fixed bounded spin (20 attempts x 10ms,
# ~200ms worst case), not adaptive backoff -- fine under light contention,
# fails closed under heavy contention; move to a blocking OS-level lock if
# that ever matters. Fallback values used only when config is missing/invalid
# (never silently 0 -- a bad value must not become zero retries or a zero
# stale age).
# ponytail: fixed bounded retry (5 attempts x 5ms, ~20ms worst case) around
# the reclaim-winner's os.remove() of the stale lock -- absorbs the transient
# Windows sharing violation (PermissionError / WinError 32) caused by the
# LOSING racer's own (unavoidable, pre-claim) _read_lock_owner() read handle
# on the same path still closing down; not a policy knob, so not read from
# config like the thresholds below.
_RECLAIM_REMOVE_RETRY_ATTEMPTS = 5
_RECLAIM_REMOVE_RETRY_SLEEP_SEC = 0.005

_THRESHOLD_DEFAULTS = {
    "allowlist_lock_retry_attempts": 20,
    "allowlist_lock_retry_sleep_sec": 0.01,
    # ponytail: fixed 30s stale-age ceiling for reclaiming a dead holder's lock
    # -- a flat guess, not measured against actual critical-section duration;
    # raise via config.thresholds.allowlist_lock_stale_age_sec if a real
    # critical section ever runs longer.
    "allowlist_lock_stale_age_sec": 30,
}


class _AllowlistBusy(Exception):
    """Raised internally when the pm-delete-allow lock cannot be acquired
    within the bounded retry window (and is not reclaimable -- see
    `_lock_is_stale`). Caught by `_try_pm_allowlist` and treated as 'not
    granted' -- consumption is irreversible, so denying a rare concurrent
    call is safer than corrupting or double-spending the ledger."""


class _AllowlistLockReclaimed(Exception):
    """Logged (never raised past this module) via `ctx.log_error` every time
    a stale lock is reclaimed, so a reclaim is never silent."""


def run(payload: dict, cfg: dict, ctx) -> io_mod.Result:
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    cmd = str(tool_input.get("command") or "")
    if not cmd.strip():
        return io_mod.ALLOW

    probes = _build_probes(cmd, cfg, ctx)

    pending_notice = ""
    for idx, rule in enumerate(cfg["command_rules"][_RULE_KEY]):
        if not rule.get("enabled", True):
            continue
        rtype = rule["type"]
        if rtype == "regex_block":
            result = _eval_regex_block(rule, probes, ctx)
        elif rtype == "regex_block_any":
            result = _eval_regex_block_any(rule, probes, ctx)
        elif rtype == "delete_allowlist":
            result, notice = _eval_delete_allowlist(rule, probes, payload, ctx)
            if notice:
                pending_notice = notice
        else:
            # Unknown rule type: a one-character typo in `type` must not
            # silently remove a check with no trace. Fail this rule open
            # (unenforceable) but log it as a config defect.
            ctx.log_config_error(
                "unknown_rule_type",
                f"command_rules.{_RULE_KEY}[{idx}].type",
                f"unrecognised rule type {rtype!r}",
                guard_scope=_RULE_KEY,
            )
            result = None
        if result is not None:
            # The ONE grant consult for this guard: the rule that decided to
            # block names itself, and an owner grant for exactly that rule turns
            # this block into `continue` -- never into ALLOW, because the rules
            # after this one have not run yet and a granted cd-compound must not
            # also wave through a delete this command performs. Decision logic
            # above is untouched; this only asks whether the owner permitted the
            # decision that was already reached.
            rule_id = rule.get("id")
            if not rule_id:
                return result  # unaddressable rule: nothing a grant could name
            ident = f"command_rules.{_RULE_KEY}/{rule_id}"
            message = grant_mod.check(ident, result.stderr, payload, cfg, ctx)
            if message is None:
                continue
            return io_mod.block(message)
    if pending_notice:
        # A non-blocking notice on an ALLOW is dropped by the dispatcher
        # today (exit_code == 0 -> stderr dropped), but is still produced
        # here for audit rather than silently discarded.
        return io_mod.Result(0, "", pending_notice)
    return io_mod.ALLOW


# ------------------------------------------------------------------ probes


def _build_probes(cmd: str, cfg: dict, ctx) -> dict:
    """`raw` unmodified; `probe_redirect` with harmless redirects stripped;
    `probe_quotes` with quoted strings blanked."""
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


# ------------------------------------------------------------------ Rule 1 / Rule 2


def _eval_regex_block(rule: dict, probes: dict, ctx) -> io_mod.Result | None:
    pat = ctx_mod.compile_pattern(
        rule["pattern"], "text", ctx, f"command_rules.{_RULE_KEY}[].pattern"
    )
    if pat is None:
        return None
    target = probes[rule["target"]]
    if pat.search(target):
        return io_mod.block(rule["message"])
    return None


def _eval_regex_block_any(rule: dict, probes: dict, ctx) -> io_mod.Result | None:
    target = probes[rule["target"]]
    for sub in rule.get("patterns", []):
        if not sub.get("enabled", True):
            continue
        pat = ctx_mod.compile_pattern(
            sub["pattern"], "text", ctx, f"command_rules.{_RULE_KEY}[].patterns[].pattern"
        )
        if pat is None:
            continue
        if pat.search(target):
            message = rule["message"].replace("{why}", sub["why"])
            return io_mod.block(message)
    return None


# ------------------------------------------------------------------ Rule 3 (delete_allowlist)


def _resolve_dotdot_base(rule: dict, ctx) -> Path:
    """Consumes `dotdot_base` rather than hardcoding the process cwd. Only
    `"process_cwd"` is a defined value today; an unrecognised value is a
    config defect, reported via `log_config_error` and falls back to
    `process_cwd` rather than silently ignoring the field or crashing."""
    value = rule.get("dotdot_base", "process_cwd")
    if value not in _KNOWN_DOTDOT_BASES:
        ctx.log_config_error(
            "dotdot_base",
            f"command_rules.{_RULE_KEY}[].dotdot_base",
            f"unrecognised dotdot_base {value!r}, falling back to process_cwd",
            guard_scope=_RULE_KEY,
        )
    return Path(os.getcwd())


def _full_path(token: str, base_dir: Path) -> str:
    """Resolves a relative token against `base_dir` (the resolved
    `dotdot_base`). ponytail: purely lexical (os.path.normpath), does not
    resolve symlinks/junctions -- a symlinked path can evade the allowlist
    check; switch to os.path.realpath if that becomes exploitable."""
    p = Path(token)
    base = p if p.is_absolute() else base_dir / p
    return os.path.normpath(str(base))


def _reject_token(token: str, reject_patterns: list, ctx) -> bool:
    for pattern in reject_patterns:
        pat = ctx_mod.compile_pattern(
            pattern, "text", ctx, f"command_rules.{_RULE_KEY}[].token_reject_patterns[]"
        )
        if pat is not None and pat.search(token):
            return True
    return False


def _bad_paths_in_command(rule: dict, cmd: str, ctx) -> list[str]:
    detect = ctx_mod.compile_pattern(
        rule["detect_pattern"], "text", ctx, f"command_rules.{_RULE_KEY}[].detect_pattern"
    )
    if detect is None or not detect.search(cmd):
        return []

    segment_split = ctx_mod.compile_pattern(
        rule["segment_split_pattern"], "text", ctx,
        f"command_rules.{_RULE_KEY}[].segment_split_pattern",
    )
    segment_start = ctx_mod.compile_pattern(
        rule["segment_start_pattern"], "text", ctx,
        f"command_rules.{_RULE_KEY}[].segment_start_pattern",
    )
    token_split = ctx_mod.compile_pattern(
        rule["token_split_pattern"], "text", ctx,
        f"command_rules.{_RULE_KEY}[].token_split_pattern",
    )
    path_token = ctx_mod.compile_pattern(
        rule["path_token_pattern"], "path", ctx,
        f"command_rules.{_RULE_KEY}[].path_token_pattern",
    )
    path_token_exclude = ctx_mod.compile_pattern(
        rule["path_token_exclude_pattern"], "path", ctx,
        f"command_rules.{_RULE_KEY}[].path_token_exclude_pattern",
    )
    dotdot = ctx_mod.compile_pattern(
        rule["dotdot_pattern"], "path", ctx, f"command_rules.{_RULE_KEY}[].dotdot_pattern"
    )
    allowed = ctx_mod.compile_pattern(
        rule["allowed_path_pattern"], "path", ctx,
        f"command_rules.{_RULE_KEY}[].allowed_path_pattern",
    )
    strip_chars = rule.get("token_strip_chars", "")
    reject_patterns = rule.get("token_reject_patterns", [])
    base_dir = _resolve_dotdot_base(rule, ctx)

    # re.split() with a capturing group returns the delimiters too, deliberately;
    # segments that are pure delimiter text fail the segment_start_pattern
    # check below and are skipped.
    segments = segment_split.split(cmd) if segment_split is not None else [cmd]

    bad: list[str] = []
    for seg in segments:
        if seg is None:
            continue
        if segment_start is None or not segment_start.search(seg):
            continue
        raw_tokens = token_split.split(seg) if token_split is not None else seg.split()
        tokens = []
        for tok in raw_tokens:
            if strip_chars:
                tok = tok.strip(strip_chars)
            if not tok:
                continue
            if _reject_token(tok, reject_patterns, ctx):
                continue
            tokens.append(tok)

        for tok in tokens:
            if path_token is not None and not path_token.search(tok):
                continue
            if path_token_exclude is not None and path_token_exclude.search(tok):
                continue
            candidate = tok
            if dotdot is not None and dotdot.search(tok):
                candidate = _full_path(tok, base_dir)
            if allowed is None or not paths_mod.matches_any(candidate, [allowed], ctx):
                bad.append(candidate)
    return bad


# -------------------------------------------------------- pm-delete-allow (locked, atomic)


def _pid_is_alive(pid: int, ctx) -> bool:
    """Stdlib-only liveness check (no psutil -- that's confined to
    dev_process_hygiene, off this PreToolUse hot path). Errs towards 'alive'
    on anything ambiguous: a false 'alive' just means waiting/refusing a
    little longer (safe); a false 'dead' means reclaiming a live holder's
    lock (unsafe)."""
    if pid <= 0:
        return False
    if ctx.is_windows:
        return _pid_is_alive_windows(pid)
    return _pid_is_alive_posix(pid)


def _pid_is_alive_posix(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just owned by someone else
    except OSError:
        return True  # ambiguous -- err towards not reclaiming
    return True


def _pid_is_alive_windows(pid: int) -> bool:
    import ctypes

    # ERROR_ACCESS_DENIED means the process exists but we lack permission
    # (alive); ERROR_INVALID_PARAMETER is the modern Windows signal for "no
    # such pid" (dead). Anything else is genuinely ambiguous and still errs
    # toward alive -- a bare OpenProcess failure must never be treated as
    # "dead" outright.
    ERROR_ACCESS_DENIED = 5
    ERROR_INVALID_PARAMETER = 87
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        err = ctypes.get_last_error()
        if err == ERROR_ACCESS_DENIED:
            return True
        if err == ERROR_INVALID_PARAMETER:
            return False
        return True  # ambiguous -- err towards not reclaiming
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True  # ambiguous -- err towards not reclaiming
        return exit_code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _write_lock_owner(fd: int, ctx) -> None:
    """Owner identity: pid + timestamp, so a lock left behind by a dead
    process is identifiable rather than anonymous."""
    os.write(fd, f"{os.getpid()}\n{ctx.now().timestamp()}\n".encode("utf-8"))


def _read_lock_owner(lock_path: Path) -> tuple[int, float] | None:
    try:
        text = lock_path.read_text(encoding="utf-8")
    except OSError:
        return None
    lines = text.splitlines()
    if len(lines) < 2:
        return None  # unwritten / mid-write -- not (yet) a readable owner record
    try:
        return int(lines[0]), float(lines[1])
    except ValueError:
        return None


def _threshold(key: str, ctx) -> float:
    """Reads `thresholds.<key>` off `ctx.cfg` (it already carries the full
    config), falling back to the historical default -- never to 0/None -- on
    anything missing, non-numeric, non-finite, or negative. `bool` is
    explicitly rejected even though `isinstance(True, int)` is True in Python
    -- a config typo of `true`/`false` for a numeric field must not silently
    become 1/0.

    `math.isfinite()` rejects NaN and both infinities: `json.loads` accepts
    `NaN`/`Infinity`/`-Infinity` as a stdlib extension to the JSON spec, and a
    plain `value < 0` check does not catch them (any comparison against NaN
    is False, and +Infinity is not negative) -- `allowlist_lock_retry_attempts:
    NaN` would otherwise produce an infinite retry loop, since `attempts >=
    nan` is also always False. Every rejection is reported via
    `log_config_error`, never silently absorbed."""
    default = _THRESHOLD_DEFAULTS[key]
    value = ctx.cfg.get("thresholds", {}).get(key)
    invalid = (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    )
    if invalid:
        ctx.log_config_error(
            "threshold",
            f"thresholds.{key}",
            f"missing or invalid value {value!r}, falling back to default {default!r}",
            guard_scope=_RULE_KEY,
        )
        return default
    return value


def _lock_is_stale(lock_path: Path, ctx) -> bool:
    """Bounded age AND a confirmed-dead owner -- both gates, never just one.

    The `owner is None` branch must NEVER fall back to the lock file's own
    mtime to judge staleness: that branch is exactly the window between
    another process's `os.open()` and its `_write_lock_owner()` -- a lock
    being legitimately acquired right now. No pid means death can never be
    confirmed, so a mid-write lock is ordinary contention, handled by the
    caller's normal bounded retry (see `check_unowned_lock_is_never_stale`).

    `thresholds.allowlist_lock_stale_age_sec` is a safety margin, not the sole
    protection -- reclaim is also gated on `_pid_is_alive`, unconditionally,
    below. That `and` is structural: the only path that can return True
    (stale) is owner-known + age >= threshold + `_pid_is_alive` confirms dead.
    Lowering the age can shorten how long a genuinely dead lock blocks
    grants; it can never by itself cause a live (or unidentifiable) holder's
    lock to be reclaimed -- pinned by
    check_stale_age_zero_still_never_reclaims_live_owner()."""
    owner = _read_lock_owner(lock_path)
    if owner is None:
        return False  # cannot identify a pid -> cannot confirm death -> never stale
    pid, ts = owner
    stale_age = _threshold("allowlist_lock_stale_age_sec", ctx)
    age = ctx.now().timestamp() - ts
    if age < stale_age:
        return False
    return not _pid_is_alive(pid, ctx)  # MANDATORY -- never short-circuited by age alone


def _reclaim_stale_lock(lock_path: Path, ctx) -> bool:
    """Returns True only when this call actually removed a lock it itself
    judged stale -- never log "reclaimed" when the removal failed (e.g. a
    Windows sharing violation from the true holder's still-open handle) and
    nothing was actually removed.

    Mutual exclusion between racing reclaimers is decided by
    `os.open(claim_path, O_CREAT | O_EXCL)` on a FIXED per-lock claim-marker
    path -- the SAME primitive `_acquire_allowlist_lock` already uses to
    acquire the lock itself -- NOT by `os.replace`/rename.

    A prior version of this function used a rename-to-quarantine race
    ("os.replace is atomic, so only one racing reclaimer can win") and was
    wrong on Windows: measured empirically (300-trial repro, two threads
    racing `os.replace` on the same source path, both against distinct
    per-call destination names and against one shared fixed destination
    name), BOTH renames reported success in the overwhelming majority of
    trials (257/300 and 266/300 respectively) even though only one
    destination file existed afterwards -- i.e. two callers could both
    believe they had exclusively reclaimed the identical stale lock and both
    proceed to treat it as reclaimed, which is precisely the double-winner
    outcome this function exists to prevent. `O_CREAT | O_EXCL`, run through
    the identical race 300 times, produced exactly one winner every time; it
    carries no dependency on rename atomicity, which Windows does not
    actually provide for this interleaving.

    Re-reads the lock's owner record after winning the claim and requires it
    to still match what this call observed and judged stale, in case a
    brand-new, legitimate lock replaced it in the window between the
    staleness check and the claim -- on a mismatch nothing is removed.

    The actual `os.remove(lock_path)` below is wrapped in a small, FIXED
    (non-configurable -- this is a handle-release wait, not a policy knob)
    bounded retry: measured empirically, the LOSING side's own
    `_read_lock_owner(lock_path)` call -- made before either side races for
    the claim, so it cannot be avoided by claim ordering -- can still hold a
    Windows file handle open by the microsecond the claim-winner tries to
    delete the same path, producing a transient `PermissionError` (WinError
    32, sharing violation) that has nothing to do with who legitimately won
    the race and clears on its own within microseconds once the reader's
    handle closes. Retrying a few times here is what stops that transient,
    self-resolving contention from being misreported as "removal failed" --
    without it, `check_reclaim_is_toctou_safe` still measured ~1.7% "zero
    winners" (5/300) purely from this, despite the claim decision itself
    being exactly-one-winner in all 300 trials."""
    observed = _read_lock_owner(lock_path)
    claim_path = lock_path.with_name(f"{lock_path.name}.reclaim-claim")
    try:
        claim_fd = os.open(str(claim_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except OSError:
        return False  # another reclaimer already holds the claim -- not our reclaim
    os.close(claim_fd)

    try:
        if _read_lock_owner(lock_path) != observed:
            return False  # not what we judged stale -- a legitimate lock raced in; leave it alone

        removed = False
        for attempt in range(_RECLAIM_REMOVE_RETRY_ATTEMPTS):
            try:
                os.remove(str(lock_path))
                removed = True
                break
            except OSError:
                if attempt + 1 < _RECLAIM_REMOVE_RETRY_ATTEMPTS:
                    time.sleep(_RECLAIM_REMOVE_RETRY_SLEEP_SEC)
        if not removed:
            return False  # removal failed -- nothing was actually reclaimed
        return True
    finally:
        try:
            os.remove(str(claim_path))
        except OSError:
            pass  # best-effort cleanup -- a leftover claim marker only ever
            # blocks a FUTURE reclaim attempt on this same lock_path (fails
            # closed), never lets a second concurrent reclaimer in alongside
            # this one; equivalent in severity to the lock itself being
            # unremovable, which this module already tolerates.


def _acquire_allowlist_lock(lock_path: Path, ctx) -> int:
    """Exclusive advisory lock via O_CREAT|O_EXCL (portable, stdlib-only, no
    third-party locking dependency), with owner identity written into the
    file and stale-lock reclaim.

    Every contended iteration -- whether a reclaim was attempted, succeeded,
    or failed -- must consume one attempt and sleep once before the next try.
    Skipping that bookkeeping on a reclaim attempt (combined with a lock
    falsely judged stale) can produce an unbounded hot-spin with no sleep and
    no upper bound. Uniform bounding makes a hot-spin structurally impossible
    regardless of whether the staleness/reclaim logic above ever regresses --
    defense in depth. `_reclaim_stale_lock`'s return value only controls
    whether a 'reclaimed' log line is emitted; it never exempts this
    iteration from the budget."""
    retry_attempts = _threshold("allowlist_lock_retry_attempts", ctx)
    retry_sleep = _threshold("allowlist_lock_retry_sleep_sec", ctx)
    attempts = 0
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if _lock_is_stale(lock_path, ctx) and _reclaim_stale_lock(lock_path, ctx):
                ctx.log_error(
                    "tool_discipline",
                    _AllowlistLockReclaimed(f"reclaimed stale pm-delete-allow lock at {lock_path}"),
                    ["pm_allowlist_lock_reclaimed"],
                )
            attempts += 1
            if attempts >= retry_attempts:
                raise _AllowlistBusy(str(lock_path))
            time.sleep(retry_sleep)
            continue
        _write_lock_owner(fd, ctx)
        return fd


def _try_pm_allowlist(
    rule: dict, bad: list[str], payload: dict, ctx, base_dir: Path
) -> tuple[bool, str, bool, str]:
    """Returns (granted, notice, contended, lock_path). Consumes matched
    entries on grant: all offending paths must be listed or nothing is
    granted; main-chain only, using the stricter two-signal `is_main_chain`,
    not an `agent_id` check alone. The read-decide-write is one locked,
    atomic critical section, with stale-lock reclaim -- see
    `_acquire_allowlist_lock`.

    `contended` is True only for the 'lock could not be acquired' outcome --
    it lets the caller pick a message that tells the user what actually
    happened (retry) rather than the generic 'not authorised' text, which
    would be actively misleading for a path that was pre-authorised but
    raced. `lock_path` is populated whenever `contended` is True: if a dead
    holder's pid gets reused by a live process, this lock can never be
    auto-reclaimed and 'just retry' becomes permanently wrong -- naming the
    exact file lets a human clear it (see the `{lock_path}` placeholder in
    `_eval_delete_allowlist`)."""
    pm_cfg = rule.get("pm_allowlist") or {}
    if not pm_cfg.get("enabled", True):
        return False, "", False, ""
    if pm_cfg.get("main_chain_only", False) and not ctx_mod.is_main_chain(payload):
        return False, "", False, ""

    allow_path = ctx.expand(pm_cfg["file"])
    if not allow_path.exists():
        return False, "", False, ""

    bad_norm = [paths_mod.norm_for_compare(_full_path(b, base_dir), ctx) for b in bad]
    consume = pm_cfg.get("consume_on_use", True)

    lock_path = allow_path.with_name(allow_path.name + ".lock")
    try:
        fd = _acquire_allowlist_lock(lock_path, ctx)
    except _AllowlistBusy as exc:
        # Logged like any other operational event, then fails closed -- safer
        # than corrupting or double-spending the ledger.
        ctx.log_error("tool_discipline", exc, ["pm_allowlist_contended"])
        return False, "", True, str(lock_path)

    try:
        # Fresh read, inside the lock -- never the caller's (or an earlier
        # attempt's) stale copy. This is what prevents the lost-update race.
        allow_entries = io_mod.read_lines(allow_path)
        allow_norm = [paths_mod.norm_for_compare(_full_path(a, base_dir), ctx) for a in allow_entries]
        unlisted = [b for b in bad_norm if b not in allow_norm]
        if unlisted:
            return False, "", False, ""

        if consume:
            remaining = [
                entry for entry, norm in zip(allow_entries, allow_norm) if norm not in bad_norm
            ]
            tmp_path = allow_path.with_name(allow_path.name + ".tmp")
            text = "".join(f"{e}\n" for e in remaining)
            io_mod.write_text(tmp_path, text)
            os.replace(str(tmp_path), str(allow_path))  # atomic rename, no partial file
    finally:
        os.close(fd)
        try:
            os.remove(str(lock_path))
        except OSError:
            pass

    notice_template = pm_cfg.get("notice", "")
    notice = notice_template.replace("{bad_paths}", " ".join(bad)) if notice_template else ""
    return True, notice, False, ""


def _eval_delete_allowlist(
    rule: dict, probes: dict, payload: dict, ctx
) -> tuple[io_mod.Result | None, str]:
    """Returns (block_result_or_None, non_blocking_notice)."""
    cmd = probes[rule["target"]]
    bad = _bad_paths_in_command(rule, cmd, ctx)
    if not bad:
        return None, ""

    base_dir = _resolve_dotdot_base(rule, ctx)
    granted, notice, contended, lock_path = _try_pm_allowlist(rule, bad, payload, ctx, base_dir)
    if granted:
        return None, notice

    message = rule["message"].replace("{bad_paths}", " ".join(bad))
    if contended:
        # The user-visible failure here is "my pre-authorised deletion was
        # refused for no visible reason" -- say what actually happened and
        # what to do about it. Text is config-driven (pm_allowlist.contended_message).
        # `{lock_path}` is substituted the same way `{bad_paths}` is (plain
        # str.replace) if the config template contains it.
        contended_template = rule.get("pm_allowlist", {}).get("contended_message", "")
        message += contended_template.replace("{lock_path}", lock_path)
    return io_mod.block(message), ""
