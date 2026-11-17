"""The capability registry (PORT-05, R1-e AC-6), CLI side.

`ulthar cap check <name>` runs the capability's health check -- a real call -- and
records (healthy, last_check, remaining, detail) in runtime.db `capabilities`:
  codex        the command `caps.codex.check_cmd` in .claude/ulthar.json (default: a
               one-line `codex exec`); healthy = exit 0 within CHECK_TIMEOUT_SEC; a
               stdout line `remaining: <x>` is recorded as the remaining amount;
  ntfy         one message through the ntfy adapter (ulthar.notifier);
  claude_push  never healthy: hooks and the CLI have no way to send it (P-09, spec 事前決定 4).
A capability whose latest check failed, or is older than STALE_SEC, or was never checked,
is unavailable (`status`). A check that fails where the previous one did not is an
important notification (a degraded capability, AUT-03).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone

from . import store as store_mod

NAMES = ("codex", "ntfy", "claude_push")
STALE_SEC = 24 * 3600
CHECK_TIMEOUT_SEC = 180
DEFAULT_CODEX_CHECK = ["codex", "exec", "--skip-git-repo-check", "-s", "read-only",
                       "Reply with the single word OK."]
REMAINING = re.compile(r"^remaining:\s*(.+)$", re.I | re.M)
P09 = ("P-09: hooks and the CLI have no way to send a Claude push notification "
       "(spec 事前決定 4); notifications go to ntfy")


class CapError(Exception):
    pass


def record(rt, name: str, ok: bool, detail: str, remaining: str | None = None) -> None:
    rt.execute("INSERT INTO capabilities (name, healthy, last_check, remaining, detail)"
               " VALUES (?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET healthy=excluded.healthy,"
               " last_check=excluded.last_check, remaining=COALESCE(excluded.remaining,"
               " capabilities.remaining), detail=excluded.detail",
               (name, int(ok), store_mod.now_iso(), remaining, detail[:2000]))


def status(rt, name: str) -> tuple[bool, str]:
    """(available, reason)."""
    row = rt.execute("SELECT healthy, last_check, detail FROM capabilities WHERE name=?",
                     (name,)).fetchone()
    if row is None:
        return False, f"{name} was never checked (`ulthar cap check {name}`)"
    healthy, last, detail = row
    if not healthy:
        return False, f"the latest check ({last}) failed: {detail}"
    stale = (datetime.now(timezone.utc) - timedelta(seconds=STALE_SEC)).isoformat(timespec="milliseconds")
    if not last or last < stale:
        return False, f"the latest check ({last}) is older than 24 hours"
    return True, f"checked {last}: {detail}"


def listing(rt) -> list[dict]:
    names = list(NAMES) + [r[0] for r in rt.execute("SELECT name FROM capabilities ORDER BY name")
                           if r[0] not in NAMES]
    out = []
    for name in names:
        row = rt.execute("SELECT last_check, remaining FROM capabilities WHERE name=?",
                         (name,)).fetchone()
        ok, why = status(rt, name)
        out.append({"name": name, "available": ok, "reason": why,
                    "last_check": row[0] if row else None, "remaining": row[1] if row else None})
    return out


def _codex(cfg: dict) -> tuple[bool, str, str | None]:
    raw = ((cfg.get("caps") or {}).get("codex") or {}).get("check_cmd", DEFAULT_CODEX_CHECK)
    if not isinstance(raw, list) or not raw or not all(isinstance(a, str) and a for a in raw):
        raise CapError(".claude/ulthar.json: caps.codex.check_cmd must be a non-empty list of strings")
    argv = [shutil.which(raw[0]) or raw[0], *raw[1:]]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", stdin=subprocess.DEVNULL,
                              timeout=CHECK_TIMEOUT_SEC,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return False, f"{raw[0]}: no answer within {CHECK_TIMEOUT_SEC} s", None
    except OSError as exc:
        return False, f"{raw[0]}: {type(exc).__name__}: {exc}", None
    left = REMAINING.search(proc.stdout or "")
    tail = (proc.stderr or proc.stdout or "").strip()[-300:]
    return proc.returncode == 0, f"exit {proc.returncode}: {tail}", left.group(1).strip() if left else None


def check(store: store_mod.Store, rt, name: str) -> dict:
    """Runs and records one check; returns {name, ok, detail, remaining, degraded}."""
    if name not in NAMES:
        raise CapError(f"unknown capability {name!r}: one of {', '.join(NAMES)}")
    before = rt.execute("SELECT healthy FROM capabilities WHERE name=?", (name,)).fetchone()
    remaining = None
    if name == "claude_push":
        ok, detail = False, P09
    elif name == "codex":
        ok, detail, remaining = _codex(store.config)
    else:
        from . import notifier  # noqa: PLC0415 -- CLI only (ulthar.notifier imports this module)

        try:
            ok, detail = notifier.notifier(store.config).send(
                "ULTHAR health check", f"ulthar cap check ntfy ({os.getpid()})", False)
        except notifier.NotifyError as exc:
            ok, detail = False, str(exc)
    rt.execute("BEGIN IMMEDIATE")
    try:
        record(rt, name, ok, detail, remaining)
        rt.execute("COMMIT")
    except BaseException:
        rt.execute("ROLLBACK")
        raise
    degraded = not ok and name != "claude_push" and (before is None or bool(before[0]))
    return {"name": name, "ok": ok, "detail": detail, "remaining": remaining, "degraded": degraded}
