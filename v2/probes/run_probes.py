#!/usr/bin/env python3
"""AC-10 probes: P-06, P-13, P-14 with throwaway projects and `claude -p --model haiku`.

    C:/Python314/python.exe -X utf8 v2/probes/run_probes.py [P-06 P-13 P-14] [--logs DIR]

Every probe runs in a fresh temp project with its own settings.json, passed with
--settings, and `--setting-sources project,local` so the owner's user settings
(and their hooks) do not take part. Nothing is written to ~/.claude/{settings.json,
hooks,skills,agents}. Logs (stream-json, debug) go to --logs (default: a temp dir);
the verdicts are printed as JSON for v2/probes/RESULTS.md. Each run is capped with
--max-budget-usd.
"""
from __future__ import annotations

import argparse
import json
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PY = sys.executable
BUDGET_USD = "0.30"
TIMEOUT_SEC = 300


def claude(project: Path, prompt: str, settings: dict, log: Path, extra=(), wait=True):
    settings_path = project / "probe-settings.json"
    settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    argv = ["claude", "-p", "--model", "haiku", "--settings", str(settings_path),
            "--setting-sources", "project,local", "--output-format", "stream-json",
            "--verbose", "--max-budget-usd", BUDGET_USD, *extra, prompt]
    out = open(log, "w", encoding="utf-8")
    proc = subprocess.Popen(argv, cwd=str(project), stdout=out, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8")
    if not wait:
        return proc, out
    try:
        proc.wait(timeout=TIMEOUT_SEC)
    finally:
        out.close()
    return proc.returncode


def events(log: Path) -> list[dict]:
    out = []
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def final_result(log: Path) -> str:
    for ev in reversed(events(log)):
        if ev.get("type") == "result":
            return str(ev.get("result"))
    return ""


def tool_uses(log: Path) -> list[dict]:
    uses = []
    for ev in events(log):
        for block in (ev.get("message") or {}).get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                uses.append({"name": block.get("name"), "input": block.get("input"),
                             "parent": ev.get("parent_tool_use_id")})
    return uses


def tool_results(log: Path) -> list[str]:
    res = []
    for ev in events(log):
        for block in (ev.get("message") or {}).get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                c = block.get("content")
                res.append(c if isinstance(c, str) else json.dumps(c, ensure_ascii=False))
    return res


# --------------------------------------------------------------------------- P-06

def probe_p06(root: Path, logs: Path) -> dict:
    """Does a subagent's `skills:` preload the FULL skill body at startup?
    The skill body is ~30 KB with one codeword on its first line and another on its
    last; the subagent has no file, shell or Skill tool, so it can only quote both
    if the whole body was injected into its context."""
    project = root / "p06"
    head, tail = f"HEAD-{secrets.token_hex(4)}", f"TAIL-{secrets.token_hex(4)}"
    skill = project / ".claude" / "skills" / "probe-skill" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    filler = "\n".join(f"Filler rule {i}: this line exists only to make the skill long."
                       for i in range(500))
    skill.write_text(
        "---\nname: probe-skill\ndescription: Probe skill for a preload test.\n---\n"
        f"First codeword: {head}\n\n{filler}\n\nLast codeword: {tail}\n", encoding="utf-8")
    agent = project / ".claude" / "agents" / "prober.md"
    agent.parent.mkdir(parents=True)
    agent.write_text(
        "---\nname: prober\ndescription: Reports the codewords of its preloaded skill.\n"
        "tools: Read\ndisallowedTools: Grep, Glob, Bash, PowerShell, Skill, Write, Edit\n"
        "skills: probe-skill\nmodel: haiku\n---\n"
        "Do not use any tool. You report the first and the last codeword written in your preloaded skill "
        "probe-skill, exactly as written. If you cannot see them, say NOT VISIBLE.\n",
        encoding="utf-8")
    log = logs / "p06.stream.jsonl"
    rc = claude(project, "Use the Agent tool with subagent_type \"prober\" and the prompt "
                "\"Report both codewords.\" Then reply with exactly what it returned.",
                {"permissions": {"allow": ["Agent"]}}, log)
    reply = final_result(log)
    main_tools = [u["name"] for u in tool_uses(log) if not u["parent"]]
    sub_log = subagent_transcript(log)
    sub_lines = (sub_log.read_text(encoding="utf-8", errors="replace").splitlines()
                 if sub_log else [])
    first_tool = next((i for i, ln in enumerate(sub_lines)
                       if '"type":"tool_use"' in ln.replace(" ", "")), len(sub_lines))
    before_tools = "\n".join(sub_lines[:first_tool])
    return {"probe": "P-06", "rc": rc, "skill_bytes": skill.stat().st_size,
            "reply_has_head": head in reply, "reply_has_tail": tail in reply,
            "main_chain_tools": main_tools,
            "subagent_transcript": str(sub_log) if sub_log else None,
            "subagent_tool_uses": sum('"type":"tool_use"' in ln.replace(" ", "")
                                      for ln in sub_lines),
            "subagent_context_has_head_and_tail_before_any_tool_use":
                head in before_tools and tail in before_tools,
            "reply": reply[:400], "log": str(log),
            # Holds only when the codewords came back with no file read anywhere: the
            # subagent's one tool (Read, needed to launch at all) must stay unused.
            "holds": (head in reply and tail in reply and main_tools == ["Agent"]
                      and sub_log is not None and first_tool == len(sub_lines))}


def subagent_transcript(log: Path) -> Path | None:
    """The subagent transcript Claude Code saved for this session (read only)."""
    session = next((e.get("session_id") for e in events(log) if e.get("subtype") == "init"), None)
    if not session:
        return None
    found = sorted((Path.home() / ".claude" / "projects").glob(f"*/{session}/subagents/*.jsonl"))
    return found[0] if found else None


# --------------------------------------------------------------------------- P-13

WAIT_PY = """import pathlib, sys, time
flag = pathlib.Path('waiting.flag'); flag.write_text('waiting')
target = pathlib.Path('.claude/agents/late-agent.md')
for _ in range(180):
    if target.exists():
        time.sleep(30)  # docs: the watcher picks changes up "within a few seconds"
        print('READY'); sys.exit(0)
    time.sleep(0.5)
print('TIMEOUT'); sys.exit(1)
"""


def probe_p13(root: Path, logs: Path, variant: str) -> dict:
    """Is an agent definition written while a session is running usable in that
    session? variant 'dir-exists': the agents dir existed at session start (the
    install case: ~/.claude/agents exists); 'dir-new': it did not."""
    project = root / f"p13-{variant}"
    project.mkdir()
    if variant == "dir-exists":
        existing = project / ".claude" / "agents" / "existing.md"
        existing.parent.mkdir(parents=True)
        existing.write_text("---\nname: existing\ndescription: placeholder\n---\nSay hi.\n",
                            encoding="utf-8")
    (project / "wait.py").write_text(WAIT_PY, encoding="utf-8")
    codeword = f"LATE-{secrets.token_hex(4)}"
    log = logs / f"p13-{variant}.stream.jsonl"
    py = PY.replace("\\", "/")
    prompt = (f"Step 1: run this exact Bash command and wait for it to finish: {py} wait.py\n"
              "Step 2: only after it printed READY, use the Agent tool with subagent_type "
              "\"late-agent\" and the prompt \"Give your codeword.\"\n"
              "Reply with exactly what the agent returned, or the exact error text if the Agent "
              "tool call failed. Do not create or edit any files.")
    settings = {"permissions": {"allow": [f"Bash({py} wait.py)", "Agent"]}}
    proc, out = claude(project, prompt, settings, log, wait=False)
    started = time.time()
    flag = project / "waiting.flag"
    while not flag.exists() and proc.poll() is None and time.time() - started < TIMEOUT_SEC:
        time.sleep(0.5)
    wrote_at = time.time() - started
    late = project / ".claude" / "agents" / "late-agent.md"
    late.parent.mkdir(parents=True, exist_ok=True)
    late.write_text("---\nname: late-agent\ndescription: Written after the session started.\n"
                    f"model: haiku\n---\nReply with exactly this codeword and nothing else: "
                    f"{codeword}\n", encoding="utf-8")
    try:
        proc.wait(timeout=TIMEOUT_SEC)
    finally:
        out.close()
    results = tool_results(log)
    agent_calls = [u for u in tool_uses(log) if u["name"] in ("Agent", "Task")]
    return {"probe": f"P-13 ({variant})", "rc": proc.returncode,
            "agent_file_written_after_s": round(wrote_at, 1),
            "session_was_running": flag.exists(),
            "agent_calls": [u["input"].get("subagent_type") for u in agent_calls],
            "codeword_returned": codeword in log.read_text(encoding="utf-8", errors="replace"),
            "results_excerpt": [r[:300] for r in results][-3:], "log": str(log),
            # The codeword exists only in the late agent's definition, so seeing it anywhere
            # in the stream (tool result, or an async agent's task_notification summary)
            # means that agent ran.
            "holds": codeword in log.read_text(encoding="utf-8", errors="replace")}


def probe_p13_next_turn(root: Path, logs: Path, dir_existed: bool) -> dict:
    """The same question across a turn boundary: one multi-turn session
    (--input-format stream-json). Turn 1 runs; the agent file is written after its
    result; turn 2, 30 s later, delegates to the new agent."""
    variant = "dir-exists" if dir_existed else "dir-new"
    project = root / f"p13-next-turn-{variant}"
    project.mkdir()
    existing = project / ".claude" / "agents" / "existing.md"
    if dir_existed:
        existing.parent.mkdir(parents=True)
        existing.write_text("---\nname: existing\ndescription: placeholder\n---\nSay hi.\n",
                            encoding="utf-8")
    settings_path = project / "probe-settings.json"
    settings_path.write_text(json.dumps({"permissions": {"allow": ["Agent"]}}), encoding="utf-8")
    codeword = f"LATE-{secrets.token_hex(4)}"
    log = logs / f"p13-next-turn-{variant}.stream.jsonl"
    argv = ["claude", "-p", "--model", "haiku", "--settings", str(settings_path),
            "--setting-sources", "project,local", "--input-format", "stream-json",
            "--output-format", "stream-json", "--verbose", "--max-budget-usd", BUDGET_USD]
    proc = subprocess.Popen(argv, cwd=str(project), stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8")
    lines: list[str] = []

    def send(text: str) -> None:
        proc.stdin.write(json.dumps({"type": "user", "message": {"role": "user",
                                                                 "content": text}}) + "\n")
        proc.stdin.flush()

    def until_result() -> None:
        for line in proc.stdout:
            lines.append(line)
            if '"type":"result"' in line.replace(" ", ""):
                return

    send("Reply with exactly: TURN1")
    until_result()
    late = project / ".claude" / "agents" / "late-agent.md"
    late.parent.mkdir(parents=True, exist_ok=True)
    late.write_text("---\nname: late-agent\ndescription: Written between two turns.\n"
                    f"model: haiku\n---\nReply with exactly this codeword and nothing else: "
                    f"{codeword}\n", encoding="utf-8")
    time.sleep(30)
    send("Use the Agent tool with subagent_type \"late-agent\" and the prompt \"Give your "
         "codeword.\" Reply with exactly what it returned, or the exact error text.")
    until_result()
    proc.stdin.close()
    lines += proc.stdout.readlines()
    proc.wait(timeout=TIMEOUT_SEC)
    log.write_text("".join(lines), encoding="utf-8")
    results = tool_results(log)
    return {"probe": f"P-13 (next turn, {variant})", "rc": proc.returncode,
            "agent_calls": [u["input"].get("subagent_type") for u in tool_uses(log)
                            if u["name"] in ("Agent", "Task")],
            "codeword_returned": codeword in log.read_text(encoding="utf-8", errors="replace"),
            "results_excerpt": [r[:300] for r in results][-2:], "log": str(log),
            # The codeword exists only in the late agent's definition, so seeing it anywhere
            # in the stream (tool result, or an async agent's task_notification summary)
            # means that agent ran.
            "holds": codeword in log.read_text(encoding="utf-8", errors="replace")}


# --------------------------------------------------------------------------- P-14

def probe_p14(root: Path, logs: Path) -> dict:
    """Can the Claude Code sandbox restrict where Bash writes, on native Windows?
    Run A: sandbox enabled, strict (no unsandboxed retry), `denied/` write-denied;
    the model runs a write into denied/ and we check whether the file appeared.
    Run B: the same with failIfUnavailable=true -- the documented startup refusal
    when the platform cannot sandbox."""
    project = root / "p14"
    (project / "denied").mkdir(parents=True)
    target = project / "denied" / "probe.txt"
    sandbox = {"enabled": True, "allowUnsandboxedCommands": False,
               "filesystem": {"denyWrite": [str(project / "denied").replace("\\", "/")]}}
    # All of Bash is allowed so that a permission prompt cannot stand in for the
    # sandbox: whether the write lands is then decided by the sandbox alone.
    settings = {"sandbox": sandbox, "permissions": {"allow": ["Bash"]}}
    log_a = logs / "p14-a.stream.jsonl"
    debug_a = logs / "p14-a.debug.log"
    rc_a = claude(project, "Run this exact Bash command once: echo probe > denied/probe.txt\n"
                  "Then reply with the exact output or error text. Do not retry it any other way.",
                  settings, log_a, extra=("--debug-file", str(debug_a)))
    created = target.exists()
    debug_lines = []
    if debug_a.exists():
        debug_lines = [ln.strip()[:240] for ln in debug_a.read_text(
            encoding="utf-8", errors="replace").splitlines() if "sandbox" in ln.lower()][:12]
    log_b = logs / "p14-b.stream.jsonl"
    rc_b = claude(project, "Reply with OK.", {"sandbox": dict(sandbox, failIfUnavailable=True)},
                  log_b)
    return {"probe": "P-14", "run_a_rc": rc_a, "write_into_denied_dir_succeeded": created,
            "run_a_result": final_result(log_a)[:400], "run_a_sandbox_debug": debug_lines,
            "run_b_rc": rc_b,
            "run_b_output": log_b.read_text(encoding="utf-8", errors="replace")[-600:],
            "logs": [str(log_a), str(debug_a), str(log_b)],
            "holds": not created}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("probes", nargs="*", default=["P-06", "P-13", "P-14"])
    parser.add_argument("--logs", default=None)
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix="ulthar-probes-"))
    logs = Path(args.logs) if args.logs else root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    out = []
    if "P-06" in args.probes:
        out.append(probe_p06(root, logs))
    if "P-13" in args.probes:
        out.append(probe_p13(root, logs, "dir-exists"))
        out.append(probe_p13(root, logs, "dir-new"))
    if "P-13" in args.probes or "P-13-next-turn" in args.probes:
        out.append(probe_p13_next_turn(root, logs, dir_existed=True))
        out.append(probe_p13_next_turn(root, logs, dir_existed=False))
    if "P-14" in args.probes:
        out.append(probe_p14(root, logs))
    print(json.dumps({"projects": str(root), "logs": str(logs), "results": out},
                     indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
