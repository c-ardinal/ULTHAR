#!/usr/bin/env python3
"""R1-c AC-17 / P-16: a PreToolUse hook that rewrites a Bash tool_input.command through
`updatedInput` WITHOUT a permissionDecision (as ulthar's runner token does):
  (a) is the rewritten command the one that actually runs?
  (b) do the permission rules (deny, allow) apply to the rewritten command?

    C:/Python314/python.exe -X utf8 v2/probes/run_p16.py [--logs DIR]

Each run is one short `claude -p --model haiku` in a fresh throwaway project, with its
own settings (`--setting-sources project,local`: the owner's user settings and hooks
take no part) and --max-budget-usd. The model is asked to run exactly
`<python> probe_echo.py ORIGINAL-<tag>`; the hook rewrites it to `... REWRITTEN-<tag>`.
probe_echo.py appends its argument to ran.txt, so what ran is read from disk as well as
from the stream-json tool_result. Runs (rules are exact command strings):
  A  allow original + rewritten           -> (a): which one ran
  B  allow original, deny rewritten       -> (b) deny: is the rewritten call denied?
  C  allow rewritten only (original not)  -> (b) allow: is the rewritten call allowed?
  D  no rule at all (control)               -> without an allow rule the call is denied,
                                               so C ran because of the rule on the rewrite
claude is started windowless (CREATE_NO_WINDOW); nothing else is spawned.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PY = sys.executable.replace("\\", "/")
BUDGET_USD = "0.30"
TIMEOUT_SEC = 300
HOOK = r'''import json, sys
payload = json.load(sys.stdin)
cmd = (payload.get("tool_input") or {}).get("command", "")
with open(sys.argv[1], "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"seen": cmd}) + "\n")
if "ORIGINAL-" in cmd:
    new = dict(payload["tool_input"], command=cmd.replace("ORIGINAL-", "REWRITTEN-"))
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                             "updatedInput": new}}))
'''
ECHO = r'''import pathlib, sys
pathlib.Path(__file__).with_name("ran.txt").open("a").write(sys.argv[1] + "\n")
print("ran", sys.argv[1])
'''


def run(name: str, allow: list[str], deny: list[str], logs: Path) -> dict:
    project = Path(tempfile.mkdtemp(prefix=f"ulthar-p16-{name}-"))
    tag = f"{name}{os.urandom(3).hex()}"
    (project / "hook.py").write_text(HOOK, encoding="utf-8")
    (project / "probe_echo.py").write_text(ECHO, encoding="utf-8")
    hook_log = project / "hook-seen.jsonl"
    original = f"{PY} probe_echo.py ORIGINAL-{tag}"
    rewritten = f"{PY} probe_echo.py REWRITTEN-{tag}"
    sub = {"original": original, "rewritten": rewritten}
    settings = {
        "permissions": {"allow": [f"Bash({sub[a]})" for a in allow],
                        "deny": [f"Bash({sub[d]})" for d in deny]},
        "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{
            "type": "command",
            "command": f'"{PY}" -S "{(project / "hook.py").as_posix()}" "{hook_log.as_posix()}"'}]}]},
    }
    settings_path = project / "probe-settings.json"
    settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    prompt = (f"Run exactly this one Bash command, unchanged, once, and nothing else: {original}\n"
              "Then reply with the command's output, or with the error you got.")
    log = logs / f"p16-{name}.jsonl"
    argv = ["claude", "-p", "--model", "haiku", "--settings", str(settings_path),
            "--setting-sources", "project,local", "--output-format", "stream-json", "--verbose",
            "--max-budget-usd", BUDGET_USD, prompt]
    started = time.monotonic()
    with open(log, "w", encoding="utf-8") as out:
        proc = subprocess.run(argv, cwd=str(project), stdout=out, stderr=subprocess.STDOUT,
                              stdin=subprocess.DEVNULL, timeout=TIMEOUT_SEC,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    events = []
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            pass
    uses, results, denials = [], [], []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        message = ev.get("message") if isinstance(ev.get("message"), dict) else {}
        for block in message.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                uses.append(block.get("input", {}).get("command"))
            if isinstance(block, dict) and block.get("type") == "tool_result":
                c = block.get("content")
                results.append(c if isinstance(c, str) else json.dumps(c, ensure_ascii=False))
        if ev.get("type") == "result":
            denials = ev.get("permission_denials") or []
    ran = (project / "ran.txt").read_text().split() if (project / "ran.txt").exists() else []
    seen = [json.loads(x)["seen"] for x in hook_log.read_text().splitlines()] if hook_log.exists() else []
    return {"run": name, "allow": allow, "deny": deny, "exit": proc.returncode,
            "seconds": round(time.monotonic() - started, 1), "model_tool_uses": uses,
            "hook_saw": seen, "ran_txt": ran, "tool_results": [r[:300] for r in results],
            "permission_denials": denials, "log": str(log), "tag": tag}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--logs")
    args = ap.parse_args()
    logs = Path(args.logs or tempfile.mkdtemp(prefix="ulthar-p16-logs-"))
    logs.mkdir(parents=True, exist_ok=True)
    out = [run("A", ["original", "rewritten"], [], logs),
           run("B", ["original"], ["rewritten"], logs),
           run("C", ["rewritten"], [], logs),
           run("D", [], [], logs)]
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
