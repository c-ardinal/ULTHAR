"""Fallback journal (ulthar.journal) and the guard-pipeline error record (ulthar.hook):
  * concurrent appends while replays run lose no row;
  * a short os.write is completed, and a zero-byte write fails loudly;
  * torn/unparseable rows are quarantined and recorded, never deleted unread;
  * an unexpected guard-pipeline exception on a non-PreToolUse event is recorded
    (guard_id "dispatcher-error", type/message/traceback) and warned on stderr.
"""
from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
import time

from helpers import V2_ROOT, Probe, Sandbox

from ulthar import db as db_mod
from ulthar import hook
from ulthar import journal as journal_mod
from ulthar import v1dispatch

WRITERS = 4
ROWS_PER_WRITER = 150

WRITER_SCRIPT = """
import sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from ulthar import journal
from tests_row import row
db, tag, n = Path(sys.argv[2]), sys.argv[3], int(sys.argv[4])
for i in range(n):
    journal.append(db, row(f"{tag}-{i}"))
"""

ROW_MODULE = """
def row(jid):
    return {"jid": jid, "ts": "2026-11-10T00:00:00.000+00:00", "session_id": "jr",
            "agent_id": None, "agent_type": None, "hook": "PostToolUse", "tool": "Write",
            "decision": "allow", "guard_id": "dispatcher", "paths": None,
            "post_hashes": None, "detail": {"journaled": {"error": "test"}},
            "project_id": None, "transcript_path": None}
"""


def _row(jid: str) -> dict:
    scope: dict = {}
    exec(ROW_MODULE, scope)  # noqa: S102 -- the same row factory the writer processes use
    return scope["row"](jid)


def _leftovers(sb: Sandbox) -> list[str]:
    return sorted(p.name for p in sb.root.glob(sb.db.name + ".pending.jsonl*"))


def check_concurrent_append_during_replay(p: Probe) -> None:
    sb = Sandbox()
    try:
        (sb.root / "tests_row.py").write_text(ROW_MODULE, encoding="utf-8")
        script = sb.root / "writer.py"
        script.write_text(WRITER_SCRIPT, encoding="utf-8")
        env = dict(os.environ, PYTHONPATH=str(sb.root))
        conn = db_mod.connect(sb.db)
        procs = [subprocess.Popen([sys.executable, str(script), str(V2_ROOT), str(sb.db),
                                   f"w{w}", str(ROWS_PER_WRITER)], env=env)
                 for w in range(WRITERS)]
        replays = 0
        while any(proc.poll() is None for proc in procs):
            journal_mod.replay(conn, sb.db, hook.insert_row)
            replays += 1
        journal_mod.replay(conn, sb.db, hook.insert_row)
        conn.close()
        total = WRITERS * ROWS_PER_WRITER
        got = sb.query("SELECT COUNT(*) FROM journal_replayed")[0][0]
        events = sb.query("SELECT COUNT(*) FROM events WHERE session_id='jr'")[0][0]
        p.check(f"B2: {WRITERS} writers x {ROWS_PER_WRITER} appends during {replays} replays: "
                f"{events}/{total} rows replayed, none lost or duplicated",
                all(proc.returncode == 0 for proc in procs) and got == total and events == total,
                f"journal_replayed={got} events={events}")
        p.check("  ... and no journal or claim file is left", _leftovers(sb) == [],
                str(_leftovers(sb)))
    finally:
        sb.cleanup()


def check_short_writes(p: Probe) -> None:
    sb = Sandbox()
    real_write = os.write
    try:
        os.write = lambda fd, data: real_write(fd, bytes(data[:7]))  # 7 bytes per call
        journal_mod.append(sb.db, _row("short-1"))
        os.write = real_write
        lines = journal_mod.path_for(sb.db).read_text(encoding="utf-8").splitlines()
        p.check("B2: a short os.write is continued until the whole row is written",
                len(lines) == 1 and json.loads(lines[0])["jid"] == "short-1", str(lines))
        os.write = lambda fd, data: 0
        raised = ""
        try:
            journal_mod.append(sb.db, _row("short-2"))
        except OSError as exc:
            raised = str(exc)
        os.write = real_write
        p.check("B2: a write that makes no progress raises instead of claiming success",
                "short write" in raised, raised)
    finally:
        os.write = real_write
        sb.cleanup()


