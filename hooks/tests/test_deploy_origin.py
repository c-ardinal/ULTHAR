#!/usr/bin/env python3
"""hooks/tests/test_deploy_origin.py -- regression cover for the two findings that
share one cause: the runtime assumed a master location the published install
procedure never creates.

Finding A: the installer deploys runtime files only, and the README says "clone
anywhere, then `python hooks/install.py`". So an active-skills marker naming
`ulthar` produced NO anchor for any clone outside `~/.claude/skills/ulthar`, and
nothing anywhere said why. Covered here by:
  - anchor served from the recorded clone, whose directory is literally `ULTHAR`
    (GitHub's default clone dir) while the marker says `ulthar` -- the match is on
    the configured skill NAME, casefolded, never on the directory name, which is
    what makes this work on a case-sensitive filesystem;
  - `skills_root/<name>` still wins when present (this machine's happy path);
  - install.py printing an ARMED / NOT ARMED / no-ANCHOR.md line every time, which
    is where "silence" is actually killed -- the guard may never block or print.

Finding B: recovery instructions named `~/.claude/skills/ulthar` and told the
reader to run the DEPLOYED install.py, which takes `~/.claude` as its skill root
and cannot redeploy anything. Covered here by:
  - the infra block message naming the recorded clone and ITS installer;
  - the same message saying the master is UNKNOWN, and naming no path at all, when
    the deploy holds no record;
  - the deployed copy of install.py refusing to run and naming the real master;
  - deploy_guard's message pointing at the recorded clone;
  - deploy_guard BLOCKING a direct edit of the record itself -- it is what both
    findings' fixes now trust, so leaving it writable would just move the defect.

Everything runs against temp dirs via --deploy-dir / a sandboxed Ctx: nothing here
WRITES outside a temp directory, and ~/.claude/hooks is never deployed to. It is
read-hermetic too: `test_master_installer_still_runs` drives install.py's real
cmd_default, whose global lookups would otherwise READ the real
~/.claude/ulthar-overlay.json and ~/.claude/state/active-skills, so it runs inside
`conftest.redirected_home()` (HOME/USERPROFILE/cwd pointed at its own temp tree --
the technique test_contract.py uses for its install.py subprocesses). The ONE
deliberate real read left is `SKILL_ROOT/ANCHOR.md`: it is a file of the clone
under test, not of the machine, and the anchor line is exactly what that test
asserts on.

A deploy that install.py REFUSES (a required dependency missing under the pinned
interpreter) is reported as SKIP, not FAIL: "this machine lacks psutil" and "the
behaviour under test is broken" must not arrive as the same result.

    python tests/test_deploy_origin.py
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
HOOKS_DIR = TESTS_DIR.parent
SKILL_ROOT = HOOKS_DIR.parent

sys.path.insert(0, str(HOOKS_DIR))
sys.path.insert(0, str(TESTS_DIR))

import dispatch as dispatch_mod  # noqa: E402
import install as install_mod  # noqa: E402
from guards import deploy as deploy_guard  # noqa: E402
from guards import skill_anchor  # noqa: E402
from lib import paths as paths_mod  # noqa: E402
from conftest import SandboxCtx, deploy_skip_reason, redirected_home  # noqa: E402


class Probe:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self.skipped = 0

    def check(self, name: str, cond: bool, detail: str = "") -> None:
        if cond:
            self.passed += 1
            print(f"OK   {name}")
        else:
            self.failed += 1
            print(f"FAIL {name}" + (f"\n       {detail}" if detail else ""))

    def skip(self, name: str, reason: str) -> None:
        """Counted apart from passed AND failed: a check that did not run is
        neither evidence of correctness nor of a defect."""
        self.skipped += 1
        print(f"SKIP {name}\n       {reason}")


def _anchor_body(result) -> str:
    """The anchor text out of either stdout shape."""
    if not result.stdout:
        return ""
    try:
        return json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    except Exception:  # noqa: BLE001 -- SessionStart's raw-text shape
        return result.stdout


# ------------------------------------------------------------------ Finding A


def test_anchor_from_recorded_clone(p: Probe) -> None:
    """The published flow verbatim: clone to an arbitrary directory (named
    `ULTHAR`, as `git clone` defaults to), deploy from it, arm the marker with the
    lower-case skill name. No ~/.claude/skills/ulthar exists anywhere."""
    sandbox = SandboxCtx()
    try:
        name = sandbox.cfg["skill_anchor"]["self_skill_name"]
        clone = sandbox.root / "src" / "ULTHAR"  # GitHub's default clone directory
        clone.mkdir(parents=True)
        (clone / "ANCHOR.md").write_text("anchor from the clone", encoding="utf-8")
        (sandbox.deploy_dir / paths_mod.DEPLOY_ORIGIN_FILE).write_text(
            str(clone) + "\n", encoding="utf-8")
        (sandbox.home / ".claude" / "state" / "active-skills").write_text(
            f"{name}\n", encoding="utf-8")

        payload = {"tool_name": "Agent", "cwd": str(sandbox.cwd), "hook_event_name": "PreToolUse"}
        result = skill_anchor.run(payload, sandbox.cfg, sandbox.build(payload))

        p.check("clone anchor: never blocks", result.exit_code == 0 and result.stderr == "")
        p.check("clone anchor: served from the recorded clone (dir 'ULTHAR', marker 'ulthar')",
                _anchor_body(result) == "anchor from the clone", repr(result.stdout))
    finally:
        sandbox.cleanup()


def test_skills_root_still_wins(p: Probe) -> None:
    """The happy path on a machine where the clone DOES sit under skills_root must
    be byte-identical to before: skills_root is looked up first and the recorded
    clone is never consulted."""
    sandbox = SandboxCtx()
    try:
        name = sandbox.cfg["skill_anchor"]["self_skill_name"]
        installed = sandbox.home / ".claude" / "skills" / name
        installed.mkdir(parents=True)
        (installed / "ANCHOR.md").write_text("anchor from skills_root", encoding="utf-8")
        clone = sandbox.root / "src" / "ULTHAR"
        clone.mkdir(parents=True)
        (clone / "ANCHOR.md").write_text("anchor from the clone", encoding="utf-8")
        (sandbox.deploy_dir / paths_mod.DEPLOY_ORIGIN_FILE).write_text(
            str(clone) + "\n", encoding="utf-8")
        (sandbox.home / ".claude" / "state" / "active-skills").write_text(
            f"{name}\n", encoding="utf-8")

        payload = {"tool_name": "Agent", "cwd": str(sandbox.cwd), "hook_event_name": "PreToolUse"}
        result = skill_anchor.run(payload, sandbox.cfg, sandbox.build(payload))
        p.check("precedence: skills_root wins over the recorded clone",
                _anchor_body(result) == "anchor from skills_root", repr(result.stdout))
    finally:
        sandbox.cleanup()


def test_fallback_is_self_only(p: Probe) -> None:
    """The fallback is ULTHAR's own entry, not a general rule -- ULTHAR knows where
    its master is and nothing about any other skill's."""
    sandbox = SandboxCtx()
    try:
        clone = sandbox.root / "src" / "ULTHAR"
        clone.mkdir(parents=True)
        (clone / "ANCHOR.md").write_text("anchor from the clone", encoding="utf-8")
        (sandbox.deploy_dir / paths_mod.DEPLOY_ORIGIN_FILE).write_text(
            str(clone) + "\n", encoding="utf-8")
        (sandbox.home / ".claude" / "state" / "active-skills").write_text(
            "some-other-skill\n", encoding="utf-8")

        payload = {"tool_name": "Agent", "cwd": str(sandbox.cwd), "hook_event_name": "PreToolUse"}
        result = skill_anchor.run(payload, sandbox.cfg, sandbox.build(payload))
        p.check("scope: another skill's name does not pick up ULTHAR's anchor",
                result.exit_code == 0 and result.stdout == "", repr(result.stdout))
    finally:
        sandbox.cleanup()


