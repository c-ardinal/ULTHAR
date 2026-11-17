"""R1-e AC-1 (the second window), AC-2 (relay triggers), AC-3 (enforcement while a relay is
due) and AC-5 (recovery after a lapsed lease), through the production hook entry and CLI.

Lease expiry is made by rewriting runtime.db's expiry time (R1-d AC-8: no clock variable)."""
from __future__ import annotations

import json
import os
import subprocess
import sys

from helpers import HOOK_ARGV, Probe, Sandbox, write_jsonl
from r1c_helpers import cli, contract, new_sandbox, quant_ac, set_value, to_build, transcript, write_contract
from r1d_helpers import advance, expire_lease, integrate, new_r1d_sandbox, to_accepted
from r1e_helpers import (OTHER, WINDOW, assistant_line, call, clear_relay_due, flags,
                         guard_of_last, set_config, set_relay_due)


def _overlay(sb: Sandbox) -> None:
    """As test_scope: the v1 rule against shell file creation is off (an owner overlay), so
    a redirect reaches the R1-e checks themselves."""
    (sb.work / ".claude" / "ulthar-overlay.json").write_text(json.dumps(
        {"disable": ["command_rules.tool_discipline/shell-file-creation"]}), encoding="utf-8")


def _edit(sb: Sandbox, rel: str, session: str = WINDOW, agent: str | None = None):
    return call(sb, "Edit", {"file_path": str(sb.work / rel), "old_string": "v", "new_string": "w"},
                session=session, agent=agent)


def check_second_window(p: Probe) -> None:
    sb = new_sandbox()
    try:
        _overlay(sb)
        unit_id = to_build(sb)  # the window (WINDOW) holds the lease since its first call
        holder = _edit(sb, "src/app.txt")
        edit = _edit(sb, "src/app.txt", session=OTHER)
        redirect = call(sb, "Bash", {"command": "echo x > src/app.txt"}, session=OTHER)
        advance_call = call(sb, "Bash", {"command": f"ulthar unit advance {unit_id} quant_verified"},
                            session=OTHER)
        sub = _edit(sb, "src/app.txt", session=OTHER, agent="other-sub")
        read = call(sb, "Read", {"file_path": str(sb.work / "src" / "app.txt")}, session=OTHER)
        status = call(sb, "Bash", {"command": "ulthar unit show " + unit_id}, session=OTHER)
        blocked = [r for r in (edit, redirect, advance_call, sub)
                   if r.returncode == 2 and "window lease" in r.stderr and WINDOW in r.stderr
                   and "until" in r.stderr]
        p.check("AC-1: another window's Edit, Bash redirect, `ulthar unit advance` and its "
                f"subagent's Edit are blocked naming the holder and expiry: {len(blocked)}/4",
                len(blocked) == 4, "\n".join(r.stderr[-300:] for r in (edit, redirect, advance_call, sub)))
        allowed = [r for r in (holder, read) if r.returncode == 0]
        p.check(f"AC-1: the holder's write and the other window's Read pass: {len(allowed)}/2",
                len(allowed) == 2, holder.stderr[-300:] + read.stderr[-300:])
        p.check("AC-1: a read-only ulthar command of the other window passes",
                status.returncode == 0, status.stderr[-300:])
    finally:
        sb.cleanup()


ROUNDS = 3
RACERS = 3


