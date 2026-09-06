#!/usr/bin/env python3
"""hooks/tests/test_dev_processes.py -- guards.dev_process_hygiene + stop_dev_orphans.

CONTRACT.md #5 U-5 row, #6 (B) minimum >=4 (orphan found / no orphan / dead-parent
detection / stop_hook_active) and the PE-12 fixture (stubbed python3 orphan +
node.exe .exe-stripping pairing). CONTRACT.md misreading item and #6 'Stubbing
external commands': "Never enumerate or kill real processes in a test. Stub
list_processes." -- every case here uses a STUBBED process table, never the real
machine's.

ulthar-enforcement-round1 (session-scoping): guards.dev_process_hygiene no longer
reads list_processes()/find_dev_processes() (a full-table scan) at all -- it reads
<state_dir>/session-procs/<session_id>.json via lib.procs.find_session_orphans, a
ledger dispatch.py keeps current elsewhere. Every case below that used to stub
`lib.procs.list_processes` now WRITES A LEDGER FILE for the case's session_id
instead (see _write_ledger) and stubs the two psutil-facing calls
find_dev_processes_in_ledger makes per entry (_entry_still_alive, psutil.pid_exists)
via _StubbedLedgerLookup, reproducing the exact same "ppid not in the live pid set"
predicate the old full-table stub proved, just computed from ledger entries. This is
a DELIBERATE, DOCUMENTED fixture-shape change (not a behaviour change): every case's
EXPECTED OUTCOME (exit code, which PID/name/cmdline appear in the message) is
unchanged from before this round; only how the fixed process table reaches the
guard changed. stop_dev_orphans._find_targets tests are adapted the same way,
swapping the stubbed call from lib.procs.find_dev_processes to
lib.procs.find_dev_processes_in_ledger, since _find_targets now sources candidates
from its own session's ledger(s) (lib.procs.find_ledgers_by_root) instead of a
full-table scan -- see stop_dev_orphans.py's module docstring. The ONE test left
completely untouched is test_regression_item3_real_find_dev_processes_or_semantics:
it exercises the OLD, UNCHANGED, full-table lib.procs.find_dev_processes directly
(not through the guard or the CLI), which test_contract.py also still calls
directly, so that function and its stubbing story could not change.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
HOOKS_DIR = TESTS_DIR.parent
sys.path.insert(0, str(HOOKS_DIR))
sys.path.insert(0, str(TESTS_DIR))

import importlib  # noqa: E402

from conftest import SandboxCtx  # noqa: E402
from lib import procs as procs_mod  # noqa: E402

guard_mod = importlib.import_module("guards.dev_process_hygiene")
stop_orphans_mod = importlib.import_module("stop_dev_orphans")


class Probe:
    def __init__(self) -> None:
        self.failed = 0
        self.passed = 0

    def check(self, name: str, cond: bool, detail: str = "") -> None:
        if cond:
            self.passed += 1
            print(f"OK   {name}")
        else:
            self.failed += 1
            print(f"FAIL {name}" + (f"\n       {detail}" if detail else ""))


def _proc(pid, ppid, name, cmdline):
    return procs_mod.ProcInfo(pid=pid, ppid=ppid, name=name, cmdline=cmdline)


# ------------------------------------------------------------------ ledger fixtures


def _write_ledger(sandbox: SandboxCtx, ctx, session_id: str, procs: list, root_pid: int = 1) -> Path:
    """Writes a session ledger file directly (bypassing update_session_ledger,
    which walks real psutil ancestry) so a case can hand the guard a FIXED,
    hand-authored table -- the ledger equivalent of the old _StubbedProcs."""
    entries = {}
    for p in procs:
        entries[str(p.pid)] = {
            "pid": p.pid, "ppid": p.ppid, "name": p.name, "cmdline": p.cmdline,
            "create_time": 1.0, "first_seen": 0.0,
        }
    path = procs_mod.session_ledger_path(sandbox.cfg, ctx, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"root_pid": root_pid, "entries": entries}, fh)
    return path


class _StubbedLedgerLookup:
    """Monkeypatches lib.procs._entry_still_alive and psutil.pid_exists so the
    session-ledger orphan predicate (find_dev_processes_in_ledger) can be
    exercised against a FIXED process table instead of the real machine's --
    the ledger-based equivalent of the old _StubbedProcs, reproducing the
    exact same "ppid not in the live pid set" predicate the pre-ledger tests
    proved. Every pid in `procs` is treated as ALIVE by construction (it is
    the fixed table); a pid's parent is alive iff that parent's own pid is
    also somewhere in `procs`."""

    def __init__(self, procs: list) -> None:
        self._procs = procs
        self._orig_alive = None
        self._orig_exists = None

    def __enter__(self):
        live_pids = {p.pid for p in self._procs}
        ppid_by_pid = {p.pid: p.ppid for p in self._procs}
        self._orig_alive = procs_mod._entry_still_alive
        self._orig_exists = procs_mod.psutil.pid_exists

        def _stub_alive(entry):
            pid = entry["pid"]
            if pid not in ppid_by_pid:
                return False, None
            return True, ppid_by_pid[pid]

        def _stub_exists(pid):
            return pid in live_pids

        procs_mod._entry_still_alive = _stub_alive
        procs_mod.psutil.pid_exists = _stub_exists
        return self

    def __exit__(self, *exc):
        procs_mod._entry_still_alive = self._orig_alive
        procs_mod.psutil.pid_exists = self._orig_exists
        return False


def _run_guard(sandbox: SandboxCtx, procs: list, payload: dict | None = None):
    payload = dict(payload or {})
    payload.setdefault("session_id", "test-session")
    ctx = sandbox.build(payload=payload, event="Stop")
    _write_ledger(sandbox, ctx, payload["session_id"], procs)
    with _StubbedLedgerLookup(procs):
        return guard_mod.run(payload, sandbox.cfg, ctx)


# ------------------------------------------------------------------ (B) minimum >=4


def test_orphan_found(p: Probe) -> None:
    sandbox = SandboxCtx()
    try:
        # live_pid=999 is present in the table -> ppid=999 is "alive"; ppid=1 is not
        # in the table at all -> orphan.
        procs = [
            _proc(999, 1, "node", "not-matching-cmdline"),  # a live decoy, not a match
            _proc(4242, 1, "node.exe", "npm run dev -- --port 5173"),
        ]
        result = _run_guard(sandbox, procs)
        p.check(
            "orphan found -> exit 2, message names PID/name/cmdline",
            result.exit_code == 2
            and "4242" in result.stderr
            and "node.exe" in result.stderr
            and "npm run dev" in result.stderr,
            repr(result),
        )
    finally:
        sandbox.cleanup()


def test_no_orphan(p: Probe) -> None:
    sandbox = SandboxCtx()
    try:
        # parent (pid 100) is alive -> the matching child is NOT an orphan.
        procs = [
            _proc(100, 1, "bash", "some shell"),
            _proc(200, 100, "node.exe", "npm run dev"),
        ]
        result = _run_guard(sandbox, procs)
        p.check("no orphan (live parent) -> ALLOW", result.exit_code == 0, repr(result))
    finally:
        sandbox.cleanup()


def test_dead_parent_detection(p: Probe) -> None:
    """The orphan predicate is exactly `ppid not in the live pid set` -- proven both
    ways: same cmdline/name, only the parent's liveness differs."""
    sandbox = SandboxCtx()
    try:
        alive_parent = [
            _proc(1, 0, "system", ""),
            _proc(50, 1, "workerd", "wrangler dev --local"),
        ]
        dead_parent = [
            _proc(50, 999999, "workerd", "wrangler dev --local"),  # 999999 not in table
        ]
        r_alive = _run_guard(sandbox, alive_parent, {"session_id": "alive"})
        r_dead = _run_guard(sandbox, dead_parent, {"session_id": "dead"})
        p.check(
            "dead-parent detection: alive parent -> ALLOW, dead parent -> BLOCK",
            r_alive.exit_code == 0 and r_dead.exit_code == 2,
            f"alive={r_alive!r} dead={r_dead!r}",
        )
    finally:
        sandbox.cleanup()


def test_stop_hook_active_is_dispatcher_owned(p: Probe) -> None:
    """CONTRACT.md #1/#5: stop_hook_active is the DISPATCHER's responsibility, not
    this guard's -- this guard has NO per-session marker, so the dispatcher-level
    honour_stop_hook_active_for flag is its ONLY loop guard. Proven via the real
    dispatch.py subprocess (single-guard mode), not by asserting on guard.run()
    directly, since the guard module correctly contains no stop_hook_active logic
    at all."""
    import json
    import os
    import subprocess

    sandbox = SandboxCtx()
    try:
        cfg_path = sandbox.root / "config.json"
        with open(HOOKS_DIR.parent / "config.json", "r", encoding="utf-8") as fh:
            cfg_path.write_text(fh.read(), encoding="utf-8")

        env = os.environ.copy()
        env["ULTHAR_CONFIG"] = str(cfg_path)
        env["USERPROFILE"] = str(sandbox.home)
        env["HOME"] = str(sandbox.home)
        env["TEMP"] = str(sandbox.temp_dir)
        env["TMP"] = str(sandbox.temp_dir)

        payload = {
            "hook_event_name": "Stop",
            "tool_name": "",
            "cwd": str(sandbox.cwd),
            "stop_hook_active": True,
        }
        proc = subprocess.run(
            [sys.executable, str(HOOKS_DIR / "dispatch.py"), "--event", "Stop", "--guard", "dev_process_hygiene"],
            input=json.dumps(payload).encode("utf-8"),
            capture_output=True,
            env=env,
            timeout=30,
        )
        # Even with no dev processes at all, the point is the dispatcher never even
        # calls the guard when stop_hook_active is set -- exit must be 0 regardless
        # of what psutil would report on THIS machine.
        p.check(
            "QUIET stop_hook_active (dispatcher short-circuit, real dispatch.py subprocess)",
            proc.returncode == 0,
            f"rc={proc.returncode} stderr={proc.stderr!r}",
        )
    finally:
        sandbox.cleanup()


# ------------------------------------------------------------------ PE-12


def test_pe12_and_exe_stripping(p: Probe) -> None:
    """PE-12: `python3` (never in the master's $NAMES) is the ONE deliberate
    effective-set addition; `node.exe` proves the .exe-stripped set still matches
    every legacy Windows name unchanged."""
    sandbox = SandboxCtx()
    try:
        python3_orphan = [_proc(7001, 999999, "python3", "python3 -m http.server 8000")]
        result = _run_guard(sandbox, python3_orphan, {"session_id": "s-python3"})
        p.check(
            "PE-12: python3 orphan (never in the master's $NAMES) is flagged by the new side",
            result.exit_code == 2 and "7001" in result.stderr,
            repr(result),
        )

        node_orphan = [_proc(7002, 999999, "node.exe", "node ./node_modules/.bin/vite")]
        result2 = _run_guard(sandbox, node_orphan, {"session_id": "s-node"})
        p.check(
            "PE-12 pairing: node.exe (.exe-stripped -> 'node', a legacy master name) still matches",
            result2.exit_code == 2 and "7002" in result2.stderr,
            repr(result2),
        )

        drawio_orphan = [_proc(7003, 999999, "draw.io.exe", "draw.io.exe --export foo.drawio")]
        result3 = _run_guard(sandbox, drawio_orphan, {"session_id": "s-drawio"})
        p.check(
            "PE-12 bonus: draw.io.exe keeps its internal dot (-> 'draw.io', not 'draw')",
            result3.exit_code == 2 and "7003" in result3.stderr,
            repr(result3),
        )
    finally:
        sandbox.cleanup()


# ------------------------------------------------------------------ extra coverage


def test_non_matching_name_or_pattern_allowed(p: Probe) -> None:
    sandbox = SandboxCtx()
    try:
        procs = [
            _proc(1, 0, "system", ""),
            _proc(11, 999999, "explorer.exe", "explorer.exe /nowait"),  # name not in list
            _proc(12, 999999, "node.exe", "node server.js"),  # name matches, PATTERN does not
        ]
        result = _run_guard(sandbox, procs)
        p.check(
            "non-matching name and non-matching pattern -> ALLOW",
            result.exit_code == 0,
            repr(result),
        )
    finally:
        sandbox.cleanup()


