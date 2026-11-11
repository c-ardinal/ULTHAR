"""Memory and decisions in the hook dispatcher (R1-b), after the v1 guards:

  PreToolUse        store protection for every caller (AC-4, TRUST-02); Agent/Task/
                    SendMessage blocked while L1's must-keep part is over budget; in the
                    main session, write tools and writing shell commands blocked while
                    L0's must-keep part is over budget (AC-10); AskUserQuestion's
                    tool_use_id noted for the confirm token (AC-12).
  PostToolUse       AskUserQuestion answering a decide review -> decision_confirm token.
  UserPromptSubmit  the prompt saved as an owner_msg Episode; extraction launched (AC-6).
  SessionStart      L0 injected (startup/resume/compact, AC-11).
  SubagentStart     L1 injected (AC-9; also fires on SendMessage resumes).
  Stop              one reminder per unconfirmed candidate set (AC-13).
R1-c: PreToolUse also blocks `ulthar unit advance` from a subagent, issues the runner
token of a subagent's `ulthar review record|verify` (ulthar.tokens, answered through
`updatedInput`), and limits product writes to confirmed unit scopes (ulthar.product,
GOAL-08); the AskUserQuestion PostToolUse also issues the unit owner tokens
(CONTRACT-CONFIRM, QUAL-JUDGE, UNIT-RESUME).
With ULTHAR_EXTRACTOR=1 (the extractor's own session) only the store protection, the
runner-token check and the product scope run.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from . import decide
from . import extract
from . import ingest as ingest_mod
from . import memory
from . import shellwords
from . import store as store_mod

WRITE_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
SHELL_TOOLS = frozenset({"Bash", "PowerShell"})
LAUNCH_TOOLS = frozenset({"Agent", "Task", "SendMessage"})
ASK_TOOL = "AskUserQuestion"
L0_SOURCES = frozenset({"startup", "resume", "compact"})
STORE_GUARD = "memory_store"
BUDGET_GUARD = "memory_budget"
DECIDE_GUARD = "memory_decide"
ERROR_GUARD = "memory-error"
REPAIR_PER_PROMPT = 3  # lost registrations re-sent to extraction per owner prompt
REPAIR_AFTER_SEC = 60  # a pending, unclaimed extraction this old was never started

HOOK_ONLY = frozenset({"hook", "extract"})
HOOK_ONLY_LAUNCHERS = ("ulthar", "ulthar.__main__", "__main__.py")
HOOK_ONLY_CONTEXT = re.compile(r"\$\(|`|\$'")  # command substitution / ANSI-C quoting
HOOK_ONLY_STRIP = re.compile(r"\$'|\$\(|['\"`\\(){}]")
DECIDE_READ_ONLY = frozenset({"list", "review", "-h", "--help"})
PROTECTED_IN_CMD = re.compile(
    r"\.claude[\\/]+ulthar(?![\w.\-])|\.claude[\\/]+ulthar\.json"
    r"|(?:^|[\\/\s\"'=:])\.ulthar(?=[\\/\s\"';|&)]|$)", re.I)
PROJECT_CONFIG_TAIL = "/.claude/ulthar.json"  # <repo>/.claude/ulthar.json, edited by the owner only
READ_ONLY_CMDS = frozenset({
    "ls", "dir", "cat", "head", "tail", "grep", "rg", "pwd", "echo", "wc", "which", "where",
    "stat", "file", "less", "more", "type", "tree", "date", "get-childitem", "get-content",
    "select-string", "get-item", "get-location", "write-output", "test-path"})
GIT_READ_ONLY = frozenset({"status", "log", "diff", "show", "rev-parse", "ls-files", "blame",
                           "describe", "shortlog", "grep"})


class Action:
    """What memory adds to one call: a block, an injected context, a warning, and a
    callback run right after the events row is inserted (same transaction)."""

    def __init__(self) -> None:
        self.block: str | None = None
        self.guard_id: str | None = None
        self.context: str | None = None
        self.warning: str | None = None
        self.after_insert = None
        self.updated_input: dict | None = None  # PreToolUse only (the runner token, R1-c AC-4)


class Project:
    """The call's knowledge store, resolved once (a resolution error is kept, not raised)."""

    def __init__(self, payload: dict):
        self.cwd = str(payload.get("cwd") or os.getcwd())
        self._store = None
        self._error: Exception | None = None
        self.prompt_episode: dict | None = None  # written by prompt_writer inside the ingest
        self.prompt_committed = False  # ... and that store transaction committed

    def store(self) -> store_mod.Store:
        if self._store is None and self._error is None:
            try:
                self._store = store_mod.Store.for_cwd(self.cwd)
            except Exception as exc:  # noqa: BLE001 -- re-raised at every use
                self._error = exc
        if self._error is not None:
            raise self._error
        return self._store

    def close(self) -> None:
        """Releases the store's reused index connection; never raises (end of the call)."""
        if self._store is not None:
            try:
                self._store.close()
            except Exception:  # noqa: BLE001 -- closing must not change the call's outcome
                pass


