"""Notifications, the runtime.db side (AUT-03, R1-e AC-10): what the hooks and the CLI
queue, when an important one is sent again, and the launch of the sender.

Every notification is a `notifications` row (nid, project, channel = the adapter, kind,
text, important, created ts, retries, last_sent_at, sent_ok, detail, acked_by_event_id).
Sending happens in a detached, windowless background process (`ulthar notify-send`,
ulthar.notifier: the Notifier port and its adapters), so no hook waits on the network;
it records the result on the row. Important ones (awaiting an answer, halted, an
evasion suspicion, a degraded capability) are sent again every RESEND_SEC, at most
MAX_RESENDS times, until the owner acts on the project -- an owner prompt, an
AskUserQuestion answer or a monitor operation (`owner_actions`) after the
notification; a delivered send is never taken as read. Whether a resend is due is
decided by any hook call of the project, at most once per CHECK_SEC (`due`).
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

from . import extract
from . import store as store_mod

RESEND_SEC = 30 * 60  # U-09
MAX_RESENDS = 3  # U-09
CHECK_SEC = 60
MAIN_PY = Path(__file__).resolve().parent / "__main__.py"
DEFAULT_ADAPTER = "ntfy"


def _ago(at: str, seconds: float) -> str:
    return (datetime.fromisoformat(at) - timedelta(seconds=seconds)).isoformat(timespec="milliseconds")


def adapter_name(cfg: dict) -> str:
    raw = (cfg.get("notify") or {}) if isinstance(cfg.get("notify"), dict) else {}
    return str(raw.get("adapter") or DEFAULT_ADAPTER)


def queue(rt, project_id: str, kind: str, text: str, important: bool,
          channel: str = DEFAULT_ADAPTER) -> str:
    """A new notification row (sent by `spawn` after the caller's commit)."""
    nid = "N-" + os.urandom(6).hex()
    now = store_mod.now_iso()
    rt.execute("INSERT INTO notifications (nid, project_id, channel, kind, sent_ok,"
               " acked_by_event_id, retries, ts, important, text, last_sent_at)"
               " VALUES (?,?,?,?,NULL,NULL,0,?,?,?,?)",
               (nid, project_id, channel, kind, now, int(important), text, now))
    return nid


def due(rt, project_id: str, at: str) -> list[str]:
    """Inside a write transaction, at most once per CHECK_SEC per project: marks important
    notifications read by a later owner action, and returns (counting the resend) the
    unread ones whose last send is RESEND_SEC old and that have resends left."""
    row = rt.execute("SELECT resend_checked_at FROM project_flags WHERE project_id=?",
                     (project_id,)).fetchone()
    if row is not None and row[0] and row[0] > _ago(at, CHECK_SEC):
        return []
    rt.execute("INSERT INTO project_flags (project_id, resend_checked_at) VALUES (?,?)"
               " ON CONFLICT(project_id) DO UPDATE SET resend_checked_at=excluded.resend_checked_at",
               (project_id, at))
    rt.execute("UPDATE notifications SET acked_by_event_id=(SELECT COALESCE(a.event_id, -a.id)"
               " FROM owner_actions a WHERE a.project_id=notifications.project_id"
               " AND a.ts>notifications.ts ORDER BY a.id LIMIT 1)"
               " WHERE project_id=? AND important=1 AND acked_by_event_id IS NULL AND EXISTS ("
               " SELECT 1 FROM owner_actions a WHERE a.project_id=notifications.project_id"
               " AND a.ts>notifications.ts)", (project_id,))
    nids = [r[0] for r in rt.execute(
        "SELECT nid FROM notifications WHERE project_id=? AND important=1"
        " AND acked_by_event_id IS NULL AND COALESCE(retries, 0)<? AND last_sent_at<=?",
        (project_id, MAX_RESENDS, _ago(at, RESEND_SEC))).fetchall()]
    for nid in nids:
        rt.execute("UPDATE notifications SET retries=COALESCE(retries, 0)+1, last_sent_at=?"
                   " WHERE nid=?", (at, nid))
    return nids


def spawn(db_path: Path, nid: str, project_dir: str) -> str | None:
    """Starts the sender for `nid` without waiting (windowless, detached as the
    extraction worker is, ulthar.extract). Returns the error text when it cannot start."""
    argv = [sys.executable, "-S", str(MAIN_PY), "notify-send", "--nid", nid,
            "--project-dir", project_dir, "--db", str(db_path)]
    kwargs = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                  stderr=subprocess.DEVNULL, close_fds=True, cwd=project_dir)
    try:
        try:
            subprocess.Popen(argv, **kwargs, **extract._detach_flags())
        except PermissionError:
            if os.name != "nt":
                raise
            subprocess.Popen(argv, **kwargs, **extract._detach_flags(breakaway=False))
    except OSError as exc:
        return f"the sender could not start: {type(exc).__name__}: {exc}"
    return None


def spawn_all(rt, db_path: Path, nids: list[str], project_dir: str) -> None:
    """After the commit that stored `nids`: start their senders; a start failure is
    recorded on the row as a failed send (never silent)."""
    for nid in nids:
        err = spawn(db_path, nid, project_dir)
        if err:
            rt.execute("UPDATE notifications SET sent_ok=0, detail=? WHERE nid=?", (err, nid))


def send(rt, db_path: Path, project_id: str, kind: str, text: str, important: bool,
         project_dir: str, channel: str = DEFAULT_ADAPTER) -> str:
    """CLI: queue (its own commit) and start the sender."""
    nid = queue(rt, project_id, kind, text, important, channel)
    spawn_all(rt, db_path, [nid], project_dir)
    return nid