def test_install_reports_anchor_state(p: Probe) -> None:
    """install.py is the only place allowed to be loud about this, so it must be:
    armed, not armed, and marker-present-but-not-listing all produce an actionable
    line, on deploy and on --verify alike."""
    sandbox = SandboxCtx()
    try:
        cfg = sandbox.cfg
        name = cfg["skill_anchor"]["self_skill_name"]
        # install.py builds its own Ctx from an empty payload, so ${CWD} would be
        # the process cwd and `~` the real home. Point both lookups at absolute
        # sandbox paths instead of reading the real ~/.claude/state or skills tree.
        marker = sandbox.cwd / ".claude" / "state" / "active-skills"
        marker.parent.mkdir(parents=True, exist_ok=True)
        cfg["skill_anchor"]["list_lookup_order"] = [str(marker).replace("\\", "/")]
        cfg["paths"]["skills_root"] = str(sandbox.home / ".claude" / "skills").replace("\\", "/")

        lines = "\n".join(install_mod.anchor_status_lines(cfg, sandbox.deploy_dir))
        p.check("install: says NOT ARMED and what to create when no marker exists",
                "NOT ARMED" in lines and "active-skills" in lines, lines)

        marker.write_text("# comment\nsome-other-skill\n", encoding="utf-8")
        lines = "\n".join(install_mod.anchor_status_lines(cfg, sandbox.deploy_dir))
        p.check("install: says NOT ARMED when the marker exists but omits the name",
                "NOT ARMED" in lines and "does not list" in lines, lines)

        marker.write_text(f"# comment\n{name}\n", encoding="utf-8")
        lines = "\n".join(install_mod.anchor_status_lines(cfg, sandbox.deploy_dir))
        p.check("install: says ARMED and names the resolved ANCHOR.md",
                "ARMED" in lines and "NOT ARMED" not in lines and "ANCHOR.md" in lines, lines)
    finally:
        sandbox.cleanup()