# ---------------------------------------------------------------------------- AC-4

def _forms(path: str, memo: dict | None) -> frozenset[str]:
    from .product import forms  # noqa: PLC0415 -- see run()

    return forms(path, memo)


def _protected_roots(memo: dict | None) -> set[str]:
    roots: set[str] = set()
    for root in (Path.home() / ".claude" / "ulthar", store_mod.state_dir()):
        roots |= _forms(str(root), memo)
    return roots


def _is_protected_path(raw: str, cwd: str, memo: dict | None = None) -> bool:
    p = Path(raw)
    roots = _protected_roots(memo)
    for form in _forms(str(p if p.is_absolute() else Path(cwd) / p), memo):
        if store_mod.STORE_DIRNAME in form.split("/") or form.endswith(PROJECT_CONFIG_TAIL):
            return True
        if any(form == r or form.startswith(r + "/") for r in roots):
            return True
    return False


def _hook_only_broad(command: str) -> str | None:
    """AC-4 a2, the fail-closed layer over shellwords' parsing (spec: 判定は安全側に倒す).
    (1) Any command that has a command substitution ($(...) or a backtick) or ANSI-C
    quoting ($'...') and mentions ulthar anywhere. (2) With quotes, backslashes, $', $(,
    parentheses and braces stripped, a word ending in ulthar / ulthar.__main__ /
    __main__.py followed within 3 words by `hook` or `extract`. Over-blocks on purpose
    (e.g. a commit message built with $(cat ...) inside this repository)."""
    reason = ("BLOCKED (ulthar memory store): this command may start ulthar's hook-only entry "
              "point (`ulthar hook` / `ulthar extract`, AC-4 a2), {why}. Run it without "
              "command substitution or ANSI-C quoting, or keep `ulthar` out of it.")
    if HOOK_ONLY_CONTEXT.search(command) and "ulthar" in command.casefold():
        return reason.format(why="through command substitution or ANSI-C quoting")
    words = HOOK_ONLY_STRIP.sub(" ", command).casefold().split()
    for i, word in enumerate(words):
        if word.endswith(HOOK_ONLY_LAUNCHERS) and HOOK_ONLY & set(words[i + 1:i + 4]):
            return reason.format(why=f"`{' '.join(words[i:i + 4])}`")
    return None


def _shell_command(payload: dict) -> str:
    return str((payload.get("tool_input") or {}).get("command") or "")


