"""R1-c trusted verification:
  AC-7  `ulthar verify` runs only the confirmed contract's commands; quantitative pass /
        fail / not-a-number by the last stdout line; no threshold -> exit code 0; the
        manifest carries every required item; `--self` and the reviewer's run are told
        apart; a hand-written evidence file is never used; a rewritten manifest is
        detected;
  AC-16 the verify commands run without a console window, and nothing they start is
        left running.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time

from helpers import Probe, processes_matching
from r1c_helpers import (SESSION, cli, cli_background, contract, new_sandbox, quant_ac, runner,
                         runner_token, set_value, to_build)
from test_spawn import CONSOLE_CLASSES, visible_windows

from ulthar import evidence as evidence_mod

MANIFEST_KEYS = ("command", "stdout_sha256", "stderr_sha256", "exit_code", "artifacts")
RUN_KEYS = ("python", "platform", "tree_hash", "base_commit", "modified", "untracked",
            "contract_sha", "runner_agent_id")


def _manifest(sb, unit_id: str) -> tuple[dict, str, dict]:
    row = sb.query("SELECT manifest_path, result FROM evidence WHERE unit_id=? ORDER BY id DESC"
                   " LIMIT 1", (unit_id,))
    path, result = row[0]
    return json.loads(open(path, encoding="utf-8").read()), path, json.loads(result)


def check_judgement(p: Probe) -> None:
    rc_rule = (evidence_mod.judge({"threshold": None}, 0, b"x") == (True, None)
               and evidence_mod.judge({"threshold": None}, 3, b"1") == (False, None))
    bounds = [evidence_mod.judge({"threshold": th}, 0, out)[0] for th, out in (
        ({"lo": 1, "hi": 3, "lo_incl": True, "hi_incl": True}, b"3\n"),
        ({"lo": 1, "hi": 3, "lo_incl": True, "hi_incl": False}, b"3\n"),
        ({"lo": 1, "lo_incl": False}, b"noise\n1\n\n"),
        ({"hi": 3, "hi_incl": True}, b"2.5\n"))]
    p.check("AC-7: no threshold -> exit code 0 passes, 3 fails; inclusive / exclusive ends "
            "on the last non-empty stdout line", rc_rule and bounds == [True, False, False, True],
            str(bounds))


def check_runs(p: Probe) -> None:
    sb = new_sandbox()
    try:
        doc = contract(acs=[quant_ac("AC-1", artifacts=["src/app.txt"]), quant_ac("AC-2"),
                            quant_ac("AC-3")])
        set_value(sb, "AC-2", "10")
        set_value(sb, "AC-3", "about three")
        unit_id = to_build(sb, doc)
        _, run = runner(sb, "bld-1", ["verify", unit_id, "--self"])
        manifest, path, result = _manifest(sb, unit_id)
        verdicts = [(a["id"], a["passed"], a["value"]) for a in manifest["acs"]]
        p.check("AC-7: quantitative pass (1 <= 3), fail (10), not a number ('about three') "
                "-> pass / fail / fail; the CLI exits 1",
                verdicts == [("AC-1", True, 1.0), ("AC-2", False, 10.0), ("AC-3", False, None)]
                and run.returncode == 1, f"{verdicts} {run.stdout[-300:]} {run.stderr[-300:]}")
        app_sha = hashlib.sha256((sb.work / "src" / "app.txt").read_bytes()).hexdigest()
        complete = (all(k in a for a in manifest["acs"] for k in MANIFEST_KEYS)
                    and all(manifest.get(k) is not None for k in RUN_KEYS)
                    and manifest["acs"][0]["artifacts"] == {"src/app.txt": app_sha}
                    and "checks/AC-1.txt" in manifest["untracked"]
                    and manifest["acs"][0]["command"] == doc["acs"][0]["verify_cmd"]
                    and manifest["runner_agent_id"] == "bld-1" and manifest["self"] is True)
        p.check("AC-7: the manifest holds command, stdout/stderr sha256, Python and platform, "
                "exit code, artifact sha256, worktree state (untracked included), tree hash, "
                "base commit, contract sha and the runner's agent_id",
                complete, json.dumps(manifest, ensure_ascii=False)[:600])
        p.check("AC-7: the evidence row holds the manifest's sha256 and the run's kind",
                result["manifest_sha256"] == hashlib.sha256(open(path, "rb").read()).hexdigest()
                and result["self"] is True, str(result))

        evidence_dir = sb.kr / ".ulthar" / "evidence" / unit_id
        forged = dict(manifest, run_id="f" * 16, all_quant_passed=True,
                      acs=[dict(a, passed=True) for a in manifest["acs"]])
        hook_write = sb.hook("PreToolUse", {
            "session_id": SESSION, "agent_id": "bld-1", "agent_type": "ulthar-creator",
            "tool_name": "Write", "tool_input": {"file_path": str(evidence_dir / "forged.json"),
                                                 "content": json.dumps(forged)}})
        (evidence_dir / "forged.json").write_text(json.dumps(forged), encoding="utf-8")
        adv = cli(sb, "unit", "advance", unit_id, "quant_verified")
        p.check("AC-7: a builder's hand-written evidence: the Write is blocked by the store "
                "guard, and a file placed there anyway is not used (advance refused) 1/1",
                hook_write.returncode == 2 and adv.returncode != 0
                and "self_verify_all_quant_passed" in adv.stderr, adv.stderr[-300:])

        set_value(sb, "AC-2", "2")
        set_value(sb, "AC-3", "3")
        runner(sb, "bld-1", ["verify", unit_id, "--self"])
        manifest, path, _ = _manifest(sb, unit_id)
        text = open(path, encoding="utf-8").read()
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text.replace('"runner_agent_id": "bld-1"', '"runner_agent_id": "rev-x"'))
        adv = cli(sb, "unit", "advance", unit_id, "quant_verified")
        p.check("AC-7: a rewritten manifest (passing run, then edited) is detected: advance "
                "refused 1/1", adv.returncode != 0 and "changed after ulthar wrote it" in adv.stderr,
                adv.stderr[-300:])
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        adv = cli(sb, "unit", "advance", unit_id, "quant_verified")
        _, rev = runner(sb, "rev-1", ["verify", unit_id])
        rows = sb.query("SELECT runner_agent_id, json_extract(result, '$.self') FROM evidence"
                        " WHERE unit_id=? ORDER BY id", (unit_id,))
        p.check("AC-7: `--self` (builder, build) and the reviewer's run (quant_verified) are "
                "recorded apart with their runners",
                adv.returncode == 0 and rev is not None and rev.returncode == 0
                and rows[-1] == ("rev-1", 0) and rows[0] == ("bld-1", 1), f"{rows} {adv.stderr[-200:]}")
        runner(sb, "rev-1", ["review", "record", "--unit", unit_id, "--kind", "verify",
                             "--result", "pass"])
        confirmed = sb.kr / ".ulthar" / "contracts" / unit_id / "v1.yaml"
        original = confirmed.read_bytes()
        confirmed.write_bytes(original + b"# edited after the confirmation\n")
        tampered = cli(sb, "unit", "advance", unit_id, "owner_review")
        confirmed.write_bytes(original)
        p.check("AC-7: the confirmed contract file changed on disk -> the transition is refused "
                "(its sha256 no longer equals the confirmed / evidence contract sha)",
                tampered.returncode != 0 and "confirmed_contract_intact" in tampered.stderr,
                tampered.stderr[-300:])
        main_run = runner(sb, None, ["verify", unit_id])[1]
        p.check("AC-7: a main-session `ulthar verify` runs nothing (no runner token)",
                main_run is not None and main_run.returncode != 0
                and "no runner token" in main_run.stderr, main_run.stderr[-300:])
    finally:
        sb.cleanup()


PROBE = """import ctypes, json, os, subprocess, sys, time
out, marker = sys.argv[1], sys.argv[2]
code = ("import ctypes, json, os; k32, u32 = ctypes.windll.kernel32, ctypes.windll.user32; "
        "k32.GetConsoleWindow.restype = ctypes.c_void_p; h = k32.GetConsoleWindow() or 0; "
        "print(json.dumps({'pid': os.getpid(), 'visible': bool(h) and bool(u32.IsWindowVisible(ctypes.c_void_p(h)))}))")
