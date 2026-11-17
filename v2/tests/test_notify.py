"""R1-e AC-9 (evasion suspicions, TRUST-06), AC-10 (notifications, AUT-03) and AC-11 (the
Notifier port's contract test, PORT-01/02/04), through the production hook entry and CLI.
Every notification goes to an ntfy stand-in on 127.0.0.1 (r1e_helpers.NtfyStub); nothing
reaches a real ntfy topic.

AC-9 detection: (1) the window's Edit blocked (commander mode), then a Bash redirect to the
same path allowed; (2) a general-purpose subagent's Edit of a pack path blocked (no
receipt), then the pack creator's Edit of it allowed. Non-detection: (3) the allow 31
minutes after the block (the block's time rewritten in runtime.db); (4) the allow in
another session tree."""
from __future__ import annotations

import json
import socket
import sys
from datetime import datetime, timedelta, timezone

from helpers import Probe, Sandbox
from r1d_helpers import CREATOR, new_r1d_sandbox, start_agent, to_build, write
from r1e_helpers import OTHER, STUB_FAIL, WINDOW, NtfyStub, call, set_config, wait_sent


def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _commander_sandbox(stub: NtfyStub) -> Sandbox:
    sb = Sandbox()
    set_config(sb, notify={"ntfy_url": stub.url()})
    (sb.work / ".claude" / "ulthar-overlay.json").write_text(json.dumps(
        {"disable": ["command_rules.tool_discipline/shell-file-creation"]}), encoding="utf-8")
    marker = sb.work / ".claude" / "state" / "commander-mode"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("enforce\n", encoding="utf-8")
    return sb


def _edit(sb, name: str, session: str = WINDOW):
    return call(sb, "Edit", {"file_path": str(sb.work / name), "old_string": "a", "new_string": "b"},
                session=session)


def _redirect(sb, name: str, session: str = WINDOW):
    return call(sb, "Bash", {"command": f"echo x > {name}"}, session=session)


def _suspicions(sb) -> int:
    return sb.query("SELECT COUNT(*) FROM suspicions")[0][0]