def test_max_listed_and_cmdline_truncate(p: Probe) -> None:
    sandbox = SandboxCtx()
    try:
        long_cmd = "node " + ("x" * 200) + " run dev"
        procs = [_proc(9000 + i, 999999, "node.exe", long_cmd) for i in range(15)]
        result = _run_guard(sandbox, procs)
        max_listed = sandbox.cfg["dev_processes"]["max_listed"]
        p.check(
            "max_listed truncation: 15 orphans, only max_listed lines + '(+n more)' suffix",
            result.exit_code == 2
            and result.stderr.count("PID ") == max_listed
            and f"+{15 - max_listed} more" in result.stderr,
            repr(result.stderr)[:400],
        )
        truncate_at = sandbox.cfg["dev_processes"]["cmdline_truncate"]
        p.check(
            "cmdline_truncate: a long cmdline is cut to cmdline_truncate chars + '...'",
            ("x" * (truncate_at - 5) + "...") in result.stderr or "..." in result.stderr,
            repr(result.stderr)[:400],
        )
    finally:
        sandbox.cleanup()


def test_message_placeholders_resolved(p: Probe) -> None:
    """{ports}, {python_exe}, {deploy_dir} all resolve to config-driven values, not
    the master's hardcoded literals (PE-10)."""
    sandbox = SandboxCtx()
    try:
        procs = [_proc(1, 0, "system", ""), _proc(5555, 999999, "node.exe", "npm run dev")]
        result = _run_guard(sandbox, procs)
        ports_str = sandbox.cfg["dev_processes"]["ports_join"].join(
            str(x) for x in sandbox.cfg["dev_processes"]["ports"]
        )
        p.check(
            "hygiene message interpolates {ports} from config, not a literal",
            ports_str in result.stderr,
            repr(result.stderr),
        )
        p.check(
            "hygiene message interpolates {deploy_dir} as an absolute path",
            str(sandbox.deploy_dir) in result.stderr,
            repr(result.stderr),
        )
        unresolved = [
            token for token in ("{count}", "{lines}", "{more}", "{ports}", "{python_exe}", "{deploy_dir}")
            if token in result.stderr
        ]
        p.check(
            "no unresolved {placeholder} left in the message",
            not unresolved,
            str(unresolved),
        )
    finally:
        sandbox.cleanup()


# ------------------------------------------------------------------ ulthar-enforcement-round1: session scoping


def test_other_session_process_never_reported(p: Probe) -> None:
    """Acceptance row 15, case 1: a dev-shaped, dead-parent process that belongs to
    ANOTHER session (never written into THIS session's ledger) must never be
    reported, however it looks on the live machine -- report 0, rc=0. Proven by
    writing a ledger for session 'mine' that does NOT contain the other session's
    pid, then confirming the guard run for 'mine' allows even though that pid
    would independently satisfy the orphan predicate (dead parent) if it were
    looked up."""
    sandbox = SandboxCtx()
    try:
        my_procs = [_proc(1, 0, "system", "")]  # nothing dev-shaped of MY own
        other_session_orphan = _proc(8080, 999999, "node.exe", "npm run dev")  # dead parent, matches
        ctx = sandbox.build(payload={"session_id": "mine"}, event="Stop")
        _write_ledger(sandbox, ctx, "mine", my_procs)
        # The other session's process is real on the "machine" (both pids known to
        # the stub) but was NEVER recorded in session 'mine's ledger.
        with _StubbedLedgerLookup(my_procs + [other_session_orphan]):
            result = guard_mod.run({"session_id": "mine"}, sandbox.cfg, ctx)
        p.check(
            "other-session dev process (dead parent, name+pattern match) -> 0 reports, rc=0",
            result.exit_code == 0 and "8080" not in (result.stderr or ""),
            repr(result),
        )
    finally:
        sandbox.cleanup()


def test_missing_ledger_allows_silently(p: Probe) -> None:
    """A session that ran no Bash/PowerShell tool call this session has no ledger
    file at all -- the ordinary case, never a config_error_log entry (only a
    CORRUPT ledger is, see test_corrupt_ledger_logs_once_and_allows)."""
    sandbox = SandboxCtx()
    try:
        ctx = sandbox.build(payload={"session_id": "never-ran-bash"}, event="Stop")
        result = guard_mod.run({"session_id": "never-ran-bash"}, sandbox.cfg, ctx)
        p.check("no ledger file at all -> ALLOW", result.exit_code == 0, repr(result))
        log_path = sandbox.cfg["logging"]["config_error_log"]
        # No config_error_log entry should exist under this sandbox's state dir at all.
        candidates = list((sandbox.home / ".claude").rglob("config-errors.jsonl"))
        p.check(
            "missing ledger is NOT logged to config_error_log (only a corrupt one is)",
            not candidates or not candidates[0].read_text(encoding="utf-8").strip(),
            str(candidates),
        )
    finally:
        sandbox.cleanup()


def test_corrupt_ledger_logs_once_and_allows(p: Probe) -> None:
    """Acceptance row 15, case 4: a broken ledger -> inert (rc=0) + ONE
    config_error_log line, never a crash and never treated as 'nothing
    recorded'."""
    sandbox = SandboxCtx()
    try:
        ctx = sandbox.build(payload={"session_id": "broken"}, event="Stop")
        path = procs_mod.session_ledger_path(sandbox.cfg, ctx, "broken")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not valid json", encoding="utf-8")
        result = guard_mod.run({"session_id": "broken"}, sandbox.cfg, ctx)
        p.check("corrupt ledger -> ALLOW (inert), not a crash", result.exit_code == 0, repr(result))
        log_path = Path(str(ctx.expand(sandbox.cfg["logging"]["config_error_log"])))
        logged = log_path.exists() and "session_ledger" in log_path.read_text(encoding="utf-8")
        p.check("corrupt ledger logs exactly one config_error_log line naming 'session_ledger'", logged, str(log_path))
    finally:
        sandbox.cleanup()


def test_mutation_removing_ledger_filter_would_leak_other_session(p: Probe) -> None:
    """RED/GREEN mutation evidence for the "台帳フィルタを外す" mutation named in the
    brief: a MUTATED lookup that ignores the ledger entirely and matches against
    the whole fixed table (the pre-round behaviour) WOULD report the other
    session's process; the REAL find_session_orphans (ledger-scoped) does not."""
    sandbox = SandboxCtx()
    try:
        my_procs = [_proc(1, 0, "system", "")]
        other_session_orphan = _proc(9090, 999999, "node.exe", "npm run dev")
        ctx = sandbox.build(payload={"session_id": "mine2"}, event="Stop")
        _write_ledger(sandbox, ctx, "mine2", my_procs)

        with _StubbedLedgerLookup(my_procs + [other_session_orphan]):
            # RED: the mutation -- match against the WHOLE stubbed table, as if no
            # ledger filter existed at all (this is exactly what
            # find_dev_processes used to do before this round).
            all_procs = my_procs + [other_session_orphan]
            mutated = procs_mod.find_dev_processes_in_ledger(
                sandbox.cfg, ctx,
                {str(pr.pid): {"pid": pr.pid, "ppid": pr.ppid, "name": pr.name, "cmdline": pr.cmdline,
                               "create_time": 1.0, "first_seen": 0.0} for pr in all_procs},
                orphans_only=True,
            )
            p.check(
                "RED: removing the ledger filter (matching the whole table) leaks the "
                "other session's orphan",
                any(pr.pid == 9090 for pr in mutated),
                str([pr.pid for pr in mutated]),
            )

            # GREEN: the REAL find_session_orphans, scoped to session 'mine2's ledger.
            real = procs_mod.find_session_orphans(sandbox.cfg, ctx, "mine2")
            p.check(
                "GREEN: find_session_orphans (ledger-scoped) never reports the other "
                "session's PID",
                all(pr.pid != 9090 for pr in real),
                str([pr.pid for pr in real]),
            )
    finally:
        sandbox.cleanup()


def test_mutation_removing_orphan_check_would_miss_dead_parent(p: Probe) -> None:
    """RED/GREEN mutation evidence for "祖先鎖チェックを外す": a MUTATED predicate that
    treats every ledger entry as non-orphan (as if the ppid-liveness / ancestor
    check were deleted) misses the dead-parent case entirely; the real predicate
    catches it."""
    sandbox = SandboxCtx()
    try:
        dead_parent = [_proc(50, 999999, "workerd", "wrangler dev --local")]
        ctx = sandbox.build(payload={"session_id": "mutcheck"}, event="Stop")
        _write_ledger(sandbox, ctx, "mutcheck", dead_parent)

        with _StubbedLedgerLookup(dead_parent):
            entries = {"50": {"pid": 50, "ppid": 999999, "name": "workerd",
                               "cmdline": "wrangler dev --local", "create_time": 1.0, "first_seen": 0.0}}

            def _mutated_always_non_orphan(cfg, ctx_, entries_, orphans_only=True, root=None):
                # Same name/pattern gate as the real function, but the orphan
                # check itself is deleted -- nothing is ever reported.
                return []

            p.check(
                "RED: a predicate with the orphan/ancestor check deleted reports nothing",
                _mutated_always_non_orphan(sandbox.cfg, ctx, entries) == [],
                "",
            )
            real = procs_mod.find_session_orphans(sandbox.cfg, ctx, "mutcheck")
            p.check(
                "GREEN: the real predicate catches the dead-parent orphan",
                any(pr.pid == 50 for pr in real),
                str([pr.pid for pr in real]),
            )
    finally:
        sandbox.cleanup()


# ------------------------------------------------------------------ round 2 (send-back): H-1, H-2, M-2


class _FakeProc:
    """A minimal stand-in for psutil.Process, covering every attribute
    _harvest_from_proc / _is_session_root_candidate read: name(), cmdline(),
    create_time(), exe(). Round 3 (H-2R): _ppid_map() replaces the round-2
    _process_rows() stub -- it carries NO create_time (a bare {pid: ppid}
    dict, matching the real psutil._psplatform.ppid_map() shape), so every
    fake process below must supply its own create_time() the way a real one
    would."""

    def __init__(self, name: str, cmdline: str, create_time: float = 1.0):
        self._name, self._cmdline, self._create_time = name, cmdline, create_time

    def name(self):
        return self._name

    def cmdline(self):
        return self._cmdline.split()

    def exe(self):
        return ""

    def create_time(self):
        return self._create_time


def _with_pid(pid, proc):
    """psutil.Process(pid) always exposes .pid -- the fake stand-ins here
    don't know their own pid until the stubbed constructor is called with
    one, so this sets it at that moment."""
    proc.pid = pid
    return proc


class _DeniedProc:
    """round 6: a process that is ALIVE but every attribute read raises
    AccessDenied -- name()/cmdline()/create_time()/exe() all denied. Used
    across the round-6 HIGH/MEDIUM-2 tests for "unreadable, not gone"."""

    def __init__(self, pid):
        self.pid = pid

    def name(self):
        raise procs_mod.psutil.AccessDenied(self.pid)

    def cmdline(self):
        raise procs_mod.psutil.AccessDenied(self.pid)

    def create_time(self):
        raise procs_mod.psutil.AccessDenied(self.pid)

    def exe(self):
        raise procs_mod.psutil.AccessDenied(self.pid)

    def ppid(self):
        raise procs_mod.psutil.AccessDenied(self.pid)


def test_h1_widening_wrapper_dies_worker_still_captured_and_reported(p: Probe) -> None:
    """H-1 (round 2, real machine finding): a wrapper is captured while alive;
    by the NEXT ledger update it has already exited, and only the worker it
    spawned (whose ppid points at the now-dead wrapper) is still live.
    update_session_ledger's widening rule (any live pid whose ppid is an
    ALREADY-ledgered pid) must still record the worker -- and once recorded,
    the guard must report it as an orphan (its parent, the wrapper, is dead).
    Measured gap this closes: a self-spawned `python -m http.server` orphan
    went unreported because its `bash -c` wrapper had already exited by the
    snapshot that would otherwise have caught it as root's live descendant."""
    sandbox = SandboxCtx()
    try:
        ctx = sandbox.build(payload={"session_id": "h1"}, event="Stop")

        fake_procs = {1: _FakeProc("system", "", create_time=0.5), 50: _FakeProc("bash", "bash -c run-worker")}
        orig_resolve = procs_mod.resolve_session_root
        orig_ppid_map = procs_mod._ppid_map
        orig_process = procs_mod.psutil.Process
        procs_mod.resolve_session_root = lambda cfg, ctx_: (1, None)
        try:
            # snapshot 1: root(1) alive, wrapper(50) alive as root's child.
            procs_mod._ppid_map = lambda: {1: 0, 50: 1}
            procs_mod.psutil.Process = lambda pid: _with_pid(pid, fake_procs[pid])
            procs_mod.update_session_ledger(sandbox.cfg, ctx, "h1")

            ledger1 = procs_mod.read_session_ledger(sandbox.cfg, ctx, "h1")
            p.check(
                "H-1 setup: wrapper (pid 50) recorded after snapshot 1",
                "50" in ledger1.get("entries", {}),
                str(ledger1),
            )

            # snapshot 2: wrapper(50) has exited (gone from the map); the
            # worker(80)'s ppid is 50 -- unreachable from root(1) via a live
            # parent chain, findable ONLY through the widening rule.
            fake_procs[80] = _FakeProc("python3", "python3 -m http.server 8000")
            procs_mod._ppid_map = lambda: {1: 0, 80: 50}
            procs_mod.update_session_ledger(sandbox.cfg, ctx, "h1")
        finally:
            procs_mod.resolve_session_root = orig_resolve
            procs_mod._ppid_map = orig_ppid_map
            procs_mod.psutil.Process = orig_process

        ledger2 = procs_mod.read_session_ledger(sandbox.cfg, ctx, "h1")
        p.check(
            "H-1: worker (pid 80, ppid=dead wrapper 50) captured via widening",
            "80" in ledger2.get("entries", {}),
            str(ledger2),
        )

        worker_alive = _proc(80, 50, "python3", "python3 -m http.server 8000")
        with _StubbedLedgerLookup([worker_alive]):
            result = guard_mod.run({"session_id": "h1"}, sandbox.cfg, ctx)
        p.check(
            "H-1: the captured worker is reported as an orphan (parent dead)",
            result.exit_code == 2 and "80" in result.stderr,
            repr(result),
        )
    finally:
        sandbox.cleanup()


