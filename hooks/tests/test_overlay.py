#!/usr/bin/env python3
"""hooks/tests/test_overlay.py -- unit tests for lib/ctx.py:merge_overlays (the
user/project overlay merge, the config-generalisation round).

Covers: overlay absent, user overlay present, project overlay present, both
present, and a malformed overlay (must fail loudly, never silently degrade to
"no rules").

Since 2026-09-06 an overlay may also DISABLE a named rule explicitly -- project
configuration takes precedence over personal configuration, and a repo switching
a guard's rule off is stating its own policy. The tests at the bottom of this
file are therefore not about preventing that; they are about the two things that
must hold once it is allowed: the blast radius of a disable is exactly one rule
(not one guard), and nothing is ever switched off silently -- not by a typo, not
by a structurally broken entry crashing a guard into the dispatcher's fail-open,
and not without the operator being told. Also an
end-to-end check that the shipped examples/user-overlay.example.json, applied
via merge_overlays, restores the exact pre-round1 default behaviour for
dev_server and git_discipline.

No pytest dependency; exposes main() -> int per run_all.py's discovery contract.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
HOOKS_DIR = TESTS_DIR.parent
SKILL_ROOT = HOOKS_DIR.parent
sys.path.insert(0, str(HOOKS_DIR))
sys.path.insert(0, str(TESTS_DIR))

from conftest import SandboxCtx, load_test_config  # noqa: E402
from lib import ctx as ctx_mod  # noqa: E402
from guards import dev_server, git_discipline  # noqa: E402
from lib.ctx import CmdResult  # noqa: E402


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


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _dotted(cfg: dict, path: str):
    node = cfg
    for part in path.split("."):
        node = node[part]
    return node


def _sandbox_with_cfg() -> SandboxCtx:
    cfg = copy.deepcopy(load_test_config())
    return SandboxCtx(cfg)


def check_absent_overlay_is_noop(p: Probe) -> None:
    sandbox = _sandbox_with_cfg()
    try:
        ctx = sandbox.build()
        merged = ctx_mod.merge_overlays(sandbox.cfg, ctx)
        p.check("absent overlay: returns the SAME cfg object (no copy on the hot path)",
                merged is sandbox.cfg)
    finally:
        sandbox.cleanup()


def check_user_overlay_appends(p: Probe) -> None:
    sandbox = _sandbox_with_cfg()
    try:
        ctx = sandbox.build()
        user_path = sandbox.home / ".claude" / "ulthar-overlay.json"
        _write_json(user_path, {"git_policy": {"deploy_refs": ["my-preview"]}})
        merged = ctx_mod.merge_overlays(sandbox.cfg, ctx)
        p.check("user overlay: deploy_refs appended",
                merged["git_policy"]["deploy_refs"] == ["my-preview"],
                str(merged["git_policy"]["deploy_refs"]))
        p.check("user overlay: base cfg untouched (no in-place mutation)",
                sandbox.cfg["git_policy"]["deploy_refs"] == [])
    finally:
        sandbox.cleanup()


def check_project_overlay_appends_allowed_path(p: Probe) -> None:
    sandbox = _sandbox_with_cfg()
    try:
        ctx = sandbox.build()
        project_path = sandbox.cwd / ".claude" / "ulthar-overlay.json"
        _write_json(project_path, {"git_policy": {"deploy_refs": ["repo-preview"]}})
        merged = ctx_mod.merge_overlays(sandbox.cfg, ctx)
        p.check("project overlay: deploy_refs appended (allowed path)",
                merged["git_policy"]["deploy_refs"] == ["repo-preview"],
                str(merged["git_policy"]["deploy_refs"]))
    finally:
        sandbox.cleanup()


def check_both_layers_union(p: Probe) -> None:
    sandbox = _sandbox_with_cfg()
    try:
        ctx = sandbox.build()
        _write_json(sandbox.home / ".claude" / "ulthar-overlay.json",
                    {"git_policy": {"deploy_refs": ["user-preview"]}})
        _write_json(sandbox.cwd / ".claude" / "ulthar-overlay.json",
                    {"git_policy": {"deploy_refs": ["repo-preview"]}})
        merged = ctx_mod.merge_overlays(sandbox.cfg, ctx)
        p.check("both layers: union of both, base first",
                merged["git_policy"]["deploy_refs"] == ["user-preview", "repo-preview"],
                str(merged["git_policy"]["deploy_refs"]))
    finally:
        sandbox.cleanup()


def check_project_overlay_cannot_touch_disallowed_path(p: Probe) -> None:
    """The policy/wiring line: a project overlay touching a path NOT in
    overlays.project_allowed_paths must not succeed -- it must raise, not silently
    drop just that key while applying the rest. logging.fail_open_on_guard_error
    is not a rule about this repo's development; it is ULTHAR's own wiring."""
    sandbox = _sandbox_with_cfg()
    try:
        ctx = sandbox.build()
        project_path = sandbox.cwd / ".claude" / "ulthar-overlay.json"
        _write_json(project_path, {"logging": {"fail_open_on_guard_error": False}})
        try:
            ctx_mod.merge_overlays(sandbox.cfg, ctx)
            p.check("project overlay touching logging.*: raises ConfigError", False,
                    "merge_overlays returned instead of raising")
        except ctx_mod.ConfigError as exc:
            p.check("project overlay touching logging.*: raises ConfigError", True, str(exc))
    finally:
        sandbox.cleanup()


def check_project_overlay_cannot_shadow_existing_id(p: Probe) -> None:
    """An id identifies a rule, so re-adding an existing one is ambiguous, not
    forbidden: append_by_id refuses it and the message points at the explicit
    `disable` operation instead. (Before 2026-09-06 this refusal WAS the
    non-weakening boundary; it now exists so that "switch this rule off" and "add
    this rule" cannot be confused for one another.)"""
    sandbox = _sandbox_with_cfg()
    try:
        ctx = sandbox.build()
        project_path = sandbox.cwd / ".claude" / "ulthar-overlay.json"
        _write_json(project_path, {
            "command_rules": {
                "tool_discipline": [
                    {"id": "delete-outside-disposable", "enabled": False}
                ]
            }
        })
        try:
            ctx_mod.merge_overlays(sandbox.cfg, ctx)
            p.check("project overlay reusing an existing rule id: raises ConfigError", False,
                    "merge_overlays returned instead of raising -- WEAKENING SUCCEEDED")
        except ctx_mod.ConfigError as exc:
            p.check("project overlay reusing an existing rule id: raises ConfigError", True, str(exc))
    finally:
        sandbox.cleanup()


def check_project_overlay_can_add_new_id(p: Probe) -> None:
    sandbox = _sandbox_with_cfg()
    try:
        ctx = sandbox.build()
        project_path = sandbox.cwd / ".claude" / "ulthar-overlay.json"
        _write_json(project_path, {
            "command_rules": {
                "tool_discipline": [
                    {"id": "my-repo-extra-rule", "enabled": True, "type": "regex_block",
                     "target": "raw", "pattern": "(?i)never-run-this", "message": "blocked"}
                ]
            }
        })
        merged = ctx_mod.merge_overlays(sandbox.cfg, ctx)
        ids = [r["id"] for r in merged["command_rules"]["tool_discipline"]]
        p.check("project overlay: new rule id appended alongside existing ones",
                "my-repo-extra-rule" in ids and "delete-outside-disposable" in ids, str(ids))
    finally:
        sandbox.cleanup()


def check_malformed_json_fails_loudly(p: Probe) -> None:
    sandbox = _sandbox_with_cfg()
    try:
        ctx = sandbox.build()
        user_path = sandbox.home / ".claude" / "ulthar-overlay.json"
        user_path.parent.mkdir(parents=True, exist_ok=True)
        user_path.write_text("{not valid json", encoding="utf-8")
        try:
            ctx_mod.merge_overlays(sandbox.cfg, ctx)
            p.check("malformed overlay JSON: raises ConfigError (loud, not silent)", False)
        except ctx_mod.ConfigError as exc:
            p.check("malformed overlay JSON: raises ConfigError (loud, not silent)", True, str(exc))
    finally:
        sandbox.cleanup()


def check_non_object_overlay_fails_loudly(p: Probe) -> None:
    sandbox = _sandbox_with_cfg()
    try:
        ctx = sandbox.build()
        user_path = sandbox.home / ".claude" / "ulthar-overlay.json"
        _write_json(user_path, ["not", "an", "object"])
        try:
            ctx_mod.merge_overlays(sandbox.cfg, ctx)
            p.check("non-object overlay JSON: raises ConfigError (loud, not silent)", False)
        except ctx_mod.ConfigError as exc:
            p.check("non-object overlay JSON: raises ConfigError (loud, not silent)", True, str(exc))
    finally:
        sandbox.cleanup()


def check_disabled_overlays_section_is_noop(p: Probe) -> None:
    cfg = copy.deepcopy(load_test_config())
    cfg["overlays"]["enabled"] = False
    sandbox = SandboxCtx(cfg)
    try:
        ctx = sandbox.build()
        user_path = sandbox.home / ".claude" / "ulthar-overlay.json"
        _write_json(user_path, {"git_policy": {"deploy_refs": ["should-never-apply"]}})
        merged = ctx_mod.merge_overlays(sandbox.cfg, ctx)
        p.check("overlays.enabled=false: overlay file present but ignored",
                merged is sandbox.cfg and merged["git_policy"]["deploy_refs"] == [])
    finally:
        sandbox.cleanup()


def check_example_user_overlay_restores_old_defaults(p: Probe) -> None:
    """End-to-end: examples/user-overlay.example.json, applied as the USER layer,
    must restore the exact pre-round1 shipped behaviour for both guards it moved
    rules out of."""
    example_path = SKILL_ROOT / "examples" / "user-overlay.example.json"
    example = json.loads(example_path.read_text(encoding="utf-8"))

    sandbox = _sandbox_with_cfg()
    try:
        ctx = sandbox.build()
        _write_json(sandbox.home / ".claude" / "ulthar-overlay.json", example)
        merged = ctx_mod.merge_overlays(sandbox.cfg, ctx)

        payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                   "tool_input": {"command": "npm run dev:heavy"}}
        result = dev_server.run(payload, merged, sandbox.build(payload=payload))
        p.check("example user overlay: dev_server blocks 'npm run dev:heavy' again",
                result.exit_code == 2, str(result))

        sandbox.set_run_cmd(lambda argv, kind, cwd=None: CmdResult(0, "feature/x\n", "", False))
        git_payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                       "tool_input": {"command": "git push origin preview-branch-a"},
                       "cwd": str(sandbox.cwd)}
        git_ctx = sandbox.build(payload=git_payload)
        git_result = git_discipline.run(git_payload, merged, git_ctx)
        p.check("example user overlay: git_discipline blocks push to preview-branch-a again",
                git_result.exit_code == 2, str(git_result))
    finally:
        sandbox.cleanup()


