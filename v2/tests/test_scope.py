"""R1-c AC-11 (GOAL-08): in a project with .claude/ulthar.json, product files are written
only inside the scope_globs of a unit that is contract_confirmed .. owner_review, through
the production hook entry (PreToolUse):
  blocked: no unit, a contract_draft unit's scope, outside a confirmed scope, a Bash
           redirection outside it;
  allowed: inside the confirmed scope (the event records unit_id), excluded paths
           (.claude/state/, docs/plans/, non_product_globs), PowerShell Set-Content inside.
Path resolution as R1-b AC-4 c: a case variant is the same path; a junction alias is
judged by where it leads, too.
"""
from __future__ import annotations

import json
import sys

from helpers import Probe
from r1c_helpers import SESSION, cli, contract, edit, make_unit, new_sandbox, to_build, write_contract
from test_protect import _junction


def shell(sb, tool: str, command: str, agent: str = "bld-1"):
    return sb.hook("PreToolUse", {"session_id": SESSION, "agent_id": agent,
                                  "agent_type": "ulthar-creator", "tool_name": tool,
                                  "tool_input": {"command": command}})


def last_detail(sb) -> dict:
    row = sb.query("SELECT detail FROM events WHERE hook='PreToolUse' ORDER BY id DESC LIMIT 1")
    return json.loads(row[0][0]) if row else {}


def check_scope(p: Probe) -> None:
    sb = new_sandbox(non_product_globs=["notes/**"])
    # the v1 tool-discipline rule blocks shell file creation before the memory step runs;
    # the project switches it off (an owner overlay) so the shell cases reach AC-11 itself
    (sb.work / ".claude" / "ulthar-overlay.json").write_text(json.dumps(
        {"disable": ["command_rules.tool_discipline/shell-file-creation"]}), encoding="utf-8")
    try:
        blocked = 0
        r = edit(sb, "bld-1", "src/app.txt")
        ok = r.returncode == 2 and "unit scope" in r.stderr
        blocked += ok
        p.check("AC-11: no unit -> a product write is blocked", ok, r.stderr[-300:])
        draft = make_unit(sb)
        cli(sb, "contract", "submit", draft, "--file", str(write_contract(sb, contract())))
        r = edit(sb, "bld-1", "src/app.txt")
        ok = r.returncode == 2 and "unit scope" in r.stderr
        blocked += ok
        p.check("AC-11: only a contract_draft unit with src/** -> blocked", ok, r.stderr[-300:])
        unit_id = to_build(sb)
        r = edit(sb, "bld-1", "lib/util.py", tool="Write")
        ok = r.returncode == 2 and "unit scope" in r.stderr
        blocked += ok
        p.check("AC-11: a confirmed (build) unit, a path outside its scope -> blocked", ok,
                r.stderr[-300:])
        r = shell(sb, "Bash", "echo hello > lib/out.txt")
        ok = r.returncode == 2 and "unit scope" in r.stderr
        blocked += ok
        p.check("AC-11: Bash redirection outside the scope -> blocked", ok, r.stderr[-300:])
        p.check(f"AC-11: blocked {blocked}/4", blocked == 4)

        allowed = 0
        r = edit(sb, "bld-1", "src/app.txt")
        ok = r.returncode == 0 and last_detail(sb).get("unit_id") == unit_id
        allowed += ok
        p.check("AC-11: inside the confirmed scope -> allowed, the event records unit_id", ok,
                r.stderr[-300:] + str(last_detail(sb)))
        excluded = [edit(sb, "bld-1", rel, tool="Write").returncode for rel in
                    ("docs/plans/x/STATE.md", ".claude/state/note.txt", "notes/today.md")]
        ok = excluded == [0, 0, 0]
        allowed += ok
        p.check("AC-11: excluded paths (docs/plans/, .claude/state/, non_product_globs) -> "
                "allowed", ok, str(excluded))
        r = shell(sb, "PowerShell", "Set-Content -Path src\\new.txt -Value hello")
        ok = r.returncode == 0 and last_detail(sb).get("unit_id") == unit_id
        allowed += ok
        p.check("AC-11: PowerShell Set-Content inside the scope -> allowed", ok, r.stderr[-300:])
        p.check(f"AC-11: allowed {allowed}/3", allowed == 3)

        def scoped(r) -> bool:
            return r.returncode == 2 and "unit scope" in r.stderr

        more = {
            "PowerShell Set-Content outside the scope":
                scoped(shell(sb, "PowerShell", "Set-Content lib\\x.txt hello")),
            "cp into lib/ (destination outside)": scoped(shell(sb, "Bash", "cp src/app.txt lib/")),
            # (rm is stopped earlier by the v1 delete rule, so touch stands for the operand case)
            "touch of a file outside": scoped(shell(sb, "Bash", "touch lib/old.txt")),
            "bash -c with a redirection outside":
                scoped(shell(sb, "Bash", "bash -c \"echo x > lib/y.txt\"")),
            "a case variant SRC\\APP.TXT is the same path (allowed)":
                edit(sb, "bld-1", "SRC\\APP.TXT").returncode == 0,
            "writing outside the repository is not a product write":
                shell(sb, "Bash", f"echo x > {(sb.root / 'outside.txt').as_posix()}").returncode == 0,
            "the main session is held to the same rule": scoped(edit(sb, None, "lib/main.py")),
        }
        (sb.work / "lib-real").mkdir()
        made = _junction(sb.work / "src" / "lnk", sb.work / "lib-real")
        p.check("the junction alias was created for the test", made)
        more["a junction src/lnk -> lib-real (out of scope) is judged by its target"] = (
            made and scoped(edit(sb, "bld-1", "src/lnk/z.py")))
        for label, ok in more.items():
            p.check(f"AC-11: {label}", ok)
    finally:
        sb.cleanup()


def main() -> int:
    p = Probe()
    print("\n== AC-11: product writes only inside a confirmed scope ==")
    check_scope(p)
    return p.result()


if __name__ == "__main__":
    sys.exit(main())