def test_h1_residue_short_lived_wrapper_never_seen_allows(p: Probe) -> None:
    """H-1 residue (accepted, documented in docs/DESIGN.md and this guard's
    docstring, not closed): a wrapper that is BOTH spawned and has ALREADY
    EXITED between two hook dispatches -- so no snapshot ever saw it as
    root's descendant -- is never ledgered, and the worker it left behind was
    therefore never linked to an already-ledgered pid either. Pinned as an
    ALLOW, not a bug: there was no dispatch between the wrapper's birth and
    death for the widening rule to ever observe it."""
    sandbox = SandboxCtx()
    try:
        ctx = sandbox.build(payload={"session_id": "h1res"}, event="Stop")
        fake_procs = {1: _FakeProc("system", "", create_time=0.5)}
        orig_resolve = procs_mod.resolve_session_root
        orig_ppid_map = procs_mod._ppid_map
        orig_process = procs_mod.psutil.Process
        procs_mod.resolve_session_root = lambda cfg, ctx_: (1, None)
        procs_mod.psutil.Process = lambda pid: _with_pid(pid, fake_procs[pid])
        try:
            # ONE update: root(1) only. The wrapper (would-be pid 50) and its
            # worker (90) both came and went entirely between the PREVIOUS
            # dispatch and this one -- no snapshot ever recorded pid 50, so
            # pid 90 (ppid=50) is never "a live pid whose ppid is already
            # ledgered". 90 is not even in the ppid map (it too is already
            # gone), matching the real-world shape of this residue.
            procs_mod._ppid_map = lambda: {1: 0}
            procs_mod.update_session_ledger(sandbox.cfg, ctx, "h1res")
        finally:
            procs_mod.resolve_session_root = orig_resolve
            procs_mod._ppid_map = orig_ppid_map
            procs_mod.psutil.Process = orig_process

        ledger = procs_mod.read_session_ledger(sandbox.cfg, ctx, "h1res")
        p.check(
            "H-1 residue: the worker is NOT captured (its wrapper was never seen)",
            ledger is None or "90" not in ledger.get("entries", {}),
            str(ledger),
        )
        result = guard_mod.run({"session_id": "h1res"}, sandbox.cfg, ctx)
        p.check(
            "H-1 residue: guard ALLOWs (nothing in the ledger to report) -- the "
            "stated, accepted gap, not a silent bug",
            result.exit_code == 0,
            repr(result),
        )
    finally:
        sandbox.cleanup()


def test_h2_known_pid_skips_name_cmdline_harvest(p: Probe) -> None:
    """H-2 / H-2R mutation evidence: on the SECOND update of an unchanged pid
    (same ppid, per _needs_harvest), name()/cmdline()/create_time() must not
    be called again -- no psutil.Process() construction at all for it. This
    is what removed the PreToolUse Bash regression the review measured
    (round 2: 1010/1109ms vs a 1052ms budget; round 3: process_iter() itself
    measured 12-16s cold and is no longer called on any hook path at all --
    see the companion source-text check in test_contract.py)."""
    sandbox = SandboxCtx()
    try:
        ctx = sandbox.build(payload={"session_id": "h2"}, event="Stop")
        calls = {"name": 0, "cmdline": 0, "create_time": 0}

        class _CountingProc:
            def exe(self):
                return ""

            def name(self):
                calls["name"] += 1
                return "node"

            def cmdline(self):
                calls["cmdline"] += 1
                return ["node", "vite"]

            def create_time(self):
                calls["create_time"] += 1
                return 9.0

        fake_root = _FakeProc("system", "", create_time=0.5)
        orig_resolve = procs_mod.resolve_session_root
        orig_ppid_map = procs_mod._ppid_map
        orig_process = procs_mod.psutil.Process
        procs_mod.resolve_session_root = lambda cfg, ctx_: (1, None)
        procs_mod.psutil.Process = lambda pid: _with_pid(pid, fake_root if pid == 1 else _CountingProc())
        try:
            procs_mod._ppid_map = lambda: {1: 0, 60: 1}
            procs_mod.update_session_ledger(sandbox.cfg, ctx, "h2")
            first_calls = dict(calls)
            # SAME pid, SAME ppid -- an unchanged, already-known process.
            procs_mod.update_session_ledger(sandbox.cfg, ctx, "h2")
        finally:
            procs_mod.resolve_session_root = orig_resolve
            procs_mod._ppid_map = orig_ppid_map
            procs_mod.psutil.Process = orig_process

        p.check(
            "H-2 setup: the first update harvests a brand-NEW pid (name() once for "
            "the ledger entry; cmdline() twice -- once for the entry, once for the "
            "round-4 substring-only M-1 boundary check, which no longer touches "
            "name() at all -- both legitimate on FIRST discovery)",
            first_calls == {"name": 1, "cmdline": 2, "create_time": 1},
            str(first_calls),
        )
        p.check(
            "GREEN: a second update of the SAME unchanged pid makes ZERO further "
            "name()/cmdline() calls (create_time check short-circuits BOTH the "
            "entry harvest and the cached root-boundary check)",
            calls == first_calls,
            str(calls),
        )

        def _mutated_harvest_first(pid):
            # RED, for contrast: the pre-round-2 shape harvested BEFORE
            # checking create_time, so it always paid this cost.
            proc = _CountingProc()
            proc.name()
            proc.cmdline()
            return proc

        before = dict(calls)
        _mutated_harvest_first(60)
        p.check(
            "RED: the pre-round-2 (harvest-first) shape WOULD make another "
            "name()/cmdline() call for the same unchanged pid",
            calls["name"] == before["name"] + 1 and calls["cmdline"] == before["cmdline"] + 1,
            str(calls),
        )
    finally:
        sandbox.cleanup()


def test_m2_stop_dev_orphans_merges_ledgers_sharing_root(p: Probe) -> None:
    """M-2: two session_ids (main chain + a sub-agent) resolving to the SAME
    (root_pid, root_create_time) each keep their OWN ledger file;
    stop_dev_orphans._find_targets must see the union of both, never just
    whichever ledger happens to be found first -- and must never merge in a
    ledger recorded under a DIFFERENT root."""
    sandbox = SandboxCtx()
    try:
        ctx = sandbox.build(event="CLI")
        orig_resolve = procs_mod.resolve_session_root
        orig_create_time = procs_mod.process_create_time
        procs_mod.resolve_session_root = lambda cfg, ctx_: (777, None)
        procs_mod.process_create_time = lambda pid: 42.0 if pid == 777 else None
        try:
            def _write(session_id, root_pid, root_create_time, pid, cmdline):
                path = procs_mod.session_ledger_path(sandbox.cfg, ctx, session_id)
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump({
                        "root_pid": root_pid, "root_create_time": root_create_time,
                        "entries": {str(pid): {"pid": pid, "ppid": 999999, "name": "node.exe",
                                                "cmdline": cmdline, "create_time": 1.0, "first_seen": 0.0}},
                    }, fh)

            _write("main-session", 777, 42.0, 111, "npm run dev")
            _write("subagent-session", 777, 42.0, 222, "npm run dev -- --port 5173")
            _write("other-root-session", 888, 1.0, 333, "npm run dev")  # different root

            entries = stop_orphans_mod._session_ledger_entries(sandbox.cfg, ctx)
            p.check(
                "M-2: entries merged across both ledgers sharing the resolved root",
                {"111", "222"} <= set(entries),
                str(sorted(entries)),
            )
            p.check(
                "M-2: a ledger recorded under a DIFFERENT root is never merged in",
                "333" not in entries,
                str(sorted(entries)),
            )
        finally:
            procs_mod.resolve_session_root = orig_resolve
            procs_mod.process_create_time = orig_create_time
    finally:
        sandbox.cleanup()


# ------------------------------------------------------------------ round 3 (send-back): H-2R, H-4


def test_h4_boundary_children_never_widened_in_across_three_updates(p: Probe) -> None:
    """H-4 regression (round 3, the reviewer's 'bnd' scenario): a boundary
    (pid 200, another session's own root reached as a descendant of THIS
    session's root) must never become a widening PARENT -- not on the update
    it is discovered, not on any later one, and not transitively through its
    own children. Round-2's widening used EVERY ledgered pid (boundaries
    included) as an eligible parent, so a boundary's child leaked in one
    update after being flagged, and a grandchild the update after that.

    Three updates: (1) the boundary itself appears (BFS descends into it,
    finds it matches session_root.candidate_substrings, flags it, and does
    NOT descend further); (2) its child 'bc' (pid 300, ppid=200) appears --
    must NOT be widened in; (3) a grandchild 'bgc' (pid 400, ppid=300)
    appears -- must ALSO never enter, which it cannot structurally once 300
    itself was correctly excluded (300 has no entry to serve as a further
    widening parent)."""
    sandbox = SandboxCtx()
    try:
        ctx = sandbox.build(payload={"session_id": "bnd"}, event="Stop")
        fake_procs = {
            1: _FakeProc("system", "", create_time=0.5),
            200: _FakeProc("node", "node claude-code --agent", create_time=1.0),  # matches candidate_substrings
            300: _FakeProc("bash", "bash -c child-work", create_time=2.0),
            400: _FakeProc("python3", "python3 worker.py", create_time=3.0),
        }
        orig_resolve = procs_mod.resolve_session_root
        orig_ppid_map = procs_mod._ppid_map
        orig_process = procs_mod.psutil.Process
        procs_mod.resolve_session_root = lambda cfg, ctx_: (1, None)
        procs_mod.psutil.Process = lambda pid: _with_pid(pid, fake_procs[pid])
        try:
            # update 1: root(1) -> boundary(200).
            procs_mod._ppid_map = lambda: {1: 0, 200: 1}
            procs_mod.update_session_ledger(sandbox.cfg, ctx, "bnd")
            ledger1 = procs_mod.read_session_ledger(sandbox.cfg, ctx, "bnd")
            p.check(
                "H-4 setup: the boundary itself IS recorded, flagged is_root_boundary",
                ledger1["entries"].get("200", {}).get("is_root_boundary") is True,
                str(ledger1),
            )

            # update 2: 300's ppid is 200 (the boundary) -- unreachable via
            # BFS (200 already stopped descent) and must not be widened in.
            procs_mod._ppid_map = lambda: {1: 0, 200: 1, 300: 200}
            procs_mod.update_session_ledger(sandbox.cfg, ctx, "bnd")
            ledger2 = procs_mod.read_session_ledger(sandbox.cfg, ctx, "bnd")
            p.check(
                "H-4: the boundary's child is NEVER widened in (update 2)",
                "300" not in ledger2["entries"],
                str(ledger2),
            )

            # update 3: 400's ppid is 300 -- one level further below the boundary.
            procs_mod._ppid_map = lambda: {1: 0, 200: 1, 300: 200, 400: 300}
            procs_mod.update_session_ledger(sandbox.cfg, ctx, "bnd")
            ledger3 = procs_mod.read_session_ledger(sandbox.cfg, ctx, "bnd")
            p.check(
                "H-4: the boundary's grandchild is NEVER widened in (update 3)",
                "400" not in ledger3["entries"],
                str(ledger3),
            )
        finally:
            procs_mod.resolve_session_root = orig_resolve
            procs_mod._ppid_map = orig_ppid_map
            procs_mod.psutil.Process = orig_process
    finally:
        sandbox.cleanup()


