"""Fallback journal: an event row that could not be written to runtime.db is appended
to `<runtime.db>.pending.jsonl` (one JSON object per line, fully written and fsynced)
and replayed into runtime.db by the next hook call that can open it, before that call
records its own row.

Race-freedom: writers append only while holding an exclusive lock on
`<runtime.db>.pending.lock`, and they open the journal after taking it. A replayer
takes the same lock just to rename the journal (and any orphaned claim) to a claim
file of its own, then releases it -- so no writer can still be writing to a claim,
and two replayers never own the same claim. A claim is deleted only after every row
in it is committed (rows carry a random `jid`; `journal_replayed` makes replay
idempotent) and every unreadable (torn) line in it has been copied, fsynced, to
`<runtime.db>.pending.quarantine.jsonl` and recorded as a guard_id "journal" event.
A replay that fails renames its claim to `.retry`, which the next call adopts at
once; a claim left by a crashed replayer is adopted once it is STALE_CLAIM_SEC old.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

PENDING_SUFFIX = ".pending.jsonl"
LOCK_SUFFIX = ".pending.lock"
QUARANTINE_SUFFIX = ".pending.quarantine.jsonl"
CLAIM_SUFFIX = ".replaying"
RETRY_SUFFIX = ".retry"
STALE_CLAIM_SEC = 60
JOURNAL_GUARD_ID = "journal"
LOCK_TIMEOUT_SEC = 30
LOCK_POLL_SEC = 0.005


def path_for(db_path: Path) -> Path:
    return db_path.with_name(db_path.name + PENDING_SUFFIX)


def quarantine_path(db_path: Path) -> Path:
    return db_path.with_name(db_path.name + QUARANTINE_SUFFIX)


def new_jid() -> str:
    return os.urandom(12).hex()


@contextmanager
def _locked(db_path: Path):
    """Exclusive inter-process lock; raises OSError after LOCK_TIMEOUT_SEC on Windows."""
    lock = db_path.with_name(db_path.name + LOCK_SUFFIX)
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT)
    try:
        if os.name == "nt":
            import msvcrt  # noqa: PLC0415 -- platform-specific stdlib

            os.lseek(fd, 0, os.SEEK_SET)
            deadline = time.monotonic() + LOCK_TIMEOUT_SEC
            while True:  # LK_LOCK gives up after 10 x 1 s; poll with short sleeps instead
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(LOCK_POLL_SEC)
            try:
                yield
            finally:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl  # noqa: PLC0415 -- platform-specific stdlib

            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError(f"short write: os.write returned {written} with {len(view)} bytes left")
        view = view[written:]


def _append_bytes(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0))
    try:
        _write_all(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def append(db_path: Path, row: dict) -> Path:
    """Durably appends one row; raises OSError when that is impossible."""
    target = path_for(db_path)
    line = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
    with _locked(db_path):
        _append_bytes(target, line)
    return target


def _candidates(db_path: Path) -> list[Path]:
    pending = path_for(db_path)
    prefix = pending.name + "."
    now = time.time()
    found = [pending] if pending.is_file() else []
    try:
        with os.scandir(pending.parent) as entries:
            for entry in entries:
                if not entry.name.startswith(prefix) or not entry.is_file():
                    continue
                if entry.name.endswith(RETRY_SUFFIX) or (
                        entry.name.endswith(CLAIM_SUFFIX)
                        and now - entry.stat().st_mtime >= STALE_CLAIM_SEC):
                    found.append(Path(entry.path))
    except FileNotFoundError:
        return []
    return found


def _take_claims(db_path: Path) -> list[Path]:
    if not _candidates(db_path):  # the common case: no lock taken at all
        return []
    pending = path_for(db_path)
    owned = []
    with _locked(db_path):
        for source in _candidates(db_path):
            claim = pending.with_name(f"{pending.name}.{os.getpid()}-{new_jid()}{CLAIM_SUFFIX}")
            try:
                os.replace(source, claim)
            except FileNotFoundError:
                continue
            os.utime(claim)  # fresh mtime: an adopted claim is not stale for others
            owned.append(claim)
    return owned


def _parse(data: bytes) -> tuple[list[dict], list[bytes]]:
    rows, unreadable = [], []
    for raw in data.split(b"\n"):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except ValueError:
            row = None
        if isinstance(row, dict) and row.get("jid"):
            rows.append(row)
        else:
            unreadable.append(raw)
    return rows, unreadable


def _replay_claim(conn: sqlite3.Connection, db_path: Path, claim: Path, insert_row,
                  hook: str, stats: dict) -> None:
    rows, unreadable = _parse(claim.read_bytes())
    if unreadable:
        _append_bytes(quarantine_path(db_path), b"".join(r + b"\n" for r in unreadable))
    conn.execute("BEGIN IMMEDIATE")
    try:
        for row in rows:
            if conn.execute("SELECT 1 FROM journal_replayed WHERE jid=?",
                            (row["jid"],)).fetchone():
                continue
            insert_row(conn, row)
            conn.execute("INSERT INTO journal_replayed (jid, replayed_at) VALUES (?, ?)",
                         (row["jid"], row.get("ts")))
            stats["rows"] += 1
        if unreadable:
            conn.execute(
                "INSERT INTO events (ts, session_id, hook, tool, decision, guard_id, detail)"
                " VALUES (?,?,?,?,?,?,?)",
                (datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "(journal)",
                 hook, "(none)", "allow", JOURNAL_GUARD_ID,
                 json.dumps({"gap": "journal_unreadable_lines", "lines": len(unreadable),
                             "quarantine": str(quarantine_path(db_path)),
                             "claim": claim.name})))
            stats["quarantined_lines"] += len(unreadable)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    claim.unlink()


def replay(conn: sqlite3.Connection, db_path: Path, insert_row, hook: str = "replay") -> dict:
    """Replays every journaled row through `insert_row(conn, row)`, one transaction per
    claim. Returns {"rows": n, "quarantined_lines": m}, or {} when there was nothing."""
    stats = {"rows": 0, "quarantined_lines": 0}
    claims = _take_claims(db_path)
    for i, claim in enumerate(claims):
        try:
            _replay_claim(conn, db_path, claim, insert_row, hook, stats)
        except BaseException:
            for left in claims[i:]:  # hand every unfinished claim to the next call
                try:
                    os.replace(left, left.with_name(left.name + RETRY_SUFFIX))
                except OSError:
                    pass  # stays a claim: adopted once stale
            raise
    return stats if any(stats.values()) else {}
