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

Episodes go to the project's knowledge store (`<knowledge_root>/.ulthar/episodes.jsonl`,
ulthar.store; R1-b AC-2), written in one locked store transaction before the cursor
commits: a store failure leaves the cursor where it was, so the lines are re-read. The
dedupe key (session, line uuid, kind, part) lives in the store's index. An owner_msg
already saved from the UserPromptSubmit payload (ulthar.memhook) is recognised by
its promptId and text and not written twice.
"""
from __future__ import annotations

import json
import sqlite3
import zlib
from pathlib import Path

from . import store as store_mod
from .store import now_iso  # noqa: F401 -- ulthar.hook and others use ingest.now_iso

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


def prompt_dedupe_key(prompt_id: str, text: str) -> str:
    """The key of an owner_msg saved from the UserPromptSubmit payload; the same message
    read later from the transcript (its record carries `promptId`) matches it."""
    return f"prompt:{prompt_id}:{store_mod.text_key(text)}"


def is_owner_text(text: str) -> bool:
    return not text.startswith(TASK_NOTIFICATION_PREFIX) and not text.startswith(
        OWNER_MSG_EXCLUDED_PREFIXES)


def ingest(conn: sqlite3.Connection, session_id: str, transcript_path: str,
           hook: str = "ingest", *, store: store_mod.Store, on_tx=None) -> dict:
    """Ingests new complete lines; returns counts of new Episodes by kind, plus
    'lines' consumed and 'gaps' detected. A missing transcript is a no-op. `on_tx(tx)`,
    if given, also runs inside the store transaction (after this call's Episodes)."""
    path = Path(transcript_path)
    counts = {k: 0 for k in KINDS} | {"lines": 0, "gaps": []}
    if not path.is_file():
        return counts
    key = str(path)
    extractor = _Extractor(conn, session_id)
    found: list[tuple] = []
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
                    try:
                        found += _line_episodes(extractor, key, raw, line_no + 1)
                    except _Malformed as bad:
                        _malformed_gap(conn, session_id, hook, key, line_no + 1, offset,
                                       str(bad), counts)
                        break  # the cursor stays before it: retried on every later call
                    line_no += 1
                    offset += len(raw)
                    tail_len, tail_sha = len(raw), _line_sig(raw)
                    counts["lines"] += 1
        if found or on_tx is not None:
            _store_episodes(conn, store, session_id, hook, found, counts, on_tx)
        conn.execute(
            "INSERT INTO ingest_cursors VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(session_id, transcript_path) DO UPDATE SET byte_offset=excluded.byte_offset,"
            " line_no=excluded.line_no, tail_len=excluded.tail_len, tail_sha=excluded.tail_sha",
            (session_id, key, offset, line_no, tail_len, tail_sha))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        store.release()  # a deferred manifest stays for the next open; the lock goes
        raise
    if found or on_tx is not None:
        # the store's chain rows were part of the transaction just committed: only now
        # may its manifest go (a rollback above leaves it, and the next open re-records)
        store.finalize(conn)
    return counts


class _Malformed(Exception):
    pass


def _malformed_gap(conn, session_id: str, hook: str, key: str, line_no: int, offset: int,
                   reason: str, counts: dict) -> None:
    """A complete line that is not a JSON object is never stepped over (it could be an
    owner message): it is a gap, recorded once per position, and ingestion stops there."""
    detail = {"gap": "malformed", "transcript_path": key, "line": line_no, "offset": offset,
              "reason": reason[:200]}
    last = conn.execute("SELECT detail FROM events WHERE session_id=? AND guard_id=?"
                        " ORDER BY id DESC LIMIT 1", (session_id, INGEST_GUARD_ID)).fetchone()
    previous = json.loads(last[0]) if last and last[0] else {}
    if (previous.get("gap"), previous.get("transcript_path"), previous.get("offset")) != (
            "malformed", key, offset):
        record_gap(conn, session_id, hook, detail)
    counts["gaps"].append(detail)


def _line_episodes(extractor: _Extractor, key: str, raw: bytes, line_no: int) -> list[tuple]:
    try:
        rec = json.loads(raw)
    except ValueError as exc:
        raise _Malformed(f"not JSON: {exc}") from None
    if not isinstance(rec, dict):
        raise _Malformed(f"not a JSON object: {type(rec).__name__}")
    episodes = extractor.extract(rec)
    if not episodes:
        return []
    ident = rec.get("uuid") or "L" + _line_sig(raw)
    content = (rec.get("message") or {}).get("content")
    out = []
    for kind, text, part in episodes:
        meta = {}
        if kind in ("ask", "answer") and isinstance(content, list):
            block = content[part]
            meta["tool_use_id"] = block.get("id") if kind == "ask" else block.get("tool_use_id")
        out.append((kind, text, f"{ident}:{kind}:{part}", f"{key}:{line_no}", rec, meta))
    return out


def _store_episodes(conn: sqlite3.Connection, store: store_mod.Store, session_id: str, hook: str,
                    found: list[tuple], counts: dict, on_tx=None) -> None:
    with store.transaction({"session_id": session_id, "hook": hook}, conn) as tx:
        for kind, text, dedupe, ref, rec, meta in found:
            if tx.has_dedupe("episode", session_id, dedupe):
                continue
            prompt_id = rec.get("promptId")
            if kind == "owner_msg" and isinstance(prompt_id, str) and tx.has_dedupe(
                    "episode", session_id, prompt_dedupe_key(prompt_id, text)):
                continue
            tx.add("episode", {"session_id": session_id, "kind": kind, "unit_id": None,
                               "text": text, "transcript_ref": ref,
                               "event_ts": rec.get("timestamp"), **meta}, dedupe_key=dedupe)
            counts[kind] += 1
        if on_tx is not None:
            on_tx(tx)