# --------------------------------------------------------------------------
# Regression: append-only + fail-open = disable-by-crash
#
# These go through the REAL dispatcher (dispatch.py), not through merge_overlays
# + guard.run, because the weakening they reproduce happened in dispatch.py's
# fail-open, which a direct guard.run never reaches. Each case is run twice,
# with and without the malformed project overlay, and the pair (2, 2) is the
# assertion; a pre-fix run gives (2, 0). Note where the (2, 2) now comes from:
# these entries no longer reach a guard at all -- lib/ctx.py refuses them at
# merge time (missing keys, wrong-typed scalar), so the block is a config-load
# block. Running them end-to-end is still the point: it is the only way to prove
# the fail-open is not reached.
# --------------------------------------------------------------------------

import os  # noqa: E402
import subprocess  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402

def _vanishing_file_stub(bind: str = "") -> str:
    """A guard whose FIRST call raises because a file disappeared and whose
    SECOND call succeeds -- what an approval/marker file being consumed looks
    like. `bind` optionally binds the MERGED rule list to a local first, the
    shape that makes attribution-by-container-membership blame an innocent
    overlay (see dispatch._overlay_object_in_frames)."""
    return (
        "import os\n"
        "from pathlib import Path\n"
        "from lib.io import ALLOW\n"
        "def run(payload, cfg, ctx):\n"
        f"{bind}"
        "    marker = Path(os.environ['ULTHAR_TEST_MARKER'])\n"
        "    if marker.exists():\n"
        "        marker.unlink()\n"
        "        raise FileNotFoundError('approval marker vanished mid-run')\n"
        "    return ALLOW\n"
    )


def _dispatch_rc(overlay, command, event="PreToolUse", tool="Bash",
                 guards_dir=None, guard=None, extra_env=None,
                 tool_input=None, with_stdout=False, user_overlay=None):
    """Runs the REAL dispatcher in a subprocess against a throwaway home + cwd.
    The live ~/.claude/ulthar-overlay.json is never read (payload
    `_home_override`) and never written -- a malformed overlay is only ever
    materialised under a temp directory, which is removed again on the way out
    (this used to leak one ulthar_overlay_test_* tree per call)."""
    with tempfile.TemporaryDirectory(prefix="ulthar_overlay_test_",
                                     ignore_cleanup_errors=True) as tmpdir:
        tmp = Path(tmpdir)
        home = tmp / "home"
        (home / ".claude").mkdir(parents=True)
        cwd = tmp / "repo"
        (cwd / ".claude").mkdir(parents=True)
        if overlay is not None:
            _write_json(cwd / ".claude" / "ulthar-overlay.json", overlay)
        if user_overlay is not None:
            # Under the throwaway home, so the operator's real one is untouched.
            # Needed by the pinned-path checks: those paths are outside
            # project_allowed_paths, so a PROJECT overlay is refused one fence
            # earlier and would prove the pin without exercising it.
            _write_json(home / ".claude" / "ulthar-overlay.json", user_overlay)
        payload = {"hook_event_name": event, "tool_name": tool, "cwd": str(cwd),
                   "_home_override": str(home),
                   "tool_input": tool_input if tool_input is not None else {"command": command}}
        env = dict(os.environ, ULTHAR_CONFIG=str(SKILL_ROOT / "config.json"))
        if guards_dir is not None:
            # Only safe together with --guard: without it every OTHER guard in
            # the matched rule is looked up in the override dir too and dies
            # with GuardLoadError before the stub ever runs.
            env["ULTHAR_GUARDS_DIR"] = str(guards_dir)
        env.update(extra_env or {})
        argv = [sys.executable, str(HOOKS_DIR / "dispatch.py"), "--event", event]
        if guard is not None:
            argv += ["--guard", guard]
        # encoding pinned: dispatch.py writes UTF-8 (io.configure_streams), and
        # decoding that under the parent's locale codepage raises inside
        # subprocess' reader thread, silently yielding an EMPTY stderr -- which
        # would make every "the message names X" assertion below pass vacuously.
        proc = subprocess.run(argv, input=json.dumps(payload), capture_output=True,
                              text=True, encoding="utf-8", errors="replace", env=env)
        if with_stdout:
            return proc.returncode, (proc.stderr or ""), (proc.stdout or "")
        return proc.returncode, (proc.stderr or "")


def check_malformed_project_rule_cannot_disable_a_guard(p: Probe) -> None:
    """The reproduction from the third-party finding, and its sibling.

    destroyers: an appended `{"id": ...}` with no `suffix` makes _check_list
    raise KeyError before the force_push phase. blanket_staging: the same trick
    on an EARLIER phase, so `git reset --hard` never reaches the destroyers
    phase. Different list, different command, different phase -- the fix must
    not be specific to either."""
    cases = [
        ("destroyers", "git push --force origin feature",
         {"git_policy": {"destroyers": [{"id": "project-added-incomplete"}]}}),
        ("blanket_staging", "git reset --hard HEAD~1",
         {"git_policy": {"blanket_staging": [{"id": "project-crash"}]}}),
    ]
    for name, command, overlay in cases:
        base_rc, _ = _dispatch_rc(None, command)
        p.check(f"{name}: base config blocks {command!r}", base_rc == 2, f"rc={base_rc}")
        rc, err = _dispatch_rc(overlay, command)
        p.check(f"{name}: malformed project rule does NOT weaken the guard",
                rc == 2, f"rc={rc} -- guard disabled by crash\n{err[:400]}")
        p.check(f"{name}: the block names the project overlay file",
                "ulthar-overlay.json" in err, err[:400])


def check_plain_list_rejects_non_scalar(p: Probe) -> None:
    """Codex's 'plain-list paths accept non-scalars despite the schema claiming
    plain scalars'. A dict in dev_processes.names reaches procs.py as
    `.casefold()` on a dict -- a Stop-guard fail-open. It must never merge."""
    rc, err = _dispatch_rc({"dev_processes": {"names": [{"not": "a scalar"}]}}, "git status")
    p.check("plain-list non-scalar: refused at merge time (BLOCK, not fail-open)",
            rc == 2 and "plain scalars" in err, f"rc={rc}\n{err[:400]}")
    p.check("plain-list non-scalar: the error names the overlay file",
            "ulthar-overlay.json" in err, err[:400])


def check_overlay_error_always_names_its_file(p: Probe) -> None:
    """An unhashable `id` used to escape as a bare TypeError with no path, while
    the recovery text told the reader to delete 'the overlay file named in the
    detail line above'. With BOTH overlays present the message must say which."""
    with tempfile.TemporaryDirectory(prefix="ulthar_overlay_msg_",
                                     ignore_cleanup_errors=True) as tmpdir:
        tmp = Path(tmpdir)
        home = tmp / "home"
        (home / ".claude").mkdir(parents=True)
        cwd = tmp / "repo"
        (cwd / ".claude").mkdir(parents=True)
        _write_json(home / ".claude" / "ulthar-overlay.json",
                    {"git_policy": {"destroyers": [{"id": ["not", "a", "string"], "enabled": True,
                                                    "suffix": "x", "why": "y"}]}})
        _write_json(cwd / ".claude" / "ulthar-overlay.json",
                    {"git_policy": {"deploy_refs": ["repo-preview"]}})
        payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": str(cwd),
                   "_home_override": str(home), "tool_input": {"command": "git status"}}
        proc = subprocess.run(
            [sys.executable, str(HOOKS_DIR / "dispatch.py"), "--event", "PreToolUse"],
            input=json.dumps(payload), capture_output=True, text=True,
            env=dict(os.environ, ULTHAR_CONFIG=str(SKILL_ROOT / "config.json")))
        err = proc.stderr or ""
        p.check("unhashable id: blocks with the OFFENDING (user) overlay path in the detail",
                proc.returncode == 2 and str(home) in err and "TypeError" not in err,
                f"rc={proc.returncode}\n{err[:400]}")


