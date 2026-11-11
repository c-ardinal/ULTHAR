"""Integrity of the knowledge store's JSONL files (R1-b AC-1, AC-20).

* Strict parsing: every line of a JSONL file is one JSON object carrying the common and
  type-specific fields, with an integer seq that rises through the file, and the file
  ends with a newline. Anything else is an IntegrityError -- never a skipped line.
* Chain value per append: chain = sha256(previous chain + appended bytes), recorded with
  the size after the append, in index.db and runtime.db, inside the project lock.
  Recomputing it from the file shows any change made outside ulthar: an unrecorded
  append, a same-size rewrite, a deleted line (`ulthar store verify`).
* Write-ahead manifest (`.ulthar/txn.json`): before a transaction appends to one or
  more files it records each file's size before and after. A manifest still present
  at the next open means the transaction did not finish: if the index already holds
  the new sizes it committed (only the runtime.db chain rows may be missing), else
  every file is cut back to its size before -- so no half-applied multi-file write
  (a decision without its term) and no torn final line survive.
  The manifest itself is replaced atomically (temp file, fsync, os.replace).
  ponytail: the JSONL appends are not fsynced -- they survive a crashed process, not a
  lost OS write cache (fsync per hook call would cost tens of ms on Windows).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

try:
    from _sha2 import sha256
except ImportError:  # pragma: no cover
    from hashlib import sha256

MANIFEST = "txn.json"
CHAIN_START = ""


class StoreError(Exception):
    """Any knowledge-store failure (never a bare sqlite3.Error: runtime.db's handling of
    those must not be triggered by the index). Re-exported by ulthar.store."""


class IntegrityError(StoreError):
    """The store's files are not what ulthar wrote: reads and writes stop (fail closed)."""


def chain_next(prev: str, data: bytes) -> str:
    return sha256(prev.encode("ascii") + data).hexdigest()


def parse_strict(data: bytes, name: str, required: tuple[str, ...]) -> list[dict]:
    """All records of one JSONL file, or IntegrityError naming the first bad line."""
    if data and not data.endswith(b"\n"):
        raise IntegrityError(f"{name}: the last line is incomplete (no newline) at byte "
                             f"{data.rfind(b'\n') + 1}")
    out, offset, last_seq = [], 0, 0
    for line_no, raw in enumerate(data.split(b"\n")[:-1] if data else [], 1):
        where = f"{name} line {line_no} (byte {offset})"
        offset += len(raw) + 1
        try:
            rec = json.loads(raw)
        except ValueError as exc:
            raise IntegrityError(f"{where}: not JSON ({exc})") from None
        if not isinstance(rec, dict):
            raise IntegrityError(f"{where}: not a JSON object")
        missing = [f for f in required if f not in rec]
        if missing:
            raise IntegrityError(f"{where}: missing fields {missing}")
        seq = rec["seq"]
        if not isinstance(seq, int) or isinstance(seq, bool) or seq <= last_seq:
            raise IntegrityError(f"{where}: seq {seq!r} does not rise (previous {last_seq})")
        if not isinstance(rec["id"], str) or not rec["id"]:
            raise IntegrityError(f"{where}: bad id {rec['id']!r}")
        last_seq = seq
        out.append(rec)
    return out


def chains_at(data: bytes, sizes: list[int]) -> dict[int, str | None]:
    """{size: chain over the appends ending at each recorded size} -- recomputed from
    the bytes, the recorded sizes being the append boundaries. None when a size lies
    beyond the data."""
    out: dict[int, str | None] = {}
    prev, start = CHAIN_START, 0
    for size in sorted(set(sizes)):
        if size > len(data):
            out[size] = None
            continue
        prev = chain_next(prev, data[start:size])
        out[size], start = prev, size
    return out


def write_manifest(store_dir: Path, plan: dict) -> None:
    """Replaces the manifest atomically (AC-20): written to a temp file, flushed and
    fsynced, then os.replace'd over it, then the directory fsynced where the OS allows.
    A failure at any point leaves the previous manifest whole -- never truncated in place."""
    target = store_dir / MANIFEST
    temp = store_dir / f"{MANIFEST}.{os.getpid()}.tmp"
    try:
        with open(temp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(plan))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp, target)
    except BaseException:
        try:
            os.remove(temp)
        except OSError:
            pass
        raise
    _fsync_dir(store_dir)


def _fsync_dir(path: Path) -> None:
    try:  # POSIX; Windows cannot open a directory for fsync, and NTFS orders the rename
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def read_manifest(store_dir: Path) -> dict | None:
    path = store_dir / MANIFEST
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise IntegrityError(f"{path} is unreadable: {exc}") from exc


def clear_manifest(store_dir: Path) -> None:
    try:
        os.remove(store_dir / MANIFEST)
    except FileNotFoundError:
        pass


def roll_back(store_dir: Path, plan: dict) -> None:
    """Cuts every file of an unfinished transaction back to its size before."""
    for name, step in plan["files"].items():
        path = store_dir / name
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            continue
        if size < step["old"]:
            raise IntegrityError(f"{name}: shorter ({size}) than before the unfinished "
                                 f"transaction ({step['old']}): cannot recover")
        if size > step["old"]:
            with open(path, "r+b") as fh:
                fh.truncate(step["old"])
    clear_manifest(store_dir)


def verify_file(data: bytes, name: str, records: dict[str, list[tuple[int, str]]]) -> list[str]:
    """Problems found in one file against the recorded (size, chain) rows, by source
    ("index.db" / "runtime.db"); the first divergence first. [] when it matches."""
    problems = []
    for source, rows in records.items():
        if not rows:
            continue
        computed = chains_at(data, [s for s, _ in rows])
        prev_size = 0
        for size, chain in sorted(rows):
            got = computed[size]
            if got is None:
                problems.append(f"{name}: {source} records an append ending at byte {size}, "
                                f"but the file has only {len(data)} bytes (deleted content "
                                f"from byte {prev_size} on)")
                break
            if got != chain:
                problems.append(f"{name}: content differs from what ulthar wrote in the append "
                                f"at bytes {prev_size}..{size} ({source})")
                break
            prev_size = size
        else:
            last = max(s for s, _ in rows)
            if len(data) > last:
                problems.append(f"{name}: {len(data) - last} bytes from byte {last} were not "
                                f"written by ulthar ({source} has no record of them)")
    return problems