def test_h2r_cold_update_session_ledger_under_budget(p: Probe) -> None:
    """H-2R timing regression: ONE cold update_session_ledger call (a fresh
    interpreter, as every hook dispatch is -- no warm handle cache) must
    complete well under a second on a normal machine. process_iter()'s
    per-pid Process() construction (round 2's since-deleted _process_rows)
    measured 12-16s cold on the reviewer's machine; _ppid_map() (round 3)
    must not reintroduce that. Runs against the REAL machine's own process
    tree (not stubbed) through a real subprocess, which is the only way to
    measure a genuinely cold interpreter start the way a hook dispatch is."""
    import os
    import subprocess

    sandbox = SandboxCtx()
    try:
        env = os.environ.copy()
        env["USERPROFILE"] = str(sandbox.home)
        env["HOME"] = str(sandbox.home)
        env["TEMP"] = str(sandbox.temp_dir)
        env["TMP"] = str(sandbox.temp_dir)

        config_path = HOOKS_DIR.parent / "config.json"
        code = (
            "import sys, time\n"
            "from pathlib import Path\n"
            f"sys.path.insert(0, {str(HOOKS_DIR)!r})\n"
            "from lib import ctx as ctx_mod\n"
            "from lib import procs as procs_mod\n"
            f"cfg = ctx_mod.load_config({str(config_path)!r})\n"
            f"ctx = ctx_mod.build_ctx({{}}, cfg, 'Stop', Path({str(HOOKS_DIR)!r}))\n"
            "t0 = time.perf_counter()\n"
            "procs_mod.update_session_ledger(cfg, ctx, 'timing-h2r')\n"
            "print(time.perf_counter() - t0)\n"
        )
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=30, text=True, env=env)
        p.check(
            "H-2R timing setup: the cold update_session_ledger subprocess ran cleanly",
            proc.returncode == 0,
            f"rc={proc.returncode} stderr={proc.stderr}",
        )
        elapsed = None
        if proc.returncode == 0 and proc.stdout.strip():
            try:
                elapsed = float(proc.stdout.strip().splitlines()[-1])
            except ValueError:
                elapsed = None
        p.check(
            "H-2R timing: ONE cold update_session_ledger call through a fresh "
            "subprocess takes <= 1.5s on this machine",
            elapsed is not None and elapsed <= 1.5,
            f"elapsed={elapsed} stdout={proc.stdout!r}",
        )
    finally:
        sandbox.cleanup()


def test_ppid_map_fallback_descendants_only_no_widening(p: Probe) -> None:
    """H-2R fallback (documented, LOW from round 3 review): when _ppid_map()
    is unavailable (an unsupported psutil version/platform, returns None),
    update_session_ledger falls back to
    psutil.Process(root).children(recursive=True) for DESCENDANTS ONLY, with
    NO WIDENING. Proven directly against the fallback branch (not the
    ppid_map branch other tests exercise): (a) the update completes without
    raising, (b) a genuine descendant of root (the wrapper, pid 50) IS
    recorded on the first update, (c) a process reachable ONLY via widening
    (pid 80, ppid=50 -- the wrapper, already gone from children() by the
    second update) is NOT recorded, unlike the ppid_map path's H-1 widening
    rule, which would rescue exactly this shape (see
    test_h1_widening_wrapper_dies_worker_still_captured_and_reported) -- the
    accepted gap this fallback documents rather than silently covers."""
    sandbox = SandboxCtx()
    try:
        ctx = sandbox.build(payload={"session_id": "fallback"}, event="Stop")

        class _FakeChildProc:
            def __init__(self, pid, ppid, name, cmdline, create_time=1.0):
                self.pid = pid
                self._ppid, self._name, self._cmdline, self._create_time = ppid, name, cmdline, create_time

            def ppid(self):
                return self._ppid

            def name(self):
                return self._name

            def cmdline(self):
                return self._cmdline.split()

            def exe(self):
                return ""

            def create_time(self):
                return self._create_time

        class _FakeRootProc:
            def __init__(self, pid, children_list, create_time=0.5):
                self.pid = pid
                self._children = children_list
                self._create_time = create_time

            def create_time(self):
                return self._create_time

            def children(self, recursive=False):
                return self._children

        wrapper = _FakeChildProc(50, 1, "bash", "bash -c run-worker")
        orig_resolve = procs_mod.resolve_session_root
        orig_ppid_map = procs_mod._ppid_map
        orig_process = procs_mod.psutil.Process
        procs_mod.resolve_session_root = lambda cfg, ctx_: (1, None)
        procs_mod._ppid_map = lambda: None  # force the fallback branch
        try:
            # update 1: root(1)'s children(recursive=True) = [wrapper(50)].
            procs_mod.psutil.Process = lambda pid: _FakeRootProc(1, [wrapper]) if pid == 1 else wrapper
            procs_mod.update_session_ledger(sandbox.cfg, ctx, "fallback")
        finally:
            procs_mod.resolve_session_root = orig_resolve
            procs_mod._ppid_map = orig_ppid_map
            procs_mod.psutil.Process = orig_process

        ledger1 = procs_mod.read_session_ledger(sandbox.cfg, ctx, "fallback")
        p.check(
            "fallback (a): update_session_ledger completes and writes a ledger",
            ledger1 is not None,
            str(ledger1),
        )
        p.check(
            "fallback (b): root's genuine descendant (wrapper, pid 50) IS recorded",
            ledger1 is not None and "50" in ledger1.get("entries", {}),
            str(ledger1),
        )

        # update 2: the wrapper has exited (gone from children()); a worker
        # (80, ppid=50) exists but is NOT root's child -- reachable, in the
        # ppid_map path, ONLY through H-1 widening.
        worker = _FakeChildProc(80, 50, "python3", "python3 -m http.server 8000")
        orig_resolve = procs_mod.resolve_session_root
        orig_ppid_map = procs_mod._ppid_map
        orig_process = procs_mod.psutil.Process
        procs_mod.resolve_session_root = lambda cfg, ctx_: (1, None)
        procs_mod._ppid_map = lambda: None
        try:
            procs_mod.psutil.Process = lambda pid: _FakeRootProc(1, []) if pid == 1 else worker
            procs_mod.update_session_ledger(sandbox.cfg, ctx, "fallback")
        finally:
            procs_mod.resolve_session_root = orig_resolve
            procs_mod._ppid_map = orig_ppid_map
            procs_mod.psutil.Process = orig_process

        ledger2 = procs_mod.read_session_ledger(sandbox.cfg, ctx, "fallback")
        p.check(
            "fallback (c): a process reachable ONLY via widening (pid 80) is NOT "
            "recorded -- no widening in the fallback path (documented gap)",
            ledger2 is not None and "80" not in ledger2.get("entries", {}),
            str(ledger2),
        )
    finally:
        sandbox.cleanup()


# ------------------------------------------------------------------ round 4 (send-back, Codex third-party review)


class _FakeAncestor:
    """A minimal stand-in for a psutil.Process ancestor, for resolve_session_root
    tests -- covers .pid, .name(), .cmdline(), .exe(), .parent()."""

    def __init__(self, pid, name, cmdline="", parent_proc=None):
        self.pid = pid
        self._name, self._cmdline, self._parent = name, cmdline, parent_proc

    def name(self):
        return self._name

    def cmdline(self):
        return self._cmdline.split() if self._cmdline else []

    def exe(self):
        return ""

    def parent(self):
        return self._parent


def test_root_resolution_exhausts_substrings_before_name_fallback(p: Probe) -> None:
    """item 1: a GENERIC 'node' process (a Vite dev server, matches no
    candidate_substrings but DOES match candidate_names by bare name) sits
    BETWEEN the hook process and the REAL Claude Code host (which matches a
    substring) in the ancestor chain. The whole chain must be exhausted for a
    substring match FIRST; only if none exists anywhere does the name
    fallback apply. root must resolve to the REAL host (300), never the
    generic node (200) despite it being climbed first."""
    real_host = _FakeAncestor(300, "node", "node /path/to/claude-code/cli.js")
    generic_node = _FakeAncestor(200, "node", "node vite --port 5173", parent_proc=real_host)
    shell = _FakeAncestor(100, "bash", "bash", parent_proc=generic_node)
    hook_parent = _FakeAncestor(50, "python", "python dispatch.py", parent_proc=shell)

    class _FakeSelf:
        def parent(self_inner):
            return hook_parent

    sandbox = SandboxCtx()
    try:
        ctx = sandbox.build(event="Stop")
        orig_process = procs_mod.psutil.Process
        procs_mod.psutil.Process = lambda pid: _FakeSelf()
        try:
            root_pid, err = procs_mod.resolve_session_root(sandbox.cfg, ctx)
        finally:
            procs_mod.psutil.Process = orig_process

        p.check(
            "root resolution: the REAL host (300, substring match) wins over the "
            "generic 'node' (200, name-only match) climbed first",
            root_pid == 300,
            f"root_pid={root_pid} err={err}",
        )
    finally:
        sandbox.cleanup()


def test_descendant_boundary_is_substring_only_vite_workerd_captured(p: Probe) -> None:
    """item 1: the DESCENDANT boundary predicate is substring-only -- a bare
    'node' descendant (Vite) must NEVER be treated as a boundary, or the
    `workerd` process it spawns underneath would never be reached."""
    sandbox = SandboxCtx()
    try:
        ctx = sandbox.build(payload={"session_id": "vitewd"}, event="Stop")
        fake_procs = {
            1: _FakeProc("system", "", create_time=0.5),
            50: _FakeProc("node", "node vite --port 5173", create_time=1.0),
            60: _FakeProc("workerd", "workerd --config x", create_time=2.0),
        }
        orig_resolve = procs_mod.resolve_session_root
        orig_ppid_map = procs_mod._ppid_map
        orig_process = procs_mod.psutil.Process
        procs_mod.resolve_session_root = lambda cfg, ctx_: (1, None)
        procs_mod.psutil.Process = lambda pid: _with_pid(pid, fake_procs[pid])
        try:
            procs_mod._ppid_map = lambda: {1: 0, 50: 1, 60: 50}
            procs_mod.update_session_ledger(sandbox.cfg, ctx, "vitewd")
        finally:
            procs_mod.resolve_session_root = orig_resolve
            procs_mod._ppid_map = orig_ppid_map
            procs_mod.psutil.Process = orig_process

        ledger = procs_mod.read_session_ledger(sandbox.cfg, ctx, "vitewd")
        entries = ledger.get("entries", {}) if ledger else {}
        p.check(
            "vite (bare 'node' name) is recorded and NOT flagged a boundary",
            entries.get("50", {}).get("is_root_boundary") is False,
            str(entries.get("50")),
        )
        p.check(
            "workerd (vite's child) IS captured -- descent was not stopped at 'node'",
            "60" in entries,
            str(entries),
        )
    finally:
        sandbox.cleanup()


def test_fallback_prunes_at_foreign_boundary(p: Probe) -> None:
    """item 2: the _ppid_map()-unavailable fallback must prune the same way
    the main path does -- a foreign boundary reached via
    children(recursive=True) must stop the reconstruction there; nothing
    beneath it may be recorded."""
    sandbox = SandboxCtx()
    try:
        ctx = sandbox.build(payload={"session_id": "fallback-bnd"}, event="Stop")

        class _FakeChildProc:
            def __init__(self, pid, ppid, name, cmdline, create_time=1.0):
                self.pid = pid
                self._ppid, self._name, self._cmdline, self._create_time = ppid, name, cmdline, create_time

            def ppid(self):
                return self._ppid

            def name(self):
                return self._name

            def cmdline(self):
                return self._cmdline.split()

            def exe(self):
                return ""

            def create_time(self):
                return self._create_time

        class _FakeRootProc:
            def __init__(self, pid, children_list, create_time=0.5):
                self.pid = pid
                self._children = children_list
                self._create_time = create_time

            def create_time(self):
                return self._create_time

            def children(self, recursive=False):
                return self._children

        boundary = _FakeChildProc(200, 1, "node", "node claude-code --agent")
        child_of_boundary = _FakeChildProc(300, 200, "bash", "bash -c work")
        by_pid = {200: boundary, 300: child_of_boundary}

        orig_resolve = procs_mod.resolve_session_root
        orig_ppid_map = procs_mod._ppid_map
        orig_process = procs_mod.psutil.Process
        procs_mod.resolve_session_root = lambda cfg, ctx_: (1, None)
        procs_mod._ppid_map = lambda: None  # force the fallback branch
        procs_mod.psutil.Process = (
            lambda pid: _FakeRootProc(1, [boundary, child_of_boundary]) if pid == 1 else by_pid[pid]
        )
        try:
            procs_mod.update_session_ledger(sandbox.cfg, ctx, "fallback-bnd")
        finally:
            procs_mod.resolve_session_root = orig_resolve
            procs_mod._ppid_map = orig_ppid_map
            procs_mod.psutil.Process = orig_process

        ledger = procs_mod.read_session_ledger(sandbox.cfg, ctx, "fallback-bnd")
        entries = ledger.get("entries", {}) if ledger else {}
        p.check(
            "fallback: the boundary (200) IS recorded, flagged is_root_boundary",
            entries.get("200", {}).get("is_root_boundary") is True,
            str(entries),
        )
        p.check(
            "fallback: the boundary's child (300) is PRUNED, even though "
            "children(recursive=True) returned it",
            "300" not in entries,
            str(entries),
        )
    finally:
        sandbox.cleanup()


