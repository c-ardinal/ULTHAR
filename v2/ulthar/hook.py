"""The single hook dispatcher (CORE-02): every hook event enters here.

Per call, in order:
  1. open runtime.db;
  2. ingest the session transcript from its last position (CORE-07);
  3. run the ported v1 guard pipeline (ulthar.v1dispatch);
  4. on PostToolUse of a file-writing tool, hash every written path (TRUST-07);
  5. store one `events` row and refresh the `sessions` row;
  6. write stdout/stderr once and exit with the decision.
A runtime.db failure (open, ingest write, event write) BLOCKS a PreToolUse with exit 2
and recovery steps; on other events it is reported on stderr and the call proceeds,
because blocking Stop/SessionEnd cannot be cleared by the model.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from . import db as db_mod
from . import ingest as ingest_mod
from . import v1dispatch
from .lib import io as io_mod

WRITE_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
PATH_KEYS = ("file_path", "notebook_path")
GIT_TIMEOUT_SEC = 10


def written_paths(payload: dict) -> list[str]:
    """Absolute paths a write tool targets (relative ones resolve against cwd)."""
    if payload.get("tool_name") not in WRITE_TOOLS:
        return []
    tool_input = payload.get("tool_input") or {}
    base = Path(payload.get("cwd") or ".")
    out = []
    for key in PATH_KEYS:
        raw = tool_input.get(key)
        if isinstance(raw, str) and raw:
            p = Path(raw)
            out.append(str(p if p.is_absolute() else base / p))
    return out


def raw_blob_hash(data: bytes) -> str:
    import hashlib  # noqa: PLC0415 -- lazy: ~10 ms import, only needed on this path

    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def blob_hash(path: str) -> str | None:
    """What `git hash-object <path>` prints (git applies the path's filters, e.g.
    autocrlf/.gitattributes); the raw blob hash when git cannot run. None if absent."""
    p = Path(path)
    if not p.is_file():
        return None
    try:
        proc = subprocess.run(["git", "-C", str(p.parent), "hash-object", "--", p.name],
                              capture_output=True, text=True, timeout=GIT_TIMEOUT_SEC)
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return raw_blob_hash(p.read_bytes())


def _db_failure(event: str, exc: BaseException) -> int:
    reason = ("the ULTHAR runtime database could not be written"
              if isinstance(exc, sqlite3.Error | OSError) else "the ULTHAR dispatcher failed")
    msg = v1dispatch.infra_block_message(
        reason,
        f"{type(exc).__name__}: {exc} (db: {db_mod.resolve_path()})", event)
    if event == "PreToolUse":
        io_mod.write_stderr(msg)
        return 2
    io_mod.write_stderr("[ulthar] WARNING (not blocking this event): " + msg)
    return 0


def _session_transcript(payload: dict) -> str | None:
    """The main transcript, in the same spelling ingest keys its cursor by; a call
    inside a subagent never replaces it."""
    raw = payload.get("transcript_path")
    if payload.get("agent_id") or not isinstance(raw, str) or not raw:
        return None
    return str(Path(raw))


def _record(conn: sqlite3.Connection, event: str, payload: dict, outcome, paths: list[str],
            post_hashes: dict | None, detail: dict) -> None:
    session_id = str(payload.get("session_id") or "(none)")
    ts = ingest_mod.now_iso()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT INTO events (ts, session_id, agent_id, agent_type, hook, tool, decision,"
            " guard_id, paths, post_hashes, detail) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (ts, session_id, payload.get("agent_id"), payload.get("agent_type"), event,
             str(payload.get("tool_name") or ingest_mod.NO_TOOL),
             "deny" if outcome.result.exit_code == 2 else "allow", outcome.guard_id,
             json.dumps(paths) if paths else None,
             json.dumps(post_hashes) if post_hashes is not None else None,
             json.dumps(detail, ensure_ascii=False)))
        conn.execute(
            'INSERT INTO sessions (session_id, project_id, started, last_seen, transcript_path)'
            " VALUES (?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET last_seen=excluded.last_seen,"
            " transcript_path=COALESCE(excluded.transcript_path, sessions.transcript_path)",
            (session_id, payload.get("cwd"), ts, ts, _session_transcript(payload)))
        conn.execute(
            'UPDATE sessions SET "offset"=(SELECT byte_offset FROM ingest_cursors c'
            " WHERE c.session_id=sessions.session_id AND c.transcript_path=sessions.transcript_path)"
            " WHERE session_id=?", (session_id,))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def handle(event: str, payload: dict, single_guard: str | None) -> int:
    try:
        conn = db_mod.connect(db_mod.resolve_path())
    except Exception as exc:  # noqa: BLE001 -- any open failure is an infra failure
        return _db_failure(event, exc)
    try:
        detail: dict = {}
        session_id = str(payload.get("session_id") or "")
        transcript = payload.get("transcript_path")
        if session_id and isinstance(transcript, str) and transcript:
            try:
                counts = ingest_mod.ingest(conn, session_id, transcript, hook=event)
                detail["ingested"] = {k: v for k, v in counts.items() if v}
            except sqlite3.Error:
                raise
            except Exception as exc:  # noqa: BLE001 -- an unreadable transcript never blocks
                detail["ingest_error"] = f"{type(exc).__name__}: {exc}"
        outcome = v1dispatch.run(event, payload, single_guard)
        detail["guards_run"] = outcome.guards_run
        paths = written_paths(payload)
        post_hashes = ({p: blob_hash(p) for p in paths}
                       if event == "PostToolUse" and paths else None)
        _record(conn, event, payload, outcome, paths, post_hashes, detail)
    except Exception as exc:  # noqa: BLE001 -- sqlite errors and anything unexpected
        return _db_failure(event, exc)
    finally:
        conn.close()
    if outcome.result.stdout:
        io_mod.write_stdout_bytes(outcome.result.stdout)
    if outcome.result.exit_code == 2:
        io_mod.write_stderr(outcome.result.stderr)
    return outcome.result.exit_code


def _flag(argv: list[str], name: str) -> str | None:
    for i, arg in enumerate(argv):
        if arg == name and i + 1 < len(argv):
            return argv[i + 1]
    return None


def main(argv: list[str]) -> int:
    """argv: --event <E> [--guard <id>] (hand-parsed: argparse costs ~5 ms per hook)."""
    io_mod.configure_streams()
    payload = io_mod.read_stdin_json()
    if payload is None:
        return 0  # v1 I-3: unreadable stdin fails open before anything runs
    event = (str(payload.get("hook_event_name") or "").strip()
             or _flag(argv, "--event") or "<unknown>")
    return handle(event, payload, _flag(argv, "--guard"))