def check_ulthar_own_bug_still_fails_open(p: Probe) -> None:
    """The other half of the contract: fail-open must survive for its real
    purpose. A guard that crashes with NO OVERLAY MERGED is ULTHAR's own bug and
    must still ALLOW -- a guard bug must not brick every tool call.

    ROUND 4 REWROTE THE CONDITION and this test with it. It used to read: crash
    while an overlay object is bound on a frame -> block; otherwise -> fail open,
    overlay present or not. The frame evidence was measured to miss the case that
    mattered (a guard raising in its SETUP, before it touched any rule, on an
    undeclared list the user layer had replaced), and the miss was rc=0 with
    empty stderr. The condition is now simply "was any overlay merged into this
    cfg": with one, a crash blocks and names the file; with none, it fails open
    exactly as before. So the two stubs below no longer differ in whether they
    touch overlay content -- they differ in whether an overlay is present at all,
    which is the whole of the new rule.

    Both stubs are still CALLED for real rather than raising a hand-made
    exception, so the traceback is genuine; the frame check survives only to
    enrich the message, and the third assertion pins that it still does."""
    sys.path.insert(0, str(HOOKS_DIR))
    import dispatch as dispatch_mod  # noqa: E402

    sandbox = _sandbox_with_cfg()
    try:
        ctx = sandbox.build()
        ctx.project_overlay_path = sandbox.cwd / ".claude" / "ulthar-overlay.json"
        _write_json(ctx.project_overlay_path, {"git_policy": {"destroyers": [
            {"id": "project-added", "enabled": True, "suffix": "x", "why": "y"}]}})
        merged = ctx_mod.merge_overlays(sandbox.cfg, ctx)
        ctx.cfg = merged
        p.check("merge records which objects the project overlay contributed",
                bool(ctx.project_overlay_ids))

        class AlwaysBroken:
            @staticmethod
            def run(payload, cfg, ctx):
                raise RuntimeError("ULTHAR's own bug")

        class BrokenByOverlay:
            @staticmethod
            def run(payload, cfg, ctx):
                entry = cfg["git_policy"]["destroyers"][-1]
                raise KeyError(f"suffix missing on {entry['id']}")

        # NO overlay in the cfg -> fail open, which is what fail-open is for.
        no_overlay_ctx = sandbox.build()
        no_overlay_ctx.cfg = sandbox.cfg
        p.check("no overlay merged: overlay_sources is empty (the test is not vacuous)",
                not no_overlay_ctx.overlay_sources)
        try:
            AlwaysBroken.run({}, sandbox.cfg, no_overlay_ctx)
        except Exception as exc:  # noqa: BLE001
            p.check("ULTHAR's own guard bug with NO overlay: still fails open",
                    dispatch_mod._attribute_to_project_overlay(
                        "stub", no_overlay_ctx, exc, "PreToolUse") is None)

        # An overlay IS merged -> the same bug blocks, whether or not the crash
        # had anything to do with the overlay. That is the round-4 change.
        p.check("merge recorded the overlay file(s) it applied", bool(ctx.overlay_sources),
                str(ctx.overlay_sources))
        blocked = None
        try:
            AlwaysBroken.run({}, merged, ctx)
        except Exception as exc:  # noqa: BLE001
            blocked = dispatch_mod._attribute_to_project_overlay(
                "stub", ctx, exc, "PreToolUse")
        p.check("the SAME bug with an overlay merged: BLOCKS, names the file",
                blocked is not None and blocked.exit_code == 2
                and "ulthar-overlay.json" in blocked.stderr, str(blocked))

        attributed = None
        try:
            BrokenByOverlay.run({}, merged, ctx)
        except Exception as exc:  # noqa: BLE001
            attributed = dispatch_mod._attribute_to_project_overlay(
                "stub", ctx, exc, "PreToolUse")
        p.check("crash while holding an overlay entry: BLOCKS, names the file",
                attributed is not None and attributed.exit_code == 2
                and "ulthar-overlay.json" in attributed.stderr,
                str(attributed))
        p.check("...and says so, so the frame evidence still earns its keep",
                attributed is not None and "holding content" in attributed.stderr,
                str(attributed))
    finally:
        sandbox.cleanup()


def check_malformed_design_case_cannot_disable_brief_gate(p: Probe) -> None:
    """Regression for the third scenario the review reproduced by hand: a project
    overlay appending a design case with `triggers` but no `message`. brief_gate
    reads `case["message"]` only after the triggers fire, so the crash takes out
    the guard that carries the approval handshake -- i.e. a repo could switch the
    handshake off just by shipping this file. Must BLOCK and name the overlay."""
    trigger = "zqx-project-design-case-trigger"
    filler = ("Additional narrative context follows to reach the minimum length "
              "threshold for brief detection purposes only and carries no other "
              "meaning here today. ") * 2
    prompt = ("[Creator Brief] round1. This dispatch cites STATE.md section 2 acceptance "
              "table rows 3 and 4 as the target. OWNER-CONFIRMED: the owner approved this "
              f"scope explicitly in chat today. {trigger}. " + filler)
    tool_input = {"prompt": prompt}
    overlay = {"design_cases": {"cases": [
        {"id": "project-case-no-message", "triggers": [trigger]}]}}

    # Control: with no overlay this brief reaches the approval handshake and is
    # blocked BY IT -- that is the enforcement the malformed case removes.
    base_rc, base_err = _dispatch_rc(None, "", tool="Agent", tool_input=tool_input)
    p.check("design_cases: with no overlay, brief_gate enforces the approval handshake",
            base_rc == 2 and "brief gate" in base_err, f"rc={base_rc}\n{base_err[:400]}")
    rc, err = _dispatch_rc(overlay, "", tool="Agent", tool_input=tool_input)
    p.check("design_cases: a message-less project case does NOT disable brief_gate",
            rc == 2, f"rc={rc} -- guard disabled by crash, handshake gone\n{err[:400]}")
    p.check("design_cases: the block names the project overlay file",
            "ulthar-overlay.json" in err and "brief gate" not in err, err[:400])


def check_invalid_regex_from_overlay_blames_the_overlay(p: Probe) -> None:
    """A project overlay supplying a syntactically valid but UNCOMPILABLE
    `destroyers[].suffix`. Both branches of the PatternCompileError path block,
    so the exit code alone proves nothing: the assertion is on WHICH file the
    message blames. Blaming config.json here sends the reader to redeploy the
    master, which cannot fix a file that is neither under hooks/ nor in git."""
    overlay = {"git_policy": {"destroyers": [
        {"id": "project-uncompilable", "enabled": True, "suffix": "*(", "why": "z"}]}}
    rc, err = _dispatch_rc(overlay, "git status")
    p.check("invalid overlay regex: BLOCKS", rc == 2, f"rc={rc}\n{err[:400]}")
    p.check("invalid overlay regex: blames the OVERLAY, not config.json",
            "ulthar-overlay.json" in err and "invalid pattern in config.json" not in err,
            err[:600])
    p.check("invalid overlay regex: recovery is delete/rename, not redeploy",
            "Delete or rename the overlay file" in err and "Fix the master under" not in err,
            err[:600])


def check_live_state_change_is_not_blamed_on_the_overlay(p: Probe) -> None:
    """The third-party finding this round exists for. A guard whose FIRST call
    raises because a file disappeared, and whose second call would succeed --
    exactly what an approval or marker file being consumed or removed looks
    like. The project overlay present here is valid and unrelated.

    Under the old differential re-run this was a misattributed BLOCK naming an
    innocent overlay: a lockout caused by ordinary live-state change.

    ROUND 4 CHANGED THE ANSWER AND THIS TEST WITH IT, and the change is a
    deliberate cost rather than a regression. With an overlay merged, a guard
    crash now BLOCKS whatever caused it, because "the guard crashed" and "an
    overlay made the guard crash" are not separable at crash time and only one
    of them is safe to call ALLOW. So this case -- an innocent overlay plus an
    ordinary live-state change -- is a block, and what the test pins is that the
    block is the RECOVERABLE kind: it names the overlay file and carries the
    delete-the-overlay recovery, the same lockout and the same escape a
    malformed overlay has always had.

    WHAT MUST NOT COME BACK is the mechanism, not the verdict: the block must
    still come from ONE execution. The differential re-run that produced the
    original lockout is still forbidden, for the reason in
    dispatch._attribute_to_project_overlay -- a second observation of a world
    that ULTHAR itself mutates (the pm-delete-allow ledger is consumed on grant)
    cannot be evidence about the first.

    Run twice, because the second variant broke an earlier draft: a guard that
    binds the MERGED rule list to a local before dying (`rules = cfg[...]`,
    tool_discipline's shape) holds no overlay OBJECT -- the crash is about a
    file, not a rule. Both must now block, and neither may claim the overlay was
    holding the bag."""
    variants = [
        ("", "vanishing file"),
        ("    rules = cfg['command_rules']['tool_discipline']\n",
         "vanishing file (merged rule list bound to a local)"),
    ]
    for bind, label in variants:
        with tempfile.TemporaryDirectory(prefix="ulthar_overlay_live_",
                                         ignore_cleanup_errors=True) as tmpdir:
            tmp = Path(tmpdir)
            guards_dir = tmp / "guards"
            guards_dir.mkdir()
            marker = tmp / "marker.txt"
            marker.write_text("x", encoding="utf-8")
            # Stands in for git_discipline so --guard routes to it. It lives in
            # a temp dir, never under hooks/guards/, so the guard AST contract
            # is untouched.
            (guards_dir / "git_discipline.py").write_text(
                _vanishing_file_stub(bind), encoding="utf-8")
            overlay = {"command_rules": {"tool_discipline": [
                {"id": "project-valid-extra", "enabled": True, "type": "regex_block",
                 "target": "raw", "pattern": "never-matches-anything", "message": "x"}]}}
            rc, err = _dispatch_rc(overlay, "git status", guards_dir=guards_dir,
                                   guard="git_discipline",
                                   extra_env={"ULTHAR_TEST_MARKER": str(marker)})
            p.check(f"{label}: BLOCKS rather than silently allowing (round 4)",
                    rc == 2, f"rc={rc}\n{err[:600]}")
            p.check(f"{label}: the block names the overlay file, so it is recoverable",
                    "ulthar-overlay.json" in err, err[:600])
            p.check(f"{label}: it does NOT claim the overlay was holding the bag",
                    "holding content" not in err, err[:600])


def check_scalar_type_conformance(p: Probe) -> None:
    """Scalars cannot be traced back to their overlay at guard time (interning),
    so their type is enforced at merge time instead: `5` in dev_processes.names
    is a legal JSON scalar that becomes `5..casefold()` -- an AttributeError
    inside a Stop guard, i.e. a fail-open. Must be refused before it merges."""
    rc, err = _dispatch_rc({"dev_processes": {"names": [5]}}, "git status")
    p.check("wrong-typed scalar: refused at merge time (BLOCK, not fail-open)",
            rc == 2 and "holds str entries" in err, f"rc={rc}\n{err[:400]}")
    rc, err = _dispatch_rc({"dev_processes": {"names": ["vite"]}}, "git status")
    p.check("right-typed scalar: still merges cleanly", rc == 0, f"rc={rc}\n{err[:400]}")