def test_pid_reuse_same_ppid_by_foreign_root_children_not_ingested(p: Probe) -> None:
    """item 3: a known, non-boundary pid whose ppid has NOT changed (the
    cheap round-2/3 signal) can still have been REUSED by an unrelated,
    foreign process with the SAME ppid. Before descending into its children,
    create_time must be re-verified; a mismatch triggers re-harvest and
    re-classification, and if the new identity is itself a boundary, its
    children must never be ingested."""
    sandbox = SandboxCtx()
    try:
        ctx = sandbox.build(payload={"session_id": "reuse"}, event="Stop")
        fake_procs = {1: _FakeProc("system", "", create_time=0.5), 500: _FakeProc("bash", "bash -c work", create_time=10.0)}
        orig_resolve = procs_mod.resolve_session_root
        orig_ppid_map = procs_mod._ppid_map
        orig_process = procs_mod.psutil.Process
        procs_mod.resolve_session_root = lambda cfg, ctx_: (1, None)
        procs_mod.psutil.Process = lambda pid: _with_pid(pid, fake_procs[pid])
        try:
            # update 1: pid 500 is an ordinary, non-boundary descendant.
            procs_mod._ppid_map = lambda: {1: 0, 500: 1}
            procs_mod.update_session_ledger(sandbox.cfg, ctx, "reuse")

            ledger1 = procs_mod.read_session_ledger(sandbox.cfg, ctx, "reuse")
            p.check(
                "setup: pid 500 recorded, not a boundary",
                ledger1["entries"].get("500", {}).get("is_root_boundary") is False,
                str(ledger1),
            )

            # update 2: pid 500 REUSED by a foreign root (same ppid=1, a
            # DIFFERENT create_time and identity that now matches a
            # candidate substring), with a new child 600.
            fake_procs[500] = _FakeProc("node", "node claude-code --agent", create_time=99.0)
            fake_procs[600] = _FakeProc("python3", "python3 worker.py", create_time=50.0)
            procs_mod._ppid_map = lambda: {1: 0, 500: 1, 600: 500}
            procs_mod.update_session_ledger(sandbox.cfg, ctx, "reuse")
        finally:
            procs_mod.resolve_session_root = orig_resolve
            procs_mod._ppid_map = orig_ppid_map
            procs_mod.psutil.Process = orig_process

        ledger2 = procs_mod.read_session_ledger(sandbox.cfg, ctx, "reuse")
        entries = ledger2.get("entries", {})
        p.check(
            "reused pid 500 is re-harvested and re-classified as a boundary",
            entries.get("500", {}).get("is_root_boundary") is True,
            str(entries),
        )
        p.check(
            "the reused root's child (600) is NEVER ingested",
            "600" not in entries,
            str(entries),
        )
    finally:
        sandbox.cleanup()


def test_kill_reverifies_identity_before_each_signal(p: Probe) -> None:
    """item 4: kill() takes (pid, create_time) pairs and re-checks identity
    immediately before EACH signal -- a pid whose actual create_time no
    longer matches the ledger's recorded value (reused since it was recorded)
    must be skipped, never signalled."""

    class _FakeKillProc:
        def __init__(self, pid, create_time):
            self.pid = pid
            self._ct = create_time
            self.terminated = False
            self.killed = False

        def create_time(self):
            return self._ct

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True

    matching = _FakeKillProc(100, 5.0)
    mismatched = _FakeKillProc(200, 5.0)  # ledger expects 1.0 -- reused since recorded
    sandbox = SandboxCtx()
    try:
        orig_process = procs_mod.psutil.Process
        orig_wait_procs = procs_mod.psutil.wait_procs
        procs_mod.psutil.Process = lambda pid: {100: matching, 200: mismatched}[pid]
        procs_mod.psutil.wait_procs = lambda procs, timeout: ([], procs)
        try:
            skips = procs_mod.kill([(100, 5.0), (200, 1.0)], None, sandbox.cfg)
        finally:
            procs_mod.psutil.Process = orig_process
            procs_mod.psutil.wait_procs = orig_wait_procs

        p.check("kill: identity-matching target IS terminated", matching.terminated, "")
        p.check("kill: identity-MISMATCHED (reused) target is NOT terminated", not mismatched.terminated, "")
        skip_pids = {pid for pid, _msg in skips}
        p.check("kill: the reused pid is reported as skipped, the matching one is not",
                200 in skip_pids and 100 not in skip_pids, str(skips))
    finally:
        sandbox.cleanup()


def test_session_id_sanitization_is_injective(p: Probe) -> None:
    """item 5: two different raw session_ids that COLLIDE under plain
    char-substitution (round 3's fix) must produce DIFFERENT sanitized names
    -- otherwise two genuinely different sessions would share one ledger
    file."""
    a = procs_mod._sanitize_session_id("a/b")
    b = procs_mod._sanitize_session_id("a_b")
    p.check(
        "session ids that collide after char-substitution alone now differ "
        "(hash suffix of the RAW id)",
        a != b,
        f"a={a!r} b={b!r}",
    )


def test_ledger_identity_mismatch_discards_entries(p: Probe) -> None:
    """item 5: an on-disk ledger whose (root_pid, root_create_time) does not
    match what THIS dispatch just resolved is DISCARDED (starts empty), never
    merged onto -- a mismatch means the file describes a different actual
    process than the one this session_id resolves to right now."""
    sandbox = SandboxCtx()
    try:
        ctx = sandbox.build(payload={"session_id": "mismatch"}, event="Stop")
        path = procs_mod.session_ledger_path(sandbox.cfg, ctx, "mismatch")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({
                "root_pid": 999, "root_create_time": 1.0,
                "entries": {"1234": {"pid": 1234, "ppid": 1, "name": "node.exe",
                                       "cmdline": "npm run dev", "create_time": 1.0, "first_seen": 0.0}},
            }, fh)

        fake_procs = {1: _FakeProc("system", "", create_time=0.5)}
        orig_resolve = procs_mod.resolve_session_root
        orig_ppid_map = procs_mod._ppid_map
        orig_process = procs_mod.psutil.Process
        procs_mod.resolve_session_root = lambda cfg, ctx_: (1, None)  # resolves to root_pid=1, NOT 999
        procs_mod.psutil.Process = lambda pid: _with_pid(pid, fake_procs[pid])
        try:
            procs_mod._ppid_map = lambda: {1: 0}
            procs_mod.update_session_ledger(sandbox.cfg, ctx, "mismatch")
        finally:
            procs_mod.resolve_session_root = orig_resolve
            procs_mod._ppid_map = orig_ppid_map
            procs_mod.psutil.Process = orig_process

        ledger = procs_mod.read_session_ledger(sandbox.cfg, ctx, "mismatch")
        p.check(
            "mismatched (root_pid, root_create_time) discards the stale entries "
            "instead of merging onto them",
            "1234" not in ledger.get("entries", {}),
            str(ledger),
        )
        p.check(
            "the ledger's root_pid is updated to the freshly-resolved one",
            ledger.get("root_pid") == 1,
            str(ledger),
        )
    finally:
        sandbox.cleanup()


def test_lock_contention_skips_update_without_corrupting_ledger(p: Probe) -> None:
    """item 6 (lost update): when the append lock cannot be acquired, the
    update is skipped cleanly -- the pre-existing ledger on disk is left
    completely untouched, never partially overwritten. RED-when-reverted: a
    version that ignores the lock's contention signal and writes anyway
    would change this file even though the lock said "busy"."""
    sandbox = SandboxCtx()
    try:
        ctx = sandbox.build(payload={"session_id": "lockc"}, event="Stop")
        path = procs_mod.session_ledger_path(sandbox.cfg, ctx, "lockc")
        path.parent.mkdir(parents=True, exist_ok=True)
        original = {"root_pid": 1, "root_create_time": 0.5,
                    "entries": {"77": {"pid": 77, "ppid": 1, "name": "node", "cmdline": "node x",
                                         "create_time": 1.0, "first_seen": 0.0}}}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(original, fh)

        fake_procs = {1: _FakeProc("system", "", create_time=0.5)}
        orig_resolve = procs_mod.resolve_session_root
        orig_ppid_map = procs_mod._ppid_map
        orig_process = procs_mod.psutil.Process
        orig_lock = procs_mod.io_mod._acquire_append_lock
        procs_mod.resolve_session_root = lambda cfg, ctx_: (1, None)
        procs_mod.psutil.Process = lambda pid: _with_pid(pid, fake_procs[pid])
        procs_mod._ppid_map = lambda: {1: 0, 999: 1}
        procs_mod.io_mod._acquire_append_lock = lambda lock_path: None  # simulate contention
        try:
            procs_mod.update_session_ledger(sandbox.cfg, ctx, "lockc")
        finally:
            procs_mod.resolve_session_root = orig_resolve
            procs_mod._ppid_map = orig_ppid_map
            procs_mod.psutil.Process = orig_process
            procs_mod.io_mod._acquire_append_lock = orig_lock

        with open(path, "r", encoding="utf-8") as fh:
            after = json.load(fh)
        p.check(
            "lock contention: update skipped cleanly, pre-existing ledger left untouched",
            after == original,
            str(after),
        )
    finally:
        sandbox.cleanup()


def test_sweep_race_survives_concurrent_write_after_lock_acquired(p: Probe) -> None:
    """item 6 (sweep race): the sweep re-stats a candidate file AFTER taking
    its lock and only unlinks if STILL expired. Simulated by having the
    (stubbed) lock-acquire function itself refresh the file's mtime the
    moment the lock is won -- exactly as a real concurrent writer finishing
    its own update right then would. RED-when-reverted: a sweep that unlinks
    based only on the pre-lock stat would delete the file anyway."""
    import os
    import time

    sandbox = SandboxCtx()
    try:
        ctx = sandbox.build(event="Stop")
        path = procs_mod.session_ledger_path(sandbox.cfg, ctx, "racer")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"root_pid": 99999999, "root_create_time": 1.0, "entries": {}}, fh)
        old_time = time.time() - 100000
        os.utime(path, (old_time, old_time))

        orig_lock = procs_mod.io_mod._acquire_append_lock

        def racing_lock(lock_path):
            fd = orig_lock(lock_path)
            if fd is not None and lock_path.name.startswith(path.name):
                os.utime(path, None)  # concurrent writer "just finished" -- fresh mtime
            return fd

        procs_mod.io_mod._acquire_append_lock = racing_lock
        try:
            procs_mod._sweep_stale_ledger_files(sandbox.cfg, ctx, ttl_sec=1.0, now=time.time())
        finally:
            procs_mod.io_mod._acquire_append_lock = orig_lock

        p.check(
            "sweep race: the re-stat under the lock sees the refreshed mtime and backs off",
            path.exists(),
            "",
        )
    finally:
        sandbox.cleanup()


def test_sweep_never_deletes_ledger_with_alive_root(p: Probe) -> None:
    """item 7: a ledger whose recorded ROOT is still alive (pid exists, same
    create_time) must NEVER be deleted, however old its mtime -- a session
    that dispatches infrequently must not lose its ledger just because
    nothing touched it for a while."""
    import os
    import time

    sandbox = SandboxCtx()
    try:
        ctx = sandbox.build(event="Stop")
        path = procs_mod.session_ledger_path(sandbox.cfg, ctx, "alive-root")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"root_pid": 4242, "root_create_time": 7.0, "entries": {}}, fh)
        old_time = time.time() - 999999
        os.utime(path, (old_time, old_time))

        fake_root = _FakeProc("node", "node claude-code --agent", create_time=7.0)
        orig_process = procs_mod.psutil.Process
        procs_mod.psutil.Process = lambda pid: _with_pid(pid, fake_root)
        try:
            procs_mod._sweep_stale_ledger_files(sandbox.cfg, ctx, ttl_sec=1.0, now=time.time())
        finally:
            procs_mod.psutil.Process = orig_process

        p.check(
            "sweep: a ledger whose ROOT is still alive is NEVER deleted, however old its mtime",
            path.exists(),
            "",
        )
    finally:
        sandbox.cleanup()