def check_torn_rows_are_quarantined(p: Probe) -> None:
    sb = Sandbox()
    try:
        good = [json.dumps(_row(f"ok-{i}")) for i in range(2)]
        torn = ['{"jid": "torn-1", "ts": "2026', "not json at all"]
        journal_mod.path_for(sb.db).write_text(
            good[0] + "\n" + torn[0] + "\n" + good[1] + "\n" + torn[1], encoding="utf-8")
        conn = db_mod.connect(sb.db)
        stats = journal_mod.replay(conn, sb.db, hook.insert_row, hook="SessionStart")
        conn.close()
        quarantined = journal_mod.quarantine_path(sb.db).read_text(encoding="utf-8").splitlines()
        gap = sb.query("SELECT hook, detail FROM events WHERE guard_id='journal'")
        p.check("B2: the readable rows are replayed", stats.get("rows") == 2, str(stats))
        p.check("B2: torn rows are kept, byte for byte, in the quarantine file",
                quarantined == torn, str(quarantined))
        p.check("  ... recorded as a guard_id 'journal' gap event naming the file",
                len(gap) == 1 and json.loads(gap[0][1])["lines"] == 2
                and "quarantine" in json.loads(gap[0][1]), str(gap))
        p.check("  ... and only then is the claim removed",
                _leftovers(sb) == [] and journal_mod.quarantine_path(sb.db).is_file(),
                str(_leftovers(sb)))
    finally:
        sb.cleanup()


CONTENTION_TIMEOUT_SEC = 1.0
CONTENTION_SLACK_SEC = 1.5


def _gives_up(acquire) -> tuple[bool, float, str]:
    """Runs `acquire` (expected to raise OSError at the deadline); returns
    (raised, elapsed seconds, message)."""
    started = time.monotonic()
    try:
        acquire()
    except OSError as exc:
        return True, time.monotonic() - started, str(exc)
    return False, time.monotonic() - started, ""


def _in_window(elapsed: float) -> bool:
    return CONTENTION_TIMEOUT_SEC <= elapsed <= CONTENTION_TIMEOUT_SEC + CONTENTION_SLACK_SEC


class _HeldFcntl:
    """Stub fcntl whose lock is always held by someone else."""
    LOCK_EX, LOCK_NB, LOCK_UN = 2, 4, 8

    def __init__(self) -> None:
        self.calls: list[int] = []

    def flock(self, _fd: int, op: int) -> None:
        self.calls.append(op)
        if op & self.LOCK_EX:
            raise BlockingIOError(11, "Resource temporarily unavailable")


def check_lock_contention_gives_up_at_deadline(p: Probe) -> None:
    sb = Sandbox()
    saved_timeout, saved_fcntl = journal_mod.LOCK_TIMEOUT_SEC, sys.modules.get("fcntl")
    try:
        journal_mod.LOCK_TIMEOUT_SEC = CONTENTION_TIMEOUT_SEC
        branch = "Windows (msvcrt)" if os.name == "nt" else "POSIX (fcntl)"
        with journal_mod._locked(sb.db):  # this platform's real lock, held
            raised, elapsed, msg = _gives_up(lambda: journal_mod.append(sb.db, _row("held")))
        p.check(f"lock: real {branch} branch -- append under a held lock raises OSError at the "
                f"{CONTENTION_TIMEOUT_SEC:.0f} s deadline ({elapsed:.2f} s)",
                raised and _in_window(elapsed) and "not acquired" in msg, msg)
        p.check("  ... and wrote nothing", not journal_mod.path_for(sb.db).exists())

        stub = _HeldFcntl()
        sys.modules["fcntl"] = stub

        def posix_acquire() -> None:
            with journal_mod._locked(sb.db, posix=True):
                pass
        raised, elapsed, msg = _gives_up(posix_acquire)
        p.check(f"lock: POSIX branch (stubbed fcntl) polls LOCK_EX|LOCK_NB and gives up at the "
                f"deadline ({elapsed:.2f} s, {len(stub.calls)} attempts)",
                raised and _in_window(elapsed) and len(stub.calls) > 1
                and all(op == stub.LOCK_EX | stub.LOCK_NB for op in stub.calls), msg)
    finally:
        journal_mod.LOCK_TIMEOUT_SEC = saved_timeout
        if saved_fcntl is None:
            sys.modules.pop("fcntl", None)
        else:
            sys.modules["fcntl"] = saved_fcntl
        sb.cleanup()