# ------------------------------------------------------------------ Finding B


def test_infra_message_names_recorded_clone(p: Probe) -> None:
    root = Path(tempfile.mkdtemp(prefix="ulthar-origin-msg-"))
    try:
        deploy_dir = root / "deploy"
        deploy_dir.mkdir()
        clone = root / "src" / "ULTHAR"
        clone.mkdir(parents=True)
        (deploy_dir / paths_mod.DEPLOY_ORIGIN_FILE).write_text(str(clone) + "\n", encoding="utf-8")

        msg = dispatch_mod._infra_block_message("config.json is unreadable", "boom",
                                                "PreToolUse", deploy_dir)
        p.check("infra message: names the recorded clone", str(clone) in msg, msg)
        p.check("infra message: redeploy/diagnose run the CLONE's installer",
                str(clone / "hooks" / "install.py") in msg, msg)
        p.check("infra message: never names the deployed installer",
                str(deploy_dir / "install.py") not in msg, msg)
        p.check("infra message: never names the hardcoded ~/.claude/skills/ulthar",
                "skills/ulthar" not in msg.replace("\\", "/"), msg)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_infra_message_says_unknown_when_unrecorded(p: Probe) -> None:
    root = Path(tempfile.mkdtemp(prefix="ulthar-origin-unknown-"))
    try:
        deploy_dir = root / "deploy"
        deploy_dir.mkdir()
        msg = dispatch_mod._infra_block_message("config.json is unreadable", "boom",
                                                "PreToolUse", deploy_dir)
        p.check("infra message (no record): says the master is unknown",
                "clone you installed from" in msg, msg)
        p.check("infra message (no record): still never invents ~/.claude/skills/ulthar",
                "skills/ulthar" not in msg.replace("\\", "/"), msg)
        p.check("infra message (no record): still never names the deployed installer",
                str(deploy_dir / "install.py") not in msg, msg)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_deployed_installer_refuses(p: Probe) -> None:
    """A copy of install.py sitting in a deploy dir must refuse every mode rather
    than deploying ~/.claude over itself and printing 'Deployed'."""
    root = Path(tempfile.mkdtemp(prefix="ulthar-origin-refuse-"))
    try:
        fake_deploy = root / "home" / ".claude" / "hooks"
        fake_deploy.mkdir(parents=True)
        clone = root / "src" / "ULTHAR"
        (clone / "hooks").mkdir(parents=True)
        shutil.copy2(HOOKS_DIR / "install.py", fake_deploy / "install.py")
        shutil.copytree(HOOKS_DIR / "lib", fake_deploy / "lib")
        (fake_deploy / paths_mod.DEPLOY_ORIGIN_FILE).write_text(str(clone) + "\n", encoding="utf-8")

        env = dict(os.environ)
        env["HOME"] = env["USERPROFILE"] = str(root / "home")
        (root / "home" / ".claude").mkdir(parents=True, exist_ok=True)
        for mode in ([], ["--verify"], ["--uninstall"]):
            proc = subprocess.run([sys.executable, str(fake_deploy / "install.py"), *mode],
                                  capture_output=True, timeout=60, env=env)
            out = proc.stdout.decode("utf-8", "replace")
            p.check(f"deployed install.py {mode or ['(deploy)']}: refuses, exit 1",
                    proc.returncode == 1 and "REFUSING" in out, f"rc={proc.returncode} out={out!r}")
            p.check(f"deployed install.py {mode or ['(deploy)']}: names the real master",
                    str(clone / "hooks" / "install.py") in out, out)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_master_installer_still_runs(p: Probe) -> None:
    """The refusal must not fire for a master deploying into an arbitrary dir --
    that is the ordinary install, and it is what writes the record."""
    root = Path(tempfile.mkdtemp(prefix="ulthar-origin-deploy-"))
    try:
        with redirected_home(root):
            reason = deploy_skip_reason()
            if reason:
                p.skip("master deploy: succeeds / records the master / prints the anchor line",
                       reason)
                return
            deploy_dir = root / "deploy"
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = install_mod.main(["install.py", "--deploy-dir", str(deploy_dir)])
            out = buf.getvalue()
            p.check("master deploy: succeeds", rc == 0, out)
            recorded = paths_mod.deployed_from(deploy_dir)
            p.check("master deploy: records this master root", recorded == SKILL_ROOT, repr(recorded))
            p.check("master deploy: prints the anchor line", "ANCHOR:" in out, out)

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                install_mod.main(["install.py", "--uninstall", "--deploy-dir", str(deploy_dir)])
            p.check("uninstall: removes the record it wrote",
                    not (deploy_dir / paths_mod.DEPLOY_ORIGIN_FILE).exists(), buf.getvalue())
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_deploy_guard_message_points_at_the_clone(p: Probe) -> None:
    sandbox = SandboxCtx()
    try:
        clone = sandbox.root / "src" / "ULTHAR"
        clone.mkdir(parents=True)
        (sandbox.deploy_dir / paths_mod.DEPLOY_ORIGIN_FILE).write_text(
            str(clone) + "\n", encoding="utf-8")
        payload = {"tool_name": "Edit", "cwd": str(sandbox.cwd),
                   "tool_input": {"file_path": str(sandbox.deploy_dir / "guards" / "commander.py")}}
        result = deploy_guard.run(payload, sandbox.cfg, sandbox.build(payload))
        p.check("deploy guard: still blocks", result.exit_code == 2)
        p.check("deploy guard: names the recorded clone's hooks dir",
                paths_mod.to_posix(str(clone / "hooks")) in result.stderr, result.stderr)
        p.check("deploy guard: no hardcoded ~/.claude/skills/ulthar",
                "skills/ulthar" not in result.stderr.replace("\\", "/"), result.stderr)

        (sandbox.deploy_dir / paths_mod.DEPLOY_ORIGIN_FILE).unlink()
        result = deploy_guard.run(payload, sandbox.cfg, sandbox.build(payload))
        p.check("deploy guard (no record): says which clone rather than naming a wrong one",
                sandbox.cfg["deploy_guard"]["master_unknown_label"] in result.stderr,
                result.stderr)
    finally:
        sandbox.cleanup()


