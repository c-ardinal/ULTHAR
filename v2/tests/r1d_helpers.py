"""Shared steps of the R1-d tests (not a test module): a sandbox whose work/ is a git
repository with the `develop` integration branch and a work branch checked out, two
skill packs (ui: 5 skills, api: 1) installed into a temp agents directory, pack agents
started through SubagentStart, writes through PreToolUse -> the file -> PostToolUse,
and a unit driven to accepted the production way (hook entry and CLI).

Sessions: WINDOW (= r1c_helpers.SESSION) is the main session; a subagent's calls carry
its parent's session id (P-18). No real `claude`, nothing under the real ~/.claude."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from helpers import Sandbox
from r1c_helpers import (SESSION, answer, cli, contract, make_unit, new_sandbox, quant_ac,
                         tool_use_id, write_contract)

WINDOW = SESSION
UI_SKILLS = ["ui-layout", "ui-color", "ui-type", "ui-motion", "ui-a11y"]
API_SKILLS = ["api-style"]
WORK_BRANCH = "feature/unit"
UI_AC = {"id": "UI-1", "statement": "src/ui の lint 違反は 0 件", "kind": "quant",
         "verify_cmd": "python checks/value.py checks/AC-1.txt",
         "threshold": {"lo": None, "hi": 3, "hi_incl": True}, "evidence_kind": "stdout_number",
         "sources": ["pack:ui"]}


def pack_defs(default_acs: list | None = None) -> list[dict]:
    return [{"name": "ui", "path_globs": ["src/ui/**"], "skills": UI_SKILLS,
             "default_acs": default_acs or [], "evaluator_checklist": ["余白はトークンに従う"]},
            {"name": "api", "path_globs": ["src/api/**"], "skills": API_SKILLS,
             "default_acs": [], "evaluator_checklist": ["エラーは型で返す"]}]


def git(sb: Sandbox, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(sb.work), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", check=check)


def commit_all(sb: Sandbox, msg: str) -> str:
    git(sb, "add", "-A")
    git(sb, "commit", "-q", "-m", msg)
    return git(sb, "rev-parse", "HEAD").stdout.strip()


def head(sb: Sandbox, ref: str = "HEAD") -> str:
    return git(sb, "rev-parse", ref).stdout.strip()


def agents_dir(sb: Sandbox) -> Path:
    return sb.root / "agents"


def new_r1d_sandbox(packs: list | None = None, install: bool = True) -> Sandbox:
    """The R1-c sandbox (git, checks/value.py, AC-11 active) plus the skills, the packs,
    tools/ (a formatter, a deleter, an undeclared generator), develop, the work branch,
    and the pack agents installed."""
    sb = new_sandbox()
    for name in UI_SKILLS + API_SKILLS:
        md = sb.work / ".claude" / "skills" / name / "SKILL.md"
        md.parent.mkdir(parents=True)
        md.write_text(f"---\nname: {name}\ndescription: test skill {name}\n---\nrule of {name}\n",
                      encoding="utf-8")
    tools = sb.work / "tools"
    tools.mkdir()
    (tools / "fmt.py").write_text("import pathlib, sys\np = pathlib.Path(sys.argv[1])\n"
                                  "p.write_text(p.read_text() + 'formatted\\n')\n", encoding="utf-8")
    (tools / "rm.py").write_text("import pathlib, sys\npathlib.Path(sys.argv[1]).unlink()\n",
                                 encoding="utf-8")
    (tools / "gen.py").write_text("import pathlib, sys\npathlib.Path(sys.argv[1]).write_text("
                                  "'generated\\n')\n", encoding="utf-8")
    (sb.work / "src" / "ui").mkdir()
    (sb.work / "src" / "ui" / "a.txt").write_text("ui v0\n", encoding="utf-8")
    sb.configure(["none"], non_product_globs=[], packs=pack_defs() if packs is None else packs)
    # as test_scope: the v1 rule against shell file creation is switched off (an owner
    # overlay), so the shell cases reach the R1-d checks themselves
    (sb.work / ".claude" / "ulthar-overlay.json").write_text(json.dumps(
        {"disable": ["command_rules.tool_discipline/shell-file-creation"]}), encoding="utf-8")
    commit_all(sb, "r1d sandbox")
    git(sb, "branch", "develop")
    git(sb, "checkout", "-q", "-b", WORK_BRANCH)
    if install:
        res = cli(sb, "pack", "install", "--agents-dir", str(agents_dir(sb)))
        assert res.returncode == 0, res.stderr
    return sb


def payload(tool: str, tool_input: dict, agent: str | None = None, agent_type: str | None = None,
            session: str = WINDOW) -> dict:
    body = {"session_id": session, "tool_name": tool, "tool_use_id": tool_use_id(tool),
            "tool_input": tool_input}
    if agent:
        body.update(agent_id=agent, agent_type=agent_type or "general-purpose")
    return body


def start_agent(sb: Sandbox, agent: str, agent_type: str, session: str = WINDOW):
    return sb.hook("SubagentStart", {"session_id": session, "agent_id": agent,
                                     "agent_type": agent_type})


def write(sb: Sandbox, agent: str | None, rel: str, text: str, agent_type: str | None = None,
          session: str = WINDOW, tool: str = "Write") -> subprocess.CompletedProcess:
    """A Write as Claude Code runs it: PreToolUse; if allowed, the file, then PostToolUse
    (post-hash). Returns the PreToolUse result."""
    path = sb.work / rel
    body = payload(tool, {"file_path": str(path), "content": text}, agent, agent_type, session)
    pre = sb.hook("PreToolUse", body)
    if pre.returncode == 2:
        return pre
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    sb.hook("PostToolUse", dict(body, tool_response={"filePath": str(path)}))
    return pre


def post_write(sb: Sandbox, agent: str | None, rel: str, text: str,
               agent_type: str | None = None, session: str = WINDOW) -> None:
    """Only the file and its PostToolUse (the allowed write event TRUST-04 reads): for
    the integration check's own tests, after the unit left the states that allow writes."""
    path = sb.work / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    body = payload("Write", {"file_path": str(path), "content": text}, agent, agent_type, session)
    sb.hook("PostToolUse", dict(body, tool_response={"filePath": str(path)}))


