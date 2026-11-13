"""Shared steps of the R1-c tests (not a test module): a git-backed sandbox project,
contracts, subagent calls through the hook's runner token, owner answers through
AskUserQuestion, and a unit driven through the process.

Every owner answer goes the production way: the CLI prints the question, the session
transcript gets the AskUserQuestion tool_use line, the hook sees PreToolUse and
PostToolUse, then the transcript gets the tool_result line Claude Code writes."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

from helpers import Sandbox, write_jsonl

SESSION = "r1c"
PY_DIR = str(Path(sys.executable).parent)
VALUE_SCRIPT = "import pathlib, sys\nprint(pathlib.Path(sys.argv[1]).read_text().strip())\n"


def new_sandbox(non_product_globs: list[str] | None = None) -> Sandbox:
    """A sandbox whose work/ is a git repository with one commit and `checks/value.py`
    (prints the content of the file it is given), AC-11 active."""
    sb = Sandbox()
    sb.configure(["none"], non_product_globs=non_product_globs or [])
    (sb.work / "checks").mkdir()
    (sb.work / "checks" / "value.py").write_text(VALUE_SCRIPT, encoding="utf-8")
    (sb.work / "src").mkdir()
    (sb.work / "src" / "app.txt").write_text("v0\n", encoding="utf-8")
    for args in (["init", "-q"], ["config", "user.email", "t@example.invalid"],
                 ["config", "user.name", "t"], ["config", "core.autocrlf", "false"],
                 ["add", "-A"], ["commit", "-q", "-m", "init"]):
        subprocess.run(["git", "-C", str(sb.work), *args], check=True, capture_output=True)
    set_value(sb, "AC-1", "1")
    return sb


def set_value(sb: Sandbox, ac: str, text: str) -> None:
    (sb.work / "checks" / f"{ac}.txt").write_text(text + "\n", encoding="utf-8")


def cli(sb: Sandbox, *args: str) -> subprocess.CompletedProcess:
    """The CLI with this interpreter's directory first on PATH (`python` in verify_cmd)."""
    return sb.cli(*args, PATH=PY_DIR + os.pathsep + os.environ.get("PATH", ""))


def quant_ac(ac_id: str = "AC-1", hi: float = 3, **over) -> dict:
    return {"id": ac_id, "statement": f"{ac_id} の測定値は {hi} 以下", "kind": "quant",
            "verify_cmd": f"python checks/value.py checks/{ac_id}.txt",
            "threshold": {"lo": None, "hi": hi, "hi_incl": True},
            "evidence_kind": "stdout_number", "sources": ["owner:test"], **over}


def qual_ac(ac_id: str = "AC-Q") -> dict:
    return {"id": ac_id, "statement": "画面の見た目がオーナーの選んだ案と同じ", "kind": "qual",
            "verify_cmd": "python checks/value.py checks/AC-1.txt", "threshold": None,
            "evidence_kind": "screenshot", "sources": ["owner:test"],
            "rubric": {"levels": ["合格", "不合格"], "good": ["案と同じ配置"],
                       "bad": ["ボタンの位置が違う"]}}


def contract(acs: list[dict] | None = None, **over) -> dict:
    doc = {"intent": {"raw": "ボタンを減らしたい", "interpretation": "A",
                      "alternatives": [{"id": "A", "text": "画面のボタンを 3 個以下にする"},
                                       {"id": "B", "text": "ボタンを 1 つのメニューにまとめる"}]},
           "non_goals": ["配色は変えない"], "scope_globs": ["src/**"],
           "acs": acs or [quant_ac()], "budget": {"loops": 5}}
    doc.update(over)
    return doc


def write_contract(sb: Sandbox, doc: dict | str, name: str = "contract.yaml") -> Path:
    path = sb.root / name
    text = doc if isinstance(doc, str) else yaml.safe_dump(doc, allow_unicode=True, sort_keys=False)
    path.write_text(text, encoding="utf-8")
    return path


def make_unit(sb: Sandbox) -> str:
    issue = cli(sb, "issue", "add", "--title", "ボタンを減らす").stdout.split("\t")[0]
    out = cli(sb, "unit", "new", "--issue", issue)
    return out.stdout.split("\t")[0]


def unit(sb: Sandbox, unit_id: str) -> dict:
    return json.loads(cli(sb, "unit", "show", unit_id).stdout)


_seq = [0]


def tool_use_id(tag: str) -> str:
    _seq[0] += 1
    return f"tu-{tag}-{_seq[0]}-{os.urandom(3).hex()}"


def runner(sb: Sandbox, agent: str | None, args: list[str], hand_token: str | None = None):
    """A subagent (`agent`; None = the main session) runs `ulthar <args>` through Bash:
    PreToolUse first, then -- as Claude Code would -- the possibly updated command.
    Returns (pre-hook result, cli result or None when the hook blocked)."""
    command = "ulthar " + " ".join(json.dumps(a, ensure_ascii=False) if " " in a else a
                                   for a in args)
    if hand_token:
        command += f" --runner-token {hand_token}"
    payload = {"session_id": SESSION, "tool_name": "Bash", "tool_use_id": tool_use_id("run"),
               "tool_input": {"command": command}}
    if agent:
        payload.update(agent_id=agent, agent_type="ulthar-reviewer")
    pre = sb.hook("PreToolUse", payload)
    if pre.returncode == 2:
        return pre, None
    token = hand_token
    if pre.stdout.strip():
        updated = json.loads(pre.stdout)["hookSpecificOutput"].get("updatedInput") or {}
        token = updated.get("command", "").rsplit(" ", 1)[-1] if updated else token
    extra = ["--runner-token", token] if token else []
    return pre, cli(sb, *args, *extra)


