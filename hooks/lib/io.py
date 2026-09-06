"""hooks/lib/io.py -- process I/O, encoding, structured Result type.

Kills the identical `[Console]::In.ReadToEnd() | ConvertFrom-Json` + `catch { exit 0 }`
boilerplate present in all ten PowerShell guards (CONTRACT.md #2).

Encoding rules (mandatory, CONTRACT.md #2 / I-4):
  - Every open() passes an explicit encoding=. Never rely on the platform default --
    on Windows with a non-UTF-8 ANSI codepage (e.g. a Japanese-locale install), the
    platform default is a legacy codepage such as cp932, not UTF-8.
  - READS of state/marker/anchor files use utf-8-sig (PowerShell 5.1's
    `Set-Content -Encoding UTF8` writes a BOM).
  - WRITES use utf-8 with newline='\\n' -- no BOM.
  - stdout is written as RAW BYTES (write_stdout_bytes), matching
    skill-anchor.ps1's OpenStandardOutput().Write(bytes) -- no trailing newline.
"""
from __future__ import annotations

import itertools
import json
import os
import sys
import time
from pathlib import Path
from typing import Callable, NamedTuple


class Result(NamedTuple):
    """exit_code: 0 = allow, 2 = block. No other value is ever returned by a guard."""

    exit_code: int
    stdout: str = ""
    stderr: str = ""


ALLOW = Result(0)


def block(message: str) -> Result:
    return Result(2, "", message)


def configure_streams() -> None:
    """Reconfigure stdin/stdout/stderr to UTF-8. dispatch.py calls this before anything
    else -- the pinned interpreter's default stream encoding is cp932 (verified)."""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass  # non-interactive / already-closed streams: best effort


def read_stdin_json() -> dict | None:
    """UTF-8. Returns `None` when stdin could not be read or parsed as JSON AT
    ALL (unreadable stream, empty input, undecodable bytes, invalid JSON) --
    distinguished from a legitimately empty `{}` JSON object, which parses
    fine and is returned normally like any other dict.

    The caller (dispatch.py's main()) must check for `None` and exit 0 BEFORE
    running any guard: a corrupted/unreadable payload must fail open (I-3),
    never reach guard logic, and never be treated as an empty-but-valid `{}`
    that a guard could BLOCK on."""
    try:
        raw = sys.stdin.buffer.read()
    except Exception:
        return None
    if not raw:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else {}


def write_stdout_bytes(text: str) -> None:
    """sys.stdout.buffer.write(text.encode('utf-8')); flush. No trailing newline is
    ever appended -- this matches skill-anchor.ps1's raw stream write exactly."""
    sys.stdout.buffer.write(text.encode("utf-8"))
    sys.stdout.buffer.flush()


def write_stderr(text: str) -> None:
    sys.stderr.buffer.write(text.encode("utf-8"))
    sys.stderr.buffer.flush()


def read_text(path: Path) -> str:
    """encoding='utf-8-sig' -- tolerates a PowerShell-written BOM."""
    with open(path, "r", encoding="utf-8-sig") as fh:
        return fh.read()


def write_text(path: Path, text: str) -> None:
    """encoding='utf-8', newline='\\n', NO BOM."""
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def read_lines(path: Path) -> list[str]:
    """Stripped, empties dropped."""
    return [line.strip() for line in read_text(path).splitlines() if line.strip()]


LOG_CAP_MARKER = "_ulthar_log_capped"

_LOG_CAP_NOTE = (
    "this log reached logging.max_error_log_bytes. The entries ABOVE are the "
    "FIRST ones recorded -- what started the spin -- and every entry after them "
    "was dropped, counted here. The file's mtime is the time of the most recent "
    "drop; the ts of the last entry above is roughly when dropping began. "
    "Delete this file to resume logging."
)

# Fixed WIDTH is the whole point: the counter is a zero-padded STRING so every
# footer serialises to the same number of bytes and can be rewritten in place at
# a known offset, instead of re-reading and re-writing a 2 MB file per dropped
# entry during a hot-spin. ensure_ascii=True + an ASCII note keeps bytes == chars
# here (entries themselves stay ensure_ascii=False).
_MAX_DROPPED = 10**12 - 1


