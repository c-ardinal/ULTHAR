"""Decision-candidate extraction in the background, hook side (DEC-01/02, R1-b AC-6/AC-18):
the extraction state in runtime.db and the launch of the worker (ulthar.worker).

The UserPromptSubmit hook saves the owner's message as an owner_msg Episode, records
the extraction as `pending` in runtime.db and launches the worker
(`ulthar extract --episode <id> --project-dir <dir>`) without waiting for it; the
worker marks the extraction `done` or `failed`.

Recursion: the worker and the extractor run with ULTHAR_EXTRACTOR=1. A hook call that
sees it (the extractor's own `claude -p` session) launches no extraction and stores
no memory -- that session's prompt is ours, not the owner's.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from . import store as store_mod

EXTRACTOR_ENV = "ULTHAR_EXTRACTOR"
MAIN_PY = Path(__file__).resolve().parent / "__main__.py"


def in_extractor() -> bool:
    return os.environ.get(EXTRACTOR_ENV) == "1"


def register(rt: sqlite3.Connection, project_id: str, session_id: str | None,
             episode_id: str) -> None:
    rt.execute("INSERT OR IGNORE INTO extractions (episode_id, project_id, session_id, status,"
               " started) VALUES (?,?,?, 'pending', ?)",
               (episode_id, project_id, session_id, store_mod.now_iso()))


def finish(rt: sqlite3.Connection, episode_id: str, status: str, detail: dict) -> None:
    rt.execute("UPDATE extractions SET status=?, finished=?, detail=? WHERE episode_id=?",
               (status, store_mod.now_iso(), json.dumps(detail, ensure_ascii=False), episode_id))


def _detach_flags(breakaway: bool = True) -> dict:
    """Not waited for, windowless, and at below-normal priority (inherited by the
    extractor it runs: background work must not take CPU from the session's hooks).

    Windows: CREATE_NO_WINDOW, never DETACHED_PROCESS (R1-b AC-18). A DETACHED_PROCESS
    worker has no console at all, so every console program it starts (the extractor,
    `claude -p`, their children) gets a NEW, visible console window -- it flooded the
    owner's desktop. CREATE_NO_WINDOW gives the worker a console without a window,
    which its console descendants inherit."""
    if os.name != "nt":
        return {"start_new_session": True}
    flags = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
             | subprocess.BELOW_NORMAL_PRIORITY_CLASS)
    return {"creationflags": flags | (subprocess.CREATE_BREAKAWAY_FROM_JOB if breakaway else 0)}


def launch(rt: sqlite3.Connection, episode_id: str, project_dir: str) -> None:
    """Starts the worker and returns without waiting. A failure to start is recorded
    as `failed` (the message then reaches subagents verbatim, DEC-03)."""
    argv = [sys.executable, "-S", str(MAIN_PY), "extract", "--episode", episode_id,
            "--project-dir", project_dir]
    kwargs = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                  stderr=subprocess.DEVNULL, close_fds=True, cwd=project_dir,
                  env=dict(os.environ, **{EXTRACTOR_ENV: "1"}))
    try:
        try:
            subprocess.Popen(argv, **kwargs, **_detach_flags())
        except PermissionError:
            if os.name != "nt":
                raise
            # the hook's job object forbids breakaway: start without it
            subprocess.Popen(argv, **kwargs, **_detach_flags(breakaway=False))
    except OSError as exc:
        finish(rt, episode_id, "failed", {"error": f"launch: {type(exc).__name__}: {exc}"})