def test_deploy_guard_protects_the_origin_record(p: Probe) -> None:
    """The record itself must be as unwritable as the files it points at.

    It decides which directory skill_anchor resolves ANCHOR.md from and which
    path every recovery message names, so a direct Write/Edit of it can redirect
    the anchor at an arbitrary file or falsify the instructions a locked-out
    reader is given -- with nothing else on disk contradicting it. It is not
    `.py`, so `guarded_extensions` never reached it; it is covered by
    `guarded_names` (config.json deploy_guard._note_deployed_from). Fails
    without that entry: the guard returns ALLOW."""
    sandbox = SandboxCtx()
    try:
        clone = sandbox.root / "src" / "ULTHAR"
        clone.mkdir(parents=True)
        record = sandbox.deploy_dir / paths_mod.DEPLOY_ORIGIN_FILE
        record.write_text(str(clone) + "\n", encoding="utf-8")
        for tool in ("Write", "Edit"):
            payload = {"tool_name": tool, "cwd": str(sandbox.cwd),
                       "tool_input": {"file_path": str(record)}}
            result = deploy_guard.run(payload, sandbox.cfg, sandbox.build(payload))
            p.check(f"deploy guard: {tool} of {paths_mod.DEPLOY_ORIGIN_FILE} is BLOCKED",
                    result.exit_code == 2, f"exit={result.exit_code} stderr={result.stderr!r}")
    finally:
        sandbox.cleanup()


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    p = Probe()
    print("== Finding A: an ordinary clone's anchor ==")
    test_anchor_from_recorded_clone(p)
    test_skills_root_still_wins(p)
    test_fallback_is_self_only(p)
    test_install_reports_anchor_state(p)

    print("\n== Finding B: messages that name a path the reader must act on ==")
    test_infra_message_names_recorded_clone(p)
    test_infra_message_says_unknown_when_unrecorded(p)
    test_deployed_installer_refuses(p)
    test_master_installer_still_runs(p)
    test_deploy_guard_message_points_at_the_clone(p)
    test_deploy_guard_protects_the_origin_record(p)

    print(f"\nRESULT: {p.passed} passed, {p.failed} failed, {p.skipped} skipped")
    return 1 if p.failed else 0


if __name__ == "__main__":
    sys.exit(main())