def check_scalar_at_empty_declared_list_cannot_disable_a_guard(p: Probe) -> None:
    """The reviewer's exact reproduction, run through THE CONSUMING GUARD.

    `git_policy.deploy_refs` ships empty, and an empty base list used to be
    treated as "nothing to conform to, accept anything". So `deploy_refs: [5]`
    merged, git_discipline called re.escape(5), raised TypeError, and the
    fail-open turned a blocked `git push origin main` into an ALLOW: base config
    exit 2, with the overlay exit 0. The previous regression here asserted that
    acceptance as correct behaviour and never ran the guard, which is why the
    hole survived it -- it is deleted, not weakened.

    The base list still ships empty; what changed is that overlays.list_merge
    now DECLARES its element type, so the merge refuses 5 outright."""
    command = "git push origin main"
    base_rc, _ = _dispatch_rc(None, command)
    p.check(f"deploy_refs: base config blocks {command!r}", base_rc == 2, f"rc={base_rc}")

    rc, err = _dispatch_rc({"git_policy": {"deploy_refs": [5]}}, command)
    p.check("deploy_refs: an int among the refs does NOT disable git_discipline",
            rc == 2, f"rc={rc} -- guard disabled by crash\n{err[:400]}")
    p.check("deploy_refs: refused as a type violation, naming the overlay file",
            "holds str entries" in err and "ulthar-overlay.json" in err, err[:500])

    rc, err = _dispatch_rc({"git_policy": {"deploy_refs": ["repo-preview"]}}, command)
    p.check("deploy_refs: a str of the declared element type still merges",
            rc == 2 and "ulthar-overlay.json" not in err, f"rc={rc}\n{err[:400]}")


def check_empty_list_without_a_declared_shape_refuses_appends(p: Probe) -> None:
    """The other half of the empty-base decision. A declared shape is what makes
    an empty list appendable; a list that ships empty and declares NOTHING has
    no schema at all, so it refuses appends rather than accepting unvalidated
    content. Simulated on dev_processes.ports (emptied in this cfg copy, and
    declaring only a bare 'append' strategy) because every empty list ULTHAR
    actually ships does declare a shape."""
    cfg = copy.deepcopy(load_test_config())
    cfg["dev_processes"]["ports"] = []
    sandbox = SandboxCtx(cfg)
    try:
        ctx = sandbox.build()
        _write_json(sandbox.cwd / ".claude" / "ulthar-overlay.json",
                    {"dev_processes": {"ports": [4173]}})
        try:
            ctx_mod.merge_overlays(sandbox.cfg, ctx)
            p.check("empty list with no declared shape: refuses the append", False,
                    "merge_overlays returned instead of raising -- UNVALIDATED CONTENT MERGED")
        except ctx_mod.ConfigError as exc:
            msg = str(exc)
            p.check("empty list with no declared shape: refuses the append", True, msg)
            p.check("...naming the path, the overlay file and what to declare",
                    "dev_processes.ports" in msg and "ulthar-overlay.json" in msg
                    and "overlays.list_merge" in msg, msg)
    finally:
        sandbox.cleanup()


def check_empty_append_by_id_without_required_keys_refuses(p: Probe) -> None:
    """The object half of the empty-base decision, and the one that decides what
    happens to command_rules.dev_server -- the empty list config.json documents
    as THE user extension point. It stays appendable because overlays.list_merge
    declares its `required_keys`; strip that declaration back to a bare
    'append_by_id' and the same entry is refused, because there is then nothing
    to validate it against. Refusing is the default; declaring is what buys an
    empty list its extension point back."""
    cfg = copy.deepcopy(load_test_config())
    cfg["overlays"]["list_merge"]["command_rules.dev_server"] = "append_by_id"
    sandbox = SandboxCtx(cfg)
    try:
        ctx = sandbox.build()
        _write_json(sandbox.cwd / ".claude" / "ulthar-overlay.json",
                    {"command_rules": {"dev_server": [
                        {"id": "project-rule", "enabled": True, "type": "regex_require",
                         "target": "raw", "pattern": "x", "requires": "y", "message": "z"}]}})
        try:
            ctx_mod.merge_overlays(sandbox.cfg, ctx)
            p.check("empty append_by_id list with no required_keys: refuses the append", False,
                    "merge_overlays returned instead of raising -- UNVALIDATED CONTENT MERGED")
        except ctx_mod.ConfigError as exc:
            msg = str(exc)
            p.check("empty append_by_id list with no required_keys: refuses the append", True, msg)
            p.check("...naming the path, the overlay file and what to declare",
                    "command_rules.dev_server" in msg and "ulthar-overlay.json" in msg
                    and "required_keys" in msg, msg)
    finally:
        sandbox.cleanup()


def check_example_project_overlay_merges(p: Probe) -> None:
    """The shipped project example must survive its own validation -- it touches
    both declared-shape paths (git_policy.deploy_refs, command_rules.dev_server)
    and one derived-shape path (dev_processes.ports)."""
    example = json.loads(
        (SKILL_ROOT / "examples" / "project-overlay.example.json").read_text(encoding="utf-8"))
    sandbox = _sandbox_with_cfg()
    try:
        ctx = sandbox.build()
        _write_json(sandbox.cwd / ".claude" / "ulthar-overlay.json", example)
        merged = ctx_mod.merge_overlays(sandbox.cfg, ctx)
        p.check("examples/project-overlay.example.json merges as the PROJECT layer",
                merged["git_policy"]["deploy_refs"] == ["this-repos-staging-preview"]
                and [r["id"] for r in merged["command_rules"]["dev_server"]]
                == ["this-repo-e2e-requires-flag"]
                and 4173 in merged["dev_processes"]["ports"]
                and "clean" not in [d["id"] for d in merged["git_policy"]["destroyers"]]
                and "reset-hard" in [d["id"] for d in merged["git_policy"]["destroyers"]],
                str(merged["git_policy"]["deploy_refs"])
                + str([d["id"] for d in merged["git_policy"]["destroyers"]]))
    finally:
        sandbox.cleanup()


def check_append_by_id_entry_must_match_the_base_shape(p: Probe) -> None:
    """The object half of the same rule, at the merge level: the keys EVERY
    existing entry carries are required, and their types are required to match.
    Derived from config.json's own entries, so it cannot rot as config.json
    grows -- the objection that kept validation off the table before."""
    sandbox = _sandbox_with_cfg()
    try:
        ctx = sandbox.build()
        overlay_path = sandbox.cwd / ".claude" / "ulthar-overlay.json"
        _write_json(overlay_path, {"design_cases": {"cases": [
            {"id": "project-shape-ok", "enabled": True, "triggers": ["zzz"],
             "message": "m", "applies_to": ["Agent"], "satisfied_by": ["x"]}]}})
        merged = ctx_mod.merge_overlays(sandbox.cfg, ctx)
        ids = [c["id"] for c in merged["design_cases"]["cases"]]
        p.check("append_by_id: an entry with the base shape is accepted",
                "project-shape-ok" in ids, str(ids))

        _write_json(overlay_path, {"design_cases": {"cases": [
            {"id": "project-no-message", "enabled": True, "triggers": ["zzz"],
             "applies_to": ["Agent"], "satisfied_by": ["x"]}]}})
        try:
            ctx_mod.merge_overlays(sandbox.cfg, ctx)
            p.check("append_by_id: a missing required key is refused AT MERGE TIME", False,
                    "merge_overlays returned instead of raising -- the entry reached a guard")
        except ctx_mod.ConfigError as exc:
            p.check("append_by_id: a missing required key is refused AT MERGE TIME", True, str(exc))
            p.check("...naming the missing key and the overlay file",
                    "'message'" in str(exc) and "ulthar-overlay.json" in str(exc), str(exc))

        _write_json(overlay_path, {"design_cases": {"cases": [
            {"id": "project-wrong-type", "enabled": True, "triggers": "zzz",
             "message": "m", "applies_to": ["Agent"], "satisfied_by": ["x"]}]}})
        try:
            ctx_mod.merge_overlays(sandbox.cfg, ctx)
            p.check("append_by_id: a wrong-typed key is refused", False,
                    "merge_overlays returned instead of raising")
        except ctx_mod.ConfigError as exc:
            p.check("append_by_id: a wrong-typed key is refused", True, str(exc))
            p.check("...naming the entry, the key and the overlay file",
                    "'triggers'" in str(exc) and "project-wrong-type" in str(exc)
                    and "ulthar-overlay.json" in str(exc), str(exc))
    finally:
        sandbox.cleanup()


# --------------------------------------------------------------------------
# Explicit disabling (owner decision, 2026-09-06): a project overlay MAY switch
# a named rule off. The tests below are about the two things that must hold once
# it can: the blast radius is exactly one rule, and nothing is switched off
# silently or by accident.
# --------------------------------------------------------------------------


def check_disable_stops_one_rule_and_only_that_rule(p: Probe) -> None:
    """THE POINT of the whole operation. `git clean` stops being blocked; its
    SIBLING in the same list (reset --hard), another list in the same guard
    (blanket staging), and another guard entirely (tool_discipline) all keep
    enforcing. What this replaces is a malformed entry crashing git_discipline,
    which removed every one of those at once, silently."""
    overlay = {"disable": ["git_policy.destroyers/clean"]}
    disabled_cmd = "git clean -fd"
    base_rc, _ = _dispatch_rc(None, disabled_cmd)
    p.check(f"disable: base config blocks {disabled_cmd!r}", base_rc == 2, f"rc={base_rc}")
    rc, err = _dispatch_rc(overlay, disabled_cmd)
    p.check("disable: the named rule stops blocking", rc == 0, f"rc={rc}\n{err[:400]}")

    survivors = [
        ("sibling entry in the SAME list", "git reset --hard HEAD~1"),
        ("another list in the same guard", "git add -A"),
        ("another guard entirely (tool_discipline)", "cd /tmp && ls"),
    ]
    for label, command in survivors:
        rc, err = _dispatch_rc(overlay, command)
        p.check(f"disable: {label} still blocks {command!r}", rc == 2,
                f"rc={rc} -- blast radius exceeded one rule\n{err[:400]}")


