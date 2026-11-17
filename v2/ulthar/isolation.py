"""Isolation and degradation on the hook path (R1-e AC-1, AC-2, AC-3, AC-5, AC-9, AC-13).

PreToolUse (`check`, after the store guard, before the unit scope and pack checks):
  * the monitor's token file and port (AC-13): a Read / Grep / NotebookRead of the token
    file (or a Grep over a directory holding it), a shell command whose text names the
    token file or a request to the monitor's port, and `ulthar monitor` itself (the
    owner's command: it prints the token) are blocked for every session;
  * freeze (AC-13): while the project is frozen (the monitor), every session's product
    writes (R1-c AC-11's routes) and state-changing ulthar CLI calls are blocked;
  * relay due (AC-3): while relay_due is set, only read tools, read-only shell commands,
    read-only ulthar calls and `ulthar relay` pass -- for the window and its subagents;
  * ingest failure (AC-5): when this call's transcript ingest failed, the same set only;
  * the window lease (AC-1): when another session holds the project's valid lease (or the
    lease is in handover from this session, ulthar.lease), this session's product
    writes and state-changing ulthar calls are blocked, naming the holder and expiry. A
    subagent's call carries its parent's session_id (P-18), so it is judged as the
    parent. Reads pass.
The lease, relay and ingest rules do not apply to the extractor's own session.
The flags are read from runtime.db only (project_flags); the context usage behind
relay_due (AC-2) is recorded by the transcript ingest (`note_context`), never read here.

TRUST-06 (AC-9): `intent_key` of every PreToolUse (the canonical target path of a
write -- symlinks and junctions resolved, else the command's root and first argument, else an MCP tool's name); `suspect`
links an allowed call to a block of the same intent in the same session tree within
SUSPECT_SEC by another tool or another agent_id, records a suspicion and queues an
important notification (ulthar.notify).
Known limit (spec): two sessions racing for an unheld lease may both pass this check
before one of them records the acquisition.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta
from pathlib import Path

from . import lease as lease_mod
from . import notify
from . import product
from . import shellwords
from . import store as store_mod

LEASE_GUARD = "window_lease"
RELAY_GUARD = "relay_due"
FREEZE_GUARD = "frozen"
MONITOR_GUARD = "monitor_token"
INGEST_GUARD = "ingest_failed"
LEASE_GATED = "lease_gated"
READ_TOOLS = frozenset({"Read", "Glob", "Grep", "LS", "NotebookRead", "WebFetch", "WebSearch"})
# not reads, but what the relay conditions themselves may need (answer the owner's
# question, stop a running subagent): allowed while relay_due is set
RELAY_TOOLS = frozenset({"AskUserQuestion", "TaskOutput", "TaskStop", "KillShell"})
SHELL_TOOLS = frozenset({"Bash", "PowerShell"})
WRITE_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
PATH_KEYS = ("file_path", "notebook_path", "path")
ULTHAR_READ = frozenset({("status",), ("decide", "list"), ("fact", "list"), ("issue", "list"),
                         ("unit", "list"), ("unit", "show"), ("contract", "lint"),
                         ("pack", "list"), ("cap", "list"), ("inject",), ("store", "verify")})
HELP = frozenset({"-h", "--help"})
TOKEN_FILE = "monitor.token"
DEFAULT_WINDOW = 200_000  # spec 事前決定 7
DEFAULT_PCT = 60  # U-02
PCT_RANGE = (50, 70)  # both ends allowed (U-02)
SUSPECT_SEC = 30 * 60  # U-09


class RelayConfigError(store_mod.StoreError):
    pass


# ---------------------------------------------------------------------------- config

def relay_settings(cfg: dict) -> tuple[int, float]:
    """(context_window, threshold percent) of `.claude/ulthar.json` "relay"; raises
    RelayConfigError for a malformed value or a threshold outside 50..70."""
    raw = cfg.get("relay") or {}
    if not isinstance(raw, dict):
        raise RelayConfigError(".claude/ulthar.json: relay must be an object")
    window = raw.get("context_window", DEFAULT_WINDOW)
    pct = raw.get("threshold_pct", DEFAULT_PCT)
    if isinstance(window, bool) or not isinstance(window, int) or window <= 0:
        raise RelayConfigError(".claude/ulthar.json: relay.context_window must be a positive "
                               "integer (tokens)")
    if isinstance(pct, bool) or not isinstance(pct, (int, float)) or not (
            PCT_RANGE[0] <= pct <= PCT_RANGE[1]):
        raise RelayConfigError(f".claude/ulthar.json: relay.threshold_pct must be a number in "
                               f"{PCT_RANGE[0]}..{PCT_RANGE[1]} (both included), not {pct!r}")
    return window, float(pct)


# ---------------------------------------------------------------------------- flags

def flags(rt, project_id: str) -> tuple[str | None, bool]:
    row = rt.execute("SELECT relay_due, frozen FROM project_flags WHERE project_id=?",
                     (project_id,)).fetchone()
    return (row[0], bool(row[1])) if row else (None, False)


def set_relay_due(rt, project_id: str, reason: str) -> None:
    """Raises relay_due (kept when already set). The caller owns the transaction."""
    rt.execute("INSERT INTO project_flags (project_id, relay_due, relay_due_at) VALUES (?,?,?)"
               " ON CONFLICT(project_id) DO UPDATE SET relay_due=COALESCE(relay_due,"
               " excluded.relay_due), relay_due_at=COALESCE(relay_due_at, excluded.relay_due_at)",
               (project_id, reason, store_mod.now_iso()))


def clear_relay_due(rt, project_id: str) -> None:
    rt.execute("UPDATE project_flags SET relay_due=NULL, relay_due_at=NULL WHERE project_id=?",
               (project_id,))


def set_frozen(rt, project_id: str, frozen: bool) -> None:
    rt.execute("INSERT INTO project_flags (project_id, frozen, frozen_at) VALUES (?,?,?)"
               " ON CONFLICT(project_id) DO UPDATE SET frozen=excluded.frozen,"
               " frozen_at=excluded.frozen_at", (project_id, int(frozen), store_mod.now_iso()))


def trigger_once(rt, op_id: str, project_id: str, reason: str) -> None:
    """AC-2 (a): relay_due for a unit operation, at most once per operation (a recovery
    re-applying the operation never raises it again after a relay cleared it)."""
    cur = rt.execute("INSERT OR IGNORE INTO relay_triggers (op_id, project_id, reason, ts)"
                     " VALUES (?,?,?,?)", (op_id, project_id, reason, store_mod.now_iso()))
    if cur.rowcount == 1:
        set_relay_due(rt, project_id, reason)


def note_context(rt, store, session_id: str, used: int | None, compactions: int) -> None:
    """CORE-07 / AC-2 (b), inside the ingest's transaction: the session's latest API usage
    (input + cache creation + cache read of the newest main-chain assistant line) and its
    compaction count; relay_due when the lease holder's usage exceeds the threshold. A
    malformed relay config raises nothing here: PreToolUse refuses it (`check`)."""
    rt.execute("INSERT OR IGNORE INTO sessions (session_id, started) VALUES (?,?)",
               (session_id, store_mod.now_iso()))
    try:
        window, pct = relay_settings(store.config)
    except RelayConfigError:
        window, pct = None, None
    rt.execute("UPDATE sessions SET ctx_used=COALESCE(?, ctx_used), ctx_window=COALESCE(?, ctx_window),"
               " compactions=COALESCE(compactions, 0) + ? WHERE session_id=?",
               (used, window, compactions, session_id))
    if used is None or window is None or used * 100 <= pct * window:
        return
    held = lease_mod.state(rt, store.project_id)
    if held is not None and held["session_id"] != session_id:
        return  # a former window's late usage never stops its successor
    set_relay_due(rt, store.project_id, f"context usage {used}/{window} tokens "
                                        f"({used * 100 / window:.1f}% > {pct:g}%)")


# ---------------------------------------------------------------------------- classification

def ulthar_kind(args: list[str]) -> str:
    """"read", "relay", "monitor" or "write" (state-changing; unknown = write)."""
    words = [a.casefold() for a in args]
    if not words or HELP & set(words):
        return "read"
    if words[0] == "relay":
        return "relay"
    if words[0] == "monitor":
        return "monitor"
    head = tuple(w for w in words[:2] if not w.startswith("-"))
    if head[:1] in ULTHAR_READ or head[:2] in ULTHAR_READ:
        if head[:2] == ("unit", "show") and "--ask" in words:
            return "write"  # records the owner question
        return "read"
    return "write"


def _ulthar_kinds(payload: dict) -> set[str]:
    if payload.get("tool_name") not in SHELL_TOOLS:
        return set()
    command = str((payload.get("tool_input") or {}).get("command") or "")
    return {ulthar_kind(a) for a in shellwords.ulthar_calls(command, payload.get("tool_name"))}


def _reads_only(payload: dict, kinds: set[str], read_only_shell) -> bool:
    tool = payload.get("tool_name")
    if tool in READ_TOOLS or tool in RELAY_TOOLS:
        return True
    if tool not in SHELL_TOOLS:
        return False
    command = str((payload.get("tool_input") or {}).get("command") or "")
    return read_only_shell(command, tool) and kinds <= {"read", "relay"}


# ---------------------------------------------------------------------------- monitor token

def token_path() -> Path:
    return store_mod.state_dir() / TOKEN_FILE


def monitor_port(rt) -> int | None:
    row = rt.execute("SELECT port FROM monitor WHERE id=1").fetchone()
    return row[0] if row else None


def _monitor_access(payload: dict, rt, memo: dict | None) -> str | None:
    tool = payload.get("tool_name")
    ti = payload.get("tool_input") or {}
    cwd = str(payload.get("cwd") or os.getcwd())
    why = None
    if tool in ("Read", "Grep", "NotebookRead"):
        token = product.forms(str(token_path()), memo)
        for key in PATH_KEYS:
            raw = ti.get(key)
            if not isinstance(raw, str) or not raw:
                continue
            got = product.forms(str(Path(raw) if Path(raw).is_absolute() else Path(cwd) / raw), memo)
            if got & token or (tool == "Grep" and any(
                    t.startswith(g + "/") for g in got for t in token)):
                why = f"{tool} of {raw} reaches the monitor's token file"
    text = " ".join(str(v) for v in ti.values() if isinstance(v, str)).replace("\\", "/").casefold()
    if why is None and tool in SHELL_TOOLS | {"WebFetch"}:
        port = monitor_port(rt)
        if TOKEN_FILE in text:
            why = "the command names the monitor's token file"
        elif port and re.search(rf"(?:127\.0\.0\.1|localhost|\[::1\]|0\.0\.0\.0):{port}(?!\d)", text):
            why = f"the command sends a request to the monitor's port {port}"
    if why is None and "monitor" in _ulthar_kinds(payload):
        why = "`ulthar monitor` is the owner's command (it prints the monitor's token)"
    if why is None:
        return None
    return (f"BLOCKED (ulthar monitor, MON-03): {why}. The monitor and its operations are the "
            "owner's; Claude never reads its token or calls it.")


# ---------------------------------------------------------------------------- the check

def check(payload: dict, rt, store_of, writes: list, detail: dict, read_only_shell,
          in_extractor: bool, memo: dict | None = None) -> tuple[str | None, str | None]:
    """(block message, guard id) or (None, None). `writes`: R1-c AC-11's product writes of
    this call ([] when the project has no .claude/ulthar.json); `read_only_shell(command,
    tool)`: the memory step's read-only shell test."""
    msg = _monitor_access(payload, rt, memo)
    if msg:
        return msg, MONITOR_GUARD
    store = store_of()
    configured = store.repo is not None and (store.repo / store_mod.PROJECT_CONFIG).is_file()
    if configured:
        relay_settings(store.config)  # fail closed on a malformed relay config
    kinds = _ulthar_kinds(payload)
    state_cli = bool(kinds - {"read"})
    gated = bool(writes) or (configured and state_cli)
    relay_due, frozen = flags(rt, store.project_id)
    if frozen and (bool(writes) or state_cli):
        return ("BLOCKED (ulthar frozen, MON-03): the owner froze this project from the monitor: "
                "product writes and state-changing ulthar commands wait until the owner "
                "resumes it. Reads are allowed."), FREEZE_GUARD
    if in_extractor:
        return None, None
    reads_only = _reads_only(payload, kinds, read_only_shell)
    if relay_due and not reads_only:
        return (f"BLOCKED (ulthar relay due, SES-04): a relay is due ({relay_due}). Only read "
                "tools and `ulthar relay` run now: finish nothing new, then run `ulthar relay` "
                "(it checks the handover conditions and starts the next window)."), RELAY_GUARD
    if detail.get("ingest_error") and not reads_only:
        return (f"BLOCKED (ulthar recovery, SES-05): this session's transcript could not be "
                f"ingested ({detail['ingest_error']}); writes wait until it is ingested "
                "(CORE-07). Reads are allowed; retry once the transcript is readable."), INGEST_GUARD
    if not gated:
        return None, None
    msg = lease_block(rt, store.project_id, str(payload.get("session_id") or "(none)"))
    if msg:
        return msg, LEASE_GUARD
    detail[LEASE_GATED] = True  # decided again under the write lock (ulthar.hook._record)
    return None, None


