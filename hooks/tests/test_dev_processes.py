#!/usr/bin/env python3
"""hooks/tests/test_dev_processes.py -- guards.dev_process_hygiene + stop_dev_orphans.

CONTRACT.md #5 U-5 row, #6 (B) minimum >=4 (orphan found / no orphan / dead-parent
detection / stop_hook_active) and the PE-12 fixture (stubbed python3 orphan +
node.exe .exe-stripping pairing). CONTRACT.md misreading item and #6 'Stubbing
external commands': "Never enumerate or kill real processes in a test. Stub
list_processes." -- every case here uses a STUBBED process table, never the real
machine's.

STUBBING NOTE: `lib/procs.find_dev_processes()` calls the bare module-level
`list_processes(ctx, cfg)` name -- i.e. `lib.procs.list_processes`, NOT
`ctx.list_processes()` -- so `Ctx._list_processes_fn` (the seam CONTRACT.md #1
documents as "what makes ... the orphan predicate testable without touching the
real machine") is never consulted by `find_dev_processes`. Tests here instead
monkeypatch the `lib.procs.list_processes` module attribute directly for the
duration of one case (try/finally restore) -- this stubs the SAME call site
`find_dev_processes` actually reaches, without editing lib/procs.py.
"""
from __future__ import annotations

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


class _StubbedProcs:
    """Context manager: monkeypatches lib.procs.list_processes for one case.

    ponytail: a fixed, hand-authored process table, never the real psutil
    listing. Fine as long as find_dev_processes' own logic (name/pattern match,
    orphan predicate) is the thing under test; a real-psutil integration pass
    would be the upgrade path if this stub ever drifts from what psutil.Process
    actually reports on this platform."""

    def __init__(self, procs: list) -> None:
        self._procs = procs
        self._orig = None

    def __enter__(self):
        self._orig = procs_mod.list_processes

        def _stub(ctx, cfg):
            return list(self._procs)

        procs_mod.list_processes = _stub
        return self

    def __exit__(self, *exc):
        procs_mod.list_processes = self._orig
        return False


def _proc(pid, ppid, name, cmdline):
    return procs_mod.ProcInfo(pid=pid, ppid=ppid, name=name, cmdline=cmdline)


def _run_guard(sandbox: SandboxCtx, procs: list, payload: dict | None = None):
    ctx = sandbox.build(payload=payload, event="Stop")
    with _StubbedProcs(procs):
        return guard_mod.run(payload or {}, sandbox.cfg, ctx)


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
        r_alive = _run_guard(sandbox, alive_parent)
        r_dead = _run_guard(sandbox, dead_parent)
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
        result = _run_guard(sandbox, python3_orphan)
        p.check(
            "PE-12: python3 orphan (never in the master's $NAMES) is flagged by the new side",
            result.exit_code == 2 and "7001" in result.stderr,
            repr(result),
        )

        node_orphan = [_proc(7002, 999999, "node.exe", "node ./node_modules/.bin/vite")]
        result2 = _run_guard(sandbox, node_orphan)
        p.check(
            "PE-12 pairing: node.exe (.exe-stripped -> 'node', a legacy master name) still matches",
            result2.exit_code == 2 and "7002" in result2.stderr,
            repr(result2),
        )

        drawio_orphan = [_proc(7003, 999999, "draw.io.exe", "draw.io.exe --export foo.drawio")]
        result3 = _run_guard(sandbox, drawio_orphan)
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


# ------------------------------------------------------------------ stop_dev_orphans._find_targets composition


def test_stop_dev_orphans_default_orphans_only(p: Probe) -> None:
    sandbox = SandboxCtx()
    try:
        ctx = sandbox.build(event="CLI")
        orphan = _proc(1, 999999, "node.exe", "npm run dev")
        rooted_non_orphan = _proc(2, 3, "node.exe", "npm run dev -- C:\\path\\to\\project")

        orig = procs_mod.find_dev_processes

        def _stub(cfg, ctx_, orphans_only=True, root=None):
            # Mirrors the REAL (fixed) lib.procs.find_dev_processes OR semantics:
            # is_orphan OR (not orphans_only AND (root is None or root in cmdline)) --
            # NOT a branch that picks one or the other (see
            # test_regression_item3_real_find_dev_processes_or_semantics below for a
            # test against the REAL function, unstubbed).
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

        procs_mod.find_dev_processes = _stub
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
            procs_mod.find_dev_processes = orig
    finally:
        sandbox.cleanup()