def check_disable_of_a_nonexistent_rule_fails_loudly(p: Probe) -> None:
    """A typo that silently disabled nothing would be the same invisible failure
    this operation replaces -- so it BLOCKS, naming the file, the identifier and
    the ids that are actually there."""
    rc, err = _dispatch_rc({"disable": ["git_policy.destroyers/clea"]}, "git status")
    p.check("disable: an id matching no rule BLOCKS", rc == 2, f"rc={rc}\n{err[:400]}")
    p.check("disable: the error names the identifier, the real ids and the overlay file",
            "matches no rule" in err and "'clean'" in err and "ulthar-overlay.json" in err,
            err[:600])

    rc, err = _dispatch_rc({"disable": ["destroyers-clean"]}, "git status")
    p.check("disable: a malformed identifier BLOCKS", rc == 2 and "<config path>/<rule id>" in err,
            f"rc={rc}\n{err[:400]}")

    rc, err = _dispatch_rc({"disable": "git_policy.destroyers/clean"}, "git status")
    p.check("disable: a non-list 'disable' BLOCKS",
            rc == 2 and "must be a list" in err, f"rc={rc}\n{err[:400]}")


def check_project_disable_cannot_reach_ulthars_own_wiring(p: Probe) -> None:
    """The one carve-out, and it is about the tool's integrity rather than about
    the repo's policy: deploy_guard protects the deployed copy of every guard, so
    a repo able to switch it off could rewrite the mechanism -- including this
    restriction. Same refusal for any other non-rule path."""
    for ident in ("deploy_guard/enabled", "paths/python_exe", "dispatch.stdout_emitters/x"):
        rc, err = _dispatch_rc({"disable": [ident]}, "git status")
        p.check(f"disable: a project overlay naming {ident!r} is refused",
                rc == 2 and "project_allowed_paths" in err, f"rc={rc}\n{err[:400]}")


def check_never_exempt_rules_cannot_be_disabled_by_an_overlay(p: Probe) -> None:
    """`grants.never_exempt` was enforced against grant FILES and not against an
    overlay `disable` -- the same power with a longer lifetime, and
    `command_rules.tool_discipline` is overlay-writable, so a cloned repo could
    switch off the rule that stops it writing grants. Both layers, because
    materialising a user overlay (`cp x ~/.claude/`) is no harder than a project
    one: the shell rule matches a command that NAMES the file, and neither
    spelling does."""
    sandbox = _sandbox_with_cfg()
    never = list(sandbox.cfg["grants"]["never_exempt"])
    p.check("grants.never_exempt is not empty (this test would be vacuous)", bool(never))
    try:
        ctx = sandbox.build()
        for layer, path in (("project", sandbox.cwd / ".claude" / "ulthar-overlay.json"),
                            ("user", sandbox.home / ".claude" / "ulthar-overlay.json")):
            for ident in never:
                _write_json(path, {"disable": [ident]})
                raised = ""
                try:
                    ctx_mod.merge_overlays(sandbox.cfg, ctx)
                except ctx_mod.ConfigError as exc:
                    raised = str(exc)
                p.check(f"{layer} overlay disabling {ident!r} is refused loudly",
                        "never_exempt" in raised and ident in raised, raised or "no error")
                path.unlink()

        # ...and the owner's standing decision is untouched: an ORDINARY rule in
        # the same list still disables from the same file.
        project = sandbox.cwd / ".claude" / "ulthar-overlay.json"
        _write_json(project, {"disable": ["command_rules.tool_discipline/cd-compound"]})
        merged = ctx_mod.merge_overlays(sandbox.cfg, ctx)
        p.check("an ORDINARY rule in the same list still disables",
                "cd-compound" not in [r["id"] for r in
                                      merged["command_rules"]["tool_discipline"]],
                str([r["id"] for r in merged["command_rules"]["tool_discipline"]]))
        project.unlink()

        # THE SECOND HALF: the LIST is pinned too. It is not in
        # overlays.list_merge, so it merges by replacement, and the user layer has
        # no path restriction -- `{"grants": {"never_exempt": []}}` would hand back
        # both the disable above and (grant.validate reads the MERGED cfg) the
        # grant route. An overlay able to rewrite the carve-out has it for free.
        for layer, path in (("user", sandbox.home / ".claude" / "ulthar-overlay.json"),
                            ("project", sandbox.cwd / ".claude" / "ulthar-overlay.json")):
            for label, doc in (("emptied", {"grants": {"never_exempt": []}}),
                               ("shortened", {"grants": {"never_exempt": never[:1]}})):
                _write_json(path, doc)
                raised = ""
                try:
                    ctx_mod.merge_overlays(sandbox.cfg, ctx)
                except ctx_mod.ConfigError as exc:
                    raised = str(exc)
                # The project layer is refused one fence earlier (grants is not in
                # project_allowed_paths); the user layer has no fence and is
                # refused by the pin itself. Both must refuse, for either reason.
                # Round 2: the refusal now names the pinned SUBTREE (`grants`)
                # rather than the leaf, so accept either -- what must not change
                # is that it is refused, loudly, from both layers.
                p.check(f"{layer} overlay with grants.never_exempt {label} is refused",
                        "never_exempt" in raised or "project_allowed_paths" in raised
                        or "pinned_paths" in raised,
                        raised or "no error")
                path.unlink()

        # THE THIRD ROUTE, and the reason it needs no code: SHADOWING BY ID. An
        # appended entry reusing a protected rule's id would be a `disable` under
        # another name, and _refuse_never_exempt would not see it. _merge_list
        # already refuses a duplicate id outright, so every rule list is
        # append-only in the strict sense -- pinned here because that property is
        # now load-bearing for the carve-out, not just for merge hygiene.
        for enabled in (False, True):
            _write_json(project, {"command_rules": {"tool_discipline": [
                {"id": "authority-file-in-shell", "enabled": enabled,
                 "type": "regex_block", "target": "raw",
                 "pattern": "zzz-never-matches", "message": "m"}]}})
            raised = ""
            try:
                ctx_mod.merge_overlays(sandbox.cfg, ctx)
            except ctx_mod.ConfigError as exc:
                raised = str(exc)
            p.check("an overlay cannot SHADOW a never-exempt rule by reusing its id "
                    f"(enabled={enabled})", "authority-file-in-shell" in raised,
                    raised or "no error")
            project.unlink()
    finally:
        sandbox.cleanup()


def check_pinned_paths_cannot_be_changed_by_an_overlay(p: Probe) -> None:
    """2026-09-06 final review. `overlays.pinned_paths` names the json paths no
    overlay may change in EITHER layer. Every one of them was MEASURED as a
    one-file switch-off first; each is pinned here in both of the two ways it
    was reachable -- by REWRITING the value, and by a `disable` whose left half
    is that path (which `_apply_disables` honours for any dotted list, and which
    runs after the merge, so the value comparison alone never sees it).

    The user layer stays otherwise unrestricted: the last case below is the
    regression for that, and check_user_overlay_can_disable_too covers the rest.
    """
    sandbox = _sandbox_with_cfg()
    pinned = list(sandbox.cfg["overlays"]["pinned_paths"])
    p.check("overlays.pinned_paths is not empty (this test would be vacuous)", bool(pinned))
    refs = [e["ref"] for e in pinned]
    p.check("...and it COVERS ITSELF, so no overlay can shorten it on its way past",
            any("overlays.pinned_paths" == r or "overlays.pinned_paths".startswith(r + ".")
                for r in refs), str(refs))
    # Round 2: leaves became SUBTREES, because enumerating leaves was chasing
    # instances of a class -- five more single-overlay switch-offs were measured
    # that four leaf pins did not reach. Pin the sections that decide what a rule
    # can do, and the instances nobody has thought of yet are closed too.
    p.check("the pins are SECTIONS, not leaf paths (no dot in any ref)",
            all("." not in r for r in refs), str(refs))
    try:
        ctx = sandbox.build()
        user = sandbox.home / ".claude" / "ulthar-overlay.json"
        project = sandbox.cwd / ".claude" / "ulthar-overlay.json"
        for entry in pinned:
            ref = entry["ref"]
            # A REWRITE. For a LIST-valued pin, emptying it is the shortest
            # spelling and the one that removes every entry. For a SECTION, an
            # empty object is a no-op since round 3 (it expresses nothing), so
            # the rewrite has to actually change something: adding one key does,
            # and it is the mildest change there is -- if even that is refused,
            # every larger one is.
            value = _dotted(sandbox.cfg, ref)
            doc = {ref: [] if isinstance(value, list) else {"zzprobe": 1}}
            for layer, path in (("user", user), ("project", project)):
                _write_json(path, doc)
                raised = ""
                try:
                    ctx_mod.merge_overlays(sandbox.cfg, ctx)
                except ctx_mod.ConfigError as exc:
                    raised = str(exc)
                # The project layer is refused one fence earlier (none of these
                # paths is in project_allowed_paths); the user layer has no
                # fence and is refused by the pin itself. Both must refuse.
                p.check(f"{layer} overlay rewriting {ref!r} is refused loudly",
                        ref in raised or "project_allowed_paths" in raised,
                        raised or "no error")
                # ...naming the FILE. The rewrite branch compares values and
                # cannot attribute to a layer, so it names every overlay merged;
                # the recovery text says to delete the file named here, and a
                # refusal that named none would be the pointer-to-nothing
                # lockout _merge_layer exists to prevent.
                p.check(f"{layer} overlay rewriting {ref!r} names the overlay file",
                        str(path) in raised, raised or "no error")
                path.unlink()

            # THE OTHER DOOR: a `disable` naming the same path. Only meaningful
            # for a list of entries with ids; dispatch.events is a dict and
            # _apply_disables refuses it for a different reason, which is fine.
            value = _dotted(sandbox.cfg, ref)
            if isinstance(value, list) and value:
                first = value[0]
                ident = f"{ref}/{first['id'] if isinstance(first, dict) else first}"
                _write_json(user, {"disable": [ident]})
                raised = ""
                try:
                    ctx_mod.merge_overlays(sandbox.cfg, ctx)
                except ctx_mod.ConfigError as exc:
                    raised = str(exc)
                p.check(f"user overlay disabling into {ref!r} is refused loudly",
                        ref in raised, raised or "no error")
                user.unlink()

        # THE REGRESSION FOR WHAT WAS NOT TAKEN AWAY: the user layer may still
        # repoint ordinary wiring and switch ordinary rules off. If this fails,
        # the pin has grown into the general restriction it is explicitly not.
        _write_json(user, {"thresholds": {"subprocess_timeout_local_sec": 99},
                           "disable": ["command_rules.tool_discipline/cd-compound"]})
        merged = ctx_mod.merge_overlays(sandbox.cfg, ctx)
        p.check("a user overlay still sets an ordinary scalar",
                merged["thresholds"]["subprocess_timeout_local_sec"] == 99,
                str(merged["thresholds"]["subprocess_timeout_local_sec"]))
        p.check("...and still switches an ordinary rule off",
                "cd-compound" not in [r["id"] for r in
                                      merged["command_rules"]["tool_discipline"]])
        user.unlink()
    finally:
        sandbox.cleanup()