def test_max_entries_evicts_dead_before_alive_and_oldest_dead_first(p: Probe) -> None:
    """item 7: over max_entries, DEAD entries are evicted first (oldest dead
    first); an ALIVE entry is never evicted. RED-when-reverted: round 3's
    "newest-first, regardless of alive/dead" policy would instead evict the
    two NEWEST entries here -- one of which is alive."""
    entries = {}
    for pid, first_seen in ((1, 500.0), (2, 600.0), (10, 100.0), (11, 200.0), (12, 300.0)):
        entries[str(pid)] = {"pid": pid, "ppid": 1, "name": "node", "cmdline": "node x",
                               "create_time": 1.0, "first_seen": first_seen}
    alive_pids = {1, 2}

    orig_alive = procs_mod._entry_still_alive
    procs_mod._entry_still_alive = lambda entry: (entry["pid"] in alive_pids, 1)
    try:
        procs_mod._prune_ledger(entries, max_entries=3, ttl_sec=10**9, now=1000.0)
    finally:
        procs_mod._entry_still_alive = orig_alive

    p.check(
        "max_entries: alive entries (1, 2) survive; dead entries evicted OLDEST-first "
        "(10, then 11), keeping the newest dead (12)",
        set(entries) == {"1", "2", "12"},
        str(sorted(entries)),
    )


def test_inaccessible_intermediary_is_recorded_but_never_traversed(p: Probe) -> None:
    """round-6 send-back HIGH: REVERSES round 4's item 8 decision. A pid
    whose own identity cannot be read (AccessDenied, still ALIVE) is
    recorded with `is_root_boundary: None` ("unknown"), and -- unlike round
    4's "continue traversal regardless" -- its children are NEVER
    descended through: an identity that cannot be confirmed safe must be
    treated the SAME as a confirmed foreign boundary. Owner's absolute-
    isolation requirement means "we don't know" cannot default to "assume
    ours". Documented residue: an unreadable wrapper's own children are
    missed until the wrapper itself dies, at which point ordinary H-1
    widening (dead parent) can still link them in."""
    sandbox = SandboxCtx()
    try:
        ctx = sandbox.build(payload={"session_id": "denied"}, event="Stop")
        fake_procs = {
            1: _FakeProc("system", "", create_time=0.5),
            50: _DeniedProc(50),
            60: _FakeProc("workerd", "workerd --config x", create_time=2.0),
        }
        orig_resolve = procs_mod.resolve_session_root
        orig_ppid_map = procs_mod._ppid_map
        orig_process = procs_mod.psutil.Process
        procs_mod.resolve_session_root = lambda cfg, ctx_: (1, None)
        procs_mod.psutil.Process = lambda pid: _with_pid(pid, fake_procs[pid])
        try:
            procs_mod._ppid_map = lambda: {1: 0, 50: 1, 60: 50}
            procs_mod.update_session_ledger(sandbox.cfg, ctx, "denied")
        finally:
            procs_mod.resolve_session_root = orig_resolve
            procs_mod._ppid_map = orig_ppid_map
            procs_mod.psutil.Process = orig_process

        ledger = procs_mod.read_session_ledger(sandbox.cfg, ctx, "denied")
        entries = ledger.get("entries", {}) if ledger else {}
        p.check(
            "the inaccessible node (pid 50) IS recorded, is_root_boundary is "
            "None (unknown), not False",
            "50" in entries and entries["50"].get("name") is None
            and entries["50"].get("is_root_boundary") is None,
            str(entries.get("50")),
        )
        p.check(
            "its child (pid 60, workerd) is NEVER traversed/captured while the "
            "parent's identity is unknown",
            "60" not in entries,
            str(entries),
        )
    finally:
        sandbox.cleanup()


def test_tmp_file_cleaned_up_on_write_crash(p: Probe) -> None:
    """LOW #3: a crash between writing the `.tmp` file and the atomic
    os.replace() must not leave the `.tmp` file behind forever -- cleaned up
    in `finally`."""
    sandbox = SandboxCtx()
    try:
        ctx = sandbox.build(payload={"session_id": "crashy"}, event="Stop")
        fake_procs = {1: _FakeProc("system", "", create_time=0.5)}
        orig_resolve = procs_mod.resolve_session_root
        orig_ppid_map = procs_mod._ppid_map
        orig_process = procs_mod.psutil.Process
        orig_dump = procs_mod.json.dump
        procs_mod.resolve_session_root = lambda cfg, ctx_: (1, None)
        procs_mod.psutil.Process = lambda pid: _with_pid(pid, fake_procs[pid])
        procs_mod._ppid_map = lambda: {1: 0}

        def crashing_dump(*a, **k):
            raise RuntimeError("simulated crash mid-write")

        procs_mod.json.dump = crashing_dump
        try:
            try:
                procs_mod.update_session_ledger(sandbox.cfg, ctx, "crashy")
            except RuntimeError:
                pass
        finally:
            procs_mod.resolve_session_root = orig_resolve
            procs_mod._ppid_map = orig_ppid_map
            procs_mod.psutil.Process = orig_process
            procs_mod.json.dump = orig_dump

        path = procs_mod.session_ledger_path(sandbox.cfg, ctx, "crashy")
        leftover = list(path.parent.glob(f"{path.name}.*.tmp"))
        p.check(
            "a crash mid-write leaves NO .tmp file behind (cleaned up in finally)",
            not leftover,
            str(leftover),
        )
    finally:
        sandbox.cleanup()


# ------------------------------------------------------------------ round 5 (send-back): widening + reused parent


def test_widening_excludes_reused_live_foreign_parent(p: Probe) -> None:
    """round-5 MEDIUM: a ledger pid recorded as a (now-dead) wrapper must NOT
    be an eligible widening PARENT any more once the OS reuses that pid for
    an unrelated, LIVE, foreign process -- a live process's create_time must
    match the ledger's recorded one to stay eligible; a dead pid stays
    eligible unconditionally (the ordinary H-1 case). Owner requirement is
    "never another session's", so this closes the leak in code, not merely
    documents it. The reused pid is deliberately given a DIFFERENT ppid
    (9999, not root) so it is unreachable via the BFS descendant walk --
    the only route its child could take is WIDENING, isolating the fix under
    test from item 3's separate (BFS-descent) re-verification."""
    sandbox = SandboxCtx()
    try:
        ctx = sandbox.build(payload={"session_id": "widen-reuse"}, event="Stop")
        path = procs_mod.session_ledger_path(sandbox.cfg, ctx, "widen-reuse")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({
                "root_pid": 1, "root_create_time": 0.5,
                "entries": {"500": {"pid": 500, "ppid": 1, "name": "bash", "cmdline": "bash -c work",
                                      "create_time": 10.0, "first_seen": 0.0, "is_root_boundary": False}},
            }, fh)

        fake_procs = {
            1: _FakeProc("system", "", create_time=0.5),
            500: _FakeProc("node", "node unrelated-foreign-app", create_time=99.0),  # REUSED, live, different identity
            700: _FakeProc("python3", "python3 worker.py", create_time=50.0),
        }
        orig_resolve = procs_mod.resolve_session_root
        orig_ppid_map = procs_mod._ppid_map
        orig_process = procs_mod.psutil.Process
        procs_mod.resolve_session_root = lambda cfg, ctx_: (1, None)
        procs_mod.psutil.Process = lambda pid: _with_pid(pid, fake_procs[pid])
        try:
            # 500 is now LIVE with a DIFFERENT parent (9999, not root) --
            # unreachable via BFS descent; its child (700) can only be
            # ingested through widening.
            procs_mod._ppid_map = lambda: {1: 0, 500: 9999, 700: 500}
            procs_mod.update_session_ledger(sandbox.cfg, ctx, "widen-reuse")
        finally:
            procs_mod.resolve_session_root = orig_resolve
            procs_mod._ppid_map = orig_ppid_map
            procs_mod.psutil.Process = orig_process

        ledger = procs_mod.read_session_ledger(sandbox.cfg, ctx, "widen-reuse")
        entries = ledger.get("entries", {})
        p.check(
            "widening: the reused live foreign parent's child (700) is NEVER ingested",
            "700" not in entries,
            str(entries),
        )
    finally:
        sandbox.cleanup()


# ------------------------------------------------------------------ round 6 (send-back, Codex delta r2)


def test_medium1_stale_true_boundary_after_reuse_unblocks_own_subtree(p: Probe) -> None:
    """round-6 MEDIUM 1, direction (a): a CACHED confirmed boundary
    (is_root_boundary=True) whose pid the OS later reuses for an ORDINARY
    (non-boundary) process must be RE-VERIFIED before its stale True keeps
    pruning what is now our own legitimate subtree forever. round 4 only
    re-verified non-boundary cached nodes; a stale True was never
    re-checked at all."""
    sandbox = SandboxCtx()
    try:
        ctx = sandbox.build(payload={"session_id": "m1-a"}, event="Stop")
        fake_procs = {
            1: _FakeProc("system", "", create_time=0.5),
            500: _FakeProc("node", "node claude-code --agent", create_time=10.0),  # a real boundary
        }
        orig_resolve = procs_mod.resolve_session_root
        orig_ppid_map = procs_mod._ppid_map
        orig_process = procs_mod.psutil.Process
        procs_mod.resolve_session_root = lambda cfg, ctx_: (1, None)
        procs_mod.psutil.Process = lambda pid: _with_pid(pid, fake_procs[pid])
        try:
            procs_mod._ppid_map = lambda: {1: 0, 500: 1}
            procs_mod.update_session_ledger(sandbox.cfg, ctx, "m1-a")

            ledger1 = procs_mod.read_session_ledger(sandbox.cfg, ctx, "m1-a")
            p.check(
                "setup: pid 500 recorded as a confirmed boundary",
                ledger1["entries"].get("500", {}).get("is_root_boundary") is True,
                str(ledger1),
            )

            # pid 500 is REUSED by an ORDINARY process (no substring match),
            # same ppid=1, now with a legitimate child (600).
            fake_procs[500] = _FakeProc("bash", "bash -c legit-work", create_time=20.0)
            fake_procs[600] = _FakeProc("python3", "python3 worker.py", create_time=30.0)
            procs_mod._ppid_map = lambda: {1: 0, 500: 1, 600: 500}
            procs_mod.update_session_ledger(sandbox.cfg, ctx, "m1-a")
        finally:
            procs_mod.resolve_session_root = orig_resolve
            procs_mod._ppid_map = orig_ppid_map
            procs_mod.psutil.Process = orig_process

        ledger2 = procs_mod.read_session_ledger(sandbox.cfg, ctx, "m1-a")
        entries = ledger2.get("entries", {})
        p.check(
            "pid 500 is re-classified False after reuse by an ordinary process",
            entries.get("500", {}).get("is_root_boundary") is False,
            str(entries),
        )
        p.check(
            "its now-legitimate child (600) IS captured -- our own subtree is "
            "no longer wrongly pruned by the stale True",
            "600" in entries,
            str(entries),
        )
    finally:
        sandbox.cleanup()