def protect(payload: dict, memo: dict | None = None) -> str | None:
    """AC-4: the block message, or None. (a) `ulthar decide` write subcommands (add,
    ingest, ...) from any Claude tool, and `decide confirm` from a subagent -- the main
    session may confirm, as the token it consumes only the hook issues; (c) write tools
    into a store; (d) shell commands naming a store path that are not ulthar CLI calls.
    (b) `ulthar fact add` stays allowed. `memo`: see product.forms."""
    tool = payload.get("tool_name")
    cwd = str(payload.get("cwd") or os.getcwd())
    if tool in WRITE_TOOLS:
        ti = payload.get("tool_input") or {}
        for key in ("file_path", "notebook_path"):
            raw = ti.get(key)
            if isinstance(raw, str) and raw and _is_protected_path(raw, cwd, memo):
                return (f"BLOCKED (ulthar memory store): {tool} into {raw} -- the knowledge store "
                        "(<knowledge_root>/.ulthar/, ~/.claude/ulthar/) is written only by the "
                        "ulthar CLI and hooks (TRUST-02), and the project config "
                        "<repo>/.claude/ulthar.json only by the owner, by hand. Use `ulthar fact "
                        "add` for facts; decisions come from the owner via `ulthar decide review`.")
        return None
    if tool not in SHELL_TOOLS:
        return None
    command = _shell_command(payload)
    broad = _hook_only_broad(command)
    if broad:
        return broad
    in_agent = bool(payload.get("agent_id"))
    for args in shellwords.ulthar_calls(command, tool):
        sub = args[0].casefold() if args else ""
        if sub in HOOK_ONLY:
            return (f"BLOCKED (ulthar memory store): `ulthar {sub}` is an entry point for Claude "
                    "Code's hooks and the extraction worker only (AC-4 a2): a hook event or an "
                    "extraction started from a tool call would be forged evidence.")
        if sub == "unit" and in_agent and len(args) > 1 and args[1].casefold() == "advance":
            return ("BLOCKED (ulthar unit process): `ulthar unit advance` is the window's (main "
                    "session's) command; a subagent never moves a unit (R1-c AC-6). Report to "
                    "the window instead.")
        if sub != "decide":
            continue
        verb = args[1].casefold() if len(args) > 1 else ""
        if not verb or verb in DECIDE_READ_ONLY or (verb == "confirm" and not in_agent):
            continue  # confirm only consumes a hook-issued token: the main session may run it
        who = "a subagent" if verb == "confirm" else "Claude"
        return (f"BLOCKED (ulthar memory store): `ulthar decide {verb}` from {who} -- decisions "
                "come only from the owner's messages (extraction) and are confirmed only "
                "with the token the hook issues when the owner answers the `ulthar decide "
                "review` question; the main session runs `ulthar decide confirm --token`, "
                "a subagent never does. Allowed everywhere: `ulthar decide list|review`.")
    for toks in shellwords.variants(command, tool):
        for seg in shellwords.segments(toks):
            text = " ".join(seg)
            if PROTECTED_IN_CMD.search(text) and not shellwords.leading_ulthar(seg):
                return (f"BLOCKED (ulthar memory store): this command touches the knowledge "
                        f"store or the project config ({text[:120]}) without the ulthar CLI. "
                        "Read and write the store only through `ulthar` (decide list/review, "
                        "fact add/list, inject, index rebuild); the config is the owner's.")
    return None


# ---------------------------------------------------------------------------- AC-10

def _is_read_only_shell(command: str, tool: str | None) -> bool:
    for toks in shellwords.variants(command, tool):
        if shellwords.REDIRECT in toks:
            return False
        for seg in shellwords.segments(toks):
            words = [w for w in seg if not re.match(r"^\w+=", w)] or seg
            exe = shellwords.basename(words[0])
            if exe == "git":
                sub = next((w for w in words[1:] if not w.startswith("-")), "")
                if sub not in GIT_READ_ONLY:
                    return False
            elif not (exe in READ_ONLY_CMDS or shellwords.leading_ulthar(seg)):
                return False
    return True


def _over_notice(packed: memory.Packed, layer: str) -> str:
    return (f"[ULTHAR] {layer} の切り詰められない部分（拘束力のある決定"
            f"{'・オーナーの未整理の発言' if layer == 'L1' else ''}）が {packed.required_chars} 字で、"
            f"上限 {packed.limit} 字を超えています。決定を切り詰めずに"
            + ("担当の送り出し（Agent / SendMessage）を遮断します。" if layer == "L1"
               else "編集・書き込みを遮断します。")
            + "オーナーに決定の整理（supersede による統合、または単位の分割）を求めてください。")