me = json.loads(subprocess.run([sys.executable, "-c", code], capture_output=True, text=True).stdout)
k32, u32 = ctypes.windll.kernel32, ctypes.windll.user32
k32.GetConsoleWindow.restype = ctypes.c_void_p
h = k32.GetConsoleWindow() or 0
subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", marker],
                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
json.dump({"verify_cmd": {"pid": os.getpid(), "visible": bool(h) and bool(u32.IsWindowVisible(ctypes.c_void_p(h)))},
           "child": me}, open(out, "w"))
time.sleep(3)  # alive (with its child) while the test lists the windows
print(1)
"""


def check_windows(p: Probe) -> None:
    if os.name != "nt":
        print("SKIP AC-16: console windows are a Windows matter")
        return
    sb = new_sandbox()
    try:
        marker = f"ulthar-verify-left-{os.urandom(4).hex()}"
        out = sb.root / "verify-console.json"
        (sb.work / "checks" / "probe.py").write_text(PROBE, encoding="utf-8")
        doc = contract(acs=[quant_ac(verify_cmd=f"python checks/probe.py {out.as_posix()} {marker}")])
        unit_id = to_build(sb, doc)
        token = runner_token(sb, "bld-1", ["verify", unit_id, "--self"])
        before = visible_windows()
        proc = cli_background(sb, "verify", unit_id, "--self", "--runner-token", token)
        deadline = time.monotonic() + 120  # wall-clock tolerance for a loaded machine
        while not out.exists() and proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.1)
        time.sleep(0.3)
        during = visible_windows()  # the verify command and its children are alive now
        stdout, stderr = proc.communicate(timeout=600)
        run = subprocess.CompletedProcess(proc.args, proc.returncode,
                                          stdout.decode("utf-8", "replace"),
                                          stderr.decode("utf-8", "replace"))
        report = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}
        pids = {(report.get("verify_cmd") or {}).get("pid"), (report.get("child") or {}).get("pid")}
        new = {h: v for h, v in during.items() if h not in before
               and (v[0] in CONSOLE_CLASSES or v[1] in pids)}
        time.sleep(0.5)
        left = processes_matching(marker)
        p.check("AC-16: the verify command ran (exit 0, its AC passed)",
                run is not None and run.returncode == 0 and bool(report),
                (run.stdout + run.stderr)[-300:] if run else "")
        p.check("AC-16: the verify command and its child have no visible console window, and "
                f"no new visible console window appeared ({len(new)})",
                bool(report) and not report["verify_cmd"]["visible"]
                and not report["child"]["visible"] and not new, f"{report} {new}")
        p.check(f"AC-16: nothing the verify command started is left running ({len(left)})",
                not left, str(left))
    finally:
        sb.cleanup()


def main() -> int:
    p = Probe()
    print("\n== AC-7: trusted verification ==")
    check_judgement(p)
    check_runs(p)
    print("\n== AC-16: no window, nothing left ==")
    check_windows(p)
    return p.result()


if __name__ == "__main__":
    sys.exit(main())
