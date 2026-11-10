"""The single hook dispatcher (CORE-02): every hook event enters here.

Per call, in order:
  1. open runtime.db and replay the fallback journal, if any (ulthar.journal);
  2. ingest the session transcript from its last position (CORE-07);
  3. run the ported v1 guard pipeline (ulthar.v1dispatch);
  4. on PostToolUse of a file-writing tool, hash every written path (TRUST-07);
  5. store one `events` row and refresh the `sessions` row;
  6. write stdout/stderr once and exit with the decision.
When runtime.db cannot be written, the call's event row goes to the fallback journal
and is replayed by the next successful call -- no event is dropped. A PreToolUse is
then BLOCKED (exit 2) with recovery steps; other events proceed with a warning,
because blocking Stop/SessionEnd cannot be cleared by the model. Only if the journal
cannot be written either is a PostToolUse also answered with exit 2, so the loss of
its post-write hashes is never silent.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from collections import namedtuple

from . import db as db_mod
from . import ingest as ingest_mod
from . import journal as journal_mod
from . import v1dispatch
from .lib import io as io_mod

WRITE_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
PATH_KEYS = ("file_path", "notebook_path")
GIT_TIMEOUT_SEC = 10
HASH_FAILED = "hash_failed"


Processed = namedtuple("Processed", ("result", "guards_run"))


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


def blob_hash(path: str) -> str | dict | None:
    """What `git hash-object <path>` prints (git applies the path's filters, e.g.
    autocrlf/.gitattributes). None when the file is absent. When git cannot produce
    it, {"hash_failed": reason} -- never a substitute hash, which could disagree with
    the blob git would store and so poison TRUST-04's comparison. Never raises."""
    import subprocess  # noqa: PLC0415 -- lazy: only PostToolUse of a write tool hashes

    p = Path(path)
    try:
        if not p.is_file():
            return None
        proc = subprocess.run(["git", "-C", str(p.parent), "hash-object", "--", p.name],
                              capture_output=True, text=True, timeout=GIT_TIMEOUT_SEC)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return {HASH_FAILED: f"{type(exc).__name__}: {exc}"}
    out = proc.stdout.strip()
    if proc.returncode == 0 and len(out) == 40:
        return out
    return {HASH_FAILED: f"git hash-object exit {proc.returncode}: {proc.stderr.strip()[:300]}"}


def _session_transcript(payload: dict) -> str | None:
    """The main transcript, in the same spelling ingest keys its cursor by; a call
    inside a subagent never replaces it."""
    raw = payload.get("transcript_path")
    if payload.get("agent_id") or not isinstance(raw, str) or not raw:
        return None
    return str(Path(raw))


def _event_row(event: str, payload: dict, outcome, paths: list[str],
               post_hashes: dict | None, detail: dict) -> dict:
    return {
        "jid": journal_mod.new_jid(), "ts": ingest_mod.now_iso(),
        "session_id": str(payload.get("session_id") or "(none)"),
        "agent_id": payload.get("agent_id"), "agent_type": payload.get("agent_type"),
        "hook": event, "tool": str(payload.get("tool_name") or ingest_mod.NO_TOOL),
        "decision": "deny" if outcome.result.exit_code == 2 else "allow",
        "guard_id": outcome.guard_id, "paths": paths or None, "post_hashes": post_hashes,
        "detail": detail, "project_id": payload.get("cwd"),
        "transcript_path": _session_transcript(payload),
    }


def insert_row(conn: sqlite3.Connection, row: dict) -> None:
    """One events row plus the sessions refresh; the caller owns the transaction."""
    conn.execute(
        "INSERT INTO events (ts, session_id, agent_id, agent_type, hook, tool, decision,"
        " guard_id, paths, post_hashes, detail) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (row["ts"], row["session_id"], row["agent_id"], row["agent_type"], row["hook"],
         row["tool"], row["decision"], row["guard_id"],
         json.dumps(row["paths"]) if row["paths"] else None,
         json.dumps(row["post_hashes"]) if row["post_hashes"] is not None else None,
         json.dumps(row["detail"], ensure_ascii=False)))
    conn.execute(
        "INSERT INTO sessions (session_id, project_id, started, last_seen, transcript_path)"
        " VALUES (?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET"
        " last_seen=MAX(excluded.last_seen, COALESCE(sessions.last_seen, '')),"
        " transcript_path=COALESCE(excluded.transcript_path, sessions.transcript_path)",
        (row["session_id"], row["project_id"], row["ts"], row["ts"], row["transcript_path"]))
    conn.execute(
        'UPDATE sessions SET "offset"=(SELECT byte_offset FROM ingest_cursors c'
        " WHERE c.session_id=sessions.session_id AND c.transcript_path=sessions.transcript_path)"
        " WHERE session_id=?", (row["session_id"],))