def _pretool(payload: dict, project: Project, rt, act: Action, detail: dict) -> None:
    tool = payload.get("tool_name")
    if tool == ASK_TOOL:
        detail["tool_use_id"] = payload.get("tool_use_id")
    if tool in LAUNCH_TOOLS:
        packed = memory.build_l1(project.store(), rt, _writer(payload, "PreToolUse"))
        if packed.over:
            act.block, act.guard_id = _over_notice(packed, "L1"), BUDGET_GUARD
        return
    if payload.get("agent_id"):
        return
    is_write = tool in WRITE_TOOLS or (
        tool in SHELL_TOOLS and not _is_read_only_shell(_shell_command(payload), tool))
    if is_write:
        packed, _ = memory.build_l0(project.store(), rt, required_only=True)
        if packed.over:
            act.block, act.guard_id = _over_notice(packed, "L0"), BUDGET_GUARD


# ---------------------------------------------------------------------------- events

def _writer(payload: dict, hook: str) -> dict:
    return {"session_id": payload.get("session_id"), "agent_id": payload.get("agent_id"),
            "hook": hook}


def _owner_prompt(payload: dict) -> str | None:
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip() or not ingest_mod.is_owner_text(prompt):
        return None
    return prompt


def _add_prompt(tx, payload: dict) -> dict:
    """The prompt's owner_msg Episode (the existing one when already saved)."""
    prompt = _owner_prompt(payload)
    session_id = str(payload.get("session_id") or "(none)")
    prompt_id = str(payload.get("prompt_id") or "noid")
    key = ingest_mod.prompt_dedupe_key(prompt_id, prompt)
    return tx.find_dedupe("episode", session_id, key) or tx.add(
        "episode", {"session_id": session_id, "kind": "owner_msg", "unit_id": None,
                    "text": prompt, "transcript_ref": None, "prompt_id": prompt_id,
                    "status": memory.NEEDS_EXTRACTION},
        dedupe_key=key)


def prompt_writer(event: str, payload: dict, project: Project):
    """For the transcript ingest of a UserPromptSubmit: writes the prompt's Episode in the
    same store transaction (one lock and commit fewer per prompt). None otherwise."""
    if (event != "UserPromptSubmit" or payload.get("agent_id") or extract.in_extractor()
            or _owner_prompt(payload) is None):
        return None

    def write(tx) -> None:
        project.prompt_episode = _add_prompt(tx, payload)
    return write


def _user_prompt(payload: dict, project: Project, rt, detail: dict) -> None:
    if _owner_prompt(payload) is None:
        return
    store = project.store()
    ep = project.prompt_episode if project.prompt_committed else None
    if ep is None:
        with store.transaction(_writer(payload, "UserPromptSubmit"), rt) as tx:
            ep = _add_prompt(tx, payload)
    detail["owner_msg_episode"] = ep["id"]
    todo = [ep] if not rt.execute("SELECT 1 FROM extractions WHERE episode_id=?",
                                  (ep["id"],)).fetchone() else []
    with store.reader(rt) as conn:  # earlier owner messages whose registration was lost
        todo += [e for e, state in memory.unextracted(rt, conn, store.project_id)
                 if state == "unregistered" and e["id"] != ep["id"]][:REPAIR_PER_PROMPT]
    for e in todo:
        extract.register(rt, store.project_id, e.get("session_id"), e["id"])
        extract.launch(rt, e["id"], project.cwd)
    # ... and those registered but never started (a crash between registering and
    # launching, e.g. a confirm correction): pending, unclaimed, older than the grace
    stale = store_mod.now_iso_ago(REPAIR_AFTER_SEC)
    for (episode_id,) in rt.execute(
            "SELECT episode_id FROM extractions WHERE project_id=? AND status='pending'"
            " AND detail IS NULL AND started < ? AND episode_id != ? LIMIT ?",
            (store.project_id, stale, ep["id"], REPAIR_PER_PROMPT)).fetchall():
        extract.launch(rt, episode_id, project.cwd)


def _session_start(payload: dict, project: Project, rt, act: Action) -> None:
    if str(payload.get("source") or "startup") not in L0_SOURCES:
        return
    store = project.store()
    packed, top = memory.build_l0(store, rt)
    if packed.over:  # never a shortened decision: the notice, and the full text via the CLI
        act.context = _over_notice(packed, "L0") + "（全文は `ulthar decide list`）"
        return
    act.context = packed.text
    memory.mark_l0(rt, store.project_id, top)


