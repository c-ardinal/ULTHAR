"""R1-b AC-4: who may write the memory store (fixtures/store_protect.json, through the
production hook, main session and subagent alike):
  (a) `ulthar decide` write subcommands from Bash/PowerShell -- 3 blocked;
  (b) `ulthar fact add` -- allowed;
  (c) Edit/Write/MultiEdit/NotebookEdit into <knowledge_root>/.ulthar/ or ~/.claude/ulthar/
      -- 4 blocked;
  (d) shell commands naming those paths that are not ulthar CLI calls -- 2 blocked.
Each block must come from the memory-store guard itself (memhook.protect), not only
from some other rule, so the guard is also checked directly on every fixture.
"""
from __future__ import annotations

import json
import sys

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
        p.check(f"AC-4: allowed {allowed}/1", allowed == 1)
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


def main() -> int:
    p = Probe()
    check_fixtures(p)
    check_read_paths_allowed(p)
    return p.result()


if __name__ == "__main__":
    sys.exit(main())