def check_a_rule_list_can_only_be_merged_as_a_list(p: Probe) -> None:
    """Found by the round-2 sweep, and it needed no pinned path at all:
    `{"command_rules": {"tool_discipline": {"zz": 1}}}` put a DICT where the base
    holds a rule list, so _merge_walk recursed into it as a dict and _set_dotted
    replaced the whole list with `{}`. EVERY tool_discipline rule vanished --
    including `authority-file-in-shell`, which grants.never_exempt says nothing
    may switch off. Measured rc=0 on a shell `touch` of a grant path that is
    rc=2 with no overlay; a scalar did the same.

    _merge_list already refused the mismatch the other way round ("base config
    value at this path is not a list"); this is the missing mirror
    (_refuse_container_swap). The pinned subtrees do NOT cover it -- command_rules
    is a rule section, deliberately overlayable -- so it is its own check."""
    sandbox = _sandbox_with_cfg()
    try:
        ctx = sandbox.build()
        user = sandbox.home / ".claude" / "ulthar-overlay.json"
        for label, doc in (
            ("a dict where a rule list lives",
             {"command_rules": {"tool_discipline": {"zz": 1}}}),
            ("a scalar where a rule list lives",
             {"command_rules": {"tool_discipline": 0}}),
            ("a list where a section lives", {"command_rules": []}),
            # ROUND 5, the third side of the mirror, and the only one whose
            # consequence is not a fail-open but a CRASH OUT of the dispatcher:
            # dispatch.py's _notice_stdout reads skill_anchor.envelope_key
            # OUTSIDE the guard try/except, so the round-4 crash rule cannot
            # catch it. Measured rc=1 with a traceback -- and the
            # once-per-session marker for the project overlay's disable notice
            # had already been consumed, so the operator lost the notice as well.
            ("a dict where a scalar lives", {"skill_anchor": {"envelope_key": {"x": 1}}}),
            ("a list where a scalar lives", {"skill_anchor": {"envelope_key": ["x"]}}),
        ):
            _write_json(user, doc)
            raised = ""
            try:
                ctx_mod.merge_overlays(sandbox.cfg, ctx)
            except ctx_mod.ConfigError as exc:
                raised = str(exc)
            p.check(f"{label} is refused loudly", bool(raised), raised or "no error")
            # The refusal has to name the offending PATH and the FILE -- the
            # recovery text tells the reader to delete the file it names, so a
            # message that named neither would be a pointer to nothing.
            path_named = next(iter(doc)) if isinstance(doc, dict) else ""
            p.check(f"...naming the path ({label})", path_named in raised,
                    raised or "no error")
            p.check(f"...naming the overlay file ({label})", str(user) in raised,
                    raised or "no error")
            user.unlink()

        # NOT a ban on touching the path: the legitimate operation still merges.
        _write_json(user, {"command_rules": {"tool_discipline": [
            {"id": "zz-probe", "enabled": False, "type": "regex_block",
             "target": "raw", "pattern": "zzz-never-matches", "message": "m"}]}})
        merged = ctx_mod.merge_overlays(sandbox.cfg, ctx)
        ids = [r["id"] for r in merged["command_rules"]["tool_discipline"]]
        p.check("an ordinary append to the same list still works", "zz-probe" in ids, str(ids))
        p.check("...and every shipped rule is still there",
                "authority-file-in-shell" in ids, str(ids))
        user.unlink()

        # ...and overriding a SCALAR with a scalar is still the plain, supported
        # thing it always was. The mirror refuses containers there, not values.
        _write_json(user, {"skill_anchor": {"envelope_key": "hookSpecificOutput"}})
        merged = ctx_mod.merge_overlays(sandbox.cfg, ctx)
        p.check("a scalar override of a scalar still merges",
                merged["skill_anchor"]["envelope_key"] == "hookSpecificOutput",
                str(merged["skill_anchor"].get("envelope_key")))
        user.unlink()
    finally:
        sandbox.cleanup()


def check_a_section_cannot_be_erased(p: Probe) -> None:
    """ROUND 3, and the cheapest erasure found so far: `{"command_rules": 0}` --
    also `null`, `""`, `false`, and `{}` -- put a scalar (or an empty object)
    where a whole SECTION lives. The walk sent it to the leaf branch and
    _set_dotted replaced the section. Every tool_discipline rule went with it,
    BOTH grants.never_exempt rules among them, and the consuming guard's KeyError
    became ALLOW through the dispatcher's fail-open. Measured rc=0 for a shell
    write to a grant path AND for a delete outside the disposable allowlist;
    `{"deploy_guard": 0}` did the same to the deploy guard.

    TWO independent defences, because the first is a shape check and shape checks
    are what keep turning out to be incomplete:
      (1) _refuse_container_swap refuses dict -> non-dict as well now, and an
          empty dict recurses (expressing nothing) instead of replacing.
      (2) _refuse_authority_erasure asks the question the shapes were a proxy
          for, on the merge OUTPUT: are the never_exempt rules still present, and
          does every grants.protected ref still resolve? A future hole in (1) is
          then a loud refusal rather than a silent fail-open."""
    sandbox = _sandbox_with_cfg()
    try:
        ctx = sandbox.build()
        user = sandbox.home / ".claude" / "ulthar-overlay.json"
        for label, doc in (
            ("an int", {"command_rules": 0}),
            ("null", {"command_rules": None}),
            ("an empty string", {"command_rules": ""}),
            ("false", {"command_rules": False}),
            ("deploy_guard as an int", {"deploy_guard": 0}),
        ):
            _write_json(user, doc)
            raised = ""
            try:
                ctx_mod.merge_overlays(sandbox.cfg, ctx)
            except ctx_mod.ConfigError as exc:
                raised = str(exc)
            p.check(f"a section replaced by {label} is refused loudly",
                    bool(raised), raised or "no error")
            p.check(f"...naming the overlay file ({label})", str(user) in raised,
                    raised or "no error")
            user.unlink()

        # An EMPTY object expresses nothing and must therefore CHANGE nothing --
        # neither replace the section nor raise.
        _write_json(user, {"command_rules": {}})
        merged = ctx_mod.merge_overlays(sandbox.cfg, ctx)
        ids = [r["id"] for r in merged["command_rules"]["tool_discipline"]]
        p.check("an EMPTY object at a section is a no-op, not an erasure",
                "authority-file-in-shell" in ids and "delete-outside-disposable" in ids,
                str(ids))
        user.unlink()
    finally:
        sandbox.cleanup()


def check_the_authority_rules_must_survive_the_merge(p: Probe) -> None:
    """Defence (2) on its own terms. The invariant is stated over the merge
    OUTPUT, so it holds regardless of which input shape produced the loss --
    which is the whole point of adding it next to the shape checks. Driven here
    by calling it with a merged cfg that has already lost a rule: the situation a
    future hole in the shape checks would produce."""
    sandbox = _sandbox_with_cfg()
    try:
        base = sandbox.cfg
        ident = base["grants"]["never_exempt"][0]
        path, _, rule_id = ident.partition("/")

        merged = copy.deepcopy(base)
        head, _, tail = path.partition(".")
        merged[head][tail] = [r for r in merged[head][tail] if r.get("id") != rule_id]
        raised = ""
        try:
            ctx_mod._refuse_authority_erasure(base, merged, ("<overlay>",))
        except ctx_mod.ConfigError as exc:
            raised = str(exc)
        p.check("a merge result missing a never_exempt rule is refused",
                rule_id in raised, raised or "no error")

        merged = copy.deepcopy(base)
        merged.pop("command_rules", None)
        raised = ""
        try:
            ctx_mod._refuse_authority_erasure(base, merged, ("<overlay>",))
        except ctx_mod.ConfigError as exc:
            raised = str(exc)
        p.check("a merge result missing the whole section is refused too",
                bool(raised), raised or "no error")

        # ...and the protected-file half: grants.protected refs go through the
        # SAME resolver grant_files uses, so a ref that resolves to nothing here
        # is a file nothing protects there.
        merged = copy.deepcopy(base)
        for rule in merged["command_rules"]["tool_discipline"]:
            rule.pop("pm_allowlist", None)
        raised = ""
        try:
            ctx_mod._refuse_authority_erasure(base, merged, ("<overlay>",))
        except ctx_mod.ConfigError as exc:
            raised = str(exc)
        p.check("a grants.protected ref that stops resolving is refused",
                "resolves to nothing" in raised, raised or "no error")

        # The shipped configuration must of course satisfy its own invariant.
        ctx_mod._refuse_authority_erasure(base, copy.deepcopy(base), ())
        p.check("the shipped config satisfies the invariant (so it is not vacuous)", True)

        # WIRING. The three checks above call the invariant directly, which
        # proves what it decides but not that merge_overlays still runs it -- and
        # with the container mirror complete there is no overlay document that
        # reaches (2) without (1) refusing first, so the ordinary path cannot
        # prove the wiring either. (That is itself the useful finding: the two
        # defences overlap by design, and (2) is the one that survives a future
        # hole in (1).) Driven instead from a cfg whose never_exempt names a rule
        # that is not there, which only the wired-in call can catch.
        sandbox.cfg = copy.deepcopy(base)
        sandbox.cfg["grants"]["never_exempt"] = [f"{path}/no-such-rule-id"]
        _write_json(sandbox.home / ".claude" / "ulthar-overlay.json",
                    {"thresholds": {"subprocess_timeout_local_sec": 7}})
        raised = ""
        try:
            ctx_mod.merge_overlays(sandbox.cfg, sandbox.build())
        except ctx_mod.ConfigError as exc:
            raised = str(exc)
        p.check("merge_overlays RUNS the invariant (not just defined next to it)",
                "no-such-rule-id" in raised, raised or "no error")
    finally:
        sandbox.cleanup()