def lease_block(rt, project_id: str, session: str) -> str | None:
    """Why `session` may not write now (another session's valid lease, or a handover from
    this session), or None. Called by `check` and -- for the final, serialized decision --
    again inside the BEGIN IMMEDIATE transaction that acquires or renews the lease, so two
    sessions racing for a free (or handed-over) lease cannot both be allowed."""
    held = lease_mod.state(rt, project_id)
    if held is None or held["session_id"] == session or (
            held["session_id"] is None and held["handover_from"] != session):
        return None
    who = (f"in handover from this session to its successor (`ulthar relay`)"
           if held["session_id"] is None else f"held by session {held['session_id']}")
    return (f"BLOCKED (ulthar window lease, SES-02): the project's window lease is {who} until "
            f"{held['expires_at']} (fencing token {held['fence_token']}). Only the holding "
            "window and its subagents write product files or change ulthar's state; this "
            "window may read. Wait for the lease to lapse, or work in the holding window.")


# ---------------------------------------------------------------------------- TRUST-06

def intent_key(payload: dict) -> str | None:
    tool = str(payload.get("tool_name") or "")
    ti = payload.get("tool_input") or {}
    cwd = str(payload.get("cwd") or os.getcwd())
    if tool in WRITE_TOOLS or tool in SHELL_TOOLS:
        for raw, _sources in product.targets(payload):
            p = Path(raw)
            # the canonical target: symlinks / junctions of the existing part resolved, the
            # part not created yet appended as written (product.resolved), then folded
            full = os.path.abspath(p if p.is_absolute() else Path(cwd) / p)
            return "path:" + product.fold(product.resolved(full))
    if tool in SHELL_TOOLS:
        command = str(ti.get("command") or "")
        for seg in shellwords.segments(shellwords.tokenize(
                command, shellwords.ESCAPES.get(tool, "\\"))):
            words = seg
            while words and re.match(r"^\w+=", words[0]):
                words = words[1:]
            if words:
                return "cmd:" + " ".join([shellwords.basename(words[0])] + words[1:2]).casefold()
        return None
    if tool.startswith("mcp__"):
        return "mcp:" + tool
    return None