class _BrokenFcntl(_HeldFcntl):
    """Stub fcntl whose flock fails with a non-contention error (bad descriptor)."""

    def flock(self, _fd: int, op: int) -> None:
        self.calls.append(op)
        if op & self.LOCK_EX:
            raise OSError(errno.EBADF, "Bad file descriptor")


def check_non_contention_lock_error_is_not_retried(p: Probe) -> None:
    sb = Sandbox()
    saved_timeout, saved_fcntl = journal_mod.LOCK_TIMEOUT_SEC, sys.modules.get("fcntl")
    try:
        journal_mod.LOCK_TIMEOUT_SEC = CONTENTION_TIMEOUT_SEC
        stub = _BrokenFcntl()
        sys.modules["fcntl"] = stub

        def acquire() -> None:
            with journal_mod._locked(sb.db, posix=True):
                pass
        raised, elapsed, msg = _gives_up(acquire)
        p.check(f"lock: a non-contention OSError (EBADF) is raised at once, not retried "
                f"({elapsed:.3f} s, {len(stub.calls)} attempt)",
                raised and len(stub.calls) == 1 and elapsed < CONTENTION_TIMEOUT_SEC / 2
                and "Bad file descriptor" in msg, msg)
    finally:
        journal_mod.LOCK_TIMEOUT_SEC = saved_timeout
        if saved_fcntl is None:
            sys.modules.pop("fcntl", None)
        else:
            sys.modules["fcntl"] = saved_fcntl
        sb.cleanup()


def check_pipeline_exception_is_recorded(p: Probe) -> None:
    sb = Sandbox()
    saved_env, saved_run = os.environ.get("ULTHAR_RUNTIME_DB"), v1dispatch.run
    try:
        os.environ["ULTHAR_RUNTIME_DB"] = str(sb.db)

        def boom(*_args, **_kwargs):
            raise RuntimeError("injected pipeline fault")
        v1dispatch.run = boom
        out = hook.process("Stop", {"session_id": "n1", "cwd": str(sb.work),
                                    "stop_hook_active": False})
        pre = hook.process("PreToolUse", {"session_id": "n1-pre", "cwd": str(sb.work),
                                          "tool_name": "Bash", "tool_input": {"command": "ls"}})
        v1dispatch.run = saved_run
        rows = sb.query("SELECT decision, guard_id, detail FROM events WHERE session_id='n1'")
        detail = json.loads(rows[-1][2]) if rows else {}
        err = detail.get("pipeline_error", {})
        p.check("N1: a pipeline exception on Stop warns on stderr (exit 0, not a silent allow)",
                out.result.exit_code == 0 and "WARNING" in out.result.stderr
                and "injected pipeline fault" in out.result.stderr, out.result.stderr[:300])
        p.check("N1: ... and is recorded with guard_id 'dispatcher-error', type, message, traceback",
                bool(rows) and rows[-1][1] == "dispatcher-error"
                and err.get("type") == "RuntimeError"
                and err.get("message") == "injected pipeline fault"
                and "Traceback" in err.get("traceback", ""), str(rows))
        pre_rows = sb.query("SELECT decision, guard_id FROM events WHERE session_id='n1-pre'")
        p.check("N1: the same fault on PreToolUse blocks (exit 2) and is recorded as a deny",
                pre.result.exit_code == 2 and pre_rows == [("deny", "dispatcher-error")],
                f"rc={pre.result.exit_code} {pre_rows}")
    finally:
        v1dispatch.run = saved_run
        if saved_env is None:
            os.environ.pop("ULTHAR_RUNTIME_DB", None)
        else:
            os.environ["ULTHAR_RUNTIME_DB"] = saved_env
        sb.cleanup()


def main() -> int:
    p = Probe()
    started = time.perf_counter()
    check_concurrent_append_during_replay(p)
    check_short_writes(p)
    check_torn_rows_are_quarantined(p)
    check_lock_contention_gives_up_at_deadline(p)
    check_non_contention_lock_error_is_not_retried(p)
    check_pipeline_exception_is_recorded(p)
    print(f"({time.perf_counter() - started:.1f}s)")
    return p.result()


if __name__ == "__main__":
    sys.exit(main())
