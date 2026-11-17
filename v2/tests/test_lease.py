"""R1-d AC-8: the window lease and its fencing tokens (spec 事前決定 1 (a)..(d)), through the
production hook entry. Expiry is made by rewriting runtime.db's expiry time (no clock
variable exists)."""
from __future__ import annotations

import sys

from helpers import Probe, Sandbox
from r1d_helpers import expire_lease

A, B = "lease-a", "lease-b"


def _call(sb: Sandbox, session: str, agent: str | None = None):
    body = {"session_id": session, "tool_name": "Bash", "tool_input": {"command": "ls"}}
    if agent:
        body.update(agent_id=agent, agent_type="general-purpose")
    res = sb.hook("PreToolUse", body)
    token = sb.query("SELECT fence_token FROM events ORDER BY id DESC LIMIT 1")[0][0]
    return res, token


def _lease(sb: Sandbox) -> tuple:
    return sb.query("SELECT session_id, fence_token, renewed_at, expires_at FROM leases")[0]


def check_lease(p: Probe) -> None:
    sb = Sandbox()
    try:
        held = 0
        res, t1 = _call(sb, A)
        lease = _lease(sb)
        history = sb.query("SELECT token, session_id, acquired_at, expires_at FROM lease_tokens")
        ok = (res.returncode == 0 and lease[:2] == (A, t1) and t1 is not None and t1 >= 1
              and history == [(t1, A, history[0][2], lease[3])] and lease[3] > history[0][2])
        held += ok
        p.check("AC-8 (a)(b): the first call acquires the lease with a fencing token, recorded "
                "in the issue history (token, session, acquired, expires)", ok, f"{lease} {history}")
        _, again = _call(sb, A)
        renewed = _lease(sb)
        history2 = sb.query("SELECT token, expires_at FROM lease_tokens")
        ok = (again == t1 and renewed[1] == t1 and renewed[3] > lease[3]
              and history2 == [(t1, renewed[3])])
        held += ok
        p.check("AC-8 (a): the holder's next call renews it (same token, later expiry, history "
                "moved)", ok, f"{renewed} {history2}")
        _, sub = _call(sb, A, agent="sub-1")
        held += sub == t1
        p.check("AC-8 (d): a subagent's event (the parent's session_id, P-18) carries the "
                "parent's token", sub == t1, str(sub))
        _, other = _call(sb, B)
        p.check("AC-8: another session's call while the lease is valid neither takes it nor "
                "gets a token", other is None and _lease(sb)[0] == A, str(other))
        expire_lease(sb)
        _, t2 = _call(sb, B)
        lease_b = _lease(sb)
        closed = sb.query("SELECT expires_at FROM lease_tokens WHERE token=?", (t1,))[0][0]
        ok = (t2 is not None and t2 >= t1 + 1 and lease_b[:2] == (B, t2)
              and closed <= lease_b[2])
        held += ok
        p.check("AC-8 (c): after the expiry another session acquires it with a token at least 1 "
                "higher; the lapsed token's history ends before it", ok, f"{t1} {t2} {lease_b} {closed}")
        _, stale = _call(sb, A)
        p.check("AC-8: the former holder's later call keeps its (stale) last token and does not "
                "take the valid lease", stale == t1 and _lease(sb)[0] == B, str(stale))
        same_tx = sb.query("SELECT COUNT(*) FROM events WHERE fence_token IS NOT NULL")[0][0]
        p.check(f"AC-8: {held}/4 (events stamped: {same_tx})", held == 4)
    finally:
        sb.cleanup()


def main() -> int:
    p = Probe()
    print("\n== R1-d AC-8: lease and fencing tokens ==")
    check_lease(p)
    return p.result()


if __name__ == "__main__":
    sys.exit(main())
