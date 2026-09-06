#!/usr/bin/env python3
"""hooks/tests/test_artifact_hygiene.py -- guards.artifact_hygiene.

CONTRACT.md #5 U-5 row, #6 'Fixture inventory': migrates all 8 golden cases from
test-artifact-hygiene-check.ps1 (QUIET no artifact dirs / QUIET empty dir / QUIET
fresh file / FIRE stale artifact / QUIET 2nd time (marker) / FIRE new session /
QUIET stop_hook_active / QUIET missing cwd), plus coverage for max_named
truncation, hidden-file inclusion, recursive-subdir scanning, and the
marker-written-only-when-found ordering (misreading item, artifact_dirs
`marker_written_only_when_found`).

Each case builds a real temp directory tree via SandboxCtx (conftest.py) and calls
guards.artifact_hygiene.run() directly with a real, frozen `ctx.now()` -- no
subprocess, no real machine state (the golden `.ps1` harness used $env:TEMP
directly; here the marker lives under the sandboxed ctx.temp_dir).
"""
from __future__ import annotations

import importlib
import sys
from datetime import datetime
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
HOOKS_DIR = TESTS_DIR.parent
sys.path.insert(0, str(HOOKS_DIR))
sys.path.insert(0, str(TESTS_DIR))

from conftest import SandboxCtx  # noqa: E402
from lib import ctx as ctx_mod  # noqa: E402

guard_mod = importlib.import_module("guards.artifact_hygiene")


def _build_ctx(sandbox: SandboxCtx, payload: dict, event: str = "Stop") -> ctx_mod.Ctx:
    """Local replacement for SandboxCtx.build(), which hardcodes `cwd=self.cwd` and
    ignores `payload["cwd"]`. This guard's behaviour depends on cwd varying per case
    (clean/dirty/missing trees), so tests build the Ctx directly instead, resolving
    cwd the way lib.ctx.build_ctx does: `payload.get("cwd")`, falling back to the
    sandbox cwd."""
    return ctx_mod.Ctx(
        home=sandbox.home,
        state_dir=sandbox.home / ".claude" / "state",
        deploy_dir=sandbox.deploy_dir,
        skills_root=sandbox.home / ".claude" / "skills",
        logs_dir=sandbox.deploy_dir / "logs",
        temp_dir=sandbox.temp_dir,
        cwd=Path(payload.get("cwd") or sandbox.cwd),
        is_windows=sys.platform.startswith("win"),
        event=event,
        cfg=sandbox.cfg,
        _now_fn=lambda: sandbox._frozen_now,
        _run_cmd_fn=sandbox._run_cmd_stub,
        _list_processes_fn=sandbox._list_processes_stub,
    )


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