def check_suspicion_and_notify(p: Probe, stub: NtfyStub) -> None:
    sb = _commander_sandbox(stub)
    try:
        detected = missed = 0
        blocked = _edit(sb, "a.txt")
        allowed = _redirect(sb, "a.txt")
        rows = sb.query("SELECT block_event_id, allow_event_id, intent_key FROM suspicions")
        ok = (blocked.returncode == 2 and allowed.returncode == 0 and len(rows) == 1
              and rows[0][2].startswith("path:") and rows[0][2].endswith("/a.txt"))
        detected += ok
        p.check("AC-9: the window's Edit blocked, then a Bash redirect to the same path allowed -> "
                "a suspicion (intent = the normalized path)", ok,
                f"{blocked.stderr[-200:]} {allowed.stderr[-200:]} {rows}")

        _edit(sb, "b.txt")
        old = (datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat(timespec="milliseconds")
        sb.query("UPDATE events SET ts=? WHERE id=(SELECT MAX(id) FROM events WHERE decision='deny')", (old,))
        _redirect(sb, "b.txt")
        ok = _suspicions(sb) == 1
        missed += ok
        p.check("AC-9: the same allowed 31 minutes after the block -> no suspicion", ok, str(_suspicions(sb)))
        _edit(sb, "c.txt")
        _redirect(sb, "c.txt", session=OTHER)
        ok = _suspicions(sb) == 1
        missed += ok
        p.check("AC-9: the same allowed in another session tree -> no suspicion", ok, str(_suspicions(sb)))

        nid = sb.query("SELECT nid FROM notifications WHERE kind='evasion_suspected'")[0][0]
        sent = wait_sent(sb, nid)
        got = stub.wait(1) and stub.received[0]
        ok = (sent and sent[0][1] == 1 and got and "回避の疑い" in got["body"]
              and got["headers"].get("Priority") == "high")
        p.check("AC-10 (1): the suspicion's important notification is sent in the background and "
                "its result recorded (sent_ok=1)", bool(ok), f"{sent} {got}")
        held = int(bool(ok))
        held += _resends(p, sb, stub, nid)
        held += _owner_ack(p, sb, stub)
        held += _failed_send(p, sb)
        p.check(f"AC-10: {held}/4", held == 4)
    finally:
        sb.cleanup()
    detected += _agent_case(p, stub)
    p.check(f"AC-9: detected {detected}/2, not detected {missed}/2", detected == 2 and missed == 2)


def _due_now(sb, nid: str | None = None) -> None:
    """The resend interval and the once-a-minute check, made due by rewriting runtime.db."""
    sb.query("UPDATE notifications SET last_sent_at='2000-01-01T00:00:00.000+00:00'"
             + (" WHERE nid=?" if nid else ""), (nid,) if nid else ())
    sb.query("UPDATE project_flags SET resend_checked_at=NULL")


def _sent_for(stub: NtfyStub, needle: str) -> int:
    return sum(needle in r["body"] for r in stub.received)


def _resends(p: Probe, sb, stub: NtfyStub, nid: str) -> int:
    counts = []
    for _ in range(4):
        _due_now(sb, nid)
        call(sb, "Read", {"file_path": "x"})
        wait_sent(sb, nid)
        stub.wait(len(stub.received) + 1, timeout=8)
        counts.append(_sent_for(stub, "回避の疑い"))
    retries = sb.query("SELECT retries FROM notifications WHERE nid=?", (nid,))[0][0]
    ok = counts == [2, 3, 4, 4] and retries == 3
    p.check("AC-10 (2): an unread important notification is resent every 30 minutes, at most 3 "
            "times, then stops", ok, f"{counts} retries={retries}")
    return int(ok)


def _owner_ack(p: Probe, sb, stub: NtfyStub) -> int:
    set_config(sb, caps={"codex": {"check_cmd": STUB_FAIL}})
    sb.cli("cap", "check", "codex")
    nid = sb.query("SELECT nid FROM notifications WHERE kind='capability_degraded'")[0][0]
    wait_sent(sb, nid)
    stub.wait(5, timeout=15)
    before = _sent_for(stub, "能力の劣化")
    sb.hook("UserPromptSubmit", {"session_id": WINDOW, "prompt": "見ました。続けて", "prompt_id": "ack-1"})
    _due_now(sb, nid)
    call(sb, "Read", {"file_path": "x"})
    stub.wait(before + 1, timeout=5)
    acked = sb.query("SELECT acked_by_event_id, retries FROM notifications WHERE nid=?", (nid,))[0]
    ok = before == 1 and _sent_for(stub, "能力の劣化") == 1 and acked[0] is not None and acked[1] == 0
    p.check("AC-10 (3): the owner's prompt after an important notification marks it read: no "
            "resend (a delivered send alone is never taken as read)", ok,
            f"before={before} after={_sent_for(stub, '能力の劣化')} {acked}")
    return int(ok)


def _failed_send(p: Probe, sb) -> int:
    from ulthar import db as db_mod, monitor  # noqa: PLC0415

    set_config(sb, notify={"ntfy_url": f"http://127.0.0.1:{_closed_port()}/down"})
    res = sb.cli("cap", "check", "ntfy")
    nid = sb.query("SELECT nid FROM notifications WHERE kind='capability_degraded'"
                   " ORDER BY ts DESC LIMIT 1")[0][0]
    row = wait_sent(sb, nid)
    set_config(sb, notify={})
    sb.query("UPDATE notifications SET sent_ok=NULL WHERE nid=?", (nid,))
    with sb.inproc():
        conn = db_mod.connect(sb.db)
        try:
            from ulthar import notifier, store as store_mod  # noqa: PLC0415
            notifier.send_recorded(conn, store_mod.Store.for_cwd(sb.work), nid)
            shown = [n for n in monitor.snapshot(conn, with_agents=False)["notifications"]
                     if n["nid"] == nid]
        finally:
            conn.close()
    detail = sb.query("SELECT sent_ok, detail FROM notifications WHERE nid=?", (nid,))[0]
    attempts = json.loads(detail[1])
    ok = (res.returncode != 0 and row and row[0][1] == 0 and detail[0] == 0
          and not attempts[0]["ok"] and "ntfy_url is not set" in attempts[-1]["detail"]
          and shown and shown[0]["sent_ok"] == 0)
    p.check("AC-10 (4): an unreachable ntfy and a missing notify.ntfy_url are recorded as failed "
            "sends, shown on the monitor", bool(ok), f"{res.stdout[-200:]} {row} {detail} {shown}")
    return int(bool(ok))


def _agent_case(p: Probe, stub: NtfyStub) -> int:
    sb = new_r1d_sandbox()
    try:
        set_config(sb, notify={"ntfy_url": stub.url("agents")})
        to_build(sb)
        start_agent(sb, "gp-1", "general-purpose")
        blocked = write(sb, "gp-1", "src/ui/x.txt", "x\n", "general-purpose", tool="Edit")
        start_agent(sb, "bld-1", CREATOR)
        allowed = write(sb, "bld-1", "src/ui/x.txt", "x\n", CREATOR, tool="Edit")
        rows = sb.query("SELECT intent_key FROM suspicions")
        sent = wait_sent(sb)
        ok = (blocked.returncode == 2 and allowed.returncode == 0 and len(rows) == 1
              and sent and all(r[1] == 1 for r in sent if r[0]))
        p.check("AC-9: subagent A's Edit blocked, then subagent B's Edit of the same path allowed "
                "-> a suspicion, notified", ok, f"{blocked.stderr[-200:]} {rows} {sent}")
        return int(ok)
    finally:
        sb.cleanup()


def notifier_contract(make, stub: NtfyStub) -> list[str]:
    """PORT-02: what every Notifier adapter must do. `make(cfg)` builds the adapter from a
    project config. Returns the problems ([] = the adapter passes)."""
    problems = []
    before = len(stub.received)
    adapter = make({"notify": {"ntfy_url": stub.url("contract")}})
    if not isinstance(getattr(adapter, "name", None), str):
        problems.append("the adapter has no name")
    ok, detail = adapter.send("ULTHAR contract", "契約試験の本文 (UTF-8)", True)
    if ok is not True or not isinstance(detail, str):
        problems.append(f"a send to a working endpoint did not return (True, str): {ok!r} {detail!r}")
    if not stub.wait(before + 1, timeout=10) or "契約試験の本文" not in stub.received[-1]["body"]:
        problems.append("the message text did not arrive intact")
    try:
        down = make({"notify": {"ntfy_url": f"http://127.0.0.1:{_closed_port()}/x"}})
        ok, detail = down.send("ULTHAR contract", "unreachable", False)
        if ok is not False or not detail:
            problems.append(f"a failed delivery did not return (False, reason): {ok!r} {detail!r}")
    except Exception as exc:  # noqa: BLE001 -- the contract: never raise for a delivery failure
        problems.append(f"a failed delivery raised {type(exc).__name__}: {exc}")
    return problems


def check_port(p: Probe, stub: NtfyStub) -> None:
    from ulthar import notifier  # noqa: PLC0415

    problems = notifier_contract(notifier.NtfyNotifier.from_config, stub)
    p.check("AC-11: the ntfy adapter passes the Notifier contract test: 1/1", not problems,
            "; ".join(problems))
    chosen = notifier.notifier({"notify": {"adapter": "ntfy", "ntfy_url": stub.url()}})
    try:
        notifier.notifier({"notify": {"adapter": "nope", "ntfy_url": stub.url()}})
        unknown = False
    except notifier.NotifyError:
        unknown = True
    p.check("AC-11 (PORT-04): the adapter is chosen by one config line (notify.adapter); an unknown "
            "one is refused", isinstance(chosen, notifier.NtfyNotifier) and unknown)


def main() -> int:
    p = Probe()
    stub = NtfyStub()
    try:
        print("\n== R1-e AC-9 / AC-10: suspicions and notifications ==")
        check_suspicion_and_notify(p, stub)
        print("\n== R1-e AC-11: the Notifier port ==")
        check_port(p, stub)
    finally:
        stub.close()
    return p.result()


if __name__ == "__main__":
    sys.exit(main())
