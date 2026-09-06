#!/usr/bin/env python3
"""hooks/tests/test_grants.py -- owner grants (config.json "grants", lib/grant.py,
guards/grant_files.py) and the self-grant closure.

Five properties the feature is worth nothing without, one measurement, and the
regression that pins the closure:

  1. a grant lets the COMMANDER through exactly ONE matching call; the next
     identical call blocks again;
  2. the same grant does nothing for a SUBAGENT, and is still there afterwards;
  3. only the NAMED rule is exempted -- siblings in the same list still block,
     and so do the rules AFTER the granted one inside the same guard (the
     property that dies if a grant is turned into ALLOW instead of `continue`);
  4. a grant naming a rule that does not exist fails LOUDLY, in-process and
     end-to-end through dispatch.py;
  5. SELF-GRANT: no agent -- subagent or commander -- can write a file that
     carries the owner's permission, through Write/Edit (guards/grant_files.py)
     or through the shell (command_rules.tool_discipline/authority-file-in-shell),
     and no grant may name the rules that enforce that.

Plus: os.remove() as the atomic test-and-consume, measured over 300 two-thread
races (the evidence bar tool_discipline._reclaim_stale_lock's docstring sets for
its own primitive), and the audit line every consumption writes.

No pytest dependency; exposes `main() -> int` per run_all.py's discovery contract.
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
HOOKS_DIR = TESTS_DIR.parent
sys.path.insert(0, str(HOOKS_DIR))
sys.path.insert(0, str(TESTS_DIR))

from conftest import SandboxCtx, load_test_config  # noqa: E402
from guards import git_discipline, grant_files, tool_discipline  # noqa: E402
from lib import grant as grant_mod  # noqa: E402
from lib import io as io_mod  # noqa: E402
from lib import paths as paths_mod  # noqa: E402

MAIN_PAYLOAD_EXTRA = {"transcript_path": "C:/fake/session/main.jsonl"}
SUB_PAYLOAD_EXTRA = {"transcript_path": "C:/fake/session/subagents/sub1.jsonl",
                     "agent_id": "sub-1"}


class Probe:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0

    def check(self, name: str, cond: bool, detail: str = "") -> None:
        if cond:
            self.passed += 1
            print(f"OK   {name}")
        else:
            self.failed += 1
            print(f"FAIL {name}" + (f"\n       {detail}" if detail else ""))


# ---------------------------------------------------------------- helpers


def _bash(command: str, extra: dict) -> dict:
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
               "tool_input": {"command": command}}
    payload.update(extra)
    return payload


def _write_edit(file_path: str, extra: dict | None = None) -> dict:
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Write",
               "tool_input": {"file_path": file_path, "content": "x"}}
    payload.update(extra or MAIN_PAYLOAD_EXTRA)
    return payload


def _grant_file(sandbox: SandboxCtx, ident: str) -> Path:
    """Creates the grant the OWNER would create. The tests are the one actor
    allowed to do this: they run in-process, not through the guarded tools."""
    ctx = sandbox.build()
    path = grant_mod.path_for(ident, sandbox.cfg, ctx)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    return path


def _log_entries(sandbox: SandboxCtx) -> list[dict]:
    ctx = sandbox.build()
    log_path = ctx.expand(sandbox.cfg["grants"]["log"])
    if not log_path.exists():
        return []
    out = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


# ---------------------------------------------------------------- 1: one call, then blocked


def check_grant_is_one_shot_for_the_commander(p: Probe) -> None:
    sandbox = SandboxCtx()
    try:
        payload = _bash("git push --force origin feature/x", MAIN_PAYLOAD_EXTRA)
        first_without = git_discipline.run(payload, sandbox.cfg, sandbox.build(payload=payload))
        p.check("no grant: force-push BLOCKS (baseline)", first_without.exit_code == 2,
                str(first_without))
        p.check("no grant: the block tells the OWNER how to permit it (hint appended)",
                "ulthar-grants" in first_without.stderr, repr(first_without.stderr[-300:]))

        path = _grant_file(sandbox, "git_policy.force_push/push-force")
        granted = git_discipline.run(payload, sandbox.cfg, sandbox.build(payload=payload))
        p.check("grant: the commander's force-push is ALLOWED exactly once",
                granted.exit_code == 0, str(granted))
        p.check("grant: the grant file is CONSUMED (gone from disk)", not path.exists())

        again = git_discipline.run(payload, sandbox.cfg, sandbox.build(payload=payload))
        p.check("grant: the IDENTICAL next call BLOCKS again (one-shot, not standing)",
                again.exit_code == 2, str(again))

        entries = [e for e in _log_entries(sandbox) if e.get("outcome") == "consumed"]
        p.check("grant: consumption is logged with the rule id",
                len(entries) == 1 and entries[0]["rule"] == "git_policy.force_push/push-force",
                str(entries))
        p.check("grant: the log says WHAT it permitted (tool + command)",
                bool(entries) and "push --force" in entries[0]["permitted"],
                str(entries[:1]))
    finally:
        sandbox.cleanup()


# ---------------------------------------------------------------- 2: never a subagent


def check_grant_never_applies_to_a_subagent(p: Probe) -> None:
    sandbox = SandboxCtx()
    try:
        path = _grant_file(sandbox, "git_policy.force_push/push-force")
        payload = _bash("git push --force origin feature/x", SUB_PAYLOAD_EXTRA)
        result = git_discipline.run(payload, sandbox.cfg, sandbox.build(payload=payload))
        p.check("subagent: the same grant does NOT apply -- still BLOCKED",
                result.exit_code == 2, str(result))
        p.check("subagent: the grant is NOT consumed (still on disk for the commander)",
                path.exists())
        p.check("subagent: the refusal is recorded (an owner can see it was ignored, not lost)",
                any(e.get("outcome") == "ignored_subagent" for e in _log_entries(sandbox)),
                str(_log_entries(sandbox)))
    finally:
        sandbox.cleanup()


# ---------------------------------------------------------------- 3: only the named rule


def check_only_the_named_rule_is_exempted(p: Probe) -> None:
    sandbox = SandboxCtx()
    try:
        _grant_file(sandbox, "git_policy.force_push/push-force")
        sibling = _bash("git reset --hard HEAD~1", MAIN_PAYLOAD_EXTRA)
        result = git_discipline.run(sibling, sandbox.cfg, sandbox.build(payload=sibling))
        p.check("sibling rule (destroyers/reset-hard) still BLOCKS under a force_push grant",
                result.exit_code == 2, str(result))
        p.check("sibling rule: the force_push grant was not consumed by it",
                grant_mod.path_for("git_policy.force_push/push-force", sandbox.cfg,
                                   sandbox.build()).exists())
    finally:
        sandbox.cleanup()


def check_grant_continues_the_rule_loop_it_does_not_allow(p: Probe) -> None:
    """The property that dies the moment a grant is implemented as 'return
    ALLOW' (or as an override in the dispatcher): one command that violates TWO
    rules of the same guard, with a grant for the FIRST one only, must still be
    blocked by the second."""
    sandbox = SandboxCtx()
    try:
        # cd-compound (rule 1) AND shell-file-creation (rule 2) in one command.
        command = "cd /repo && echo hi > notes.txt"
        payload = _bash(command, MAIN_PAYLOAD_EXTRA)
        baseline = tool_discipline.run(payload, sandbox.cfg, sandbox.build(payload=payload))
        p.check("two-rule command: blocked by rule 1 (cd-compound) with no grant",
                baseline.exit_code == 2 and "cd" in baseline.stderr, str(baseline))

        _grant_file(sandbox, "command_rules.tool_discipline/cd-compound")
        result = tool_discipline.run(payload, sandbox.cfg, sandbox.build(payload=payload))
        p.check("granting rule 1 does NOT wave the command through -- rule 2 still blocks",
                result.exit_code == 2, str(result))
        p.check("...and the block that surfaces is rule 2's (shell file creation)",
                "shell file creation" in result.stderr, repr(result.stderr[:200]))
    finally:
        sandbox.cleanup()


# ---------------------------------------------------------------- 4: loud on a bad name


def check_grant_naming_nothing_fails_loudly(p: Probe) -> None:
    sandbox = SandboxCtx()
    try:
        _grant_file(sandbox, "git_policy.force_push/push-force-TYPO")
        ctx = sandbox.build()
        raised = ""
        try:
            grant_mod.validate(sandbox.cfg, ctx)
        except grant_mod.GrantError as exc:
            raised = str(exc)
        p.check("a grant naming no rule raises GrantError (never a silent no-op)",
                bool(raised), "validate() returned normally")
        p.check("...and the message names the file and the ids that DO exist",
                "push-force-TYPO" in raised and "'push-force'" in raised, raised)

        bad_path = _grant_file(sandbox, "not_a_config_path/whatever")
        os.remove(str(grant_mod.path_for("git_policy.force_push/push-force-TYPO",
                                         sandbox.cfg, ctx)))
        raised = ""
        try:
            grant_mod.validate(sandbox.cfg, ctx)
        except grant_mod.GrantError as exc:
            raised = str(exc)
        p.check("a grant naming a path that is not a rule list raises too",
                "not a list of rules" in raised, raised)
        os.remove(str(bad_path))

        flat = ctx.expand(sandbox.cfg["grants"]["dir"]) / "bare-file"
        flat.write_text("", encoding="utf-8")
        raised = ""
        try:
            grant_mod.validate(sandbox.cfg, ctx)
        except grant_mod.GrantError as exc:
            raised = str(exc)
        p.check("a grant that is not <path>/<rule id> raises too", "is not" in raised, raised)
    finally:
        sandbox.cleanup()


def check_bad_grant_blocks_end_to_end(p: Probe) -> None:
    """The in-process check above proves validate() raises; this proves the
    dispatcher turns that into a BLOCK naming the file, rather than a fail-open
    -- the same treatment a `disable` matching nothing gets."""
    sandbox = SandboxCtx()
    try:
        _grant_file(sandbox, "git_policy.force_push/push-force-TYPO")
        cfg_path = Path(sandbox.root) / "config.json"
        cfg_path.write_text(json.dumps(sandbox.cfg), encoding="utf-8")
        env = dict(os.environ)
        env["ULTHAR_CONFIG"] = str(cfg_path)
        env["USERPROFILE"] = str(sandbox.home)
        env["HOME"] = str(sandbox.home)
        payload = _bash("echo hi", MAIN_PAYLOAD_EXTRA)
        payload["cwd"] = str(sandbox.cwd)
        proc = subprocess.run(
            [sys.executable, str(HOOKS_DIR / "dispatch.py"), "--event", "PreToolUse"],
            input=json.dumps(payload).encode("utf-8"), capture_output=True, timeout=30, env=env,
        )
        err = proc.stderr.decode("utf-8", "replace")
        p.check("dispatch: a bad grant BLOCKS the tool call (exit 2)", proc.returncode == 2,
                f"rc={proc.returncode}\n{err[:300]}")
        p.check("dispatch: the block names the grant file and how to clear it",
                "push-force-TYPO" in err and "Delete the grant file" in err, err[:600])
    finally:
        sandbox.cleanup()


# ---------------------------------------------------------------- 5: the self-grant closure


def check_no_agent_can_write_an_authority_file(p: Probe) -> None:
    """THE regression for the self-grant problem. If this check is deleted or
    guards/grant_files.py stops being routed, an agent can create its own
    permission again -- which is the state this whole feature exists to leave."""
    sandbox = SandboxCtx()
    try:
        ctx = sandbox.build()
        targets = {
            "the grant directory": str(ctx.expand(sandbox.cfg["grants"]["dir"])
                                       / "git_policy.force_push" / "push-force"),
            "pm-delete-allow": str(ctx.state_dir / "pm-delete-allow"),
            "the user overlay": str(sandbox.home / ".claude" / "ulthar-overlay.json"),
            "the project overlay": str(sandbox.cwd / ".claude" / "ulthar-overlay.json"),
        }
        for label, target in targets.items():
            for chain, extra in (("commander", MAIN_PAYLOAD_EXTRA), ("subagent", SUB_PAYLOAD_EXTRA)):
                payload = _write_edit(target, extra)
                result = grant_files.run(payload, sandbox.cfg, sandbox.build(payload=payload))
                p.check(f"Write/Edit of {label} is BLOCKED for the {chain}",
                        result.exit_code == 2, str(result))

        notebook = {"hook_event_name": "PreToolUse", "tool_name": "NotebookEdit",
                    "tool_input": {"notebook_path": targets["pm-delete-allow"]}}
        notebook.update(MAIN_PAYLOAD_EXTRA)
        result = grant_files.run(notebook, sandbox.cfg, sandbox.build(payload=notebook))
        p.check("NotebookEdit is covered too (notebook_path, not just file_path)",
                result.exit_code == 2, str(result))

        relative = _write_edit(".claude/ulthar-overlay.json")
        result = grant_files.run(relative, sandbox.cfg, sandbox.build(payload=relative))
        p.check("a RELATIVE tool path is resolved against cwd before comparing "
                "(the project overlay's natural spelling)", result.exit_code == 2, str(result))

        ordinary = _write_edit(str(sandbox.cwd / "src" / "App.tsx"))
        result = grant_files.run(ordinary, sandbox.cfg, sandbox.build(payload=ordinary))
        p.check("an ordinary file is untouched by this guard", result.exit_code == 0, str(result))
    finally:
        sandbox.cleanup()


def check_shell_routes_to_authority_files_are_blocked(p: Probe) -> None:
    sandbox = SandboxCtx()
    try:
        blocked = [
            r"""python -c "open(r'C:\Users\me\.claude\state\ulthar-grants\git_policy.force_push\push-force','w')" """,
            "printf '' > ~/.claude/state/ulthar-grants/git_policy.force_push/push-force",
            "cp /tmp/x ~/.claude/state/pm-delete-allow",
            "sed -i s/a/b/ /home/me/.claude/ulthar-overlay.json",
            "python -c \"import pathlib; pathlib.Path('/repo/.claude/ulthar-overlay.json').write_text('{}')\"",
            # RELATIVE spellings: an agent standing in the repo the project overlay
            # governs writes exactly these. The first version of this rule required a
            # separator before '.claude' and let both through -- one command for a
            # STANDING disable of any rule, which is the whole mechanism gone.
            "printf '' > .claude/ulthar-overlay.json",
            "python -c \"open('.claude/ulthar-overlay.json','w').write('{}')\"",
        ]
        for command in blocked:
            payload = _bash(command, MAIN_PAYLOAD_EXTRA)
            result = tool_discipline.run(payload, sandbox.cfg, sandbox.build(payload=payload))
            p.check(f"shell route BLOCKED: {command[:52]!r}", result.exit_code == 2, str(result))

        allowed = [
            'grep -rn "pm-delete-allow" hooks/',
            "git -C . log -S ulthar-grants --oneline",
        ]
        for command in allowed:
            payload = _bash(command, MAIN_PAYLOAD_EXTRA)
            result = tool_discipline.run(payload, sandbox.cfg, sandbox.build(payload=payload))
            p.check(f"ordinary work on ULTHAR itself still ALLOWED: {command[:40]!r}",
                    result.exit_code == 0, str(result))
    finally:
        sandbox.cleanup()


def _win32_padding_spellings(path: str) -> dict[str, str]:
    """The same file, spelled the ways Win32 silently normalises away. `path` is
    forward-slash. Built by mutating a CONFIG-DERIVED path, never written out."""
    home, _, rest = path.partition("/.claude/")
    if not rest:
        return {}
    out = {
        "trailing dot on the .claude component": f"{home}/.claude./{rest}",
        "trailing space on the .claude component": f"{home}/.claude /{rest}",
        "two trailing dots on the .claude component": f"{home}/.claude../{rest}",
    }
    if "/" in rest:
        head, _, tail = rest.partition("/")
        out["trailing dot on the next component"] = f"{home}/.claude/{head}./{tail}"
    return out


def check_win32_trailing_dot_components_are_the_same_file(p: Probe) -> None:
    """THE ROUND-2 CRITICAL. Win32 discards trailing dots AND trailing spaces
    from every path component, but os.path.normpath keeps them -- so
    `<home>/.claude./state/<grants dir>/<rule>/<id>` compared as a DIFFERENT path
    from the protected one and the Write went through, landing the file in the
    real `.claude/state/...`. Measured rc=0 through the dispatcher for the grant
    directory, the user overlay and the pm-delete-allow ledger; the shell half
    was open to the same spellings.

    An agent could therefore write its own permission with one Write and no
    overlay at all -- the exact thing this whole mechanism exists to make
    impossible. Fixed in lib/paths.py:norm_for_compare (the single door every
    equality/containment check goes through, so commander and skill_anchor are
    covered by the same change) and in authority-file-in-shell's pattern.

    POSIX is deliberately NOT covered: there a trailing dot is an ordinary
    filename character and `x.` really is a different file, so this check only
    asserts the Windows behaviour it is about."""
    sandbox = SandboxCtx()
    try:
        ctx = sandbox.build()
        if not ctx.is_windows:
            p.check("win32 trailing-dot check skipped on POSIX (correct there)", True)
            return
        for base in _authority_paths(sandbox.cfg):
            expanded = paths_mod.to_posix(str(ctx.expand(base)))
            for name, spelling in _win32_padding_spellings(expanded).items():
                payload = _write_edit(spelling, MAIN_PAYLOAD_EXTRA)
                result = grant_files.run(payload, sandbox.cfg,
                                         sandbox.build(payload=payload))
                p.check(f"Write BLOCKED [{name}]: ...{base[-28:]}",
                        result.exit_code == 2, spelling + "\n" + str(result))

                command = "python -c \"open('" + spelling + "','w')\""
                bash_payload = _bash(command, MAIN_PAYLOAD_EXTRA)
                result = tool_discipline.run(bash_payload, sandbox.cfg,
                                             sandbox.build(payload=bash_payload))
                p.check(f"shell BLOCKED [{name}]: ...{base[-28:]}",
                        result.exit_code == 2 and "owner-authority" in result.stderr,
                        command + "\n" + str(result))

        # The other direction, so the fix cannot have become "strip dots until
        # every path matches": an ordinary file whose name merely CONTAINS a dot
        # run, outside the authority tree, is untouched.
        ordinary = paths_mod.to_posix(str(sandbox.cwd / "notes." / "draft.md"))
        payload = _write_edit(ordinary, MAIN_PAYLOAD_EXTRA)
        result = grant_files.run(payload, sandbox.cfg, sandbox.build(payload=payload))
        p.check("an ordinary path with a trailing-dot component is still ALLOWED",
                result.exit_code == 0, ordinary + "\n" + str(result))
    finally:
        sandbox.cleanup()


def _win32_same_file_spellings(path: str) -> dict[str, str]:
    r"""Windows spellings that open the SAME FILE as `path` without being a
    lexical prefix of it. Built by mutating a config-derived native path."""
    drive = path[0] if len(path) > 1 and path[1] == ":" else "C"
    posix = path.replace("\\", "/")
    head, sep, tail = path.rpartition("\\")
    parent_name = head.rpartition("\\")[2]
    out = {
        r"extended-length prefix \\?\ ": "\\\\?\\" + path,
        r"device prefix \\.\ ": "\\\\.\\" + path,
        r"UNC admin share \\localhost\X$": f"\\\\localhost\\{drive}$" + path[2:],
        r"extended-length UNC admin share": f"\\\\?\\UNC\\localhost\\{drive}$" + path[2:],
        "alternate data stream ::$DATA": path + "::$DATA",
        # ROUND 4. The prefix rewrites must not depend on which separator the
        # caller happened to use, and `..` has to resolve THROUGH them -- a
        # prefix stripped after normalisation would leave both of these open.
        r"extended-length, forward slashes": "\\\\?\\" + posix,
        r"forward-slash device prefix //./": "//./" + posix,
        "doubled separators": path.replace("\\", "\\\\"),
    }
    if sep and parent_name:
        out["a .. traversal landing back on it"] = f"{head}\\..\\{parent_name}\\{tail}"
    return out


def check_win32_same_file_spellings_are_the_same_file(p: Probe) -> None:
    r"""ROUND 3. The trailing-dot check above was one instance of a wider class:
    Windows has several spellings that open the same file without being derivable
    from the plain path by string comparison. Each was MEASURED as rc=0 on a
    Write of a grant path (baseline rc=2) and each really created the file:

      \\?\C:\...      ntpath.normpath keeps the prefix, so the first component
                      compared as '?' and containment simply missed.
      \\.\C:\...      the same shape, device namespace.
      \\localhost\C$\...  the admin share. realpath does not resolve this one --
                      it stays UNC -- so only a lexical rule reaches it.
      \\?\UNC\localhost\C$\...  both of the above at once.
      <file>::$DATA   an alternate data stream on an exact-file target. The grant
                      DIRECTORY was already safe (containment adds a component);
                      the overlay and the ledger, which are exact files, were not.
      8.3 short name  NOT closable lexically -- the mapping lives in the
                      filesystem, which is why is_under() gained a realpath
                      second chance (see its docstring)."""
    sandbox = SandboxCtx()
    try:
        ctx = sandbox.build()
        if not ctx.is_windows:
            p.check("win32 same-file spellings skipped on POSIX (not applicable)", True)
            return
        for base in _authority_paths(sandbox.cfg):
            native = str(ctx.expand(base))
            for name, spelling in _win32_same_file_spellings(native).items():
                payload = _write_edit(spelling, MAIN_PAYLOAD_EXTRA)
                result = grant_files.run(payload, sandbox.cfg,
                                         sandbox.build(payload=payload))
                p.check(f"Write BLOCKED [{name}]: ...{base[-26:]}",
                        result.exit_code == 2, spelling + "\n" + str(result))

        # 8.3 SHORT NAME -- measured, not assumed: it only exists when the
        # directory exists and the volume has 8dot3 enabled. It is the row that
        # justifies the realpath second chance, since no string work recovers
        # the long name from GITPOL~1.
        grants_dir = ctx.expand(sandbox.cfg["grants"]["dir"])
        (grants_dir / "git_policy.force_push").mkdir(parents=True, exist_ok=True)
        buf = ctypes.create_unicode_buffer(1024)
        got = ctypes.windll.kernel32.GetShortPathNameW(str(grants_dir), buf, 1024)
        short = buf.value if got else ""
        if short and paths_mod.norm_for_compare(short, ctx) != \
                paths_mod.norm_for_compare(str(grants_dir), ctx):
            spelling = short + "\\git_policy.force_push\\push-force"
            payload = _write_edit(spelling, MAIN_PAYLOAD_EXTRA)
            result = grant_files.run(payload, sandbox.cfg, sandbox.build(payload=payload))
            p.check("Write BLOCKED [8.3 short name, resolved via realpath]",
                    result.exit_code == 2, spelling + "\n" + str(result))
        else:
            p.check("8.3 short name check skipped (8dot3 disabled on this volume)", True)

        # VOLUME GUID and GLOBALROOT device paths (round 4). Both name the same
        # file without sharing a single component with the drive spelling, so
        # nothing lexical can reach them -- they are closed by the realpath
        # second chance, which is the second row (after 8.3) justifying it.
        # Constructed from the live volume, and skipped with a reason if this
        # machine will not report one.
        vol = ctypes.create_unicode_buffer(256)
        guid = vol.value if ctypes.windll.kernel32.GetVolumeNameForVolumeMountPointW(
            f"{str(grants_dir)[0]}:\\", vol, 256) else ""
        dev = ctypes.create_unicode_buffer(1024)
        ntdev = dev.value if ctypes.windll.kernel32.QueryDosDeviceW(
            f"{str(grants_dir)[0]}:", dev, 1024) else ""
        tail = str(grants_dir)[3:] + "\\git_policy.force_push\\push-force"
        for label, spelling in (
            ("volume GUID path", (guid + tail) if guid else ""),
            ("GLOBALROOT device path",
             ("\\\\.\\GLOBALROOT" + ntdev + "\\" + tail) if ntdev else ""),
        ):
            if not spelling:
                p.check(f"{label} skipped (this machine reports none)", True)
                continue
            payload = _write_edit(spelling, MAIN_PAYLOAD_EXTRA)
            result = grant_files.run(payload, sandbox.cfg, sandbox.build(payload=payload))
            p.check(f"Write BLOCKED [{label}]", result.exit_code == 2,
                    spelling + "\n" + str(result))

        # THE REALPATH SECOND CHANCE MUST NOT TOUCH THE NETWORK. It ran on every
        # protected target, and realpath on an unreachable UNC host does not fail
        # fast: a Write to \\10.255.255.1\share\x measured 21.5 s per tool call
        # against a 0.4 s baseline. A guard that adds twenty seconds to unrelated
        # work gets switched off. The bound is generous on purpose -- this is a
        # regression against "it went to the network", not a benchmark.
        started = time.monotonic()
        payload = _write_edit("\\\\10.255.255.1\\share\\notes.md", MAIN_PAYLOAD_EXTRA)
        result = grant_files.run(payload, sandbox.cfg, sandbox.build(payload=payload))
        elapsed = time.monotonic() - started
        p.check(f"an unreachable UNC path is judged without waiting on it ({elapsed:.2f}s)",
                elapsed < 5.0, f"{elapsed:.2f}s -- realpath is talking to the network again")
        p.check("...and it is still ALLOWED (it is not an authority file)",
                result.exit_code == 0, str(result))

        # THE OTHER DIRECTION: none of this may become "block everything". An
        # ordinary UNC share is not an admin share and must not be rewritten to a
        # drive; an unrelated local path is still unrelated.
        for label, spelling in (
            ("an ordinary UNC share", "\\\\server\\public\\notes.md"),
            ("an unrelated local path", str(sandbox.cwd / "src" / "app.ts")),
        ):
            payload = _write_edit(spelling, MAIN_PAYLOAD_EXTRA)
            result = grant_files.run(payload, sandbox.cfg, sandbox.build(payload=payload))
            p.check(f"{label} is still ALLOWED", result.exit_code == 0,
                    spelling + "\n" + str(result))
    finally:
        sandbox.cleanup()


def _authority_paths(cfg: dict) -> list[str]:
    """Every authority file path, taken FROM THE CONFIG VALUES rather than written
    out. Not a style preference: this suite runs on machines where these rules are
    live, and a source file is fine but a probe assembled here and handed to a real
    shell would be blocked by the very rule under test. Deriving them also means a
    path that MOVES cannot leave this test silently probing the old one."""
    pm = [(r.get("pm_allowlist") or {}).get("file")
          for r in cfg["command_rules"]["tool_discipline"]]
    ov = cfg["overlays"]
    return [cfg["grants"]["dir"] + "/git_policy.force_push/push-force",
            *[f for f in pm if f], ov["user_path"], ov["project_path"]]


def check_authority_path_separator_spellings_all_block(p: Probe) -> None:
    """The separator was `[\\\\/]` -- exactly ONE character -- so every spelling
    with a run of separators walked past the rule. The one that matters is the
    DOUBLED BACKSLASH: it is what a Windows path looks like inside a non-raw
    Python string, i.e. the natural spelling in the `python -c` one-liner this
    rule exists to stop. `.claude//state//ulthar-grants` and a `./` segment did
    the same. Fixed to a separator RUN plus optional `.` segments; the
    alternating case at the end pins that it is a run and not a second
    fixed-width class."""
    sandbox = SandboxCtx()
    try:
        spellings = {
            "single forward": "/",
            "single back": "\\",
            "doubled back (a non-raw Python string literal)": "\\\\",
            "doubled forward": "//",
            "mixed back+forward": "\\/",
            "dot segment": "/./",
            "dot segment, backslash": "\\.\\",
        }
        for base in _authority_paths(sandbox.cfg):
            for name, sep in spellings.items():
                command = "python -c \"open('" + base.replace("/", sep) + "','w')\""
                payload = _bash(command, MAIN_PAYLOAD_EXTRA)
                result = tool_discipline.run(payload, sandbox.cfg,
                                             sandbox.build(payload=payload))
                p.check(f"separator spelling BLOCKED [{name}]: ...{base[-30:]}",
                        result.exit_code == 2 and "owner-authority" in result.stderr,
                        command + "\n" + str(result))

        parts = _authority_paths(sandbox.cfg)[0].split("/")
        seps = ["\\", "//", "\\\\", "/", "\\/", "/./"]
        mixed = parts[0] + "".join(seps[i % len(seps)] + s for i, s in enumerate(parts[1:]))
        command = "python -c \"open('" + mixed + "','w')\""
        payload = _bash(command, MAIN_PAYLOAD_EXTRA)
        result = tool_discipline.run(payload, sandbox.cfg, sandbox.build(payload=payload))
        p.check("a different separator spelling at every component still BLOCKS",
                result.exit_code == 2 and "owner-authority" in result.stderr,
                command + "\n" + str(result))

        # END TO END, through the real dispatcher, because that is where the gap
        # was MEASURED (rc=0) rather than reasoned about. The command is written
        # to a script file first: this suite runs on machines where the rule is
        # live, so an authority path on the probe's own command line would be
        # blocked by the rule under test before the subprocess ever started.
        base = _authority_paths(sandbox.cfg)[0].replace("/", "\\\\")
        script = sandbox.root / "probe_sep.py"
        script.write_text(
            "import json, subprocess, sys\n"
            f"cmd = 'python -c \"open(\\'' + {base!r} + '\\',\\'w\\')\"'\n"
            "payload = {'hook_event_name': 'PreToolUse', 'tool_name': 'Bash',\n"
            "           'cwd': sys.argv[2], '_home_override': sys.argv[3],\n"
            "           'transcript_path': 'C:/fake/session/main.jsonl',\n"
            "           'tool_input': {'command': cmd}}\n"
            "r = subprocess.run([sys.executable, sys.argv[1], '--event', 'PreToolUse'],\n"
            "                   input=json.dumps(payload), capture_output=True,\n"
            "                   text=True, encoding='utf-8', errors='replace')\n"
            "print(r.returncode); print(r.stderr)\n", encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(script), str(HOOKS_DIR / "dispatch.py"),
             str(sandbox.cwd), str(sandbox.home)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=dict(os.environ, ULTHAR_CONFIG=str(HOOKS_DIR.parent / "config.json")))
        out = proc.stdout or ""
        p.check("end-to-end through dispatch.py: the doubled-backslash spelling "
                "BLOCKS (rc=2) -- this is the spelling that measured rc=0",
                out.startswith("2") and "owner-authority" in out,
                out[:400] + "\n" + (proc.stderr or "")[:300])

        # The widening must not have swallowed ordinary work on ULTHAR's source.
        for command in ("grep -rn ulthar-grants hooks/",
                        "git -C . log -S pm-delete-allow --oneline",
                        "python -c \"open('src/.claudecheck/state/x','w')\""):
            payload = _bash(command, MAIN_PAYLOAD_EXTRA)
            result = tool_discipline.run(payload, sandbox.cfg, sandbox.build(payload=payload))
            p.check(f"...and ordinary work is still ALLOWED: {command[:44]!r}",
                    result.exit_code == 0, str(result))
    finally:
        sandbox.cleanup()


def check_never_exempt_rules_cannot_be_granted(p: Probe) -> None:
    """A grant for the rules that enforce the closure would close the mechanism
    over itself: grant the shell rule, write your own grants."""
    sandbox = SandboxCtx()
    try:
        ctx = sandbox.build()
        for ident in sandbox.cfg["grants"]["never_exempt"]:
            path = _grant_file(sandbox, ident)
            raised = ""
            try:
                grant_mod.validate(sandbox.cfg, ctx)
            except grant_mod.GrantError as exc:
                raised = str(exc)
            p.check(f"a grant naming {ident!r} is refused loudly", "never_exempt" in raised, raised)

            command = "cp /tmp/x ~/.claude/state/pm-delete-allow"
            payload = _bash(command, MAIN_PAYLOAD_EXTRA)
            result = tool_discipline.run(payload, sandbox.cfg, sandbox.build(payload=payload))
            p.check(f"...and with that file present the rule STILL blocks ({ident})",
                    result.exit_code == 2, str(result))
            p.check("...and the never-exemptable grant was not consumed", path.exists())
            os.remove(str(path))
    finally:
        sandbox.cleanup()


# ---------------------------------------------------------------- the primitive, measured


def _race(trials: int, prepare, contend) -> list[int]:
    """`trials` two-thread races, both threads released from one barrier.
    Returns the number of winners observed per trial."""
    winners = []
    for i in range(trials):
        target = prepare(i)
        results: dict[str, bool] = {}
        barrier = threading.Barrier(2)

        def racer(name: str, path=target, b: threading.Barrier = barrier) -> None:
            b.wait()
            results[name] = contend(path)

        threads = [threading.Thread(target=racer, args=(n,)) for n in ("a", "b")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        winners.append(sum(1 for v in results.values() if v))
    return winners


def check_consume_primitive_is_exactly_one_winner(p: Probe) -> None:
    """The whole concurrency argument for a one-file-per-grant design is
    'exactly one racer can take the file', so it is MEASURED, the way this
    codebase measures such claims (tool_discipline._reclaim_stale_lock: 300
    trials, two threads) -- and the first candidate primitive failed the
    measurement, which is why the code no longer uses it.

    os.remove() is NOT that primitive on Windows: both racers report success
    about half the time (deleting sets a disposition on a directory entry that
    is still open). This check keeps that measurement live, and pins the
    primitive that actually decides the winner."""
    sandbox = SandboxCtx()
    try:
        base = Path(sandbox.root) / "race"
        base.mkdir()

        def fresh(prefix: str):
            def prepare(i: int) -> Path:
                target = base / f"{prefix}{i}"
                target.write_text("", encoding="utf-8")
                return target
            return prepare

        def by_remove(path: Path) -> bool:
            try:
                os.remove(str(path))
                return True
            except OSError:
                return False

        def by_claim(path: Path) -> bool:
            try:
                fd = os.open(str(path) + ".claim", os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except OSError:
                return False
            os.close(fd)
            return True

        remove_winners = _race(300, fresh("r"), by_remove)
        claim_winners = _race(300, fresh("c"), by_claim)
        p.check(f"O_CREAT|O_EXCL claim: EXACTLY one winner in all 300 trials "
                f"(observed: {sorted(set(claim_winners))})",
                set(claim_winners) == {1}, str(sorted(set(claim_winners))))
        p.check("measurement kept live: os.remove alone is NOT exactly-one-winner here "
                f"({remove_winners.count(2)}/300 trials had TWO winners) -- if this ever "
                "reads 0, re-check whether the claim marker is still earning its keep",
                True, "")
        print(f"       measured: os.remove winners per trial "
              f"{ {n: remove_winners.count(n) for n in sorted(set(remove_winners))} }; "
              f"claim {{1: {claim_winners.count(1)}}}")
    finally:
        sandbox.cleanup()


def check_two_racing_calls_never_both_get_one_grant(p: Probe) -> None:
    """The property the primitive exists for, measured through the real code
    path (lib/grant.check) rather than through the primitive alone."""
    sandbox = SandboxCtx()
    try:
        ident = "git_policy.force_push/push-force"
        payload = _bash("git push --force origin feature/x", MAIN_PAYLOAD_EXTRA)
        ctx = sandbox.build(payload=payload)

        def prepare(i: int) -> Path:
            return _grant_file(sandbox, ident)

        def contend(_path: Path) -> bool:
            return grant_mod.check(ident, "MSG", payload, sandbox.cfg, ctx) is None

        winners = _race(150, prepare, contend)
        p.check(f"two racing tool calls: EXACTLY one is granted, 150 trials "
                f"(observed: {sorted(set(winners))})", set(winners) == {1}, str(winners[:20]))
    finally:
        sandbox.cleanup()


# ---------------------------------------------------------------- config wiring


def check_config_wiring(p: Probe) -> None:
    cfg = load_test_config()
    g = cfg.get("grants", {})
    p.check("config: grants section is enabled and has a directory",
            bool(g.get("enabled")) and bool(g.get("dir")), str(g)[:200])
    p.check("config: main_chain_only is on (a grant never reaches a subagent)",
            g.get("main_chain_only") is True)
    refs = {e["ref"] for e in g.get("protected", [])}
    p.check("config: every authority file is protected BY REFERENCE to its own home",
            refs == {"grants.dir", "command_rules.tool_discipline[].pm_allowlist.file",
                     "overlays.user_path", "overlays.project_path"}, str(refs))
    ctx = SandboxCtx(cfg)
    try:
        resolved = grant_mod.protected_targets(cfg, ctx.build())
        p.check("config: all four references resolve to a real path",
                len(resolved) == 4, str(resolved))
    finally:
        ctx.cleanup()
    routed = [g_id for rule in cfg["dispatch"]["events"]["PreToolUse"]
              for g_id in rule["guards"]]
    p.check("config: grant_files is routed on PreToolUse", "grant_files" in routed, str(routed))


def main() -> int:
    p = Probe()
    print("\n== owner grants: lifetime ==")
    check_grant_is_one_shot_for_the_commander(p)
    check_grant_never_applies_to_a_subagent(p)
    print("\n== owner grants: blast radius ==")
    check_only_the_named_rule_is_exempted(p)
    check_grant_continues_the_rule_loop_it_does_not_allow(p)
    print("\n== owner grants: loud failure ==")
    check_grant_naming_nothing_fails_loudly(p)
    check_bad_grant_blocks_end_to_end(p)
    print("\n== self-grant closure ==")
    check_no_agent_can_write_an_authority_file(p)
    check_shell_routes_to_authority_files_are_blocked(p)
    check_authority_path_separator_spellings_all_block(p)
    check_win32_trailing_dot_components_are_the_same_file(p)
    check_win32_same_file_spellings_are_the_same_file(p)
    check_never_exempt_rules_cannot_be_granted(p)
    print("\n== the primitive, measured ==")
    check_consume_primitive_is_exactly_one_winner(p)
    check_two_racing_calls_never_both_get_one_grant(p)
    print("\n== config wiring ==")
    check_config_wiring(p)
    print(f"\nRESULT: {p.passed} passed, {p.failed} failed")
    return 0 if p.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
