"""R1-e AC-12 (the minimal monitor, MON-01/02/04) and AC-13 (its operations, MON-03), with the
real `ulthar monitor` process (127.0.0.1, a free port) on a sandbox runtime.db, the
production hook entry and the CLI. The test stops the monitor it started."""
from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
import urllib.request

from helpers import MAIN_PY, Probe
from r1c_helpers import cli, new_sandbox, runner, to_quant_verified, write_jsonl, transcript
from r1e_helpers import OTHER, WINDOW, assistant_line, call, project_id

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _start(sb):
    proc = subprocess.Popen([sys.executable, str(MAIN_PY), "monitor"], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True,
                            encoding="utf-8", env=sb.env(), cwd=str(sb.work), creationflags=NO_WINDOW)
    url = proc.stdout.readline().strip()
    return proc, url


def _get(url: str):
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def _post(url: str, body: dict):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, {}


def _seed(sb) -> str:
    unit_id = to_quant_verified(sb)
    runner(sb, "rev-1", ["verify", unit_id])
    call(sb, "Edit", {"file_path": str(sb.work / "src" / "app.txt")}, session=OTHER)  # a lease block
    path = transcript(sb)
    write_jsonl(path, [assistant_line("m-1", 90000)], mode="a")
    call(sb, "Read", {"file_path": "x"}, transcript_path=str(path))
    cli(sb, "contract", "show", unit_id)  # a question awaiting the answer
    return unit_id


def _matches(sb, snap: dict, unit_id: str) -> list[str]:
    bad = []
    for g in snap["guards"]:
        n = sb.query("SELECT COUNT(*) FROM events WHERE hook='PreToolUse' AND decision='deny'"
                     " AND guard_id=?", (g["guard_id"],))[0][0]
        if n != g["blocks"]:
            bad.append(f"guard {g['guard_id']}: {g['blocks']} != {n}")
    total = sb.query("SELECT COUNT(*) FROM events WHERE hook='PreToolUse'")[0][0]
    if snap["pretooluse_calls"] != total or not any(g["guard_id"] == "window_lease" for g in snap["guards"]):
        bad.append(f"PreToolUse calls {snap['pretooluse_calls']} != {total}, or no window_lease block")
    me = [s for s in snap["sessions"] if s["session_id"] == WINDOW]
    row = sb.query("SELECT ctx_used, ctx_window, compactions FROM sessions WHERE session_id=?", (WINDOW,))[0]
    if not me or (me[0]["ctx_used"], me[0]["ctx_window"], me[0]["compactions"]) != (row[0], row[1], row[2] or 0) \
            or me[0]["ctx_pct"] != 45.0 or not any(u["id"] == unit_id for u in me[0]["units"]):
        bad.append(f"session {me} vs {row}")
    caps = {c["name"]: c["available"] for c in snap["capabilities"]}
    healthy = sb.query("SELECT healthy FROM capabilities WHERE name='codex'")[0][0]
    if caps.get("codex") != bool(healthy) or caps.get("claude_push") is not False:
        bad.append(f"capabilities {caps}")
    asked = {r[0] for r in sb.query("SELECT qid FROM questions WHERE status='asked' AND qid NOT IN"
                                    " (SELECT json_extract(bound, '$.qid') FROM tokens"
                                    " WHERE json_extract(bound, '$.qid') IS NOT NULL)")}
    if {a["qid"] for a in snap["awaiting_answer"]} != asked or not asked:
        bad.append(f"awaiting {snap['awaiting_answer']} vs {asked}")
    unit = [u for u in snap["units"] if u["unit_id"] == unit_id]
    ev = sb.query("SELECT id, result FROM evidence WHERE unit_id=? ORDER BY id DESC LIMIT 1", (unit_id,))[0]
    if not unit or unit[0]["results"]["evidence_id"] != ev[0] or unit[0]["results"]["acs"] != json.loads(ev[1])["acs"]:
        bad.append(f"unit results {unit} vs {ev}")
    if len(snap["suspicions"]) != sb.query("SELECT COUNT(*) FROM suspicions")[0][0]:
        bad.append("suspicions")
    if not ("agents" in snap["agents"] or snap["agents"].get("error")):
        bad.append(f"agents {snap['agents']}")
    return bad