def _race(sb: Sandbox, sessions: list[str]) -> list[int]:
    """The same product Edit from several windows at once, as real concurrent hook
    processes; their exit codes."""
    procs = []
    for s in sessions:
        body = {"session_id": s, "tool_name": "Edit", "tool_use_id": f"race-{s}",
                "hook_event_name": "PreToolUse", "cwd": str(sb.work),
                "tool_input": {"file_path": str(sb.work / "src" / "app.txt"),
                               "old_string": "v", "new_string": "w"}}
        proc = subprocess.Popen(HOOK_ARGV + ["--event", "PreToolUse"], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=sb.env(),
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        procs.append((proc, json.dumps(body).encode("utf-8")))
    for proc, data in procs:  # every process is started before any gets its input
        proc.stdin.write(data)
        proc.stdin.close()
    codes = []
    for proc, _ in procs:
        proc.stdout.read()
        proc.stderr.read()
        codes.append(proc.wait(timeout=120))
        proc.stdout.close()
        proc.stderr.close()
    return codes


def check_race(p: Probe) -> None:
    """AC-1 / AC-4: the allow and the lease's acquisition are one serialized decision -- of
    windows racing for a free lease, or for a lease in handover (ulthar relay), exactly one
    is allowed, and it is the session that then holds the lease."""
    sb = new_sandbox()
    try:
        to_build(sb)
        results = []
        for kind in ("free", "handover"):
            for r in range(ROUNDS):
                if kind == "free":
                    expire_lease(sb)
                else:
                    sb.query("UPDATE leases SET session_id=NULL, handover_from=?,"
                             " expires_at='2999-01-01T00:00:00.000+00:00'", (WINDOW,))
                sessions = [f"race-{kind}-{r}-{i}" for i in range(RACERS)]
                codes = _race(sb, sessions)
                holder = sb.query("SELECT session_id FROM leases")[0][0]
                allowed = [s for s, c in zip(sessions, codes) if c == 0]
                results.append(allowed == [holder] and sorted(codes) == [0] + [2] * (RACERS - 1))
                p.check(f"AC-1: {RACERS} windows racing for a {kind} lease (round {r + 1}) -> exactly "
                        "one allowed, the one holding the lease", results[-1], f"{codes} {holder}")
        p.check(f"AC-1: races with exactly one allow {sum(results)}/{len(results)}", all(results))
    finally:
        sb.cleanup()


def check_relay_due(p: Probe) -> None:
    sb = new_sandbox()
    try:
        _overlay(sb)
        to_build(sb)
        set_relay_due(sb)
        blocked = {
            "Edit": _edit(sb, "src/app.txt"),
            "Bash write": call(sb, "Bash", {"command": "echo x > notes.txt"}),
            "Agent": call(sb, "Agent", {"description": "w", "prompt": "work"}),
            "subagent Edit": _edit(sb, "src/app.txt", agent="sub-1"),
        }
        n = sum(r.returncode == 2 and "relay due" in r.stderr for r in blocked.values())
        p.check(f"AC-3: while relay_due is set, Edit, a Bash write and an Agent launch are "
                f"blocked (and a subagent's Edit): {n}/4", n == 4,
                "\n".join(f"{k}: {r.returncode} {r.stderr[-200:]}" for k, r in blocked.items()))
        read = call(sb, "Read", {"file_path": str(sb.work / "src" / "app.txt")})
        relay = call(sb, "Bash", {"command": "ulthar relay"})
        ok = read.returncode == 0 and relay.returncode == 0 and "--runner-token" in relay.stdout
        p.check("AC-3: Read and `ulthar relay` pass (the relay gets its runner token): 2/2", ok,
                read.stderr[-200:] + relay.stdout[-200:] + relay.stderr[-200:])
    finally:
        sb.cleanup()


def _usage_case(p: Probe, pct: int, expect_due: bool) -> bool:
    sb = Sandbox()
    try:
        path = transcript(sb)
        write_jsonl(path, [{"type": "user", "uuid": "u-1", "message": {"role": "user", "content": "go"}},
                           assistant_line("a-1", 2000 * pct)])  # of the default 200,000 window
        call(sb, "Read", {"file_path": str(path)}, transcript_path=str(path))
        due = flags(sb)[0]
        used = sb.query("SELECT ctx_used, ctx_window FROM sessions WHERE session_id=?", (WINDOW,))
        ok = bool(due) == expect_due and used == [(2000 * pct, 200000)]
        p.check(f"AC-2 (b): usage {pct}% of relay.context_window -> relay_due "
                f"{'set' if expect_due else 'not set'}", ok, f"{due} {used}")
        return ok
    finally:
        sb.cleanup()


def _bad_threshold(p: Probe) -> bool:
    sb = Sandbox()
    try:
        results = {}
        for pct in (49, 71, 50, 70):
            set_config(sb, relay={"threshold_pct": pct})
            results[pct] = call(sb, "Bash", {"command": "ulthar issue add --title x"})
        ok = (all(results[x].returncode == 2 and "threshold_pct" in results[x].stderr for x in (49, 71))
              and all(results[x].returncode == 0 for x in (50, 70)))
        p.check("AC-2: relay.threshold_pct outside 50..70 is refused (49, 71 block with the reason); "
                "the ends 50 and 70 are accepted", ok,
                " | ".join(f"{k}: {r.returncode} {r.stderr[-150:]}" for k, r in results.items()))
        return ok
    finally:
        sb.cleanup()


def _halted(p: Probe) -> bool:
    sb = new_sandbox()
    try:
        unit_id = to_build(sb, contract(acs=[quant_ac()], budget={"loops": 1}))
        set_value(sb, "AC-1", "9")
        from r1c_helpers import runner  # noqa: PLC0415
        runner(sb, "bld-1", ["verify", unit_id, "--self"])
        state = json.loads(cli(sb, "unit", "show", unit_id).stdout)["state"]
        due = flags(sb)[0]
        ok = state == "halted" and bool(due) and unit_id in due
        p.check("AC-2 (a): a unit moved to halted -> relay_due", ok, f"{state} {due}")
        clear_relay_due(sb)
        cli(sb, "unit", "show", unit_id)  # opening the unit re-applies its operation (recovery)
        p.check("AC-2 (a): ... raised once per operation: a recovery re-run does not raise it again",
                flags(sb)[0] is None, str(flags(sb)))
        return ok
    finally:
        sb.cleanup()


def _done(p: Probe) -> bool:
    sb = new_r1d_sandbox()
    try:
        unit_id = to_accepted(sb)
        res = integrate(sb, unit_id)
        mid = flags(sb)[0]
        advance(sb, unit_id, "done")
        due = flags(sb)[0]
        ok = res.returncode == 0 and mid is None and bool(due) and "done" in due
        p.check("AC-2 (a): a unit moved to done -> relay_due (not before)", ok,
                f"{res.stderr[-300:]} {mid} {due}")
        return ok
    finally:
        sb.cleanup()


def check_triggers(p: Probe) -> None:
    n = sum([_done(p), _halted(p), _usage_case(p, 59, False), _usage_case(p, 61, True),
             _bad_threshold(p)])
    p.check(f"AC-2: {n}/5", n == 5)


def _lock(path):
    """Holds the transcript so the hook cannot read it (another process's exclusive lock);
    returns the release function."""
    fh = open(path, "rb+")
    if os.name == "nt":
        import msvcrt  # noqa: PLC0415

        size = os.path.getsize(path)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, size)

        def release():
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, size)
            fh.close()
        return release
    os.chmod(path, 0)

    def release_posix():
        os.chmod(path, 0o644)
        fh.close()
    return release_posix