def test_medium1_fallback_stale_true_boundary_after_reuse_unblocks_subtree(p: Probe) -> None:
    """round-6 MEDIUM 1, direction (a), FALLBACK path: the same
    stale-True-after-reuse re-verification must also apply when
    _ppid_map() is unavailable."""
    sandbox = SandboxCtx()
    try:
        ctx = sandbox.build(payload={"session_id": "m1-fb"}, event="Stop")

        class _FakeChildProc:
            def __init__(self, pid, ppid, name, cmdline, create_time=1.0):
                self.pid = pid
                self._ppid, self._name, self._cmdline, self._create_time = ppid, name, cmdline, create_time

            def ppid(self):
                return self._ppid

            def name(self):
                return self._name

            def cmdline(self):
                return self._cmdline.split()

            def exe(self):
                return ""

            def create_time(self):
                return self._create_time

        class _FakeRootProc:
            def __init__(self, pid, children_list, create_time=0.5):
                self.pid = pid
                self._children = children_list
                self._create_time = create_time

            def create_time(self):
                return self._create_time

            def children(self, recursive=False):
                return self._children

        boundary = _FakeChildProc(500, 1, "node", "node claude-code --agent", create_time=10.0)
        orig_resolve = procs_mod.resolve_session_root
        orig_ppid_map = procs_mod._ppid_map
        orig_process = procs_mod.psutil.Process
        procs_mod.resolve_session_root = lambda cfg, ctx_: (1, None)
        procs_mod._ppid_map = lambda: None  # force the fallback branch
        try:
            procs_mod.psutil.Process = lambda pid: _FakeRootProc(1, [boundary]) if pid == 1 else boundary
            procs_mod.update_session_ledger(sandbox.cfg, ctx, "m1-fb")

            ledger1 = procs_mod.read_session_ledger(sandbox.cfg, ctx, "m1-fb")
            p.check(
                "fallback setup: pid 500 recorded as a confirmed boundary",
                ledger1["entries"].get("500", {}).get("is_root_boundary") is True,
                str(ledger1),
            )

            reused = _FakeChildProc(500, 1, "bash", "bash -c legit-work", create_time=20.0)
            child = _FakeChildProc(600, 500, "python3", "python3 worker.py", create_time=30.0)
            procs_mod.psutil.Process = lambda pid: _FakeRootProc(1, [reused, child]) if pid == 1 else {500: reused, 600: child}[pid]
            procs_mod.update_session_ledger(sandbox.cfg, ctx, "m1-fb")
        finally:
            procs_mod.resolve_session_root = orig_resolve
            procs_mod._ppid_map = orig_ppid_map
            procs_mod.psutil.Process = orig_process

        ledger2 = procs_mod.read_session_ledger(sandbox.cfg, ctx, "m1-fb")
        entries = ledger2.get("entries", {})
        p.check(
            "fallback: pid 500 is re-classified False after reuse",
            entries.get("500", {}).get("is_root_boundary") is False,
            str(entries),
        )
        p.check(
            "fallback: its now-legitimate child (600) IS captured",
            "600" in entries,
            str(entries),
        )
    finally:
        sandbox.cleanup()


def test_medium2_sweep_preserves_unknown_root(p: Probe) -> None:
    """round-6 MEDIUM 2: a ledger whose root cannot be read (AccessDenied,
    "unknown") must NEVER be deleted by the sweep -- only a CONFIRMED dead
    root justifies deletion. Previously AccessDenied collapsed to "dead"."""
    import os
    import time

    sandbox = SandboxCtx()
    try:
        ctx = sandbox.build(event="Stop")
        path = procs_mod.session_ledger_path(sandbox.cfg, ctx, "unknown-root")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"root_pid": 7777, "root_create_time": 5.0, "entries": {}}, fh)
        old_time = time.time() - 999999
        os.utime(path, (old_time, old_time))

        orig_process = procs_mod.psutil.Process
        procs_mod.psutil.Process = lambda pid: _with_pid(pid, _DeniedProc(pid))
        try:
            procs_mod._sweep_stale_ledger_files(sandbox.cfg, ctx, ttl_sec=1.0, now=time.time())
        finally:
            procs_mod.psutil.Process = orig_process

        p.check(
            "sweep: a ledger whose root identity is UNKNOWN (AccessDenied) is "
            "never deleted, however old its mtime",
            path.exists(),
            "",
        )
    finally:
        sandbox.cleanup()


def test_medium2_prune_preserves_unknown_entries(p: Probe) -> None:
    """round-6 MEDIUM 2: TTL/max_entries pruning must never evict an entry
    whose liveness cannot be confirmed (AccessDenied, "unknown") -- the same
    protection as a confirmed-alive entry."""
    entries = {
        "50": {"pid": 50, "ppid": 1, "name": "node", "cmdline": "node x", "create_time": 1.0, "first_seen": 100.0},
    }
    orig_process = procs_mod.psutil.Process
    procs_mod.psutil.Process = lambda pid: _with_pid(pid, _DeniedProc(pid))
    try:
        procs_mod._prune_ledger(entries, max_entries=0, ttl_sec=1.0, now=1000000.0)
    finally:
        procs_mod.psutil.Process = orig_process

    p.check(
        "prune: an entry with UNKNOWN liveness survives both the TTL pass and "
        "max_entries=0 (which would evict everything dead)",
        "50" in entries,
        str(entries),
    )


def test_medium2_guard_never_reports_unknown_identity(p: Probe) -> None:
    """round-6 MEDIUM 2: the guard must never report an entry whose liveness
    is UNKNOWN as an orphan -- kill() downstream would then act on an
    unconfirmed identity."""
    entries = {
        "50": {"pid": 50, "ppid": 1, "name": "python3", "cmdline": "python3 -m http.server 8000",
               "create_time": 1.0, "first_seen": 0.0},
    }
    orig_process = procs_mod.psutil.Process
    procs_mod.psutil.Process = lambda pid: _with_pid(pid, _DeniedProc(pid))
    try:
        sandbox = SandboxCtx()
        try:
            ctx = sandbox.build(event="Stop")
            result = procs_mod.find_dev_processes_in_ledger(sandbox.cfg, ctx, entries, orphans_only=True)
        finally:
            sandbox.cleanup()
    finally:
        procs_mod.psutil.Process = orig_process

    p.check(
        "guard report: an UNKNOWN-liveness entry is never reported as an orphan",
        result == [],
        str(result),
    )


def test_medium2_cli_kill_never_signals_unknown_identity(p: Probe) -> None:
    """round-6 MEDIUM 2: kill() must never signal a target whose live
    create_time cannot be read (AccessDenied) -- an unconfirmed identity
    must never be acted on. Pins the existing (already correct) behaviour
    explicitly under the tri-state framing."""
    denied = _DeniedProc(200)
    orig_process = procs_mod.psutil.Process
    procs_mod.psutil.Process = lambda pid: denied
    try:
        skips = procs_mod.kill([(200, 1.0)], None, {"dev_processes": {"stop_orphans": {"terminate_grace_seconds": 0}}})
    finally:
        procs_mod.psutil.Process = orig_process

    skip_pids = {pid for pid, _msg in skips}
    p.check(
        "CLI kill: a target with an unreadable (AccessDenied) identity is "
        "skipped, never signalled",
        200 in skip_pids,
        str(skips),
    )


# ------------------------------------------------------------------ round 7 (send-back, Codex delta r3)


class _ExeDeniedProc:
    """round 7: cmdline()/name()/create_time() all READABLE, but exe() --
    the ONLY field that would reveal a substring match for this fake -- is
    denied. Used to prove a foreign root identifiable solely by its exe()
    path is not wrongly confirmed 'not a boundary' just because the
    (unrelated, generic) cmdline text was readable."""

    def __init__(self, name, cmdline, create_time=1.0):
        self._name, self._cmdline, self._create_time = name, cmdline, create_time

    def name(self):
        return self._name

    def cmdline(self):
        return self._cmdline.split()

    def create_time(self):
        return self._create_time

    def exe(self):
        raise procs_mod.psutil.AccessDenied(getattr(self, "pid", 0))


def test_high_partial_identity_exe_denied_boundary_is_unknown(p: Probe) -> None:
    """round-7 send-back HIGH: a foreign root whose only identifying field is
    exe() (its cmdline reveals nothing on its own) and whose exe() raises
    AccessDenied must be classified `is_root_boundary: None` ("unknown"),
    NEVER a confirmed `False` just because the READABLE part (cmdline)
    happened not to match. A confirmed False must only ever come from a
    COMPLETE identity text."""
    sandbox = SandboxCtx()
    try:
        ctx = sandbox.build(payload={"session_id": "partial"}, event="Stop")
        fake_procs = {
            1: _FakeProc("system", "", create_time=0.5),
            200: _ExeDeniedProc("node", "node --harmless-flag", create_time=1.0),
            300: _FakeProc("python3", "python3 worker.py", create_time=2.0),
        }
        orig_resolve = procs_mod.resolve_session_root
        orig_ppid_map = procs_mod._ppid_map
        orig_process = procs_mod.psutil.Process
        procs_mod.resolve_session_root = lambda cfg, ctx_: (1, None)
        procs_mod.psutil.Process = lambda pid: _with_pid(pid, fake_procs[pid])
        try:
            procs_mod._ppid_map = lambda: {1: 0, 200: 1, 300: 200}
            procs_mod.update_session_ledger(sandbox.cfg, ctx, "partial")
        finally:
            procs_mod.resolve_session_root = orig_resolve
            procs_mod._ppid_map = orig_ppid_map
            procs_mod.psutil.Process = orig_process

        ledger = procs_mod.read_session_ledger(sandbox.cfg, ctx, "partial")
        entries = ledger.get("entries", {}) if ledger else {}
        p.check(
            "pid 200 (exe() denied, cmdline readable but non-matching) is "
            "is_root_boundary None, NOT a confirmed False",
            entries.get("200", {}).get("is_root_boundary") is None,
            str(entries.get("200")),
        )
        p.check(
            "its child (300) is NEVER ingested while the parent's identity is "
            "only partially known",
            "300" not in entries,
            str(entries),
        )
    finally:
        sandbox.cleanup()


def test_medium_stale_placeholder_reharvested_and_reclassified(p: Probe) -> None:
    """round-7 send-back MEDIUM: a cached placeholder (is_root_boundary None,
    or any of name/cmdline/create_time None) is re-harvested on EVERY
    update, LEAF entries included -- not only when about to be descended
    into. Once access is restored it is reclassified: boundary/non-boundary,
    and if dev-shaped, reportable as an orphan like any other entry."""
    sandbox = SandboxCtx()
    try:
        ctx = sandbox.build(payload={"session_id": "transient"}, event="Stop")
        fake_procs = {1: _FakeProc("system", "", create_time=0.5), 50: _DeniedProc(50)}
        orig_resolve = procs_mod.resolve_session_root
        orig_ppid_map = procs_mod._ppid_map
        orig_process = procs_mod.psutil.Process
        procs_mod.resolve_session_root = lambda cfg, ctx_: (1, None)
        procs_mod.psutil.Process = lambda pid: _with_pid(pid, fake_procs[pid])
        try:
            # update 1: pid 50 is a leaf, fully denied -- a placeholder.
            procs_mod._ppid_map = lambda: {1: 0, 50: 1}
            procs_mod.update_session_ledger(sandbox.cfg, ctx, "transient")

            ledger1 = procs_mod.read_session_ledger(sandbox.cfg, ctx, "transient")
            p.check(
                "setup: pid 50 recorded as a placeholder (is_root_boundary None, name None)",
                ledger1["entries"].get("50", {}).get("is_root_boundary") is None
                and ledger1["entries"].get("50", {}).get("name") is None,
                str(ledger1),
            )

            # update 2: access restored, SAME ppid (unchanged) -- only the
            # stale-placeholder rule, not the ppid-changed rule, can trigger
            # the re-harvest here.
            fake_procs[50] = _FakeProc("python3", "python3 -m http.server 8000", create_time=9.0)
            procs_mod.update_session_ledger(sandbox.cfg, ctx, "transient")
        finally:
            procs_mod.resolve_session_root = orig_resolve
            procs_mod._ppid_map = orig_ppid_map
            procs_mod.psutil.Process = orig_process

        ledger2 = procs_mod.read_session_ledger(sandbox.cfg, ctx, "transient")
        entries = ledger2.get("entries", {})
        p.check(
            "pid 50 is reclassified once readable: name filled in, is_root_boundary False",
            entries.get("50", {}).get("name") == "python3"
            and entries.get("50", {}).get("is_root_boundary") is False,
            str(entries),
        )

        # And once reclassified as a real dev-shaped process with a dead
        # parent, it is reportable exactly like any other ledger entry.
        dead_parent_shape = _proc(50, 999999, "python3", "python3 -m http.server 8000")
        with _StubbedLedgerLookup([dead_parent_shape]):
            result = procs_mod.find_dev_processes_in_ledger(
                sandbox.cfg, ctx, {"50": entries["50"]}, orphans_only=True)
        p.check(
            "reclassified dev-shaped entry with a dead parent IS reported as an orphan",
            any(r.pid == 50 for r in result),
            str(result),
        )
    finally:
        sandbox.cleanup()


# ------------------------------------------------------------------ stop_dev_orphans._find_targets composition


