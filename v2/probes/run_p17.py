#!/usr/bin/env python3
"""R1-d premises (spec 未確認の点) with throwaway projects and `claude -p --model haiku`:
  P-17  the Agent tool_input field that names the agent type, and its value when the
        model does not name one;
  P-18  whether a subagent's PreToolUse payload carries the PARENT session's session_id
        (needed to stamp a subagent's write events with the parent's fence_token);
  P-19  whether a PreToolUse `updatedInput` that rewrites that field (no
        permissionDecision) changes the agent that actually starts (R1-d AC-3);
  P-20  whether an agent named `ulthar-creator@<pack>` (the AC-2 naming, with `@`) and
        `skills: a, b` (two skills, comma-separated) works: started by that name, both
        skills preloaded.

    C:/Python314/python.exe -X utf8 v2/probes/run_p17.py [--logs DIR]

Every run: a fresh temp project with its own settings (`--setting-sources project,local`:
the owner's user settings and hooks take no part), a project agent `probe-worker`
(.claude/agents, created before the session starts, P-13) and a hook on PreToolUse,
SubagentStart and SubagentStop that appends each payload's keys and the fields of
interest to hooks.jsonl. Nothing is written to ~/.claude/{settings.json,hooks,skills,
agents}. claude is started windowless (CREATE_NO_WINDOW). Runs:
  A  "launch probe-worker"                       -> the field name, SubagentStart agent_type
  B  "launch a subagent, do not choose a type"   -> the value when unnamed
  C  as B, the hook rewrites the type to probe-worker through updatedInput
  D  "launch ulthar-creator@probe" (skills probe-a, probe-b; one codeword each; no tool use)
  E  as B (unnamed), the hook rewrites the type to ulthar-creator@probe
Each subagent is asked to Read probe.txt, so its own PreToolUse fires (P-18).
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
BUDGET_USD = "0.50"
TIMEOUT_SEC = 400
FIELDS = ("hook_event_name", "session_id", "agent_id", "agent_type", "tool_name",
          "tool_use_id", "source")
HOOK = r'''import json, sys
payload = json.load(sys.stdin)
keep = %r
rec = {k: payload.get(k) for k in keep if k in payload}
rec["keys"] = sorted(payload)
ti = payload.get("tool_input")
if payload.get("tool_name") in ("Agent", "Task"):
    rec["tool_input"] = {k: (v if k != "prompt" else str(v)[:80]) for k, v in (ti or {}).items()}
with open(sys.argv[1], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(rec) + "\n")
if sys.argv[2:3] == ["rewrite"] and payload.get("hook_event_name") == "PreToolUse" and payload.get("tool_name") in ("Agent", "Task"):
    new = dict(ti or {}, subagent_type=sys.argv[3])
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": new}}))
''' % (FIELDS,)
AGENT = """---
name: probe-worker
description: Probe agent. Reads probe.txt and reports its first line.
tools: Read
model: haiku
---
You are probe-worker. Read the file probe.txt in the current directory with the Read tool,
then answer with exactly: "probe-worker read: <its first line>".
"""
PACK_AGENT = """---
name: ulthar-creator@probe
description: Probe pack agent. Reports the codewords of its preloaded skills.
tools: Read
skills: probe-a, probe-b
model: haiku
---
Do not use any tool. Answer with the codeword written in each of your preloaded skills
probe-a and probe-b, exactly as written, as "probe-a: <word>, probe-b: <word>". If you cannot
see one, write NOT VISIBLE for it.
"""
PROMPTS = {
    "A": "Use the Agent tool once with subagent_type \"probe-worker\" and the prompt "
         "\"Read probe.txt and report its first line.\" Then reply with what it returned.",
    "B": "Use the Agent tool once to start a subagent. Do NOT set any subagent_type "
         "parameter (leave it out entirely). Prompt: \"Read the file probe.txt with the Read "
         "tool and report its first line. If you are probe-worker, say so.\" Then reply "
         "with what it returned.",
    "D": "Use the Agent tool once with subagent_type \"ulthar-creator@probe\" and the prompt "
         "\"Report your skills' codewords.\" Then reply with exactly what it returned.",
    "E": "Use the Agent tool once to start a subagent. Do NOT set any subagent_type "
         "parameter (leave it out entirely). Prompt: \"Report the codewords of your preloaded "
         "skills probe-a and probe-b without using any tool.\" Then reply with what it returned.",
}


def run(name: str, prompt_key: str, rewrite: str | None, logs: Path) -> dict:
    project = Path(tempfile.mkdtemp(prefix=f"ulthar-p17-{name}-"))
    (project / ".claude" / "agents").mkdir(parents=True)
    (project / ".claude" / "agents" / "probe-worker.md").write_text(AGENT, encoding="utf-8")
    (project / ".claude" / "agents" / "ulthar-creator@probe.md").write_text(PACK_AGENT,
                                                                         encoding="utf-8")
    codewords = {}
    for skill in ("probe-a", "probe-b"):
        codewords[skill] = f"CODE-{os.urandom(3).hex()}"
        md = project / ".claude" / "skills" / skill / "SKILL.md"
        md.parent.mkdir(parents=True)
        md.write_text(f"---\nname: {skill}\ndescription: Probe skill {skill}.\n---\n"
                      f"Codeword: {codewords[skill]}\n", encoding="utf-8")
    word = f"WORD-{os.urandom(3).hex()}"
    (project / "probe.txt").write_text(word + "\n", encoding="utf-8")
    (project / "hook.py").write_text(HOOK, encoding="utf-8")
    hook_log = project / "hooks.jsonl"
    cmd = f'"{PY}" -S "{(project / "hook.py").as_posix()}" "{hook_log.as_posix()}"'
    entry = [{"hooks": [{"type": "command", "command": cmd + (f" rewrite {rewrite}" if rewrite else "")}]}]
    plain = [{"hooks": [{"type": "command", "command": cmd}]}]
    settings = {"permissions": {"allow": ["Agent", "Task", "Read"]},
                "hooks": {"PreToolUse": entry, "SubagentStart": plain, "SubagentStop": plain}}
    settings_path = project / "probe-settings.json"
    settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    log = logs / f"p17-{name}.jsonl"
    argv = ["claude", "-p", "--model", "haiku", "--settings", str(settings_path),
            "--setting-sources", "project,local", "--output-format", "stream-json", "--verbose",
            "--max-budget-usd", BUDGET_USD, PROMPTS[prompt_key]]
    started = time.monotonic()
    with open(log, "w", encoding="utf-8") as out:
        proc = subprocess.run(argv, cwd=str(project), stdout=out, stderr=subprocess.STDOUT,
                              stdin=subprocess.DEVNULL, timeout=TIMEOUT_SEC,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    init_session, result = None, None
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict) and ev.get("subtype") == "init":
            init_session = ev.get("session_id")
        if isinstance(ev, dict) and ev.get("type") == "result":
            result = str(ev.get("result"))[:300]
    hooks = ([json.loads(x) for x in hook_log.read_text(encoding="utf-8").splitlines()]
             if hook_log.exists() else [])
    return {"run": name, "rewrite": rewrite, "exit": proc.returncode,
            "seconds": round(time.monotonic() - started, 1), "word": word, "codewords": codewords,
            "main_session_id": init_session, "hooks": hooks, "result": result,
            "log": str(log), "project": str(project)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--logs")
    ap.add_argument("runs", nargs="*", default=["A", "B", "C", "D", "E"])
    args = ap.parse_args()
    logs = Path(args.logs or tempfile.mkdtemp(prefix="ulthar-p17-logs-"))
    logs.mkdir(parents=True, exist_ok=True)
    plan = {"A": ("A", None), "B": ("B", None), "C": ("B", "probe-worker"),
            "D": ("D", None), "E": ("E", "ulthar-creator@probe")}
    out = [run(r, *plan[r], logs) for r in args.runs]
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