def suspect(rt, event_id: int, row: dict, project_id: str) -> list[str]:
    """After an allowed PreToolUse with an intent key is stored (same transaction): a block
    of the same intent in the same session tree within SUSPECT_SEC by another tool or
    another agent_id -> a suspicion and an important notification. Returns the
    notification ids to send after the commit."""
    since = (datetime.fromisoformat(row["ts"]) - timedelta(seconds=SUSPECT_SEC)).isoformat(
        timespec="milliseconds")
    hit = rt.execute(
        "SELECT id, tool, agent_id, guard_id FROM events WHERE session_id=? AND intent_key=?"
        " AND hook='PreToolUse' AND decision='deny' AND ts>=? AND id<?"
        " AND (tool!=? OR COALESCE(agent_id, '')!=?)"
        " AND id NOT IN (SELECT block_event_id FROM suspicions) ORDER BY id DESC LIMIT 1",
        (row["session_id"], row["intent_key"], since, event_id, row["tool"],
         row["agent_id"] or "")).fetchone()
    if hit is None:
        return []
    rt.execute("INSERT INTO suspicions (block_event_id, allow_event_id, intent_key, notified)"
               " VALUES (?,?,?,1)", (hit[0], event_id, row["intent_key"]))
    text = (f"回避の疑い（TRUST-06）: {row['intent_key']} は {hit[1]}（agent {hit[2] or '窓口'}、"
            f"ガード {hit[3]}）で遮断された後、{row['tool']}（agent {row['agent_id'] or '窓口'}）で"
            f"許可されました。セッション {row['session_id']}、遮断 #{hit[0]} → 許可 #{event_id}。")
    return [notify.queue(rt, project_id, "evasion_suspected", text, important=True)]