def test_stop_dev_orphans_default_orphans_only(p: Probe) -> None:
    """Adapted for ulthar-enforcement-round1: _find_targets now sources
    candidates from lib.procs.find_dev_processes_in_ledger (its own session's
    ledger) instead of the full-table lib.procs.find_dev_processes -- the
    composition invariant under test (orphans always qualify; --all unions in
    root-matched non-orphans) is unchanged, only the stubbed call site moved."""
    sandbox = SandboxCtx()
    try:
        ctx = sandbox.build(event="CLI")
        orphan = _proc(1, 999999, "node.exe", "npm run dev")
        rooted_non_orphan = _proc(2, 3, "node.exe", "npm run dev -- C:\\path\\to\\project")

        orig = procs_mod.find_dev_processes_in_ledger

        def _stub(cfg, ctx_, entries, orphans_only=True, root=None):
            # Mirrors the REAL (fixed) semantics: is_orphan OR (not orphans_only
            # AND (root is None or root in cmdline)) -- NOT a branch that picks
            # one or the other.
            all_procs = [orphan, rooted_non_orphan]
            out = []
            for proc in all_procs:
                is_orphan = proc.pid == orphan.pid
                if orphans_only:
                    if is_orphan:
                        out.append(proc)
                else:
                    root_match = root is None or root in proc.cmdline
                    if is_orphan or root_match:
                        out.append(proc)
            return out

        procs_mod.find_dev_processes_in_ledger = _stub
        try:
            targets = stop_orphans_mod._find_targets(sandbox.cfg, ctx, all_flag=False, root="")
            p.check(
                "default (no --all): only orphans, never the rooted non-orphan",
                [t.pid for t in targets] == [1],
                str([t.pid for t in targets]),
            )

            targets_all = stop_orphans_mod._find_targets(sandbox.cfg, ctx, all_flag=True, root="")
            p.check(
                "--all (no --root): union of orphan AND rooted non-orphan chains",
                sorted(t.pid for t in targets_all) == [1, 2],
                str(sorted(t.pid for t in targets_all)),
            )

            targets_root_miss = stop_orphans_mod._find_targets(
                sandbox.cfg, ctx, all_flag=True, root="C:\\NoSuchRoot"
            )
            p.check(
                "--all --root <non-matching>: orphan STILL included (union, not "
                "root-filtered-only) even though the root doesn't match",
                [t.pid for t in targets_root_miss] == [1],
                str([t.pid for t in targets_root_miss]),
            )
        finally:
            procs_mod.find_dev_processes_in_ledger = orig
    finally:
        sandbox.cleanup()


# ------------------------------------------------------------------ regression checks


def test_regression_item2_root_matching_is_case_insensitive(p: Probe) -> None:
    """Pins `--root` against a case-sensitivity regression: PowerShell's `-like` is
    case-insensitive by default, but a Python port using `in` is not. Fixed in
    stop_dev_orphans._find_targets() by never handing `root` to
    find_dev_processes_in_ledger() (always root=None there) and instead filtering
    the candidate set locally with `.casefold()` (module docstring's COMPOSITION
    NOTE). Adapted for ulthar-enforcement-round1: stubs
    find_dev_processes_in_ledger instead of find_dev_processes."""
    sandbox = SandboxCtx()
    try:
        ctx = sandbox.build(event="CLI")
        orphan = _proc(1, 999999, "node.exe", "npm run dev")
        in_root_different_case = _proc(2, 3, "node.exe", "npm run dev -- C:\\REPO\\PROJECT")
        out_of_root = _proc(3, 4, "node.exe", "npm run dev -- C:\\Nowhere")
        root_arg = "c:\\repo\\project"  # deliberately lower-case vs the cmdline's upper-case

        all_procs = [orphan, in_root_different_case, out_of_root]

        def _stub(cfg, ctx_, entries, orphans_only=True, root=None):
            out = []
            for proc in all_procs:
                is_orphan = proc.pid == orphan.pid
                if orphans_only:
                    if is_orphan:
                        out.append(proc)
                else:
                    root_match = root is None or root in proc.cmdline
                    if is_orphan or root_match:
                        out.append(proc)
            return out

        orig = procs_mod.find_dev_processes_in_ledger
        procs_mod.find_dev_processes_in_ledger = _stub
        try:
            # RED: the OLD _find_targets (pre-fix) handed `root` straight into
            # the ledger lookup, inheriting its case-SENSITIVE `in` check.
            def _old_find_targets(cfg, ctx_, all_flag, root):
                orphans = procs_mod.find_dev_processes_in_ledger(cfg, ctx_, {}, orphans_only=True)
                if not all_flag:
                    return orphans
                rooted = procs_mod.find_dev_processes_in_ledger(cfg, ctx_, {}, orphans_only=False, root=(root or None))
                merged: dict[int, object] = {}
                for proc in (*orphans, *rooted):
                    merged[proc.pid] = proc
                return list(merged.values())

            red_targets = _old_find_targets(sandbox.cfg, ctx, all_flag=True, root=root_arg)
            red_pids = sorted(t.pid for t in red_targets)
            p.check(
                "RED: pre-fix _find_targets loses a real match on case alone "
                "(root lower-case, cmdline upper-case)",
                red_pids == [1],  # in_root_different_case (pid 2) wrongly dropped
                str(red_pids),
            )

            # GREEN: the REAL, current _find_targets.
            green_targets = stop_orphans_mod._find_targets(sandbox.cfg, ctx, all_flag=True, root=root_arg)
            green_pids = sorted(t.pid for t in green_targets)
            p.check(
                "GREEN: the current _find_targets matches --root case-insensitively "
                "(parity with PowerShell -like)",
                green_pids == [1, 2],
                str(green_pids),
            )
        finally:
            procs_mod.find_dev_processes_in_ledger = orig
    finally:
        sandbox.cleanup()


def test_regression_item3_real_find_dev_processes_or_semantics(p: Probe) -> None:
    """UNCHANGED by ulthar-enforcement-round1. Pins the OR-semantics fix in
    lib/procs.py (find_dev_processes: is_orphan OR (--all AND root-match), never
    a branch that picks one or the other) -- this is the full-table function,
    left untouched (test_contract.py calls it directly too), so its own test and
    stubbing story (list_processes) are unaffected by the session-ledger round."""
    sandbox = SandboxCtx()
    try:
        ctx = sandbox.build(event="CLI")
        orphan_outside_root = _proc(101, 999999, "node.exe", "npm run dev -- C:\\Elsewhere")
        non_orphan_in_root = _proc(102, 1, "node.exe", "npm run dev -- C:\\path\\to\\project")
        non_orphan_outside_root = _proc(103, 1, "node.exe", "npm run dev -- C:\\Nowhere")
        table = [
            _proc(1, 0, "system", ""),  # keeps pid 1 "alive" as a live parent
            orphan_outside_root,
            non_orphan_in_root,
            non_orphan_outside_root,
        ]

        orig_list_processes = procs_mod.list_processes

        def _stub_list_processes(ctx_, cfg):
            return list(table)

        procs_mod.list_processes = _stub_list_processes
        try:
            # RED: the OLD branch-based implementation (pre-fix), reproduced
            # here verbatim, driven through the SAME real psutil-facing boundary
            # (procs_mod.list_processes, stubbed above) and the SAME
            # name/pattern matching rules -- only the final boolean is the old,
            # broken `if orphans_only: <orphan> else: <root>` branch.
            def _old_branch_find_dev_processes(cfg, ctx_, orphans_only=True, root=None):
                dp = cfg["dev_processes"]
                names = {n.casefold() for n in dp["names"]}
                pattern = re.compile(dp["pattern"], re.IGNORECASE)
                all_procs = procs_mod.list_processes(ctx_, cfg)
                live_pids = {proc.pid for proc in all_procs}
                out = []
                for proc in all_procs:
                    base = proc.name[:-4] if proc.name.casefold().endswith(".exe") else proc.name
                    if base.casefold() not in names:
                        continue
                    if not pattern.search(proc.cmdline):
                        continue
                    if orphans_only:
                        if proc.ppid not in live_pids:
                            out.append(proc)
                    else:
                        if root is None or root in proc.cmdline:
                            out.append(proc)
                return out

            red_result = _old_branch_find_dev_processes(
                sandbox.cfg, ctx, orphans_only=False, root="C:\\path\\to\\project"
            )
            red_pids = sorted(proc.pid for proc in red_result)
            p.check(
                "RED: pre-fix branch semantics DROPS a real orphan outside --root under --all",
                101 not in red_pids,
                str(red_pids),
            )

            # GREEN: the REAL, current, UNSTUBBED (at this level) lib.procs.find_dev_processes.
            real_result = procs_mod.find_dev_processes(
                sandbox.cfg, ctx, orphans_only=False, root="C:\\path\\to\\project"
            )
            real_pids = sorted(proc.pid for proc in real_result)
            p.check(
                "GREEN: the REAL find_dev_processes keeps the orphan (OR, not "
                "branch) AND the in-root non-orphan, drops the outside-root non-orphan",
                real_pids == [101, 102],
                str(real_pids),
            )
        finally:
            procs_mod.list_processes = orig_list_processes
    finally:
        sandbox.cleanup()


def main() -> int:
    p = Probe()
    print("== guards.dev_process_hygiene: minimum (B) coverage ==")
    test_orphan_found(p)
    test_no_orphan(p)
    test_dead_parent_detection(p)
    test_stop_hook_active_is_dispatcher_owned(p)

    print("\n== PE-12: python3 addition + .exe-stripping pairing ==")
    test_pe12_and_exe_stripping(p)

    print("\n== extra coverage ==")
    test_non_matching_name_or_pattern_allowed(p)
    test_max_listed_and_cmdline_truncate(p)
    test_message_placeholders_resolved(p)

    print("\n== ulthar-enforcement-round1: session scoping ==")
    test_other_session_process_never_reported(p)
    test_missing_ledger_allows_silently(p)
    test_corrupt_ledger_logs_once_and_allows(p)
    test_mutation_removing_ledger_filter_would_leak_other_session(p)
    test_mutation_removing_orphan_check_would_miss_dead_parent(p)

    print("\n== ulthar-enforcement-round1 SEND-BACK round 2: H-1 / H-2 / M-2 ==")
    test_h1_widening_wrapper_dies_worker_still_captured_and_reported(p)
    test_h1_residue_short_lived_wrapper_never_seen_allows(p)
    test_h2_known_pid_skips_name_cmdline_harvest(p)
    test_m2_stop_dev_orphans_merges_ledgers_sharing_root(p)

    print("\n== ulthar-enforcement-round1 SEND-BACK round 3: H-2R / H-4 ==")
    test_h4_boundary_children_never_widened_in_across_three_updates(p)
    test_h2r_cold_update_session_ledger_under_budget(p)
    test_ppid_map_fallback_descendants_only_no_widening(p)

    print("\n== ulthar-enforcement-round1 SEND-BACK round 4 (Codex third-party) ==")
    test_root_resolution_exhausts_substrings_before_name_fallback(p)
    test_descendant_boundary_is_substring_only_vite_workerd_captured(p)
    test_fallback_prunes_at_foreign_boundary(p)
    test_pid_reuse_same_ppid_by_foreign_root_children_not_ingested(p)
    test_kill_reverifies_identity_before_each_signal(p)
    test_session_id_sanitization_is_injective(p)
    test_ledger_identity_mismatch_discards_entries(p)
    test_lock_contention_skips_update_without_corrupting_ledger(p)
    test_sweep_race_survives_concurrent_write_after_lock_acquired(p)
    test_sweep_never_deletes_ledger_with_alive_root(p)
    test_max_entries_evicts_dead_before_alive_and_oldest_dead_first(p)
    test_inaccessible_intermediary_is_recorded_but_never_traversed(p)
    test_tmp_file_cleaned_up_on_write_crash(p)

    print("\n== ulthar-enforcement-round1 SEND-BACK round 5 ==")
    test_widening_excludes_reused_live_foreign_parent(p)

    print("\n== ulthar-enforcement-round1 SEND-BACK round 6 (Codex delta r2) ==")
    test_medium1_stale_true_boundary_after_reuse_unblocks_own_subtree(p)
    test_medium1_fallback_stale_true_boundary_after_reuse_unblocks_subtree(p)
    test_medium2_sweep_preserves_unknown_root(p)
    test_medium2_prune_preserves_unknown_entries(p)
    test_medium2_guard_never_reports_unknown_identity(p)
    test_medium2_cli_kill_never_signals_unknown_identity(p)

    print("\n== ulthar-enforcement-round1 SEND-BACK round 7 (Codex delta r3) ==")
    test_high_partial_identity_exe_denied_boundary_is_unknown(p)
    test_medium_stale_placeholder_reharvested_and_reclassified(p)

    print("\n== stop_dev_orphans._find_targets composition ==")
    test_stop_dev_orphans_default_orphans_only(p)

    print("\n== round-2 send-back regressions (RED then GREEN) ==")
    test_regression_item2_root_matching_is_case_insensitive(p)
    test_regression_item3_real_find_dev_processes_or_semantics(p)

    print(f"\nRESULT: {p.passed} passed, {p.failed} failed")
    return 1 if p.failed else 0


if __name__ == "__main__":
    sys.exit(main())