def check_recovery(p: Probe) -> None:
    sb = Sandbox()
    try:
        call(sb, "Read", {"file_path": "x"}, session="old-window")
        old = sb.query("SELECT fence_token FROM leases")[0][0]
        expire_lease(sb)
        path = sb.root / "next-window.jsonl"
        write_jsonl(path, [{"type": "user", "uuid": f"n-{i}", "message": {"role": "user",
                                                                         "content": f"owner line {i}"}}
                           for i in range(3)])
        start = sb.hook("SessionStart", {"session_id": "next-window", "source": "startup",
                                         "transcript_path": str(path)})
        lease = sb.query("SELECT session_id, fence_token FROM leases")[0]
        cursor = sb.query("SELECT byte_offset FROM ingest_cursors WHERE session_id='next-window'")
        eps = sb.episodes("owner_msg", "next-window")
        edit = call(sb, "Edit", {"file_path": str(sb.work / "a.txt")}, session="next-window",
                    transcript_path=str(path))
        ok = (start.returncode == 0 and lease == ("next-window", old + 1)
              and cursor == [(os.path.getsize(path),)] and len(eps) == 3 and edit.returncode == 0)
        p.check("AC-5: after the lapse, the next window's SessionStart takes the lease (token +1) "
                "and ingests its whole transcript; its work is then allowed", ok,
                f"{lease} {cursor} {len(eps)} {start.stderr[-200:]} {edit.stderr[-200:]}")
        write_jsonl(path, [{"type": "user", "uuid": "n-9", "message": {"role": "user",
                                                                      "content": "later"}}], mode="a")
        release = _lock(path)
        try:
            blocked = call(sb, "Edit", {"file_path": str(sb.work / "a.txt")}, session="next-window",
                           transcript_path=str(path))
            read = call(sb, "Read", {"file_path": str(sb.work / "a.txt")}, session="next-window",
                        transcript_path=str(path))
        finally:
            release()
        after = call(sb, "Edit", {"file_path": str(sb.work / "a.txt")}, session="next-window",
                     transcript_path=str(path))
        ok = (blocked.returncode == 2 and "could not be ingested" in blocked.stderr
              and read.returncode == 0 and after.returncode == 0)
        p.check("AC-5: a failed transcript ingest blocks the write (reads pass); once readable, "
                "the write passes", ok, blocked.stderr[-300:] + read.stderr[-200:] + after.stderr[-200:])
        p.check("AC-5: 2/2", ok)
    finally:
        sb.cleanup()


def main() -> int:
    p = Probe()
    print("\n== R1-e AC-1: the second window ==")
    check_second_window(p)
    print("\n== R1-e AC-1: racing windows ==")
    check_race(p)
    print("\n== R1-e AC-3: enforcement while a relay is due ==")
    check_relay_due(p)
    print("\n== R1-e AC-2: relay triggers ==")
    check_triggers(p)
    print("\n== R1-e AC-5: recovery after a lapsed lease ==")
    check_recovery(p)
    return p.result()


if __name__ == "__main__":
    sys.exit(main())
