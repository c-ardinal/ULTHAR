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

import errno
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
# "Held by someone else": POSIX flock(LOCK_NB) -> EWOULDBLOCK/EAGAIN (EACCES on some
# systems); Windows msvcrt.locking(LK_NBLCK) -> EACCES (measured), EDEADLOCK for LK_LOCK.
LOCK_CONTENTION_ERRNOS = frozenset(
    {errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES, getattr(errno, "EDEADLOCK", errno.EDEADLK)})


def path_for(db_path: Path) -> Path:
    return db_path.with_name(db_path.name + PENDING_SUFFIX)


def quarantine_path(db_path: Path) -> Path:
    return db_path.with_name(db_path.name + QUARANTINE_SUFFIX)


def new_jid() -> str:
    return os.urandom(12).hex()


def _lock_ops(fd: int, posix: bool):
    """(try_lock, unlock) for this platform; try_lock raises OSError when held."""
    if posix:
        import fcntl  # noqa: PLC0415 -- platform-specific stdlib

        return (lambda: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB),
                lambda: fcntl.flock(fd, fcntl.LOCK_UN))
    import msvcrt  # noqa: PLC0415 -- platform-specific stdlib

    def try_lock() -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def unlock() -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    return try_lock, unlock


def _locked(db_path: Path, posix: bool | None = None):
    return file_lock(db_path.with_name(db_path.name + LOCK_SUFFIX), posix)


@contextmanager
def file_lock(lock: Path, posix: bool | None = None):
    """Exclusive inter-process lock on the file `lock`, both platforms non-blocking and
    polled: a stalled holder makes this raise OSError after LOCK_TIMEOUT_SEC (read per
    call), which the caller reports -- never an indefinite hang. `posix` overrides the
    platform choice (tests only). Also the knowledge store's project lock (ulthar.store)."""
    if not lock.parent.is_dir():
        lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT)
    try:
        try_lock, unlock = _lock_ops(fd, os.name != "nt" if posix is None else posix)
        deadline = time.monotonic() + LOCK_TIMEOUT_SEC
        while True:
            try:
                try_lock()
                break
            except OSError as exc:
                if exc.errno not in LOCK_CONTENTION_ERRNOS:
                    raise  # not "someone else holds it": waiting cannot help
                if time.monotonic() >= deadline:
                    raise OSError(f"lock {lock} not acquired within "
                                  f"{LOCK_TIMEOUT_SEC} s: {exc}") from exc
                time.sleep(LOCK_POLL_SEC)
        try:
            yield
        finally:
            unlock()
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