def bash(sb: Sandbox, agent: str | None, command: str, agent_type: str | None = None,
         session: str = WINDOW) -> subprocess.CompletedProcess:
    return sb.hook("PreToolUse", payload("Bash", {"command": command}, agent, agent_type, session))


def run_ulthar(sb: Sandbox, agent: str | None, args: list[str], session: str = WINDOW,
               agent_type: str | None = None):
    """`ulthar <args>` through Bash: PreToolUse (which may append the runner token),
    then the CLI with the possibly updated command. (pre, cli or None when blocked)."""
    pre = bash(sb, agent, "ulthar " + " ".join(args), agent_type, session)
    if pre.returncode == 2:
        return pre, None
    token = None
    if pre.stdout.strip():
        updated = json.loads(pre.stdout)["hookSpecificOutput"].get("updatedInput") or {}
        if "--runner-token" in updated.get("command", ""):
            token = updated["command"].rsplit(" ", 1)[-1]
    return pre, cli(sb, *args, *(["--runner-token", token] if token else []))


def r1d_contract(**over) -> dict:
    """A ui-pack unit: scope src/ui/** (the lint's LINT-H then infers exactly ["ui"])."""
    return contract(**{"acs": [quant_ac()], "packs": ["ui"], "scope_globs": ["src/ui/**"], **over})


def confirm(sb: Sandbox, unit_id: str, reviewer: str = "rev-c") -> None:
    run_ulthar(sb, reviewer, ["review", "record", "--unit", unit_id, "--kind", "contract",
                              "--result", "pass"], agent_type="general-purpose")
    question = json.loads(cli(sb, "contract", "show", unit_id).stdout)
    answer(sb, question, ["確定"], "confirm")


def advance(sb: Sandbox, unit_id: str, to: str) -> subprocess.CompletedProcess:
    res = cli(sb, "unit", "advance", unit_id, to)
    assert res.returncode == 0, f"{to}: {res.stderr}"
    return res


CREATOR, EVALUATOR = "ulthar-creator@ui", "ulthar-evaluator@ui"


def to_build(sb: Sandbox, doc: dict | None = None) -> str:
    unit_id = make_unit(sb)
    res = cli(sb, "contract", "submit", unit_id, "--file", str(write_contract(sb, doc or r1d_contract())))
    assert res.returncode == 0, res.stderr
    confirm(sb, unit_id)
    advance(sb, unit_id, "contract_confirmed")
    advance(sb, unit_id, "build")
    return unit_id


def to_accepted(sb: Sandbox, builder: str = "bld-1", reviewer: str = "rev-1",
                doc: dict | None = None, commit: bool = True) -> str:
    """A unit whose creator wrote src/ui/a.txt (receipt, post-hash), verified, reviewed by
    the ui evaluator, accepted; the change committed on the work branch."""
    unit_id = to_build(sb, doc)
    start_agent(sb, builder, CREATOR)
    pre = write(sb, builder, "src/ui/a.txt", "ui v1\n", CREATOR)
    assert pre.returncode == 0, pre.stderr
    run_ulthar(sb, builder, ["verify", unit_id, "--self"], agent_type=CREATOR)
    advance(sb, unit_id, "quant_verified")
    start_agent(sb, reviewer, EVALUATOR)
    run_ulthar(sb, reviewer, ["verify", unit_id], agent_type=EVALUATOR)
    _, rec = run_ulthar(sb, reviewer, ["review", "record", "--unit", unit_id, "--kind", "verify",
                                       "--result", "pass", "--via", "codex"], agent_type=EVALUATOR)
    assert rec is not None and rec.returncode == 0, rec.stderr if rec else "blocked"
    advance(sb, unit_id, "owner_review")
    advance(sb, unit_id, "accepted")
    if commit:
        commit_all(sb, f"unit {unit_id}")
    return unit_id


def integrate(sb: Sandbox, unit_id: str, session: str = WINDOW):
    """The window runs `ulthar unit advance <u> integrated` (its runner token). The CLI's
    result -- or, when the hook blocked the call (R1-e AC-1: a window without the lease),
    the hook's."""
    pre, res = run_ulthar(sb, None, ["unit", "advance", unit_id, "integrated"], session=session)
    return res if res is not None else pre


def expire_lease(sb: Sandbox) -> None:
    """Expiry is judged by rewriting runtime.db's expiry time (spec AC-8: no clock env)."""
    from ulthar import db as db_mod  # noqa: PLC0415

    conn = db_mod.connect(sb.db)
    try:
        conn.execute("UPDATE leases SET expires_at='2000-01-01T00:00:00.000+00:00'")
    finally:
        conn.close()


def unit(sb: Sandbox, unit_id: str) -> dict:
    return json.loads(cli(sb, "unit", "show", unit_id).stdout)


def reset_to(sb: Sandbox, commit: str) -> None:
    git(sb, "reset", "-q", "--hard", commit)
    git(sb, "clean", "-q", "-fd", "--", "src", "notes.txt")