# ------------------------------------------------------------------ regression checks


def _stub_or_semantics_matching_real_lib(orphan, in_root, out_of_root):
    """A stub whose `orphans_only=False` branch mirrors the CURRENT (fixed)
    lib.procs.find_dev_processes exactly: `is_orphan OR (root is None or root in
    cmdline)`, case-SENSITIVE root match -- the same shape the real function has
    today, so a test exercising THIS stub's case-sensitivity is exercising the same
    bug class the real function's `root in proc.cmdline` (lib/procs.py line ~100)
    still has."""
    all_procs = [orphan, in_root, out_of_root]

    def _stub(cfg, ctx_, orphans_only=True, root=None):
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

    return _stub


def test_regression_item2_root_matching_is_case_insensitive(p: Probe) -> None:
    """Pins `--root` against a case-sensitivity regression: PowerShell's `-like` is
    case-insensitive by default, but a Python port using `in` is not. Fixed in
    stop_dev_orphans._find_targets() by never handing `root` to
    find_dev_processes() (always root=None there) and instead filtering the
    candidate set locally with `.casefold()` (module docstring's COMPOSITION
    NOTE)."""
    sandbox = SandboxCtx()
    try:
        ctx = sandbox.build(event="CLI")
        orphan = _proc(1, 999999, "node.exe", "npm run dev")
        in_root_different_case = _proc(2, 3, "node.exe", "npm run dev -- C:\\REPO\\PROJECT")
        out_of_root = _proc(3, 4, "node.exe", "npm run dev -- C:\\Nowhere")
        stub = _stub_or_semantics_matching_real_lib(orphan, in_root_different_case, out_of_root)
        root_arg = "c:\\repo\\project"  # deliberately lower-case vs the cmdline's upper-case

        orig = procs_mod.find_dev_processes
        procs_mod.find_dev_processes = stub
        try:
            # RED: the OLD _find_targets (pre-fix) handed `root` straight into
            # find_dev_processes(), inheriting its case-SENSITIVE `in` check.
            def _old_find_targets(cfg, ctx_, all_flag, root):
                orphans = procs_mod.find_dev_processes(cfg, ctx_, orphans_only=True)
                if not all_flag:
                    return orphans
                rooted = procs_mod.find_dev_processes(cfg, ctx_, orphans_only=False, root=(root or None))
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
            procs_mod.find_dev_processes = orig
    finally:
        sandbox.cleanup()


def test_regression_item3_real_find_dev_processes_or_semantics(p: Probe) -> None:
    """Pins the OR-semantics fix in lib/procs.py (find_dev_processes: is_orphan OR
    (--all AND root-match), never a branch that picks one or the other):
    test_stop_dev_orphans_default_orphans_only stubs find_dev_processes itself, so
    a mutation reverting the fix back to branch semantics would stay invisible
    there. This test calls the REAL, unstubbed lib.procs.find_dev_processes,
    stubbing only at the psutil-facing boundary (lib.procs.list_processes, via
    _StubbedProcs)."""
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

        with _StubbedProcs(table):
            # RED: the OLD branch-based implementation (pre-fix), reproduced
            # here verbatim, driven through the SAME real psutil-facing boundary
            # (procs_mod.list_processes, stubbed by _StubbedProcs) and the SAME
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

            # GREEN: the REAL, current, UNSTUBBED lib.procs.find_dev_processes.
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

    print("\n== stop_dev_orphans._find_targets composition ==")
    test_stop_dev_orphans_default_orphans_only(p)

    print("\n== round-2 send-back regressions (RED then GREEN) ==")
    test_regression_item2_root_matching_is_case_insensitive(p)
    test_regression_item3_real_find_dev_processes_or_semantics(p)

    print(f"\nRESULT: {p.passed} passed, {p.failed} failed")
    return 1 if p.failed else 0


if __name__ == "__main__":
    sys.exit(main())