def _write_stale_file(path: Path, age_minutes: int, reference: datetime) -> None:
    """File mtime is set relative to `reference` (the SandboxCtx's frozen `now()`),
    NOT the real wall clock -- ctx.now() is frozen for the test, so "stale" must be
    measured against that same frozen point or the cutoff comparison is comparing
    two unrelated clocks."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")
    stale_ts = reference.timestamp() - age_minutes * 60
    import os

    os.utime(path, (stale_ts, stale_ts))


def _run(sandbox: SandboxCtx, session_id: str, cwd: Path, now: datetime | None = None):
    if now is not None:
        sandbox.set_now(now)
    payload = {"session_id": session_id, "cwd": str(cwd)}
    ctx = _build_ctx(sandbox, payload, event="Stop")
    return guard_mod.run(payload, sandbox.cfg, ctx)


# ------------------------------------------------------------------ golden set (8, migrated)


def test_quiet_no_artifact_dirs(p: Probe) -> None:
    sandbox = SandboxCtx()
    try:
        tmp = sandbox.cwd / "clean_root"
        tmp.mkdir(parents=True, exist_ok=True)
        result = _run(sandbox, "s1", tmp)
        p.check("QUIET no artifact dirs", result.exit_code == 0, repr(result))
    finally:
        sandbox.cleanup()


def test_quiet_empty_dir(p: Probe) -> None:
    sandbox = SandboxCtx()
    try:
        clean = sandbox.cwd / "clean"
        (clean / ".playwright-mcp").mkdir(parents=True, exist_ok=True)  # exists but empty
        result = _run(sandbox, "s2", clean)
        p.check("QUIET empty dir", result.exit_code == 0, repr(result))
    finally:
        sandbox.cleanup()


def test_quiet_fresh_file(p: Probe) -> None:
    sandbox = SandboxCtx()
    try:
        fresh = sandbox.cwd / "fresh"
        # written "just now" -> inside the grace window, must stay quiet
        _write_stale_file(fresh / "test-results" / "run.json", age_minutes=0, reference=sandbox._frozen_now)
        result = _run(sandbox, "s3", fresh)
        p.check("QUIET fresh file (inside grace window)", result.exit_code == 0, repr(result))
    finally:
        sandbox.cleanup()


def test_fire_stale_artifact(p: Probe) -> None:
    sandbox = SandboxCtx()
    try:
        dirty = sandbox.cwd / "dirty"
        _write_stale_file(dirty / ".playwright-mcp" / "shot.png", age_minutes=30, reference=sandbox._frozen_now)
        result = _run(sandbox, "s4", dirty)
        p.check(
            "FIRE stale artifact -> exit 2, message names dir and file",
            result.exit_code == 2 and ".playwright-mcp" in result.stderr and "shot.png" in result.stderr,
            repr(result),
        )
    finally:
        sandbox.cleanup()


def test_quiet_second_time_marker(p: Probe) -> None:
    sandbox = SandboxCtx()
    try:
        dirty = sandbox.cwd / "dirty2"
        _write_stale_file(dirty / ".playwright-mcp" / "shot.png", age_minutes=30, reference=sandbox._frozen_now)
        first = _run(sandbox, "s4b", dirty)
        second = _run(sandbox, "s4b", dirty)  # same session_id, same dirty cwd
        p.check(
            "FIRE then QUIET 2nd time (marker) -- same session_id",
            first.exit_code == 2 and second.exit_code == 0,
            f"first={first!r} second={second!r}",
        )
    finally:
        sandbox.cleanup()


def test_fire_new_session(p: Probe) -> None:
    sandbox = SandboxCtx()
    try:
        dirty = sandbox.cwd / "dirty3"
        _write_stale_file(dirty / ".playwright-mcp" / "shot.png", age_minutes=30, reference=sandbox._frozen_now)
        first = _run(sandbox, "s5a", dirty)
        second = _run(sandbox, "s5b", dirty)  # DIFFERENT session_id, same dirty cwd
        p.check(
            "FIRE new session -- a different session_id is not silenced by another's marker",
            first.exit_code == 2 and second.exit_code == 2,
            f"first={first!r} second={second!r}",
        )
    finally:
        sandbox.cleanup()


def test_quiet_stop_hook_active(p: Probe) -> None:
    """CONTRACT.md #1/#5: stop_hook_active is the DISPATCHER's responsibility --
    proven via the real dispatch.py subprocess (single-guard mode), against a dirty
    cwd that would otherwise fire, exactly like the golden fixture."""
    import json
    import os
    import subprocess

    sandbox = SandboxCtx()
    try:
        dirty = sandbox.cwd / "dirty4"
        _write_stale_file(dirty / ".playwright-mcp" / "shot.png", age_minutes=30, reference=sandbox._frozen_now)

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
            "session_id": "s6",
            "cwd": str(dirty),
            "stop_hook_active": True,
        }
        proc = subprocess.run(
            [sys.executable, str(HOOKS_DIR / "dispatch.py"), "--event", "Stop", "--guard", "artifact_hygiene"],
            input=json.dumps(payload).encode("utf-8"),
            capture_output=True,
            env=env,
            timeout=30,
        )
        p.check(
            "QUIET stop_hook_active (dispatcher short-circuit, real dispatch.py subprocess, "
            "cwd IS dirty -- would fire if the guard itself ran)",
            proc.returncode == 0,
            f"rc={proc.returncode} stderr={proc.stderr!r}",
        )
    finally:
        sandbox.cleanup()


def test_quiet_missing_cwd(p: Probe) -> None:
    sandbox = SandboxCtx()
    try:
        result = _run(sandbox, "s7", sandbox.root / "no" / "such" / "path")
        p.check("QUIET missing cwd", result.exit_code == 0, repr(result))
    finally:
        sandbox.cleanup()


# ------------------------------------------------------------------ ordering: marker written only when found


def test_marker_not_consumed_by_clean_scan(p: Probe) -> None:
    """marker_written_only_when_found: a session that scans CLEAN must not burn its
    marker -- it can still fire later in the SAME session once artifacts appear."""
    sandbox = SandboxCtx()
    try:
        target = sandbox.cwd / "later-dirty"
        target.mkdir(parents=True, exist_ok=True)
        (target / ".playwright-mcp").mkdir(parents=True, exist_ok=True)  # empty -> clean scan

        clean_result = _run(sandbox, "s8", target)
        p.check("clean scan (empty artifact dir) -> ALLOW", clean_result.exit_code == 0, repr(clean_result))

        # NOW artifacts appear, same session
        _write_stale_file(target / ".playwright-mcp" / "shot.png", age_minutes=30, reference=sandbox._frozen_now)
        dirty_result = _run(sandbox, "s8", target)
        p.check(
            "the SAME session still fires once artifacts actually appear "
            "(clean scan did not consume the marker)",
            dirty_result.exit_code == 2,
            repr(dirty_result),
        )
    finally:
        sandbox.cleanup()


# ------------------------------------------------------------------ extra coverage


def test_max_named_truncation(p: Probe) -> None:
    sandbox = SandboxCtx()
    try:
        target = sandbox.cwd / "many-stale"
        for i in range(10):
            _write_stale_file(target / "test-results" / f"f{i:02d}.json", age_minutes=30, reference=sandbox._frozen_now)
        result = _run(sandbox, "s9", target)
        max_named = sandbox.cfg["artifact_dirs"]["max_named"]
        p.check(
            f"max_named truncation: 10 files, only {max_named} named + '(+n more)' suffix",
            result.exit_code == 2 and f"+{10 - max_named} more" in result.stderr,
            repr(result.stderr),
        )
    finally:
        sandbox.cleanup()


def test_hidden_file_included(p: Probe) -> None:
    """M-7: the guard always includes hidden/dot files (unconditional, not config-gated)."""
    sandbox = SandboxCtx()
    try:
        target = sandbox.cwd / "hidden-stale"
        _write_stale_file(target / "playwright-report" / ".hidden-trace.zip", age_minutes=30, reference=sandbox._frozen_now)
        result = _run(sandbox, "s10", target)
        p.check(
            "hidden (dot-prefixed) stale file is included in the count",
            result.exit_code == 2 and ".hidden-trace.zip" in result.stderr,
            repr(result.stderr),
        )
    finally:
        sandbox.cleanup()


def test_recursive_subdir_scan(p: Probe) -> None:
    """M-7: the guard always descends the WHOLE subtree (unconditional, not
    config-gated), not just the top level."""
    sandbox = SandboxCtx()
    try:
        target = sandbox.cwd / "nested-stale"
        _write_stale_file(
            target / "test-results" / "run-1" / "trace" / "deep.trace.zip",
            age_minutes=30,
            reference=sandbox._frozen_now,
        )
        result = _run(sandbox, "s11", target)
        p.check(
            "a stale file several directories deep under an artifact dir is found",
            result.exit_code == 2 and "deep.trace.zip" in result.stderr,
            repr(result.stderr),
        )
    finally:
        sandbox.cleanup()


def test_disabled_via_config(p: Probe) -> None:
    sandbox = SandboxCtx()
    try:
        sandbox.cfg = dict(sandbox.cfg)
        sandbox.cfg["artifact_dirs"] = dict(sandbox.cfg["artifact_dirs"])
        sandbox.cfg["artifact_dirs"]["enabled"] = False
        target = sandbox.cwd / "would-fire"
        _write_stale_file(target / ".playwright-mcp" / "shot.png", age_minutes=30, reference=sandbox._frozen_now)
        result = _run(sandbox, "s12", target)
        p.check("artifact_dirs.enabled=false -> ALLOW even with stale files present", result.exit_code == 0, repr(result))
    finally:
        sandbox.cleanup()


# ------------------------------------------------------------------ regression checks


def _old_buggy_stale_files_under(root: Path, cutoff_ts: float) -> list[Path]:
    """Reproduces the PRE-FIX scan logic verbatim: the whole listing wrapped in one
    try/except that treats ANY OSError as "nothing in this directory", and an
    UNGUARDED stat() call. Used only to demonstrate RED before GREEN below; never
    shipped."""
    try:
        candidates = [f for f in root.rglob("*") if f.is_file()]
    except OSError:
        return []
    return [f for f in candidates if f.stat().st_mtime < cutoff_ts]


def test_regression_item1a_directory_level_swallow(p: Probe) -> None:
    """Item 1a: 'a single unreadable subdirectory makes the whole artifact
    directory report zero findings.' Path.rglob is monkeypatched to always raise,
    simulating exactly that. RED: the old helper (whole-listing try/except) reports
    NOTHING. GREEN: the fixed guard does not even call Path.rglob any more (it
    walks with os.walk + a per-directory onerror callback), so it is unaffected by
    the very fault that broke the old code, and still reports the real file."""
    sandbox = SandboxCtx()
    try:
        target = sandbox.cwd / "mixed-fault-a"
        keep = target / "test-results" / "keep-me.json"
        _write_stale_file(keep, age_minutes=30, reference=sandbox._frozen_now)

        real_rglob = Path.rglob

        def _raising_rglob(self, pattern):  # noqa: ANN001
            raise PermissionError("simulated: directory became unreadable mid-scan")
            yield  # pragma: no cover -- makes this a generator function, like the real rglob

        Path.rglob = _raising_rglob
        try:
            real_fn = guard_mod._stale_files_under
            guard_mod._stale_files_under = _old_buggy_stale_files_under
            try:
                red_result = _run(sandbox, "reg1a-old", target)
            finally:
                guard_mod._stale_files_under = real_fn
            p.check(
                "RED: pre-fix scan helper reports NOTHING when Path.rglob raises "
                "(should have reported keep-me.json)",
                red_result.exit_code == 0,
                repr(red_result),
            )

            green_result = _run(sandbox, "reg1a-new", target)
            p.check(
                "GREEN: the fixed guard (os.walk + onerror, not Path.rglob) is "
                "unaffected by the SAME simulated Path.rglob failure",
                green_result.exit_code == 2 and "keep-me.json" in green_result.stderr,
                repr(green_result),
            )
        finally:
            Path.rglob = real_rglob
    finally:
        sandbox.cleanup()


def test_regression_item1b_stat_failure_mid_scan(p: Probe) -> None:
    """Item 1b: 'stat() runs outside the try, so a file deleted mid-scan raises and
    turns the entire check into an ALLOW.' RED: the old helper's unguarded stat()
    call raises OUT of run() entirely -- exactly what dispatch.py's fail-open
    wrapper turns into a silent ALLOW for the whole guard, whole-session, not just
    for the one vanished file. GREEN: the fixed guard tolerates the vanished file
    and still reports the file that is still there."""
    sandbox = SandboxCtx()
    try:
        target = sandbox.cwd / "mixed-fault-b"
        keep = target / "test-results" / "keep-me.json"
        vanish = target / "test-results" / "vanish-me.json"
        _write_stale_file(keep, age_minutes=30, reference=sandbox._frozen_now)
        _write_stale_file(vanish, age_minutes=30, reference=sandbox._frozen_now)

        real_stat = Path.stat
        vanish_str = str(vanish)

        def _flaky_stat(self, *a, **kw):  # noqa: ANN001
            if str(self) == vanish_str:
                raise FileNotFoundError("simulated: file vanished mid-scan")
            return real_stat(self, *a, **kw)

        Path.stat = _flaky_stat
        try:
            real_fn = guard_mod._stale_files_under
            guard_mod._stale_files_under = _old_buggy_stale_files_under
            try:
                old_crashed = False
                old_result = None
                try:
                    old_result = _run(sandbox, "reg1b-old", target)
                except OSError:
                    old_crashed = True
            finally:
                guard_mod._stale_files_under = real_fn
            p.check(
                "RED: pre-fix helper's unguarded stat() raises OUT of run() instead "
                "of returning a Result (dispatch.py would silently ALLOW the whole check)",
                old_crashed,
                repr(old_result),
            )

            new_result = _run(sandbox, "reg1b-new", target)
            p.check(
                "GREEN: the fixed guard tolerates the vanished file and still "
                "reports keep-me.json (not vanish-me.json)",
                new_result.exit_code == 2
                and "keep-me.json" in new_result.stderr
                and "vanish-me.json" not in new_result.stderr,
                repr(new_result),
            )
        finally:
            Path.stat = real_stat
    finally:
        sandbox.cleanup()


def main() -> int:
    p = Probe()
    print("== golden set migrated from test-artifact-hygiene-check.ps1 (8 cases) ==")
    test_quiet_no_artifact_dirs(p)
    test_quiet_empty_dir(p)
    test_quiet_fresh_file(p)
    test_fire_stale_artifact(p)
    test_quiet_second_time_marker(p)
    test_fire_new_session(p)
    test_quiet_stop_hook_active(p)
    test_quiet_missing_cwd(p)

    print("\n== marker-written-only-when-found ordering ==")
    test_marker_not_consumed_by_clean_scan(p)

    print("\n== extra coverage ==")
    test_max_named_truncation(p)
    test_hidden_file_included(p)
    test_recursive_subdir_scan(p)
    test_disabled_via_config(p)

    print("\n== round-2 send-back regressions (RED then GREEN) ==")
    test_regression_item1a_directory_level_swallow(p)
    test_regression_item1b_stat_failure_mid_scan(p)

    print(f"\nRESULT: {p.passed} passed, {p.failed} failed")
    return 1 if p.failed else 0


if __name__ == "__main__":
    sys.exit(main())
