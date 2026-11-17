"""R1-e AC-8: direct writes to external tools are blocked (PORT-03, spec 事前決定 3), through the
production hook entry, in a project that has `.claude/ulthar.json`. No external tool is
called: only the PreToolUse decision is taken."""
from __future__ import annotations

import sys

from helpers import Probe, Sandbox
from r1e_helpers import call, guard_of_last, set_config

ALLOWED_TOOL = "mcp__pencil__batch_design"


def check_external(p: Probe) -> None:
    sb = Sandbox()
    try:
        set_config(sb, mcp_allow=[ALLOWED_TOOL])
        cases = {
            "Linear MCP save_issue": ("mcp__linear-server__save_issue", {"title": "x"}, True),
            "gh pr create": ("Bash", {"command": "gh pr create --title t --body b"}, True),
            "curl -X POST api.linear.app": ("Bash", {"command": "curl -X POST https://api.linear.app/graphql "
                                                                "-d '{\"query\":\"mutation\"}'"}, True),
            "bash -c gh issue comment": ("Bash", {"command": "bash -c \"gh issue comment 12 -b done\""}, True),
            "Linear MCP list_issues": ("mcp__linear-server__list_issues", {}, False),
            f"mcp_allow {ALLOWED_TOOL}": (ALLOWED_TOOL, {}, False),
            "gh pr view": ("Bash", {"command": "gh pr view 12 --json title"}, False),
        }
        blocked = allowed = 0
        for name, (tool, ti, should_block) in cases.items():
            res = call(sb, tool, ti)
            if should_block:
                ok = res.returncode == 2 and "external write" in res.stderr and guard_of_last(sb) == "external_write"
                blocked += ok
            else:
                ok = res.returncode == 0
                allowed += ok
            p.check(f"AC-8: {name} -> {'blocked' if should_block else 'allowed'}", ok,
                    f"{res.returncode} {res.stderr[-300:]}")
        p.check(f"AC-8: blocked {blocked}/4, allowed {allowed}/3", blocked == 4 and allowed == 3)
        extra = {
            "gh api -X PATCH": ("gh api -X PATCH repos/o/r/issues/1 -f state=closed", True),
            "gh api read": ("gh api repos/o/r/issues", False),
            "Invoke-RestMethod -Method Post api.trello.com": (
                "Invoke-RestMethod -Uri https://api.trello.com/1/cards -Method Post", True),
            "curl GET api.github.com": ("curl -s https://api.github.com/repos/o/r", False),
            "gh -R o/r issue close": ("gh -R o/r issue close 3", True),
            "gh three shells deep (fail closed)": ("bash -c \"bash -c 'bash -c gh'\"", True),
        }
        for name, (command, should_block) in extra.items():
            tool = "PowerShell" if command.startswith("Invoke") else "Bash"
            res = call(sb, tool, {"command": command})
            p.check(f"AC-8 (more): {name} -> {'blocked' if should_block else 'allowed'}",
                    (res.returncode == 2) == should_block, res.stderr[-300:])
    finally:
        sb.cleanup()
    plain = Sandbox()
    try:
        (plain.work / ".claude" / "ulthar.json").unlink()
        res = call(plain, "mcp__linear-server__save_issue", {"title": "x"})
        p.check("AC-8: a project without .claude/ulthar.json is not judged", res.returncode == 0,
                res.stderr[-300:])
    finally:
        plain.cleanup()


def main() -> int:
    p = Probe()
    print("\n== R1-e AC-8: external writes ==")
    check_external(p)
    return p.result()


if __name__ == "__main__":
    sys.exit(main())