def check_an_undeclared_list_replacement_is_shape_checked(p: Probe) -> None:
    """ROUND 4, defence B. The user layer may replace a list that declares no
    merge strategy -- the ones that are not rule lists (`redirect_noise_strips`,
    `quote_strip_patterns`). That went in with NO shape check, and it is the one
    hole none of the other defences could see: the path is not pinned (it lives
    inside `command_rules`), a list replaces a list so the container mirror is
    satisfied, and every rule still exists afterwards so the erasure invariant is
    satisfied too. `{"command_rules": {"redirect_noise_strips": [["x"]]}}` then
    made tool_discipline raise inside `_build_probes` BEFORE it reached a single
    rule, and the fail-open turned that into rc=0 with EMPTY stderr."""
    sandbox = _sandbox_with_cfg()
    try:
        ctx = sandbox.build()
        user = sandbox.home / ".claude" / "ulthar-overlay.json"
        for label, doc in (
            ("a one-item pair where two-item pairs live",
             {"command_rules": {"redirect_noise_strips": [["x"]]}}),
            ("a bare string where a pair lives",
             {"command_rules": {"redirect_noise_strips": ["x"]}}),
            ("an object where a pair lives",
             {"command_rules": {"redirect_noise_strips": [{"id": "z"}]}}),
            ("the sibling path", {"command_rules": {"quote_strip_patterns": [["x"]]}}),
        ):
            _write_json(user, doc)
            raised = ""
            try:
                ctx_mod.merge_overlays(sandbox.cfg, ctx)
            except ctx_mod.ConfigError as exc:
                raised = str(exc)
            p.check(f"{label} is refused at merge", bool(raised), raised or "no error")
            p.check(f"...naming the file ({label})", str(user) in raised,
                    raised or "no error")
            user.unlink()

        # A REPLACEMENT OF THE RIGHT SHAPE STILL WORKS: this is a supported
        # operation and must not have become "you may not touch these".
        base = list(sandbox.cfg["command_rules"]["redirect_noise_strips"])
        p.check("the base list is non-empty (this test would be vacuous)", bool(base))
        good = [["2>/dev/null", ""]]
        _write_json(user, {"command_rules": {"redirect_noise_strips": good}})
        merged = ctx_mod.merge_overlays(sandbox.cfg, ctx)
        p.check("a correctly-shaped replacement still merges",
                merged["command_rules"]["redirect_noise_strips"] == good,
                str(merged["command_rules"]["redirect_noise_strips"]))
        user.unlink()
    finally:
        sandbox.cleanup()


def check_a_guard_crash_under_an_overlay_blocks(p: Probe) -> None:
    """ROUND 4, defence A, the structural half: whatever shape slips past
    merge-time validation, a guard exception may not become ALLOW while an
    overlay is in the cfg. These two halves are the whole contract -- with an
    overlay, block; without one, fail open exactly as before, because then the
    crash is unambiguously ULTHAR's own bug and bricking every tool call over an
    ULTHAR bug is the worse failure."""
    guards_dir = Path(tempfile.mkdtemp(prefix="ulthar_crash_guard_"))
    try:
        (guards_dir / "__init__.py").write_text("", encoding="utf-8")
        (guards_dir / "tool_discipline.py").write_text(
            "def run(payload, cfg, ctx):\n"
            "    raise ValueError('injected crash, no overlay content involved')\n",
            encoding="utf-8")
        rc, err = _dispatch_rc(None, "echo hi", guards_dir=guards_dir,
                               guard="tool_discipline")
        p.check("no overlay: a guard crash still FAILS OPEN (rc=0), as it always has",
                rc == 0, f"rc={rc}\n{err[:300]}")

        for layer, overlay, user_overlay in (
            ("user", None, {"thresholds": {"subprocess_timeout_local_sec": 6}}),
            ("project", {"git_policy": {"protected_branches": ["release"]}}, None),
        ):
            rc, err = _dispatch_rc(overlay, "echo hi", guards_dir=guards_dir,
                                   guard="tool_discipline", user_overlay=user_overlay)
            p.check(f"{layer} overlay merged: the SAME crash BLOCKS (rc=2)", rc == 2,
                    f"rc={rc}\n{err[:300]}")
            p.check(f"...naming the overlay file ({layer})",
                    "ulthar-overlay.json" in err, err[:400])
            p.check(f"...and naming the guard and the exception type ({layer})",
                    "tool_discipline" in err and "ValueError" in err, err[:400])
    finally:
        shutil.rmtree(guards_dir, ignore_errors=True)


def check_pinned_paths_block_end_to_end(p: Probe) -> None:
    """THE MEASUREMENT THAT FOUND THIS, inverted. Before the pin,
    `{"dispatch": {"events": {"PreToolUse": []}}}` in the USER overlay gave rc=0
    on a command the delete rule blocks: every PreToolUse guard was gone, and no
    operator notice fired either, because the disable notice covers a project
    overlay's `disable` and this disabled nothing -- it unrouted everything.

    rc=2 alone would pass for the wrong reason here (the delete rule gives rc=2
    too), so the stderr has to name the pin."""
    rc, err = _dispatch_rc(None, "rm -rf owner-files/important-data.db",
                           user_overlay={"dispatch": {"events": {"PreToolUse": []}}})
    p.check("end-to-end: a user overlay emptying dispatch.events blocks (rc=2)", rc == 2,
            f"rc={rc}\n{err[:400]}")
    p.check("...and the block names the pinned path, not the delete rule",
            "dispatch" in err and "pinned_paths" in err, err[:600])

    rc, err = _dispatch_rc(None, "rm -rf owner-files/important-data.db",
                           user_overlay={"grants": {"protected": []}})
    p.check("end-to-end: a user overlay emptying grants.protected blocks (rc=2)",
            rc == 2 and "grants" in err and "pinned_paths" in err, f"rc={rc}\n{err[:400]}")

    # ROUND 2, the five the leaf pins did not reach. Each was measured rc=0 on a
    # Write of a grant file (rc=2 with no overlay) before `pinned_paths` became
    # subtrees; the child-path `disable` is the one equality could never see.
    for label, doc in (
        ("a CHILD path disable", {"disable": ["dispatch.events.PreToolUse/None"]}),
        ("the routing alternation", {"regex": {"named_alternations": {"write_edit_tools": "__never__"}}}),
        ("grants.enabled false", {"grants": {"enabled": False}}),
        ("grants.dir repointed", {"grants": {"dir": "${CWD}/mygrants"}}),
        ("a non-string protected_message", {"grants": {"protected_message": 5}}),
    ):
        rc, err = _dispatch_rc(None, "rm -rf owner-files/important-data.db", user_overlay=doc)
        p.check(f"end-to-end: {label} is refused, naming the pin (rc=2)",
                rc == 2 and "pinned_paths" in err, f"rc={rc}\n{err[:400]}")


def check_never_exempt_disable_blocks_end_to_end(p: Probe) -> None:
    """Through the real dispatcher: the refusal has to be a BLOCK on the next
    tool call, not an exception something swallows."""
    sandbox = _sandbox_with_cfg()
    ident = sandbox.cfg["grants"]["never_exempt"][0]
    sandbox.cleanup()
    rc, err = _dispatch_rc({"disable": [ident]}, "git status")
    p.check(f"end-to-end: an overlay disabling {ident!r} blocks (rc=2)",
            rc == 2 and "never_exempt" in err, f"rc={rc}\n{err[:400]}")


def check_user_overlay_can_disable_too(p: Probe) -> None:
    """The user layer is the operator's own file, so there is no argument for
    denying it the operation the project layer has -- only a consistency one for
    giving it the same syntax. Scalar lists are addressed by the scalar itself.
    ONE EXCEPTION, and it is the same in both layers: grants.never_exempt -- see
    check_never_exempt_rules_cannot_be_disabled_by_an_overlay."""
    sandbox = _sandbox_with_cfg()
    try:
        ctx = sandbox.build()
        _write_json(sandbox.home / ".claude" / "ulthar-overlay.json",
                    {"disable": ["git_policy.protected_branches/main",
                                 "command_rules.tool_discipline/cd-compound"]})
        merged = ctx_mod.merge_overlays(sandbox.cfg, ctx)
        p.check("user overlay: disables a scalar entry by its own value",
                merged["git_policy"]["protected_branches"] == ["master"],
                str(merged["git_policy"]["protected_branches"]))
        p.check("user overlay: disables an object rule by id, siblings untouched",
                [r["id"] for r in merged["command_rules"]["tool_discipline"]]
                == ["shell-file-creation", "delete-outside-disposable",
                    "authority-file-in-shell"],
                str([r["id"] for r in merged["command_rules"]["tool_discipline"]]))
        p.check("user overlay disable: base cfg untouched (no in-place mutation)",
                sandbox.cfg["git_policy"]["protected_branches"] == ["main", "master"])

        # Both layers naming the same rule is agreement, not a typo -- it must not
        # block, or a personal preference would break every repo that shares it.
        _write_json(sandbox.cwd / ".claude" / "ulthar-overlay.json",
                    {"disable": ["git_policy.protected_branches/main"]})
        merged = ctx_mod.merge_overlays(sandbox.cfg, ctx)
        p.check("both layers disabling the SAME rule is not an error",
                merged["git_policy"]["protected_branches"] == ["master"],
                str(merged["git_policy"]["protected_branches"]))
    finally:
        sandbox.cleanup()