def runner_token(sb: Sandbox, agent: str, args: list[str]) -> str:
    """Only the PreToolUse step of `runner`: the token the hook appended."""
    pre = sb.hook("PreToolUse", {"session_id": SESSION, "agent_id": agent,
                                 "agent_type": "ulthar-creator", "tool_name": "Bash",
                                 "tool_use_id": tool_use_id("run"),
                                 "tool_input": {"command": "ulthar " + " ".join(args)}})
    return json.loads(pre.stdout)["hookSpecificOutput"]["updatedInput"]["command"].rsplit(" ", 1)[-1]


def cli_background(sb: Sandbox, *args: str) -> subprocess.Popen:
    from helpers import MAIN_PY  # noqa: PLC0415

    return subprocess.Popen([sys.executable, str(MAIN_PY), *args], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, cwd=str(sb.work),
                            env=sb.env(PATH=PY_DIR + os.pathsep + os.environ.get("PATH", "")))


def transcript(sb: Sandbox) -> Path:
    return sb.root / f"{SESSION}-session.jsonl"


def answer(sb: Sandbox, question_input: dict, answers: list[str], tag: str,
           transcript_answers: list[str] | None = None, in_transcript: bool = True):
    """The owner answers `question_input` (one answer per question). Returns the
    PostToolUse event's detail."""
    from test_decide import tool_use_line  # noqa: PLC0415

    tu = tool_use_id(tag)
    path = transcript(sb)
    if in_transcript:
        write_jsonl(path, [tool_use_line(tu, question_input)], mode="a")
    elif not path.exists():
        path.write_text("", encoding="utf-8")
    qs = [q["question"] for q in question_input["questions"]]
    base = {"session_id": SESSION, "tool_name": "AskUserQuestion", "tool_input": question_input,
            "tool_use_id": tu, "transcript_path": str(path)}
    sb.hook("PreToolUse", base)
    sb.hook("PostToolUse", dict(base, tool_response={
        "questions": question_input["questions"], "answers": dict(zip(qs, answers))}))
    if in_transcript:
        said = transcript_answers or answers
        pairs = ", ".join(f'"{q}"="{a}"' for q, a in zip(qs, said))
        write_jsonl(path, [{"type": "user", "uuid": f"r-{tu}", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tu,
             "content": f"Your questions have been answered: {pairs}. You can now continue "
                        "with these answers in mind."}]}}], mode="a")
    row = sb.query("SELECT detail FROM events WHERE hook='PostToolUse' ORDER BY id DESC LIMIT 1")
    return json.loads(row[0][0]) if row else {}


def confirm_contract(sb: Sandbox, unit_id: str, reviewer: str = "rev-1",
                     answer_text: str = "確定") -> dict:
    """review (pass) + the owner's CONTRACT-CONFIRM answer; returns the answer's detail."""
    runner(sb, reviewer, ["review", "record", "--unit", unit_id, "--kind", "contract",
                          "--result", "pass"])
    question = json.loads(cli(sb, "contract", "show", unit_id).stdout)
    return answer(sb, question, [answer_text], "confirm")


def to_build(sb: Sandbox, doc: dict | None = None) -> str:
    """A unit whose contract `doc` is confirmed, in build."""
    unit_id = make_unit(sb)
    cli(sb, "contract", "submit", unit_id, "--file", str(write_contract(sb, doc or contract())))
    confirm_contract(sb, unit_id)
    for to in ("contract_confirmed", "build"):
        res = cli(sb, "unit", "advance", unit_id, to)
        assert res.returncode == 0, res.stderr
    return unit_id


def to_quant_verified(sb: Sandbox, doc: dict | None = None, builder: str = "bld-1") -> str:
    unit_id = to_build(sb, doc)
    edit(sb, builder, "src/app.txt")
    (sb.work / "src" / "app.txt").write_text("v1\n", encoding="utf-8")
    runner(sb, builder, ["verify", unit_id, "--self"])
    res = cli(sb, "unit", "advance", unit_id, "quant_verified")
    assert res.returncode == 0, res.stderr
    return unit_id


def reviewer_pass(sb: Sandbox, unit_id: str, reviewer: str = "rev-1", *review_extra: str):
    runner(sb, reviewer, ["verify", unit_id])
    runner(sb, reviewer, ["review", "record", "--unit", unit_id, "--kind", "verify",
                          "--result", "pass", *review_extra])


def to_owner_review(sb: Sandbox, doc: dict | None = None, builder: str = "bld-1") -> str:
    unit_id = to_quant_verified(sb, doc, builder)
    reviewer_pass(sb, unit_id)
    res = cli(sb, "unit", "advance", unit_id, "owner_review")
    assert res.returncode == 0, res.stderr
    return unit_id


def edit(sb: Sandbox, agent: str | None, rel: str, tool: str = "Edit"):
    payload = {"session_id": SESSION, "tool_name": tool, "tool_use_id": tool_use_id("ed"),
               "tool_input": {"file_path": str(sb.work / rel), "old_string": "a",
                              "new_string": "b", "content": "x"}}
    if agent:
        payload.update(agent_id=agent, agent_type="ulthar-creator")
    return sb.hook("PreToolUse", payload)


def units_lines(sb: Sandbox) -> int:
    return len(sb.jsonl("units.jsonl"))
