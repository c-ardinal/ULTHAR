"""R1-b process hygiene of the background extractor:
  AC-18 (Windows) the worker the hook starts, the extractor command and the extractor's
        own child open no new visible console window: each reports its console window
        (GetConsoleWindow) as absent or hidden, and no new visible top-level console
        window appears while they run;
  AC-19 an extractor running past extractor.timeout_sec is killed with its whole tree
        and the extraction recorded failed; nothing it started survives.
"""
from __future__ import annotations

import json
import os
import sys
import time

from helpers import Probe, Sandbox, processes_matching
from test_extract import wait_extractions

CONSOLE_CLASSES = {"ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS"}
TIMEOUT_SEC = 2


def visible_windows() -> dict[int, tuple[str, int]]:
    """{hwnd: (class name, owning pid)} of every visible top-level window."""
    import ctypes  # noqa: PLC0415
    from ctypes import wintypes  # noqa: PLC0415

    user32 = ctypes.windll.user32
    found: dict[int, tuple[str, int]] = {}
    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def collect(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd):
            name = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, name, 256)
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            found[int(hwnd)] = (name.value, pid.value)
        return True

    user32.EnumWindows(proto(collect), 0)
    return found


def check_no_console_window(p: Probe) -> None:
    if os.name != "nt":
        print("SKIP AC-18: console windows are a Windows matter")
        return
    sb = Sandbox()
    try:
        out = sb.root / "console.json"
        sb.configure(["console", str(out), "3"])
        sb.cli("db", "init")
        before = visible_windows()
        proc = sb.hook("UserPromptSubmit", {"session_id": "win", "prompt": "窓を出さないで",
                                            "prompt_id": "win-1"})
        deadline = time.monotonic() + 30
        while not out.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        during = visible_windows()  # the worker, extractor and grandchild are alive now
        report = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}
        rows = wait_extractions(sb)
        tree = {report.get("worker_pid"), report.get("extractor_pid"),
                (report.get("grandchild") or {}).get("pid")}
        new = {h: v for h, v in during.items() if h not in before
               and (v[0] in CONSOLE_CLASSES or v[1] in tree)}
        states = [report.get("extractor") or {}, report.get("grandchild") or {}]
        p.check("AC-18: the extraction ran through the probe (worker -> extractor -> child)",
                proc.returncode == 0 and bool(report) and [r[1] for r in rows] == ["done"],
                f"{rows} {proc.stderr[-300:]}")
        p.check("AC-18: extractor and its child have no visible console window "
                f"(worker's console, inherited): {states}",
                bool(report) and all(not s.get("visible") for s in states))
        p.check(f"AC-18: new visible console windows while they ran: {len(new)}", not new,
                str(new))
    finally:
        sb.cleanup()


def check_timeout_kills_tree(p: Probe) -> None:
    sb = Sandbox()
    try:
        marker = f"ulthar-hang-{os.urandom(4).hex()}"
        sb.configure(["hang", marker])
        cfg = json.loads((sb.work / ".claude" / "ulthar.json").read_text(encoding="utf-8"))
        cfg["extractor"]["timeout_sec"] = TIMEOUT_SEC
        (sb.work / ".claude" / "ulthar.json").write_text(json.dumps(cfg), encoding="utf-8")
        started = time.monotonic()
        sb.hook("UserPromptSubmit", {"session_id": "hang", "prompt": "時間切れの試験",
                                     "prompt_id": "hang-1"})
        rows = wait_extractions(sb)
        took = time.monotonic() - started
        deadline = time.monotonic() + 5
        left = processes_matching(marker)
        while left and time.monotonic() < deadline:
            time.sleep(0.2)
            left = processes_matching(marker)
        detail = json.loads(rows[0][2] or "{}") if rows else {}
        p.check(f"AC-19: an extractor past timeout_sec={TIMEOUT_SEC} is recorded failed "
                f"(after {took:.1f} s)", [r[1] for r in rows] == ["failed"]
                and "timed out" in detail.get("error", "") and took < 30, str(rows))
        p.check(f"AC-19: ... and its whole tree is gone (left: {len(left)})", not left, str(left))
    finally:
        sb.cleanup()


def _wait_gone(marker: str, seconds: float = 5) -> list:
    deadline = time.monotonic() + seconds
    left = processes_matching(marker)
    while left and time.monotonic() < deadline:
        time.sleep(0.2)
        left = processes_matching(marker)
    return left


def check_immediate_child_contained(p: Probe) -> None:
    """Review finding 7: an extractor that starts a child at once (cmd.exe, far faster
    than a Python start-up) cannot escape: it is created suspended and joins the job
    before running."""
    if os.name != "nt":
        print("SKIP: job objects are Windows-only")
        return
    sb = Sandbox()
    try:
        marker = f"ulthar-fast-{os.urandom(4).hex()}"
        cfg_path = sb.work / ".claude" / "ulthar.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg["extractor"] = {"timeout_sec": TIMEOUT_SEC, "cmd": [
            "cmd", "/c", "start", "fastchild", "/b", sys.executable, "-S", "-c", "import time; time.sleep(60)",
            marker, "&", "ping", "-n", "60", "127.0.0.1"]}
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        sb.hook("UserPromptSubmit", {"session_id": "fast", "prompt": "すぐ子を作る抽出",
                                     "prompt_id": "fast-1"})
        rows = wait_extractions(sb)
        left = _wait_gone(marker)
        p.check("an extractor that spawns a child immediately: timed out, failed recorded",
                [r[1] for r in rows] == ["failed"] and "timed out" in (rows[0][2] or ""), str(rows))
        p.check(f"  ... and the immediately spawned child is gone too (left: {len(left)})",
                not left, str(left))
    finally:
        sb.cleanup()


def check_no_job_no_run(p: Probe) -> None:
    """Review finding 7: when the job object cannot be made, the suspended extractor is
    terminated before it runs (its child never starts) and the run fails."""
    if os.name != "nt":
        print("SKIP: job objects are Windows-only")
        return
    from ulthar import worker  # noqa: PLC0415

    sb = Sandbox()
    marker = f"ulthar-nojob-{os.urandom(4).hex()}"
    real = worker._KillJob

    def no_job(proc):
        raise OSError(5, "injected: AssignProcessToJobObject failed")

    try:
        worker._KillJob = no_job
        with sb.inproc():
            try:
                worker._run_extractor([sys.executable, "-S", "-c",
                                       "import subprocess, sys, time; subprocess.Popen([sys.executable,"
                                       " '-S', '-c', 'import time; time.sleep(60)', sys.argv[1]]);"
                                       " time.sleep(60)", marker], "prompt", 30)
                err = "ran"
            except RuntimeError as exc:
                err = str(exc)
        time.sleep(1)
        left = processes_matching(marker)
        p.check("job setup failing (forced): the extractor is not run, failure raised",
                "could not be contained" in err, err)
        p.check(f"  ... nothing it would have started exists (left: {len(left)})", not left,
                str(left))
    finally:
        worker._KillJob = real
        sb.cleanup()


def main() -> int:
    p = Probe()
    print("\n== AC-18: no console windows ==")
    check_no_console_window(p)
    print("\n== AC-19: extractor timeout kills the tree ==")
    check_timeout_kills_tree(p)
    check_immediate_child_contained(p)
    check_no_job_no_run(p)
    return p.result()


if __name__ == "__main__":
    sys.exit(main())
