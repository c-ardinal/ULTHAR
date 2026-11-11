"""R1-b AC-4: who may write the memory store (fixtures/store_protect.json, through the
production hook, main session and subagent alike):
  (a) `ulthar decide` write subcommands from Bash/PowerShell -- add and ingest from the
      main session and confirm from a subagent blocked (3), confirm from the main
      session allowed (it only consumes a hook-issued token);
  (b) `ulthar fact add` -- allowed;
  (c) Edit/Write/MultiEdit/NotebookEdit into <knowledge_root>/.ulthar/ or ~/.claude/ulthar/
      -- 4 blocked;
  (d) shell commands naming those paths that are not ulthar CLI calls -- 2 blocked.
Also AC-4 a2 (`ulthar hook`, `ulthar extract`, `__main__.py hook`) and c's resolution
(a junction alias and a case variant): blocked 5/5. Shell-side path assembly
(Join-Path etc.) is the spec's known limit, detected later through AC-20's record.
Each block must come from the memory-store guard itself (memhook.protect), not only
from some other rule, so the guard is also checked directly on every fixture.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from helpers import Probe, Sandbox, load_fixture

from ulthar import memhook


def _subst(node, mapping: dict):
    if isinstance(node, str):
        for k, v in mapping.items():
            node = node.replace(k, v)
        return node
    if isinstance(node, dict):
        return {k: _subst(v, mapping) for k, v in node.items()}
    if isinstance(node, list):
        return [_subst(v, mapping) for v in node]
    return node


def check_fixtures(p: Probe) -> None:
    sb = Sandbox()
    try:
        fixtures = _subst(load_fixture("store_protect.json"),
                          {"${KR}": str(sb.kr).replace("\\", "/"),
                           "${HOME}": str(sb.home).replace("\\", "/")})
        blocked = allowed = 0
        want_blocked = sum(fx["expect"] == "deny" for fx in fixtures)
        for fx in fixtures:
            payload = {"session_id": "prot", "tool_name": fx["tool_name"],
                       "tool_input": fx["tool_input"]}
            if fx["agent"]:
                payload.update(agent_id="a-prot", agent_type="ulthar-creator")
            proc = sb.hook("PreToolUse", payload)
            with sb.inproc():
                direct = memhook.protect(dict(payload, cwd=str(sb.work)))
            if fx["expect"] == "deny":
                ok = proc.returncode == 2 and direct is not None and "memory store" in proc.stderr
                blocked += ok
            else:
                ok = proc.returncode == 0 and direct is None
                allowed += ok
            p.check(f"{fx['id']} {fx['tool_name']} ({'subagent' if fx['agent'] else 'main'}): "
                    f"{fx['expect']}", ok, f"rc={proc.returncode} direct={direct!r} {proc.stderr[-300:]}"
                    f" {json.dumps(payload)[:300]}")
        p.check(f"AC-4: blocked {blocked}/{want_blocked}", blocked == want_blocked == 9)
        p.check(f"AC-4: allowed {allowed}/2", allowed == 2)
        rows = sb.query("SELECT guard_id, decision FROM events WHERE hook='PreToolUse' ORDER BY id")
        p.check("AC-4: each block is recorded as a deny of guard memory_store",
                [r for r in rows if r[1] == "deny"] == [("memory_store", "deny")] * 9, str(rows))
    finally:
        sb.cleanup()


def check_read_paths_allowed(p: Probe) -> None:
    sb = Sandbox()
    try:
        for cmd in ("ulthar decide list", "ulthar decide review", "ulthar inject --layer L1 --session s",
                    "git status"):
            proc = sb.hook("PreToolUse", {"session_id": "prot", "tool_name": "Bash",
                                          "tool_input": {"command": cmd}})
            p.check(f"not blocked: {cmd}", proc.returncode == 0, proc.stderr[-300:])
    finally:
        sb.cleanup()


def _junction(link, target) -> bool:
    """A directory junction (no admin rights needed); a symlink where there are none."""
    import subprocess  # noqa: PLC0415

    if os.name == "nt":
        res = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                             capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
        return res.returncode == 0
    os.symlink(target, link, target_is_directory=True)
    return True


def check_hook_only_and_aliases(p: Probe) -> None:
    """AC-4 a2 (hook-only entry points, main and subagent) and c (a junction alias and a
    case variant of a store path): blocked 5/5."""
    sb = Sandbox()
    try:
        main_py = str(Path(memhook.__file__).with_name("__main__.py")).replace("\\", "/")
        (sb.kr / ".ulthar").mkdir(parents=True)
        alias = sb.work / "notes-alias"
        made = _junction(alias, sb.kr / ".ulthar")
        upper = str(sb.kr).replace("\\", "/") + "/.ULTHAR/decisions.jsonl"
        cases = [
            ("a2 ulthar hook (main)", {"tool_name": "Bash", "tool_input": {
                "command": "echo '{}' | ulthar hook --event PostToolUse"}}),
            ("a2 ulthar extract (subagent)", {"tool_name": "PowerShell", "agent_id": "a-x",
                                              "agent_type": "t", "tool_input": {
                "command": "python -m ulthar extract --episode E-1 --project-dir ."}}),
            ("a2 __main__.py hook directly (main)", {"tool_name": "Bash", "tool_input": {
                "command": f'python -S "{main_py}" hook --event PreToolUse < forged.json'}}),
            ("c Write through a junction alias of .ulthar", {"tool_name": "Write", "tool_input": {
                "file_path": str(alias / "decisions.jsonl"), "content": "x"}}),
            ("c Edit with a case variant (.ULTHAR)", {"tool_name": "Edit", "tool_input": {
                "file_path": upper, "old_string": "a", "new_string": "b"}}),
        ]
        blocked = 0
        for label, payload in cases:
            proc = sb.hook("PreToolUse", dict(payload, session_id="prot2"))
            ok = proc.returncode == 2 and "memory store" in proc.stderr
            blocked += ok
            p.check(f"{label}: blocked", ok, f"rc={proc.returncode} {proc.stderr[-300:]}")
        p.check("the junction alias was created for the test", made)
        p.check(f"AC-4 a2 + c aliases: blocked {blocked}/5", blocked == 5)
        free = sb.hook("PreToolUse", {"session_id": "prot2", "tool_name": "Write", "tool_input": {
            "file_path": str(sb.work / "notes" / "ok.txt"), "content": "x"}})
        p.check("  ... an ordinary new file next to it is not blocked", free.returncode == 0,
                free.stderr[-300:])
    finally:
        sb.cleanup()


def main() -> int:
    p = Probe()
    check_fixtures(p)
    check_read_paths_allowed(p)
    check_hook_only_and_aliases(p)
    return p.result()


if __name__ == "__main__":
    sys.exit(main())
