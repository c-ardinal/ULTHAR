#!/usr/bin/env python3
"""R1-e P-21 (spec 未確認の点 / 事前決定 2): does a window started with
`claude --bg --session-id <uuid>` run under exactly that session_id?

    C:/Python314/python.exe -X utf8 v2/probes/run_p21.py [--logs DIR]

A fresh throwaway project (under --base: `claude --bg` refuses a workspace whose trust prompt
was never accepted, so the base must be inside a trusted directory) with its own settings (`--setting-sources
project,local`: the owner's user settings and hooks take no part) and one hook on
SessionStart, UserPromptSubmit and PreToolUse that logs each payload's session_id. The
background session is asked to Read probe.txt (so PreToolUse fires) with haiku. The probe
then reads `claude agents --json`, waits for the hook log, and stops the background
session it started (`claude stop <id>`). Nothing is written to ~/.claude/{settings.json,
hooks,skills,agents}; claude is started windowless (CREATE_NO_WINDOW)."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

PY = sys.executable.replace("\\", "/")
WAIT_SEC = 240
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
HOOK = r'''import json, sys
p = json.load(sys.stdin)
with open(sys.argv[1], "a", encoding="utf-8") as fh:
    fh.write(json.dumps({k: p.get(k) for k in ("hook_event_name", "session_id", "tool_name",
                                                "source", "transcript_path")}) + "\n")
'''


def _run(argv: list[str], cwd: Path, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8",
                          errors="replace", stdin=subprocess.DEVNULL, timeout=timeout,
                          creationflags=NO_WINDOW)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--logs")
    ap.add_argument("--base", help="parent dir of the throwaway project: `claude --bg` refuses "
                                   "an untrusted workspace, so a dir under a trusted one")
    args = ap.parse_args()
    project = Path(tempfile.mkdtemp(prefix="ulthar-p21-", dir=args.base))
    (project / "probe.txt").write_text("P21-WORD\n", encoding="utf-8")
    (project / "hook.py").write_text(HOOK, encoding="utf-8")
    log = project / "hooks.jsonl"
    cmd = f'"{PY}" -S "{(project / "hook.py").as_posix()}" "{log.as_posix()}"'
    entry = [{"hooks": [{"type": "command", "command": cmd}]}]
    settings = {"permissions": {"allow": ["Read"]},
                "hooks": {e: entry for e in ("SessionStart", "UserPromptSubmit", "PreToolUse")}}
    settings_path = project / "probe-settings.json"
    settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    sid = str(uuid.uuid4())
    started = time.monotonic()
    launch = _run(["claude", "--bg", "--session-id", sid, "--model", "haiku", "--settings",
                   str(settings_path), "--setting-sources", "project,local",
                   "Read probe.txt with the Read tool and reply with its first line only."], project)
    bg_id = (launch.stdout.strip().split() or [""])[-1]
    seen: list[dict] = []
    while time.monotonic() - started < WAIT_SEC:
        if log.exists():
            seen = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines() if x.strip()]
            if any(r.get("hook_event_name") == "PreToolUse" for r in seen):
                break
        time.sleep(2)
    agents = _run(["claude", "agents", "--json", "--all", "--cwd", str(project)], project)
    stop = _run(["claude", "stop", bg_id or sid], project) if (bg_id or sid) else None
    ids = sorted({r.get("session_id") for r in seen})
    report = {"requested_session_id": sid, "launch_exit": launch.returncode,
              "launch_stdout": launch.stdout.strip()[-300:], "launch_stderr": launch.stderr.strip()[-300:],
              "hook_events": [r.get("hook_event_name") for r in seen], "hook_session_ids": ids,
              "holds": ids == [sid], "agents_json": agents.stdout.strip()[:1500],
              "stop_exit": stop.returncode if stop else None,
              "seconds": round(time.monotonic() - started, 1), "project": str(project)}
    if args.logs:
        Path(args.logs).mkdir(parents=True, exist_ok=True)
        (Path(args.logs) / "p21.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
