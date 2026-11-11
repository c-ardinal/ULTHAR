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
(a junction alias and a case variant) and the project config (Write, Bash write):
blocked 7/7. Shell-side path assembly
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
        res = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],  # cp932 output: bytes
                             capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        return res.returncode == 0
    os.symlink(target, link, target_is_directory=True)
    return True


def check_hook_only_and_aliases(p: Probe) -> None:
    """AC-4 a2 (hook-only entry points, main and subagent), c (a junction alias and a
    case variant of a store path) and the project config .claude/ulthar.json (a Write
    and a Bash write): blocked 7/7."""
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
            ("c Write to the project config .claude/ulthar.json", {"tool_name": "Write",
                                                                   "tool_input": {
                "file_path": str(sb.work / ".claude" / "ulthar.json"), "content": "{}"}}),
            ("d Bash write to the project config", {"tool_name": "Bash", "tool_input": {
                "command": f"cp forged.json {str(sb.work).replace(chr(92), '/')}/.claude/ulthar.json"}}),
        ]
        blocked = 0
        for label, payload in cases:
            proc = sb.hook("PreToolUse", dict(payload, session_id="prot2"))
            ok = proc.returncode == 2 and "memory store" in proc.stderr
            blocked += ok
            p.check(f"{label}: blocked", ok, f"rc={proc.returncode} {proc.stderr[-300:]}")
        p.check("the junction alias was created for the test", made)
        p.check(f"AC-4 a2 + c aliases + project config: blocked {blocked}/7", blocked == 7)
        free = sb.hook("PreToolUse", {"session_id": "prot2", "tool_name": "Write", "tool_input": {
            "file_path": str(sb.work / "notes" / "ok.txt"), "content": "x"}})
        p.check("  ... an ordinary new file next to it is not blocked", free.returncode == 0,
                free.stderr[-300:])
    finally:
        sb.cleanup()


def check_quoted_module_forms(p: Probe) -> None:
    """Review r1b-2 item 1: quoted and concatenated module/script words are read the way
    the shell reads them (Bash and PowerShell quoting), so these hook-only calls are
    blocked too; an argument that merely mentions `ulthar hook` is not."""
    sb = Sandbox()
    try:
        cases = [
            ("Bash", None, 'python -m "ulthar" hook --event PostToolUse < forged.json'),
            ("PowerShell", "a-q", 'python -m "ulthar.__main__" extract --episode E-1 --project-dir .'),
            ("PowerShell", None, "& 'python' -m 'ulthar' hook --event PreToolUse"),
            ("Bash", None, 'python -m ul"thar" hook --event Stop'),
            ("Bash", "a-q", "bash -c \"python -multhar extract --episode E-2 --project-dir .\""),
        ]
        blocked = 0
        for tool, agent, cmd in cases:
            payload = {"session_id": "q", "tool_name": tool, "tool_input": {"command": cmd}}
            if agent:
                payload.update(agent_id=agent, agent_type="t")
            proc = sb.hook("PreToolUse", payload)
            ok = proc.returncode == 2 and "AC-4 a2" in proc.stderr
            blocked += ok
            p.check(f"quoted form blocked ({tool}): {cmd}", ok, f"rc={proc.returncode} {proc.stderr[-200:]}")
        p.check(f"AC-4 a2 quoted/concatenated forms: blocked {blocked}/{len(cases)}",
                blocked == len(cases))
        mention = sb.hook("PreToolUse", {"session_id": "q", "tool_name": "Bash", "tool_input": {
            "command": 'ulthar fact add --src README.md:1 "never run ulthar hook by hand"'}})
        p.check("  ... text that only mentions `ulthar hook` inside an argument is blocked too "
                "(the fail-closed layer strips quotes: accepted over-blocking, spec afa6aae7)",
                mention.returncode == 2 and "AC-4 a2" in mention.stderr, mention.stderr[-300:])
    finally:
        sb.cleanup()


ORDINARY_REPO_COMMANDS = [
    r"git -C C:\_Workspace\ULTHAR status --short v2",
    "git -C C:/_Workspace/ULTHAR log --oneline -5",
    r"C:/Python314/python.exe -X utf8 C:\_Workspace\ULTHAR\v2\tests\run_all.py",
    "cat v2/ulthar/hook.py",
    r"cat C:\_Workspace\ULTHAR\v2\ulthar\hook.py",
    r"type C:\_Workspace\ULTHAR\v2\ulthar\extract.py",
    "grep -n extract C:/_Workspace/ULTHAR/v2/ulthar/worker.py",
]


def check_fail_closed_layer(p: Probe) -> None:
    """AC-4 a2's fail-closed layer (spec afa6aae7): ANSI-C quoting and command
    substitution around ulthar are blocked 3/3; plain ulthar CLI calls stay allowed 3/3;
    ordinary commands on this repository's paths (C:\\_Workspace\\ULTHAR\\v2\\...) are
    not blocked (over-blocking 0 for them)."""
    sb = Sandbox()
    try:
        deny = ["python -m $'ulthar' hook --event PostToolUse < forged.json",
                "$(ulthar hook --event PostToolUse < forged.json)",
                "`ulthar hook --event PostToolUse < forged.json`"]
        allow = ["ulthar decide list", 'ulthar fact add --src README.md:1 "the README names it"',
                 "ulthar store verify"]
        n_deny = n_allow = 0
        for cmd in deny:
            proc = sb.hook("PreToolUse", {"session_id": "fc", "tool_name": "Bash",
                                          "tool_input": {"command": cmd}})
            ok = proc.returncode == 2 and "AC-4 a2" in proc.stderr
            n_deny += ok
            p.check(f"blocked: {cmd}", ok, f"rc={proc.returncode} {proc.stderr[-200:]}")
        for cmd in allow:
            proc = sb.hook("PreToolUse", {"session_id": "fc", "tool_name": "Bash",
                                          "tool_input": {"command": cmd}})
            ok = proc.returncode == 0
            n_allow += ok
            p.check(f"allowed: {cmd}", ok, proc.stderr[-300:])
        p.check(f"AC-4 a2 fail-closed layer: blocked {n_deny}/3, allowed {n_allow}/3",
                n_deny == 3 and n_allow == 3)
        with sb.inproc():
            over = [c for c in ORDINARY_REPO_COMMANDS if memhook.protect(
                {"tool_name": "Bash", "tool_input": {"command": c}, "cwd": str(sb.work)})]
        p.check(f"ordinary commands on this repo's paths: over-blocked {len(over)}/"
                f"{len(ORDINARY_REPO_COMMANDS)}", not over, str(over))
    finally:
        sb.cleanup()


def main() -> int:
    p = Probe()
    check_fixtures(p)
    check_read_paths_allowed(p)
    check_hook_only_and_aliases(p)
    check_quoted_module_forms(p)
    check_fail_closed_layer(p)
    return p.result()


if __name__ == "__main__":
    sys.exit(main())