def _subagent_start(payload: dict, project: Project, rt, act: Action) -> None:
    packed = memory.build_l1(project.store(), rt, _writer(payload, "SubagentStart"))
    act.context = (_over_notice(packed, "L1") if packed.over else packed.text)


def _post_ask(payload: dict, project: Project, act: Action) -> None:
    from . import tokens  # noqa: PLC0415 -- see run()

    store = project.store()

    def after_insert(rt, event_id: int, detail: dict) -> None:
        token, note = decide.issue_token(rt, payload, store, event_id)
        detail["decide_review"] = note
        if token:
            detail["issued_token"] = token
            act.context = (f"[ULTHAR] 決定の確認の回答を受け取り、確定トークン {token} を発行しました。"
                           f"窓口（このメインセッション）が `ulthar decide confirm --token {token}`"
                           " を実行して回答を適用してください（担当からは実行できません）。")
            return
        issued, unit_note = tokens.issue_owner_tokens(rt, payload, store, event_id)
        detail["unit_question"] = unit_note
        if issued:
            detail["issued_tokens"] = issued
            act.context = (f"[ULTHAR] 単位の質問への回答を受け取り、トークンを {len(issued)} 件発行しました"
                           f"（{unit_note}）。窓口が `ulthar unit advance` で次の状態に進めてください。")
    act.after_insert = after_insert


def _runner(payload: dict, project: Project, act: Action, request: dict) -> None:
    """R1-c AC-4/AC-7: a subagent's `ulthar review record|verify` gets a runner token."""
    if not payload.get("agent_id") or not payload.get("tool_use_id"):
        return  # a main-session call: no token, so the CLI records nothing
    from . import tokens  # noqa: PLC0415 -- see run()

    token_id = os.urandom(16).hex()
    project_id = project.store().project_id
    act.updated_input = tokens.updated_command(payload, token_id)

    def after_insert(rt, event_id: int, detail: dict) -> None:
        tokens.issue_runner(rt, token_id, payload, project_id, request, event_id)
        detail["issued_runner_token"] = token_id
    act.after_insert = after_insert


def run(event: str, payload: dict, rt, project: Project, detail: dict) -> Action:
    """The memory step for one call. Raises on internal failure; ulthar.hook turns that
    into a block for PreToolUse and a warning elsewhere."""
    act = Action()
    request = None
    if event == "PreToolUse":
        # R1-c's unit modules load only for the events that use them (PreToolUse and the
        # AskUserQuestion PostToolUse): hook latency, AC-14. An import failure raises here,
        # inside the memory step, so a PreToolUse is still blocked (fail-closed).
        from . import product  # noqa: PLC0415
        from . import tokens  # noqa: PLC0415

        memo: dict = {}  # path forms, shared by the store guard and the scope check
        msg = protect(payload, memo)
        if msg:
            act.block, act.guard_id = msg, STORE_GUARD
            return act
        msg, request = tokens.runner_request(payload)
        if msg:
            act.block, act.guard_id = msg, tokens.RUNNER_GUARD
            return act
        msg = product.check(payload, project.store, rt, detail, memo)
        if msg:
            act.block, act.guard_id = msg, product.GUARD_ID
            return act
    if extract.in_extractor():
        return act
    main_session = not payload.get("agent_id")
    if event == "PreToolUse":
        if request is not None:
            _runner(payload, project, act, request)
        _pretool(payload, project, rt, act, detail)
    elif event == "PostToolUse" and payload.get("tool_name") == ASK_TOOL:
        _post_ask(payload, project, act)
    elif event == "UserPromptSubmit" and main_session:
        _user_prompt(payload, project, rt, detail)
    elif event == "SessionStart" and main_session:
        _session_start(payload, project, rt, act)
    elif event == "SubagentStart":
        _subagent_start(payload, project, rt, act)
    elif event == "Stop" and main_session:
        reason = decide.stop_reminder(project.store(), rt)
        if reason:
            act.block, act.guard_id = reason, DECIDE_GUARD
    return act


def context_stdout(event: str, context: str) -> str:
    return json.dumps({"hookSpecificOutput": {"hookEventName": event,
                                              "additionalContext": context}},
                      ensure_ascii=False, separators=(",", ":"))