def _record(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        insert_row(conn, row)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _open_and_catch_up(event: str, payload: dict, db_path: Path, detail: dict):
    """Opens runtime.db, replays the journal, ingests the transcript. Raises on any
    database failure; an unreadable transcript is only noted in `detail`."""
    conn = db_mod.connect(db_path)
    try:
        replayed = journal_mod.replay(conn, db_path, insert_row, hook=event)
        if replayed:
            detail["journal_replayed"] = replayed
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
    except BaseException:
        conn.close()
        raise
    return conn


PIPELINE_ERROR_GUARD_ID = "dispatcher-error"
TRACEBACK_CHARS = 2000


def _run_guards(event: str, payload: dict, single_guard: str | None, loaded,
                detail: dict):
    """An unexpected exception from the guard pipeline is never a silent allow: it is
    recorded in the event (guard_id "dispatcher-error", detail.pipeline_error), shown
    on stderr, and blocks a PreToolUse."""
    try:
        return v1dispatch.run(event, payload, single_guard, loaded)
    except Exception as exc:  # noqa: BLE001 -- the pipeline itself must not crash the hook
        import traceback  # noqa: PLC0415 -- error path only

        detail["pipeline_error"] = {
            "type": type(exc).__name__, "message": str(exc),
            "traceback": "".join(traceback.format_exception(exc))[-TRACEBACK_CHARS:]}
        msg = v1dispatch.infra_block_message(
            "the ULTHAR guard pipeline failed", f"{type(exc).__name__}: {exc}", event)
        if event == "PreToolUse":
            return v1dispatch.Outcome(io_mod.Result(2, "", msg), PIPELINE_ERROR_GUARD_ID)
        warning = "[ulthar] WARNING (not blocking this event): " + msg
        return v1dispatch.Outcome(io_mod.Result(0, "", warning), PIPELINE_ERROR_GUARD_ID)


def _db_failure(event: str, db_path: Path, exc: BaseException, row: dict,
                guard_result: io_mod.Result) -> io_mod.Result:
    reason = "the ULTHAR runtime database could not be written"
    row["detail"]["journaled"] = {"error": f"{type(exc).__name__}: {exc}"}
    if event == "PreToolUse":
        row.update(decision="deny", guard_id="dispatcher")
        row["detail"]["blocked_by"] = "runtime.db failure"
    lost = False
    try:
        where = (f"recorded in the fallback journal {journal_mod.append(db_path, row)}; the next "
                 "hook call that can open runtime.db replays it there")
    except OSError as jexc:
        lost = True
        where = (f"NOT recorded: the fallback journal {journal_mod.path_for(db_path)} could not "
                 f"be written either ({type(jexc).__name__}: {jexc})")
    msg = v1dispatch.infra_block_message(
        reason, f"{type(exc).__name__}: {exc} (db: {db_path})\n  this call's event was {where}",
        event)
    if event == "PreToolUse" or (lost and event == "PostToolUse"):
        return io_mod.Result(2, guard_result.stdout if event != "PreToolUse" else "", msg)
    warning = "[ulthar] WARNING (not blocking this event): " + msg
    stderr = (guard_result.stderr + "\n" if guard_result.stderr else "") + warning
    return io_mod.Result(guard_result.exit_code, guard_result.stdout, stderr)


def process(event: str, payload: dict, single_guard: str | None = None,
            loaded: tuple | None = None) -> Processed:
    """The whole dispatch for one call, minus writing the streams. `loaded` =
    (cfg, ctx) is the v1 suites' seam (see v1dispatch.prepare)."""
    db_path = db_mod.resolve_path()
    detail: dict = {}
    conn, db_error = None, None
    try:
        conn = _open_and_catch_up(event, payload, db_path, detail)
    except Exception as exc:  # noqa: BLE001 -- any database failure is journaled below
        db_error = exc
    outcome = _run_guards(event, payload, single_guard, loaded, detail)
    detail["guards_run"] = outcome.guards_run
    paths = written_paths(payload)
    post_hashes = ({p: blob_hash(p) for p in paths}
                   if event == "PostToolUse" and paths else None)
    if post_hashes and any(isinstance(v, dict) for v in post_hashes.values()):
        detail[HASH_FAILED] = [p for p, v in post_hashes.items() if isinstance(v, dict)]
    row = _event_row(event, payload, outcome, paths, post_hashes, detail)
    if conn is not None:
        try:
            _record(conn, row)
        except Exception as exc:  # noqa: BLE001
            db_error = exc
        finally:
            conn.close()
    if db_error is not None:
        return Processed(_db_failure(event, db_path, db_error, row, outcome.result),
                         outcome.guards_run)
    return Processed(outcome.result, outcome.guards_run)


def handle(event: str, payload: dict, single_guard: str | None) -> int:
    result = process(event, payload, single_guard).result
    if result.stdout:
        io_mod.write_stdout_bytes(result.stdout)
    if result.stderr:
        io_mod.write_stderr(result.stderr)
    return result.exit_code


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