def _dispatch_in(home: Path, cwd: Path, command: str, event: str = "PreToolUse",
                 tool: str = "Bash", session_id: str = "sess-1"):
    """Like _dispatch_rc, but against a home + cwd the CALLER owns, so several
    dispatches can share one session. Once-per-session state is on disk (it has
    to be: a dispatcher process does not outlive one tool call), so a helper
    that makes a throwaway home per call cannot see repetition at all."""
    payload = {"hook_event_name": event, "tool_name": tool, "cwd": str(cwd),
               "session_id": session_id, "_home_override": str(home),
               "tool_input": {"command": command}}
    proc = subprocess.run(
        [sys.executable, str(HOOKS_DIR / "dispatch.py"), "--event", event],
        input=json.dumps(payload), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        env=dict(os.environ, ULTHAR_CONFIG=str(SKILL_ROOT / "config.json")))
    return proc.returncode, (proc.stderr or ""), (proc.stdout or "")


def _disable_log_entries(home: Path) -> list[dict]:
    """The `overlay_disable` lines in logging.config_error_log under `home`."""
    log = home / ".claude" / "hooks" / "logs" / "config-errors.jsonl"
    if not log.exists():
        return []
    out = []
    for line in log.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("phase") == "overlay_disable":
            out.append(entry)
    return out


def check_disable_is_visible_to_the_operator(p: Probe) -> None:
    """Visibility. The owner accepted that a cloned repo's policy applies while
    they work in it; they must still be able to SEE it. SessionStart states it
    once in context (skill_anchor emits plain text on that event, so a plain-text
    prefix concatenates safely)."""
    overlay = {"disable": ["git_policy.destroyers/clean"]}
    rc, err, out = _dispatch_rc(overlay, "", event="SessionStart", tool="startup",
                                with_stdout=True)
    p.check("visibility: SessionStart states the disabling in context",
            "[ULTHAR]" in out and "git_policy.destroyers/clean" in out
            and "ulthar-overlay.json" in out, f"rc={rc}\n{out[:600]}")
    p.check("visibility: the SessionStart notice does not block or error",
            rc == 0 and not err, f"rc={rc}\n{err[:300]}")

    rc, err, out = _dispatch_rc(None, "", event="SessionStart", tool="startup",
                                with_stdout=True)
    p.check("visibility: with no overlay, SessionStart stdout is unchanged",
            "[ULTHAR]" not in out, out[:300])


def check_disable_is_visible_on_first_encounter(p: Probe) -> None:
    """THE THIRD-PARTY FINDING. Both visibility surfaces used to fire only when
    `event == "SessionStart"`. A session that starts outside the repository and
    later enters it -- or whose overlay is created after startup -- honours the
    disables from the very next tool call while saying nothing anywhere, and no
    later SessionStart is guaranteed in that session. That is the silent
    removal the whole model was rebuilt to prevent, with the mechanism changed
    and the invisibility intact.

    So: first ENCOUNTER, not first SessionStart. Once per session per overlay
    still, because printing on every tool call would train the reader to ignore
    it and would push real entries out of a size-capped log.

    The log assertion is deliberate and load-bearing: the previous regression
    only checked that an ordinary call printed nothing, so deleting the
    config_error_log write entirely would still have passed it."""
    with tempfile.TemporaryDirectory(prefix="ulthar_overlay_first_",
                                     ignore_cleanup_errors=True) as tmpdir:
        tmp = Path(tmpdir)
        home = tmp / "home"
        (home / ".claude").mkdir(parents=True)
        cwd = tmp / "repo"
        (cwd / ".claude").mkdir(parents=True)
        overlay_path = cwd / ".claude" / "ulthar-overlay.json"
        _write_json(overlay_path, {"disable": ["git_policy.destroyers/clean"]})

        rc, err, out = _dispatch_in(home, cwd, "git status")
        p.check("first encounter: an ordinary tool call states it in context, "
                "with no SessionStart anywhere in the session",
                "[ULTHAR]" in out and "git_policy.destroyers/clean" in out
                and "ulthar-overlay.json" in out, f"rc={rc}\n{out[:600]}")
        p.check("first encounter: the notice is the context envelope, not "
                "transcript-only plain text",
                '"additionalContext"' in out and '"hookEventName":"PreToolUse"' in out,
                out[:300])
        p.check("first encounter: it does not block or error",
                rc == 0 and not err, f"rc={rc}\n{err[:300]}")
        logged = _disable_log_entries(home)
        p.check("first encounter: it is RECORDED in config_error_log, naming the "
                "overlay file and the disabled ids",
                len(logged) == 1
                and "git_policy.destroyers/clean" in logged[0].get("error", "")
                and "ulthar-overlay.json" in logged[0].get("error", ""),
                str(logged))

        rc, err, out = _dispatch_in(home, cwd, "git status")
        p.check("second tool call in the same session prints nothing",
                "[ULTHAR]" not in out, out[:300])
        rc, err, out = _dispatch_in(home, cwd, "git log")
        p.check("third tool call in the same session prints nothing",
                "[ULTHAR]" not in out, out[:300])
        p.check("and the log line is not repeated per tool call either "
                f"({len(_disable_log_entries(home))} entries after 3 calls)",
                len(_disable_log_entries(home)) == 1, str(_disable_log_entries(home)))

        rc, err, out = _dispatch_in(home, cwd, "git status", session_id="sess-2")
        p.check("a DIFFERENT session is told again (once per session, not once ever)",
                "[ULTHAR]" in out, out[:300])
        p.check("and the new session's encounter is recorded too",
                len(_disable_log_entries(home)) == 2, str(_disable_log_entries(home)))

        _write_json(overlay_path, {"disable": ["git_policy.destroyers/clean",
                                              "git_policy.destroyers/reset-hard"]})
        rc, err, out = _dispatch_in(home, cwd, "git status")
        p.check("an overlay that CHANGES what it disables mid-session is stated again",
                "[ULTHAR]" in out and "git_policy.destroyers/reset-hard" in out,
                out[:400])


def check_the_notice_is_not_burned_on_an_event_that_cannot_carry_it(p: Probe) -> None:
    """"Once per session" must not be spent on a dispatch whose stdout goes
    nowhere, or the session ends up silent again by a different route. `Stop`
    is routed (artifact_hygiene, dev_process_hygiene), emits no stdout of its
    own, and a Stop hook has no context channel to put text into -- and a `cd`
    into the repository as a turn's last Bash call makes the very NEXT dispatch
    a Stop carrying the new cwd, i.e. exactly the first encounter. The durable
    log line still fires there; the notice waits for a call that can speak."""
    with tempfile.TemporaryDirectory(prefix="ulthar_overlay_stop_",
                                     ignore_cleanup_errors=True) as tmpdir:
        tmp = Path(tmpdir)
        home = tmp / "home"
        (home / ".claude").mkdir(parents=True)
        cwd = tmp / "repo"
        (cwd / ".claude").mkdir(parents=True)
        _write_json(cwd / ".claude" / "ulthar-overlay.json",
                    {"disable": ["git_policy.destroyers/clean"]})

        rc, err, out = _dispatch_in(home, cwd, "", event="Stop", tool="*")
        p.check("first encounter on Stop: nothing is written to a channel that "
                "cannot carry it", "[ULTHAR]" not in out, f"rc={rc}\n{out[:300]}")
        p.check("first encounter on Stop: the durable record is written anyway",
                len(_disable_log_entries(home)) == 1, str(_disable_log_entries(home)))

        rc, err, out = _dispatch_in(home, cwd, "git status")
        p.check("the notice was NOT burned: the next tool call still states it",
                "[ULTHAR]" in out, out[:300])


def main() -> int:
    p = Probe()
    check_absent_overlay_is_noop(p)
    check_disabled_overlays_section_is_noop(p)
    check_user_overlay_appends(p)
    check_project_overlay_appends_allowed_path(p)
    check_both_layers_union(p)
    check_project_overlay_cannot_touch_disallowed_path(p)
    check_project_overlay_cannot_shadow_existing_id(p)
    check_project_overlay_can_add_new_id(p)
    check_malformed_json_fails_loudly(p)
    check_non_object_overlay_fails_loudly(p)
    check_example_user_overlay_restores_old_defaults(p)
    check_malformed_project_rule_cannot_disable_a_guard(p)
    check_plain_list_rejects_non_scalar(p)
    check_overlay_error_always_names_its_file(p)
    check_ulthar_own_bug_still_fails_open(p)
    check_malformed_design_case_cannot_disable_brief_gate(p)
    check_invalid_regex_from_overlay_blames_the_overlay(p)
    check_live_state_change_is_not_blamed_on_the_overlay(p)
    check_scalar_type_conformance(p)
    check_scalar_at_empty_declared_list_cannot_disable_a_guard(p)
    check_empty_list_without_a_declared_shape_refuses_appends(p)
    check_append_by_id_entry_must_match_the_base_shape(p)
    check_empty_append_by_id_without_required_keys_refuses(p)
    check_example_project_overlay_merges(p)
    check_disable_stops_one_rule_and_only_that_rule(p)
    check_disable_of_a_nonexistent_rule_fails_loudly(p)
    check_project_disable_cannot_reach_ulthars_own_wiring(p)
    check_user_overlay_can_disable_too(p)
    check_never_exempt_rules_cannot_be_disabled_by_an_overlay(p)
    check_never_exempt_disable_blocks_end_to_end(p)
    check_pinned_paths_cannot_be_changed_by_an_overlay(p)
    check_a_rule_list_can_only_be_merged_as_a_list(p)
    check_a_section_cannot_be_erased(p)
    check_the_authority_rules_must_survive_the_merge(p)
    check_an_undeclared_list_replacement_is_shape_checked(p)
    check_a_guard_crash_under_an_overlay_blocks(p)
    check_pinned_paths_block_end_to_end(p)
    check_disable_is_visible_to_the_operator(p)
    check_disable_is_visible_on_first_encounter(p)
    check_the_notice_is_not_burned_on_an_event_that_cannot_carry_it(p)
    print(f"\nRESULT: {p.passed} passed, {p.failed} failed")
    return 1 if p.failed else 0


if __name__ == "__main__":
    sys.exit(main())