def check_monitor(p: Probe) -> None:
    sb = new_sandbox()
    proc = None
    try:
        unit_id = _seed(sb)
        proc, url = _start(sb)
        base, _, token = url.partition("/?token=")
        import psutil  # noqa: PLC0415 -- tests only
        listening = [c.laddr for c in psutil.Process(proc.pid).net_connections(kind="inet")
                     if c.status == psutil.CONN_LISTEN]
        ok = base.startswith("http://127.0.0.1:") and listening and all(a.ip == "127.0.0.1" for a in listening)
        held = int(bool(ok))
        p.check("AC-12: the monitor listens on 127.0.0.1 only", bool(ok), f"{url} {listening}")
        codes = [_get(f"{base}/state")[0], _get(f"{base}/state?token=wrong")[0], _get(f"{base}/?token=")[0],
                 _post(f"{base}/op", {"op": "freeze", "project_id": "x"})[0]]
        ok = codes == [403, 403, 403, 403]
        held += ok
        p.check("AC-12: no token or another token -> 403 (GET and POST)", ok, str(codes))
        status, body = _get(f"{base}/state?token={token}")
        snap = json.loads(body) if status == 200 else {}
        bad = _matches(sb, snap, unit_id) if snap else ["no snapshot"]
        page = _get(f"{base}/?token={token}")
        ok = not bad and page[0] == 200 and "ULTHAR monitor" in page[1]
        held += ok
        p.check("AC-12: every field of the page equals runtime.db (sessions, context, blocks per "
                "guard, suspicions, awaiting answers, capabilities, AC results)", ok, "; ".join(bad)[:800])
        p.check(f"AC-12: {held}/3", held == 3)
        _operations(p, sb, base, token)
    finally:
        if proc is not None:
            proc.terminate()
            proc.wait(timeout=30)
            proc.stdout.close()
            proc.stderr.close()
        sb.cleanup()


def _edit(sb):
    return call(sb, "Edit", {"file_path": str(sb.work / "src" / "app.txt"), "old_string": "v",
                             "new_string": "w"})


def _operations(p: Probe, sb, base: str, token: str) -> None:
    pid = project_id(sb)
    op = f"{base}/op?token={token}"
    held = 0
    _post(op, {"op": "freeze", "project_id": pid})
    frozen = _edit(sb)
    state_cli = call(sb, "Bash", {"command": "ulthar issue add --title x"}, session=OTHER)
    ok = (frozen.returncode == 2 and "frozen" in frozen.stderr and state_cli.returncode == 2
          and "frozen" in state_cli.stderr)
    held += ok
    p.check("AC-13: freeze -> every session's product write and state-changing ulthar call are "
            "blocked", ok, frozen.stderr[-200:] + state_cli.stderr[-200:])
    _post(op, {"op": "resume", "project_id": pid})
    resumed = _edit(sb)
    held += resumed.returncode == 0
    p.check("AC-13: resume -> the write passes again", resumed.returncode == 0, resumed.stderr[-300:])
    _post(op, {"op": "relay", "project_id": pid})
    due = _edit(sb)
    read = call(sb, "Read", {"file_path": str(sb.work / "src" / "app.txt")})
    ok = due.returncode == 2 and "relay due" in due.stderr and read.returncode == 0
    held += ok
    p.check("AC-13: a relay request sets relay_due -> AC-3's block (reads pass)", ok, due.stderr[-300:])
    token_read = call(sb, "Read", {"file_path": str(sb.state / "monitor.token")})
    ok = token_read.returncode == 2 and "monitor" in token_read.stderr
    held += ok
    p.check("AC-13: a Read of the monitor's token file is blocked", ok, token_read.stderr[-300:])
    port = base.rsplit(":", 1)[1]
    curl = call(sb, "Bash", {"command": f"curl http://127.0.0.1:{port}/state"})
    ok = curl.returncode == 2 and "monitor's port" in curl.stderr
    held += ok
    p.check("AC-13: `curl http://127.0.0.1:<port>/...` is blocked", ok, curl.stderr[-300:])
    actions = sb.query("SELECT kind FROM owner_actions WHERE kind LIKE 'monitor:%' ORDER BY id")
    other = _post(op, {"op": "exec", "project_id": pid})[0]
    ok = [a[0] for a in actions] == ["monitor:freeze", "monitor:resume", "monitor:relay"] and other == 400
    p.check("AC-13: each operation is recorded as an owner action; any other operation is refused",
            ok, f"{actions} {other}")
    p.check(f"AC-13: {held}/5", held == 5)


def main() -> int:
    p = Probe()
    print("\n== R1-e AC-12 / AC-13: the monitor ==")
    check_monitor(p)
    return p.result()


if __name__ == "__main__":
    sys.exit(main())
