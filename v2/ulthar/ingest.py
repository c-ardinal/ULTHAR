"""Transcript ingestion (CORE-07, MEM-07/08): read a session transcript from the last
recorded position and store Episodes.

Position is (session_id, transcript_path) -> (byte offset, line number, signature of
the last consumed line: CRC-32 + length -- zlib is built in, hashlib would add
~10 ms of OpenSSL load to every hook call). A gap is recorded in `events` (guard_id "ingest") when the file
is shorter than the stored offset ("truncated") or the line ending at the stored
offset is no longer the one consumed ("discontinuous"); ingestion then restarts at
byte 0 and the per-line dedupe key keeps already stored Episodes from repeating.
Only complete ('\\n'-terminated) lines are consumed, so a line still being written
is picked up by the next call.
"""
from __future__ import annotations

import json
import sqlite3
import zlib
from datetime import datetime, timezone
from pathlib import Path

OWNER_MSG_EXCLUDED_PREFIXES = (
    "<task-notification", "<system-reminder", "[Request interrupted", "Caveat:",
    "<command-", "<local-command", "This session is being continued",
)
TASK_NOTIFICATION_PREFIX = "<task-notification"
ASK_TOOLS = frozenset({"AskUserQuestion"})
AGENT_TOOLS = frozenset({"Agent", "Task"})
ASYNC_LAUNCH_MARKER = "Async agent launched"
INGEST_GUARD_ID = "ingest"
NO_TOOL = "(none)"
KINDS = ("owner_msg", "ask", "answer", "compact_summary", "agent_report")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _texts(content) -> list[str]:
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [b.get("text") or "" for b in content
                if isinstance(b, dict) and b.get("type") == "text"]
    return []


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(_texts(content))


class _Extractor:
    """Turns one transcript record into Episode tuples (kind, text, part)."""

    def __init__(self, conn: sqlite3.Connection, session_id: str):
        self.conn = conn
        self.session_id = session_id

    def _tool_name(self, tool_use_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT name FROM tool_names WHERE session_id=? AND tool_use_id=?",
            (self.session_id, tool_use_id)).fetchone()
        return row[0] if row else None

    def extract(self, rec: dict) -> list[tuple[str, str, int]]:
        if not isinstance(rec, dict) or rec.get("isSidechain") is True:
            return []
        rtype = rec.get("type")
        content = (rec.get("message") or {}).get("content")
        if rtype == "assistant":
            return self._assistant(content)
        if rtype != "user":
            return []
        if rec.get("isCompactSummary") is True:
            return [("compact_summary", "\n".join(_texts(content)), 0)]
        out: list[tuple[str, str, int]] = []
        if rec.get("isMeta") is not True:
            for part, text in enumerate(_texts(content)):
                if text.startswith(TASK_NOTIFICATION_PREFIX):
                    out.append(("agent_report", text, part))
                elif not text.startswith(OWNER_MSG_EXCLUDED_PREFIXES):
                    out.append(("owner_msg", text, part))
        if isinstance(content, list):
            out.extend(self._tool_results(content))
        return out

    def _assistant(self, content) -> list[tuple[str, str, int]]:
        out = []
        if not isinstance(content, list):
            return out
        for part, block in enumerate(content):
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name = block.get("name")
            if name not in ASK_TOOLS and name not in AGENT_TOOLS:
                continue
            self.conn.execute("INSERT OR IGNORE INTO tool_names VALUES (?,?,?)",
                              (self.session_id, str(block.get("id")), name))
            if name in ASK_TOOLS:
                out.append(("ask", json.dumps(block.get("input"), ensure_ascii=False), part))
        return out

    def _tool_results(self, content: list) -> list[tuple[str, str, int]]:
        out = []
        for part, block in enumerate(content):
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            name = self._tool_name(str(block.get("tool_use_id")))
            text = _result_text(block.get("content"))
            if name in ASK_TOOLS:
                out.append(("answer", text, part))
            elif (name in AGENT_TOOLS and not block.get("is_error")
                  and not text.startswith(ASYNC_LAUNCH_MARKER)):
                out.append(("agent_report", text, part))
        return out


def record_gap(conn: sqlite3.Connection, session_id: str, hook: str, gap: dict) -> None:
    conn.execute(
        "INSERT INTO events (ts, session_id, hook, tool, decision, guard_id, detail) "
        "VALUES (?,?,?,?,?,?,?)",
        (now_iso(), session_id, hook, NO_TOOL, "allow", INGEST_GUARD_ID,
         json.dumps(gap, ensure_ascii=False)))


def _line_sig(raw: bytes) -> str:
    return f"{zlib.crc32(raw):08x}-{len(raw)}"


def _detect_gap(fh, cursor: tuple, size: int) -> str | None:
    offset, _line_no, tail_len, tail_sha = cursor
    if size < offset:
        return "truncated"
    if tail_len:
        fh.seek(offset - tail_len)
        if _line_sig(fh.read(tail_len)) != tail_sha:
            return "discontinuous"
    return None


def ingest(conn: sqlite3.Connection, session_id: str, transcript_path: str,
           hook: str = "ingest") -> dict:
    """Ingests new complete lines; returns counts of new Episodes by kind, plus
    'lines' consumed and 'gaps' detected. A missing transcript is a no-op."""
    path = Path(transcript_path)
    counts = {k: 0 for k in KINDS} | {"lines": 0, "gaps": []}
    if not path.is_file():
        return counts
    key = str(path)
    extractor = _Extractor(conn, session_id)
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT byte_offset, line_no, tail_len, tail_sha FROM ingest_cursors "
            "WHERE session_id=? AND transcript_path=?", (session_id, key)).fetchone()
        cursor = tuple(row) if row else (0, 0, 0, "")
        with open(path, "rb") as fh:
            size = fh.seek(0, 2)
            gap = _detect_gap(fh, cursor, size) if row else None
            if gap:
                detail = {"gap": gap, "transcript_path": key, "stored_offset": cursor[0],
                          "stored_line": cursor[1], "size": size}
                record_gap(conn, session_id, hook, detail)
                counts["gaps"].append(detail)
                cursor = (0, 0, 0, "")
            offset, line_no, tail_len, tail_sha = cursor
            if size > offset:
                fh.seek(offset)
                for raw in fh:
                    if not raw.endswith(b"\n"):
                        break
                    line_no += 1
                    offset += len(raw)
                    tail_len, tail_sha = len(raw), _line_sig(raw)
                    counts["lines"] += 1
                    _store_line(conn, extractor, session_id, key, raw, line_no, counts)
        conn.execute(
            "INSERT INTO ingest_cursors VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(session_id, transcript_path) DO UPDATE SET byte_offset=excluded.byte_offset,"
            " line_no=excluded.line_no, tail_len=excluded.tail_len, tail_sha=excluded.tail_sha",
            (session_id, key, offset, line_no, tail_len, tail_sha))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return counts


def _store_line(conn, extractor: _Extractor, session_id: str, key: str, raw: bytes,
                line_no: int, counts: dict) -> None:
    try:
        rec = json.loads(raw)
    except ValueError:
        return
    episodes = extractor.extract(rec)
    if not episodes:
        return
    ident = rec.get("uuid") or "L" + _line_sig(raw)
    for kind, text, part in episodes:
        cur = conn.execute(
            "INSERT OR IGNORE INTO episodes (session_id, kind, text, transcript_ref, ts, dedupe_key)"
            " VALUES (?,?,?,?,?,?)",
            (session_id, kind, text, f"{key}:{line_no}", rec.get("timestamp"),
             f"{ident}:{kind}:{part}"))
        counts[kind] += cur.rowcount
