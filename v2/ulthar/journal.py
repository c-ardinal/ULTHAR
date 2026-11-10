"""Fallback journal: an event row that could not be written to runtime.db is appended
to `<runtime.db>.pending.jsonl` (one JSON object per line, fsynced) and replayed into
runtime.db by the next hook call that can open it, before that call records its own
row. Each row carries a random `jid`; `journal_replayed` makes replay idempotent, so a
crash between COMMIT and the claim file's removal never duplicates a row.

Replay first renames the journal to a per-process claim file, so concurrent writers
keep appending to a fresh journal while the claim is replayed.
ponytail: on POSIX a writer that opened the journal just before the rename may land
its line in the claim after it was read (window of one write); on Windows the rename
fails while a writer holds the file, and the journal is simply replayed next time.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

PENDING_SUFFIX = ".pending.jsonl"
CLAIM_SUFFIX = ".replaying"


def path_for(db_path: Path) -> Path:
    return db_path.with_name(db_path.name + PENDING_SUFFIX)


def new_jid() -> str:
    return os.urandom(12).hex()


def append(db_path: Path, row: dict) -> Path:
    """Durably appends one row; raises OSError when even that is impossible."""
    target = path_for(db_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(target, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0))
    try:
        os.write(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)
    return target


def _claims(db_path: Path) -> list[Path]:
    pending = path_for(db_path)
    prefix = pending.name + "."
    found = []
    try:
        with os.scandir(pending.parent) as entries:
            for entry in entries:
                if (entry.name.startswith(prefix) and entry.name.endswith(CLAIM_SUFFIX)
                        and entry.is_file()):
                    found.append(Path(entry.path))
    except FileNotFoundError:
        return []
    if pending.is_file():
        claim = pending.with_name(f"{pending.name}.{os.getpid()}-{new_jid()}{CLAIM_SUFFIX}")
        try:
            os.replace(pending, claim)
            found.append(claim)
        except (FileNotFoundError, PermissionError):
            pass  # gone (another replayer) or held open by a writer: next call
    return sorted(found)


def replay(conn: sqlite3.Connection, db_path: Path, insert_row) -> dict:
    """Replays every pending/claimed row through `insert_row(conn, row)` inside one
    transaction per claim file. Returns {"rows": n, "unreadable_lines": m} (empty
    when there was nothing to do). Unparseable (torn) lines are counted, not fatal."""
    stats = {"rows": 0, "unreadable_lines": 0}
    for claim in _claims(db_path):
        try:
            raw_lines = claim.read_bytes().splitlines()
        except FileNotFoundError:
            continue
        rows = []
        for raw in raw_lines:
            try:
                row = json.loads(raw)
                if isinstance(row, dict) and row.get("jid"):
                    rows.append(row)
                    continue
            except ValueError:
                pass
            stats["unreadable_lines"] += bool(raw.strip())
        conn.execute("BEGIN IMMEDIATE")
        try:
            for row in rows:
                done = conn.execute("SELECT 1 FROM journal_replayed WHERE jid=?",
                                    (row["jid"],)).fetchone()
                if done:
                    continue
                insert_row(conn, row)
                conn.execute("INSERT INTO journal_replayed (jid, replayed_at) VALUES (?, ?)",
                             (row["jid"], row.get("ts")))
                stats["rows"] += 1
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        try:
            claim.unlink()
        except FileNotFoundError:
            pass
    return stats if any(stats.values()) else {}
