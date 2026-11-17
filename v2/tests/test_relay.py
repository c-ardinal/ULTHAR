"""R1-e AC-4: `ulthar relay` (SES-04, spec 事前決定 2 with P-21's fallback), through the production
hook entry and CLI. The launch command is a test script (`relay.launch`): no real `claude`.

Three unmet handover conditions are refused with their reason and change nothing: (a) a
unit whose last operation's runtime.db effects are missing, (b) a question awaiting the
owner, (c) a running subagent never registered. Then a successful relay (Episode, launch,
lease in handover -> the next session takes it with a higher token, the old window's
writes blocked, relay_due cleared, a notification), and a failed launch (nothing moved,
relay_due kept). No process is left behind and no window opens (CREATE_NO_WINDOW)."""
from __future__ import annotations

import json
import sys

from helpers import Probe, processes_matching
from r1c_helpers import answer, cli, confirm_contract, make_unit, write_contract, contract, new_sandbox
from r1d_helpers import run_ulthar
from r1e_helpers import WINDOW, call, flags, set_config, set_relay_due


def _relay(sb):
    pre, res = run_ulthar(sb, None, ["relay"])
    return res if res is not None else pre


def _state(sb) -> tuple:
    return (sb.query("SELECT session_id, fence_token, handover_from FROM leases"),
            len(sb.episodes("relay")), flags(sb)[0])


def _refused(p: Probe, sb, name: str, needle: str) -> bool:
    before = _state(sb)
    res = _relay(sb)
    ok = res.returncode != 0 and needle in res.stderr and _state(sb) == before
    p.check(f"AC-4: unmet condition ({name}) -> refused with the reason, nothing done", ok,
            res.stderr[-400:])
    return ok


def check_relay(p: Probe) -> None:
    sb = new_sandbox()
    try:
        marker = sb.root / "launched.txt"
        launcher = sb.root / "launch.py"
        launcher.write_text("import pathlib, sys\npathlib.Path(sys.argv[1]).write_text('started')\n"
                            "print('backgrounded 0123abcd')\n", encoding="utf-8")
        set_config(sb, relay={"launch": [sys.executable, str(launcher), str(marker)]})
        held = 0

        unit_id = make_unit(sb)
        cli(sb, "contract", "submit", unit_id, "--file", str(write_contract(sb, contract())))
        confirm_contract(sb, unit_id)
        cli(sb, "unit", "advance", unit_id, "contract_confirmed")
        spent = json.loads(cli(sb, "unit", "show", unit_id).stdout)["op"]["consume"][0]
        sb.query("UPDATE tokens SET consumed_at=NULL WHERE token_id=?", (spent,))
        held += _refused(p, sb, "a unit's state differs between the store and runtime.db",
                         "not in runtime.db")
        cli(sb, "unit", "show", unit_id)  # recovery completes the operation

        second = make_unit(sb)
        cli(sb, "contract", "submit", second, "--file", str(write_contract(sb, contract(), "c2.yaml")))
        question = json.loads(cli(sb, "contract", "show", second).stdout)
        held += _refused(p, sb, "a question awaits the owner's answer", "awaits the owner's answer")
        answer(sb, question, ["確定"], "confirm2")

        call(sb, "Read", {"file_path": "x"}, agent="ghost-1")
        held += _refused(p, sb, "a running subagent was never registered", "ghost-1")
        for done in ("ghost-1", "rev-1"):  # rev-1: the contract reviewer, also never registered
            sb.hook("SubagentStop", {"session_id": WINDOW, "agent_id": done, "agent_type": "general-purpose"})

        set_relay_due(sb, "test: usage over the threshold")
        old_token = sb.query("SELECT fence_token FROM leases")[0][0]
        res = _relay(sb)
        lease = sb.query("SELECT session_id, handover_from FROM leases")[0]
        eps = sb.episodes("relay")
        old_write = call(sb, "Edit", {"file_path": str(sb.work / "src" / "app.txt")})
        new_call = call(sb, "Read", {"file_path": "x"}, session="successor")
        new_lease = sb.query("SELECT session_id, fence_token FROM leases")[0]
        notes = sb.query("SELECT kind FROM notifications WHERE kind='relay'")
        ok = (res.returncode == 0 and marker.exists() and lease == (None, WINDOW) and len(eps) == 1
              and flags(sb)[0] is None and old_write.returncode == 2 and "lease" in old_write.stderr
              and new_lease[0] == "successor" and new_lease[1] >= old_token + 1 and bool(notes))
        held += ok
        p.check("AC-4: conditions met -> Episode, launch, lease in handover; the old window's write "
                "is blocked (AC-1); the next session takes the lease with a higher token; relay_due "
                "cleared; a notification queued", ok,
                f"{res.stdout[-200:]} {res.stderr[-300:]} {lease} {new_lease} {old_token} {len(eps)} "
                f"{flags(sb)} {old_write.stderr[-200:]} {notes}")
    finally:
        sb.cleanup()
    held += check_launch_failure(p)
    p.check(f"AC-4: {held}/5", held == 5)


def check_launch_failure(p: Probe) -> int:
    sb = new_sandbox()
    try:
        set_config(sb, relay={"launch": [sys.executable, "-c", "import sys; sys.exit(7)"]})
        set_relay_due(sb, "test")
        call(sb, "Read", {"file_path": "x"})
        before = sb.query("SELECT session_id, fence_token, handover_from FROM leases")
        res = _relay(sb)
        after = sb.query("SELECT session_id, fence_token, handover_from FROM leases")
        edit = call(sb, "Edit", {"file_path": str(sb.work / "src" / "app.txt")})
        ok = (res.returncode != 0 and "could not be started" in res.stderr and before == after
              and flags(sb)[0] == "test" and edit.returncode == 2 and "relay due" in edit.stderr)
        p.check("AC-4: a failed launch moves no lease and leaves relay_due set", ok,
                res.stderr[-300:] + f" {before} {after}")
        left = processes_matching(str(sb.root))
        p.check("AC-4: no process of the relay is left behind", not left, str(left))
        return int(ok)
    finally:
        sb.cleanup()


def main() -> int:
    p = Probe()
    print("\n== R1-e AC-4: ulthar relay ==")
    check_relay(p)
    return p.result()


if __name__ == "__main__":
    sys.exit(main())
