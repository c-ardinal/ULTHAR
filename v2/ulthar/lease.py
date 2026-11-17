"""The window lease and its fencing tokens -- R1-d's minimum of SES-02 (spec 事前決定 1, AC-8).

Hook side (`touch`, inside the transaction that stores the call's events row, so no
extra commit): a session with no valid lease on the project acquires it when nobody
else holds a valid one, a session holding it renews it; LEASE_SEC without a renewal
and it lapses. Every acquisition issues a new fencing token, one above every token
issued before, and records it in `lease_tokens` (token, project, session, acquired,
expires; a renewal moves the expiry, a lapse is closed at the lease's own expiry when
the next holder takes over). The call's events row is stamped with the token its
session holds -- or, when another session holds the lease, the session's last token,
which `invalid_because` then refuses. A subagent's calls carry the parent's
session_id (P-18), so they are stamped with the parent's token. R1-d never blocks a
write on the lease; only the integration check (TRUST-04 (d), CLI) reads it.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

LEASE_SEC = 15 * 60  # U-02


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="milliseconds")  # the format of every ulthar timestamp


def touch(conn: sqlite3.Connection, project_id: str, session_id: str, at: str) -> int | None:
    """Acquire or renew at `at` (the event's own timestamp, so the token is valid at the
    event's time); the fencing token to stamp on that event (None: the session never
    held one). The caller holds the write transaction."""
    now_s, until = at, _iso(datetime.fromisoformat(at) + timedelta(seconds=LEASE_SEC))
    row = conn.execute("SELECT session_id, fence_token, expires_at, renewed_at FROM leases"
                       " WHERE project_id=?", (project_id,)).fetchone()
    if row is not None and row[2] and row[2] > now_s:
        if row[0] == session_id:
            conn.execute("UPDATE leases SET renewed_at=?, expires_at=? WHERE project_id=?",
                         (now_s, until, project_id))
            conn.execute("UPDATE lease_tokens SET expires_at=? WHERE token=?", (until, row[1]))
            return row[1]
        return conn.execute("SELECT MAX(token) FROM lease_tokens WHERE project_id=? AND"
                            " session_id=?", (project_id, session_id)).fetchone()[0]
    if row is not None and row[1] is not None:
        # the lapsed token's history ends at its expiry -- never before its last renewal,
        # which every write the token stamped precedes
        conn.execute("UPDATE lease_tokens SET expires_at=MIN(expires_at, MAX(?, ?)) WHERE token=?",
                     (row[2] or now_s, row[3] or "", row[1]))
    token = conn.execute("SELECT COALESCE(MAX(token), 0) + 1 FROM lease_tokens").fetchone()[0]
    conn.execute("INSERT INTO lease_tokens (token, project_id, session_id, acquired_at,"
                 " expires_at) VALUES (?,?,?,?,?)", (token, project_id, session_id, now_s, until))
    conn.execute("INSERT INTO leases (project_id, host_id, session_id, fence_token, renewed_at,"
                 " expires_at) VALUES (?,NULL,?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET"
                 " session_id=excluded.session_id, fence_token=excluded.fence_token,"
                 " renewed_at=excluded.renewed_at, expires_at=excluded.expires_at",
                 (project_id, session_id, token, now_s, until))
    return token


def holder(conn: sqlite3.Connection, project_id: str) -> tuple[str, int] | None:
    """(session_id, token) of the project's valid lease, or None."""
    row = conn.execute("SELECT session_id, fence_token, expires_at FROM leases"
                       " WHERE project_id=?", (project_id,)).fetchone()
    if row is None or not row[2] or row[2] <= _iso(datetime.now(timezone.utc)):
        return None
    return row[0], row[1]


def token_of(conn: sqlite3.Connection, project_id: str, session_id: str) -> int | None:
    """The token to stamp on a CLI-recorded write of `session_id` (`ulthar run`): the
    lease's token when the session holds it, else its last one (None: never held)."""
    held = holder(conn, project_id)
    if held is not None and held[0] == session_id:
        return held[1]
    return conn.execute("SELECT MAX(token) FROM lease_tokens WHERE project_id=? AND"
                        " session_id=?", (project_id, session_id)).fetchone()[0]


def invalid_because(conn: sqlite3.Connection, token, session_id: str | None,
                    ts: str) -> str | None:
    """TRUST-04 (d): why `token` was not the token `session_id` held at `ts` (None: it
    was) -- judged on the issue history."""
    if token is None:
        return "the write carries no fencing token (its session never held the lease)"
    row = conn.execute("SELECT project_id, session_id, acquired_at, expires_at FROM"
                       " lease_tokens WHERE token=?", (token,)).fetchone()
    if row is None:
        return f"fencing token {token} was never issued"
    project_id, holder_session, acquired, expires = row
    if holder_session != session_id:
        return f"fencing token {token} belongs to session {holder_session}, not {session_id}"
    if not acquired <= ts <= expires:
        return f"fencing token {token} was not valid at {ts} (held {acquired} .. {expires})"
    newer = conn.execute("SELECT MIN(token) FROM lease_tokens WHERE project_id=? AND token>?"
                         " AND acquired_at<=?", (project_id, token, ts)).fetchone()[0]
    if newer is not None:
        return f"fencing token {token} was superseded by token {newer} before {ts}"
    return None