def _cap_footer(dropped: int) -> bytes:
    return (
        json.dumps(
            {
                LOG_CAP_MARKER: True,
                "dropped_entries": f"{min(dropped, _MAX_DROPPED):012d}",
                "note": _LOG_CAP_NOTE,
            },
            ensure_ascii=True,
        )
        + "\n"
    ).encode("utf-8")


LOG_CAP_FOOTER_BYTES = len(_cap_footer(0))


def _tail_footer(path: Path, size: int) -> int | None:
    """The drop count when the file already ENDS in a cap footer, else None.
    One fixed-size read; this is an error path, not the hot path."""
    if size < LOG_CAP_FOOTER_BYTES:
        return None
    try:
        with open(path, "rb") as fh:
            fh.seek(size - LOG_CAP_FOOTER_BYTES)
            obj = json.loads(fh.read(LOG_CAP_FOOTER_BYTES).decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    if not (isinstance(obj, dict) and obj.get(LOG_CAP_MARKER)):
        return None
    try:
        return int(obj.get("dropped_entries") or 0)
    except (TypeError, ValueError):
        return 0


def _bump_footer(path: Path, size: int, dropped: int) -> None:
    with open(path, "r+b") as fh:
        fh.seek(size - LOG_CAP_FOOTER_BYTES)
        fh.write(_cap_footer(dropped))


def _start_footer(path: Path, size: int) -> None:
    """The first drop. `size` may already be past the cap (a file the pre-cap
    code left oversized) and may end mid-line (a process killed mid-write), so
    the trailing newline is checked rather than assumed."""
    if size == 0 or not path.exists():
        path.write_bytes(_cap_footer(1))
        return
    with open(path, "r+b") as fh:
        fh.seek(size - 1)
        last = fh.read(1)
        fh.seek(0, 2)
        if last != b"\n":
            fh.write(b"\n")
        fh.write(_cap_footer(1))


def append_jsonl(path: Path, obj: dict, max_bytes: int | None = None) -> None:
    """utf-8, ensure_ascii=False, one line. Caller (dispatch.py / Ctx.log_error) wraps
    this in its own try/except -- a logging failure must never raise past the caller.

    max_bytes (config: logging.max_error_log_bytes) KEEPS THE HEAD AND COUNTS THE
    REST. The previous cap truncated the file to empty and appended, which (a)
    bounded nothing -- one entry larger than max_bytes still landed in full, and
    the real ceiling was max_bytes plus an unbounded line -- and (b) threw away
    the only entries an investigator wants: when a guard hot-spins (21,612 lines
    in 25 seconds, observed), the FIRST entries show what started it and the
    newest show only that it is still going.

    So: an entry is appended only while `size + entry + footer <= max_bytes` AND
    the file is not already capped. Once it would not fit, nothing is appended;
    instead one fixed-width footer line at the end of the file records how many
    entries were dropped, rewritten in place per drop. The footer is checked
    FIRST and is what "capped" means -- a capped log stays capped even if some
    later entry is small enough to fit, because otherwise entries would land
    BELOW the footer, its count would fork across two footers, and the head
    would no longer be a contiguous record of what happened first. On-disk size
    therefore never exceeds max_bytes (or
    LOG_CAP_FOOTER_BYTES, when max_bytes is set smaller than that), including
    for a single oversized entry -- which is dropped whole and counted rather
    than written. None (the default) keeps unbounded append.

    CONCURRENCY. Every write below happens while this process holds the
    per-path append lock (_locked_append), and a call that cannot take the
    lock in time parks its entry in a uniquely-named `.pending.*` sibling
    that the next lock holder folds in -- see _locked_append. The lock is
    derived from the LOG PATH, so the two writers that share
    logging.config_error_log (lib/ctx.py's Ctx.log_config_error and
    dispatch.py's _best_effort_config_error_log fallback) contend for the
    SAME lock rather than each holding a different one, without either of
    them having to know about the other.

    That locking is not decoration. An earlier version of this function did
    a plain unlocked `open(path, "ab")` single write, on the settled
    assumption that one small append syscall is atomic across processes. A
    real multi-process test (test_log_cap.py) disproved it on this platform:
    separate processes each opening the SAME path in "ab" mode and writing
    one line can silently lose lines outright -- consistent with two
    independent append handles resolving the same end-of-file offset instead
    of each write atomically advancing it. Every dispatcher invocation is its
    own process and several tool calls can be in flight at once, so that was
    a live hazard on every error-log write, not a theoretical one.

    Residues, stated:
      * once capped the log is frozen: a later, unrelated error is counted but
        its content is lost. Deliberate -- the cause outranks the sequel, and
        the footer says to delete the file to resume.
      * a file left oversized by the pre-cap code grows by exactly one footer
        line the first time it is capped.
      * while an entry is parked in a `.pending.*` sibling it is durably on
        disk but not yet IN the log, and does not yet count towards the cap.
        It is folded in (and only then appended-or-counted) by the next call
        that wins the lock. Eventually, not instantly -- never lost.

    Also swept against the append_jsonl_drop_oldest MEDIUM finding (unlocked
    read-modify-truncate-write of the whole file): this function never
    rewrites the whole file after creation. Every write here is either (a) a
    plain lock-held append (pre-cap, _plain_append), or (b) a FIXED-WIDTH
    in-place overwrite of
    the footer at a known offset (_bump_footer / _start_footer) -- never a
    read-the-whole-file-then-write-it-back. So a crash mid-write can corrupt
    at most the fixed footer region, never already-flushed head content."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
    _locked_append(path, data, lambda blob: _append_capped_locked(path, blob, max_bytes))


def _plain_append(path: Path, data: bytes) -> None:
    """Only ever called by the process holding the path's append lock."""
    with open(path, "ab") as fh:
        fh.write(data)


def _append_capped_locked(path: Path, data: bytes, max_bytes: int | None) -> None:
    """append_jsonl's keep-the-head cap decision for ONE line. Lock-held: the
    stat/read of the footer and the write that follows it must not interleave
    with another process's, or two droppers both read the same count and both
    write count+1."""
    if max_bytes is None:
        _plain_append(path, data)
        return
    try:
        size = path.stat().st_size
    except OSError:
        size = 0  # not there yet
    dropped = _tail_footer(path, size)
    if dropped is not None:
        _bump_footer(path, size, dropped + 1)
        return
    if size + len(data) + LOG_CAP_FOOTER_BYTES <= max_bytes:
        _plain_append(path, data)
        return
    _start_footer(path, size)


_APPEND_LOCK_RETRY_ATTEMPTS = 50
_APPEND_LOCK_RETRY_SLEEP_SEC = 0.005  # 50 * 5ms = 250ms NOMINAL bounded wait.
# Windows' ~15ms timer granularity rounds each sleep up, so the real ceiling is
# nearer 750ms; 0.4-0.5s was measured (test_lock_timeout_parks_the_entry...).
# Still bounded, still well under what a user would notice on a tool call, and
# reached only when another process genuinely holds the lock.
_APPEND_LOCK_STALE_AGE_SEC = 2.0  # a real write+trim is micro/low-millisecond

_APPEND_LOCK_MAX_HOLD_SEC = 60.0
"""The ABSOLUTE ceiling on how long any legitimate holder can hold an append
lock. A holder appends one line, at most rewrites a fixed-width footer or
rotates the file, and folds in whatever pending siblings it finds -- micro- to
low-milliseconds, and a large pending sweep is still file copies in a single
directory. Past this, the lock is reclaimed WITHOUT consulting the recorded pid.

Why age alone has to be sufficient here, when the docstrings below used to say
"never on age alone": a CONFIRMED-DEAD pid is sound evidence the lock is free,
but a LIVE pid is not evidence that it is held. Pids are recycled. A holder
killed while holding the lock, whose pid was later handed to an unrelated live
process, produced a lock that could never be reclaimed by any rule that waits
for the pid to die -- so every later writer paid the full retry budget, parked
its entry, and, because no writer could ever become the next holder, nothing
ever folded those pending files back in. The log froze permanently and each
attempted write added 0.3-0.55s to the PreToolUse path.

Residue, stated rather than hidden: a holder that legitimately ran past this
bound could be robbed, and its own `finally` os.remove() would then delete the
NEW holder's lock file. At 60s for an operation measured in milliseconds that
is not a case that occurs; a permanently unreclaimable lock was measured. No
self-check read is added on the release path to close it -- that would put a
syscall on the healthy hot path to insure against the impossible one."""


def _pid_is_alive(pid: int) -> bool:
    """Stdlib-only liveness check, duplicated (not imported) from
    guards/tool_discipline.py's _pid_is_alive: lib/ is imported BY guards/, so
    the dependency cannot run the other way, and this file is out of scope for
    adding a new shared lib module. Errs towards 'alive' on anything ambiguous
    -- a false 'alive' just waits/appends-unlocked a little longer (safe); a
    false 'dead' would reclaim a live holder's lock (unsafe)."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _pid_is_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # exists-but-denied, or ambiguous -- never treat as dead
    return True


def _pid_is_alive_windows(pid: int) -> bool:
    import ctypes

    ERROR_ACCESS_DENIED = 5
    ERROR_INVALID_PARAMETER = 87
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        err = ctypes.get_last_error()
        if err == ERROR_ACCESS_DENIED:
            return True
        if err == ERROR_INVALID_PARAMETER:
            return False
        return True  # ambiguous -- err towards not reclaiming
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True
        return exit_code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _append_lock_owner_write(fd: int) -> None:
    os.write(fd, f"{os.getpid()}\n{time.time()}\n".encode("utf-8"))


def _append_lock_owner_read(lock_path: Path) -> tuple[int, float] | None:
    try:
        text = lock_path.read_text(encoding="utf-8")
    except OSError:
        return None
    lines = text.splitlines()
    if len(lines) < 2:
        return None  # unwritten / mid-write -- not (yet) a readable owner record
    try:
        return int(lines[0]), float(lines[1])
    except ValueError:
        return None


def _append_lock_mtime_age_sec(lock_path: Path) -> float | None:
    """Seconds since the lock file was created, or None when it cannot be
    stat()ed -- which means it just vanished, and the next acquire attempt
    settles the question without any judgement from here."""
    try:
        return time.time() - lock_path.stat().st_mtime
    except OSError:
        return None


def _append_lock_age_sec(lock_path: Path) -> float | None:
    """How long this lock has been held: from the owner record when there is a
    readable one, else from the file's own mtime."""
    owner = _append_lock_owner_read(lock_path)
    if owner is not None:
        return time.time() - owner[1]
    return _append_lock_mtime_age_sec(lock_path)


def _append_lock_is_stale(lock_path: Path) -> bool:
    """Three ways a lock is judged reclaimable, by the evidence each rests on.

    1. NO READABLE OWNER RECORD, older than _APPEND_LOCK_STALE_AGE_SEC. The
       owner line is written immediately after the O_CREAT|O_EXCL that creates
       the file, so an unreadable owner record means an acquire interrupted
       between those two steps: by construction a lock nobody holds. This used
       to be "no pid -> cannot confirm death -> never stale", which is
       backwards -- nothing can prove such a lock is held and nothing ever
       will, so it stranded every later writer of that log permanently. The
       age floor is the whole of what separates it from a lock being acquired
       right now, microseconds before its owner line lands.
    2. A CONFIRMED-DEAD recorded pid, past the same age floor. Unchanged, and
       still the only rule that reclaims quickly.
    3. HELD PAST _APPEND_LOCK_MAX_HOLD_SEC, whatever the pid says -- see that
       constant for why a live pid is not evidence that a lock is held."""
    owner = _append_lock_owner_read(lock_path)
    if owner is None:
        age = _append_lock_mtime_age_sec(lock_path)
        return age is not None and age >= _APPEND_LOCK_STALE_AGE_SEC
    pid, ts = owner
    held = time.time() - ts
    if held >= _APPEND_LOCK_MAX_HOLD_SEC:
        return True
    if held < _APPEND_LOCK_STALE_AGE_SEC:
        return False
    return not _pid_is_alive(pid)


def _append_lock_reclaim(lock_path: Path) -> None:
    """Same exactly-one-winner primitive as guards/tool_discipline.py's
    _reclaim_stale_lock: a FIXED per-lock claim-marker via O_CREAT|O_EXCL, not
    os.replace/rename -- that project measured os.replace as NOT atomic across
    concurrent threads racing the same rename on Windows (257/300 and 266/300
    "both winners"), while O_CREAT|O_EXCL was exactly one winner in 300/300."""
    observed = _append_lock_owner_read(lock_path)
    claim_path = lock_path.with_name(f"{lock_path.name}.reclaim-claim")
    try:
        claim_fd = os.open(str(claim_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except OSError:
        return  # another reclaimer already holds the claim
    os.close(claim_fd)
    try:
        if _append_lock_owner_read(lock_path) != observed:
            return  # not what we judged stale -- a legitimate lock raced in
        for attempt in range(5):
            try:
                os.remove(str(lock_path))
                return
            except OSError:
                if attempt < 4:
                    time.sleep(0.005)  # transient Windows sharing violation
    finally:
        try:
            os.remove(str(claim_path))
        except OSError:
            pass


STUCK_LOCK_NOTE_SUFFIX = ".STUCK-LOCK.txt"

_STUCK_LOCK_NOTE = """ULTHAR append-lock recovery
===========================

The append lock on this log has been held longer than any legitimate writer can
hold it ({max_hold:.0f}s) and could not be reclaimed automatically. Until it is
gone, nothing is being added to

    {log}

and every new entry is parked, whole, in a sibling file named

    {log}.pending.*

Nothing has been lost. To recover, delete the lock file:

    {lock}

The next write folds every parked entry back into the log, in order, and
deletes this note.

(This is the APPEND lock on a log file. It is NOT the pm-delete-allow ledger
lock, whose own recovery message names a different file.)
"""


def _stuck_lock_note_path(path: Path) -> Path:
    return path.with_name(f"{path.name}{STUCK_LOCK_NOTE_SUFFIX}")


def _write_stuck_lock_note(path: Path, lock_path: Path) -> None:
    """The operator surface for a lock that reclaim could not clear. One file,
    written once (O_CREAT|O_EXCL, so every later timeout costs one failed
    open), sitting NEXT TO the log it is about -- which is where someone asking
    "why has this log stopped?" is already looking.

    The text is a constant here rather than a config template like the
    guard-facing messages: io.py is handed a path and bytes, has no cfg or ctx,
    and must never raise. It is never shown to the model, only to a human
    reading the log directory."""
    try:
        fd = os.open(str(_stuck_lock_note_path(path)),
                     os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except OSError:
        return  # already written, or the directory is unwritable -- nothing to add either way
    try:
        os.write(fd, _STUCK_LOCK_NOTE.format(
            log=path, lock=lock_path, max_hold=_APPEND_LOCK_MAX_HOLD_SEC).encode("utf-8"))
    finally:
        os.close(fd)


def _lock_path(path: Path) -> Path:
    """Derived from the LOG PATH, never from the caller. Two writers aimed at
    the same file by configuration -- Ctx.log_config_error and dispatch.py's
    _best_effort_config_error_log both write logging.config_error_log --
    therefore contend for the same lock without either knowing the other
    exists."""
    return path.with_name(f"{path.name}.append.lock")


def _acquire_append_lock(lock_path: Path) -> int | None:
    """Bounded O_CREAT|O_EXCL lock serialising writers of one log file.
    Returns an open fd on success, or None once the retry budget is exhausted
    -- the caller must never treat None as license to skip the write, only to
    skip taking the lock (see _locked_append).

    Catches OSError broadly, not just FileExistsError: measured empirically
    under real multi-process contention on Windows, os.open(O_CREAT|O_EXCL)
    against an existing path can raise PermissionError (WinError 5) instead
    of FileExistsError when the attempt lands in the same instant as another
    process's delete/replace of that path -- a narrower except left this
    uncaught, crashing the whole caller. Any OSError here means the same
    thing operationally either way: this attempt did not get the lock: retry
    or, at budget, fall back to the pending-file path -- never let the
    exception itself propagate and lose the entry."""
    attempts = 0
    reclaim_tried = False
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except OSError:
            # AT MOST ONE reclaim per acquire. Reclaim is not free -- it takes
            # a claim marker and retries the removal five times with sleeps --
            # and running it on all 50 retries turned a stuck lock into a
            # 4.2s-then-6.3s write (measured, real unremovable lock), on the
            # PreToolUse path. A removal that just failed will fail again in
            # the same few hundred milliseconds, and a lock legitimately
            # re-taken in the meantime is fresh and not stale anyway, so the
            # further attempts buy nothing.
            if not reclaim_tried and _append_lock_is_stale(lock_path):
                reclaim_tried = True
                _append_lock_reclaim(lock_path)
            attempts += 1
            if attempts >= _APPEND_LOCK_RETRY_ATTEMPTS:
                return None
            time.sleep(_APPEND_LOCK_RETRY_SLEEP_SEC)
            continue
        _append_lock_owner_write(fd)
        return fd


def _trim_to_cap_locked(path: Path, max_bytes: int) -> None:
    """Called only while the append lock is held. With the lock design below,
    the lock holder is the ONLY writer ever touching `path` directly -- a
    process that cannot get the lock writes to its own uniquely-named pending
    file instead (see _locked_append), never to `path` -- so this
    can safely read `path` once and trust that read for the whole operation.

    Crash safety: the trimmed content is written to a sibling temp file and
    swapped in with os.replace(), which is atomic FOR FILES on Windows (it
    cannot overwrite a directory, but that never applies here) and on POSIX --
    a crash before the replace leaves the old full file, a crash after leaves
    the new trimmed file, never a half-written one."""
    try:
        existing = path.read_bytes()
    except OSError:
        return
    if len(existing) <= max_bytes:
        return
    lines = existing.splitlines(keepends=True)
    total = len(existing)
    while total > max_bytes and len(lines) > 1:
        total -= len(lines.pop(0))
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.trim.tmp")
    tmp_path.write_bytes(b"".join(lines))
    os.replace(str(tmp_path), str(path))


_pending_seq = itertools.count()


def _pending_path(path: Path) -> Path:
    """A name that can never collide with another call, in this process or
    any other: pid + nanosecond clock + a per-process monotonic counter as a
    belt-and-braces tiebreaker. Because it is unique, CREATING it can never
    race with anything -- unlike `path` itself, which is shared."""
    return path.with_name(f"{path.name}.pending.{os.getpid()}.{time.time_ns()}.{next(_pending_seq)}")


def _sweep_pending_locked(path: Path, write: Callable[[bytes], None]) -> None:
    """Called only while the append lock is held. Folds in every sibling
    pending file left behind by a call that could not get the lock in time
    (oldest name first -- pid+time_ns+seq sorts in write order well enough
    for a decision log, where exact interleaving across processes was never
    orderable to begin with), then removes each pending file once its content
    is safely committed to the main log. Idempotent and safe to call even when
    no pending files exist.

    `write` is the caller's own commit-one-entry policy, so a parked entry is
    subject to exactly the same cap decision it would have got had it landed
    on time -- the head-preserving cap for append_jsonl, a plain append (with
    rotation afterwards) for append_jsonl_drop_oldest. Folding pending content
    in with a bare append would let a parked entry sail past append_jsonl's
    byte ceiling."""
    folded = 0
    for pfile in sorted(path.parent.glob(f"{path.name}.pending.*")):
        try:
            content = pfile.read_bytes()
        except OSError:
            continue
        if content:
            write(content)
        try:
            pfile.unlink()
        except OSError:
            pass
        folded += 1
    if folded:
        # Recovery happened: parked entries just landed, so a stuck-lock note
        # left by whoever parked them is now false. Removed HERE because this
        # is the moment it stops being true and the glob above has already run
        # -- the healthy path (no pending files) pays nothing for it.
        try:
            _stuck_lock_note_path(path).unlink()
        except OSError:
            pass


def _write_pending(path: Path, data: bytes) -> None:
    """The lock-timeout path. NEVER "drop the entry" -- losing the entry on
    timeout is the defect this whole mechanism exists to fix. The name is
    unique (pid + ns clock + counter), so this can only ever collide with
    itself in the pathological case of the clock not advancing between two
    calls in the same process -- a few retries with a fresh name is a cheap,
    sufficient guard against that and against any transient OSError (the same
    class measured for the lock file itself in _acquire_append_lock)."""
    for _ in range(5):
        try:
            fd = os.open(str(_pending_path(path)), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except OSError:
            continue
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        return
    raise OSError(f"could not create a pending-entry file for {path}")


def _locked_append(
    path: Path,
    data: bytes,
    write: Callable[[bytes], None],
    after: Callable[[], None] | None = None,
) -> None:
    """THE single append mechanism for every log in this tree. Every write to
    a shared log path goes through here; nothing else opens one for append.

    Why a lock at all, when an append is "one syscall": a real multi-process
    test (test_log_cap.py) measured separate processes each opening the SAME
    path in "ab" mode and writing one line silently losing lines on this
    platform. Every dispatcher invocation is its own process and several tool
    calls can be in flight at once, so that hazard was live on every log here.

    Contract, in the order the constraints matter:
      1. NEVER BLOCK A TOOL CALL. These writers sit on the PreToolUse path, so
         the wait is bounded (_APPEND_LOCK_RETRY_ATTEMPTS x
         _APPEND_LOCK_RETRY_SLEEP_SEC, 250ms ceiling) and never indefinite.
      2. A TIMEOUT MUST NOT LOSE THE ENTRY. On timeout the entry goes to its
         own uniquely-named `.pending.*` sibling (_write_pending). Creating a
         brand-new, never-shared filename cannot race with anything, so it is
         unconditionally safe unlike a shared-path append. The next call that
         DOES win the lock folds it in via `write`, the same policy the entry
         would have met had it landed on time.
      3. NEVER RAISE PAST THE CALLER. Not enforced here -- every caller
         (Ctx.log_error, Ctx.log_config_error, dispatch.py's fallback writer,
         guards/commander.py) already wraps its call in try/except so a
         logging failure cannot take out a guard decision. This function keeps
         that the callers' job rather than swallowing errors invisibly, so a
         test can still observe a genuine I/O failure (see
         test_crash_mid_rotation_leaves_one_whole_version).
      4. A LOCK NOBODY HOLDS IS ALWAYS RECLAIMABLE, so a crash while holding
         it cannot freeze every future write to that log. Three rules, see
         _append_lock_is_stale: a confirmed-dead owner past
         _APPEND_LOCK_STALE_AGE_SEC (the fast, evidence-backed case); an
         unreadable owner record past the same age (an interrupted acquire --
         a lock whose holding can never be proven); and any lock held past
         _APPEND_LOCK_MAX_HOLD_SEC regardless of pid, because a live pid is
         not evidence of a held lock once pids are recycled. If reclaim
         nonetheless fails, the timeout branch writes a recovery note beside
         the log naming the lock (_write_stuck_lock_note) -- a stuck log must
         not be a silent one.

    `after` runs once, still under the lock, after everything is committed:
    append_jsonl_drop_oldest uses it to rotate. It is deliberately not part of
    `write`, which runs once per entry."""
    lock_path = _lock_path(path)
    fd = _acquire_append_lock(lock_path)
    if fd is None:
        _write_pending(path, data)
        # Ordinary contention times out too, and says nothing: the entry is
        # parked and the next writer folds it in. A lock older than the
        # maximum hold is different -- reclaim has been trying and failing on
        # every retry above, so this log is not merely busy, it is stuck.
        age = _append_lock_age_sec(lock_path)
        if age is not None and age >= _APPEND_LOCK_MAX_HOLD_SEC:
            _write_stuck_lock_note(path, lock_path)
        return
    try:
        _sweep_pending_locked(path, write)
        write(data)
        if after is not None:
            after()
    finally:
        os.close(fd)
        try:
            os.remove(lock_path)
        except OSError:
            pass


def append_jsonl_drop_oldest(path: Path, obj: dict, max_bytes: int | None = None) -> None:
    """utf-8, ensure_ascii=False, one line. Same cap contract as append_jsonl
    (path/obj/max_bytes, silent on I/O failure is the CALLER's job to wrap, None
    means unbounded append) but the OPPOSITE retention policy: this keeps the
    NEWEST entries and drops the oldest, for a log whose reader wants "what just
    happened", not "what started it".

    append_jsonl's head-preserving cap (KEEP THE HEAD, freeze once capped) is
    right for paths.error_log / logging.config_error_log: those only grow when
    something is wrong, and a hot-spin's first entries show the cause while the
    newest only confirm it is still going. commander.decision_log is a
    different animal -- it appends ONE ENTRY PER MAIN-CHAIN WRITE/EDIT THE
    GUARD EVALUATED, in normal operation, whether or not anything is wrong. Its
    reader is asking "did commander just block me, and why", which is always a
    question about the TAIL. Freezing it head-first the moment it fills would
    make the log stop recording the day it becomes useful.

    REVIEW FINDING (MEDIUM): the previous version of this function did an
    unlocked read-modify-truncate-write of the WHOLE file on every call. Every
    matching guarded tool call spawns its own dispatcher process, so two
    concurrent Write/Edit calls are two processes doing this at once --
    whichever read first and wrote last would silently erase the other's just
    -written decision, and a crash mid write_bytes() could leave the log empty
    or truncated. That defeats the entire reason the tail is kept: "did
    commander just block me, and why".

    An early version of this fix assumed a plain unlocked "ab" append from
    several PROCESSES (not threads) racing the same path was safe, on the
    theory that it is a single small write() and every unbounded log in this
    codebase already relies on that being atomic. A real-subprocess
    concurrency test (test_log_cap.py) measured otherwise: on this Windows
    box, separate processes each opening the same path in "ab" mode and
    writing one line can still lose lines outright, with every surviving line
    still valid JSON -- consistent with independent append handles resolving
    the same end-of-file offset rather than each write atomically advancing
    it. So: NO code path in this function writes to the shared `path` except
    the single process holding the append lock. The mechanism that guarantees
    that -- bounded lock, pending file on timeout, stale reclaim of any lock
    nobody can be shown to hold -- is _locked_append, shared with append_jsonl
    rather than duplicated here.

    What is specific to THIS function is only the pair of policies handed to
    _locked_append: a plain append per entry, and a drop-oldest rotation once
    everything is committed. Rotation writes its trimmed result to a temp
    file and os.replace()s it into place -- atomic for files on both Windows
    and POSIX, so a crash leaves either the whole old file or the whole new
    one, never a half-written one. The lock holder is the only writer
    touching `path`, so _trim_to_cap_locked can trust one plain read.

    Residue, stated: while any pending files are un-swept, `path` can
    transiently read as if it were missing the newest entries (they are one
    directory-listing away, in a `.pending.*` sibling) and can stay over
    max_bytes until the next call that wins the lock. Never unbounded, never
    silent, never lost -- just eventually rather than instantly folded in and
    trimmed, which is the trade this design makes deliberately.

    A single entry larger than max_bytes is still kept whole (this cap bounds
    accumulation, not one oversized line -- a decision-log entry's size does
    not vary the way a traceback's does)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
    _locked_append(
        path, data,
        lambda blob: _plain_append(path, blob),
        None if max_bytes is None else lambda: _trim_to_cap_locked(path, max_bytes),
    )
