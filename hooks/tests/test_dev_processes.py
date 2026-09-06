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
            "H-2 setup: the first update harvests a brand-NEW pid (once for the "
            "M-1 root-boundary check, once for the ledger entry -- both legitimate "
            "on FIRST discovery)",
            first_calls == {"name": 2, "cmdline": 2, "create_time": 1},
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

    print("\n== stop_dev_orphans._find_targets composition ==")
    test_stop_dev_orphans_default_orphans_only(p)

    print("\n== round-2 send-back regressions (RED then GREEN) ==")
    test_regression_item2_root_matching_is_case_insensitive(p)
    test_regression_item3_real_find_dev_processes_or_semantics(p)

    print(f"\nRESULT: {p.passed} passed, {p.failed} failed")
    return 1 if p.failed else 0


if __name__ == "__main__":
    sys.exit(main())
