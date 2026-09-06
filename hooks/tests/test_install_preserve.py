#!/usr/bin/env python3
"""hooks/tests/test_install_preserve.py -- install.py must never delete a file it
did not deploy.

Regression cover for the review finding "Deployment and uninstall can delete
unrelated third-party files": `lib/` and `guards/` used to be moved aside
wholesale (deploy) and rmtree'd wholesale (uninstall), so any foreign file
already living in either directory was destroyed -- contrary to the promise
repeated in install.py's docstring, hooks/README.md and the published README.

Three later findings live here too, because all three are consequences of that
same preserve-what-we-did-not-deploy fix or of the swap it introduced:

  - a LINK at a path ULTHAR owns. Seeding the staging tree from the existing
    deployment preserves symlinks as symlinks (deliberately), and the overlay
    copy then writes THROUGH one, overwriting a file outside the deploy tree.
    Deploy must refuse before writing anything.
  - the tree swap is TWO renames and cannot be atomic on Windows; if the second
    fails the destination must be restored, not left absent (a live hook that
    finds no `lib/` fails closed and locks the operator out).
  - `--register-settings` refusing the legacy FOUR-event settings.json it exists
    to migrate.

Everything here runs against a TEMP deploy dir via --deploy-dir. It never
touches ~/.claude/hooks, and it never READS the real home either: install.py has
no payload and so no `_home_override` seam, so every in-process
`install_mod.main(...)` runs inside `conftest.redirected_home()`, which points
HOME/USERPROFILE and the process cwd at this test's own temp tree (the technique
test_contract.py uses for its install.py subprocesses). Without it the user
overlay, the active-skills marker and settings.json are whatever this machine
happens to have -- the same machine-dependent class this suite exists to fix.

A deploy install.py REFUSES (a required dependency missing under the pinned
interpreter) is reported as SKIP, not FAIL: "this machine lacks psutil" and "the
behaviour under test is broken" must not arrive as the same result.

    python tests/test_install_preserve.py
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
HOOKS_DIR = TESTS_DIR.parent

sys.path.insert(0, str(HOOKS_DIR))
sys.path.insert(0, str(TESTS_DIR))

import install as install_mod  # noqa: E402
from conftest import deploy_skip_reason, redirected_home  # noqa: E402


class Probe:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self.skipped = 0

    def skip(self, name: str, reason: str) -> None:
        """Counted apart from passed AND failed: a check that did not run is
        neither evidence of correctness nor of a defect."""
        self.skipped += 1
        print(f"SKIP {name}\n       {reason}")

    def check(self, name: str, cond: bool, detail: str = "") -> None:
        if cond:
            self.passed += 1
            print(f"OK   {name}")
        else:
            self.failed += 1
            print(f"FAIL {name}" + (f"\n       {detail}" if detail else ""))


# Plausible foreign content: another tool's hook helper, and a hand-written guard
# a user dropped next to ULTHAR's. Both live INSIDE a directory ULTHAR manages,
# which is the whole point -- a file next to them at the deploy-dir root was
# never at risk.
FOREIGN = {
    "lib/vendor_state.py": "# another tool's shared helper\nVALUE = 42\n",
    "guards/acme_guard.py": "# a user's own guard\ndef run(payload, cfg, ctx):\n    return None\n",
    "lib/notes.txt": "hand-written note under lib/\n",
}


def _seed(deploy_dir: Path) -> None:
    for rel, body in FOREIGN.items():
        p = deploy_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")


def _check_foreign_intact(p: Probe, deploy_dir: Path, phase: str) -> None:
    for rel, body in FOREIGN.items():
        f = deploy_dir / rel
        p.check(
            f"{phase}: foreign {rel} survives",
            f.is_file() and f.read_text(encoding="utf-8") == body,
            f"{f} exists={f.exists()}",
        )


def _make_link(link: Path, target: Path) -> str | None:
    """Creates the strongest link this machine is allowed to create at `link`,
    or returns None when the OS refuses.

    A plain symlink needs SeCreateSymbolicLinkPrivilege (Developer Mode) on
    Windows and raises WinError 1314 without it, which would silently turn this
    regression into a no-op. A directory JUNCTION needs no privilege and
    redirects writes identically, so it is the fallback -- and it is exactly why
    install.py's check uses `os.path.isjunction` as well as `is_symlink`."""
    try:
        os.symlink(target, link, target_is_directory=target.is_dir())
        return "symlink"
    except (OSError, NotImplementedError):
        pass
    if target.is_dir():
        try:
            import _winapi
            _winapi.CreateJunction(str(target), str(link))
            return "junction"
        except (ImportError, AttributeError, OSError):
            return None
    return None


def test_link_at_an_owned_path_is_refused(p: Probe) -> None:
    """A link occupying a path ULTHAR ships must stop the deploy before it writes.

    Without the check, `copy_deploy` seeds staging from the existing deployment
    keeping links as links, then overlays with `shutil.copy2`, which OPENS the
    link and truncates its TARGET -- a file outside the deploy directory that
    ULTHAR never deployed and was never asked to touch.

    Refusing rather than replacing: replacing deletes a link this installer did
    not deploy, breaking the same promise the rest of this file keeps, and only
    the operator knows why the link is there. A refusal that names `path ->
    target` before writing a single byte is the outcome they can act on."""
    root = Path(tempfile.mkdtemp(prefix="ulthar-install-link-"))
    stack = contextlib.ExitStack()
    stack.enter_context(redirected_home(root))
    try:
        reason = deploy_skip_reason()
        if reason:
            p.skip("link at an owned path is refused", reason)
            return
        deploy_dir = root / "deploy"
        deploy_dir.mkdir()
        install_mod.main(["install.py", "--deploy-dir", str(deploy_dir)])

        # (a) the literal reported shape: a link at a FILE the copy set ships.
        outside = root / "outside.txt"
        outside.write_text("OUTSIDE -- must never be written through\n", encoding="utf-8")
        victim = deploy_dir / "lib" / "ctx.py"
        victim.unlink()
        kind = _make_link(victim, outside)
        if kind is None:
            print("SKIP file-link case: this machine refuses to create a file symlink "
                  "(no SeCreateSymbolicLinkPrivilege / Developer Mode). The directory "
                  "case below exercises the same check.")
            shutil.copy2(SKILL_ROOT_LIB_CTX, victim)
        else:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = install_mod.main(["install.py", "--deploy-dir", str(deploy_dir)])
            out = buf.getvalue()
            p.check(f"link ({kind}) at lib/ctx.py: deploy REFUSES",
                    code == 1 and "REFUSING TO DEPLOY" in out, out)
            p.check(f"link ({kind}) at lib/ctx.py: the outside target is untouched",
                    outside.read_text(encoding="utf-8").startswith("OUTSIDE"),
                    outside.read_text(encoding="utf-8"))
            victim.unlink()
            shutil.copy2(SKILL_ROOT_LIB_CTX, victim)

        # (b) a link at the tree ROOT, which redirects the whole subtree the same
        # way and needs no privilege to create (junction). Also the case where
        # silently proceeding would orphan the operator's linked directory.
        ext_dir = root / "external-guards"
        ext_dir.mkdir()
        (ext_dir / "sentinel.txt").write_text("KEEP\n", encoding="utf-8")
        shutil.rmtree(deploy_dir / "guards")
        kind = _make_link(deploy_dir / "guards", ext_dir)
        p.check("precondition: a directory link could be created", kind is not None)
        if kind is not None:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = install_mod.main(["install.py", "--deploy-dir", str(deploy_dir)])
            out = buf.getvalue()
            p.check(f"link ({kind}) at guards/: deploy REFUSES",
                    code == 1 and "REFUSING TO DEPLOY" in out, out)
            p.check(f"link ({kind}) at guards/: the message names the path",
                    "guards" in out and str(ext_dir) in out, out)
            p.check(f"link ({kind}) at guards/: the link and its target are untouched",
                    install_mod._is_link(deploy_dir / "guards"), out)
            p.check(f"link ({kind}) at guards/: outside content survives",
                    (ext_dir / "sentinel.txt").read_text(encoding="utf-8") == "KEEP\n")
            # Detected BEFORE anything is staged: no half-deploy left behind.
            p.check("refusal happens before staging: no .tmp-deploy artefacts",
                    not list(deploy_dir.glob("*.tmp-deploy")),
                    str(list(deploy_dir.glob("*.tmp-deploy"))))
            os.rmdir(deploy_dir / "guards")
    finally:
        stack.close()  # restores cwd BEFORE rmtree -- Windows will not delete the cwd
        shutil.rmtree(root, ignore_errors=True)


def test_failed_tree_swap_restores_the_previous_tree(p: Probe) -> None:
    """`os.replace` will not rename over an existing directory on Windows, so a
    tree swap is necessarily TWO renames (old aside, new in) and cannot be made
    atomic. The existing regression in test_contract.py simulates a failure
    BEFORE the first rename, where nothing has moved yet. This one fails the
    SECOND rename -- the only point at which the destination is already gone --
    and pins that the previous tree is put back rather than left absent. That
    matters beyond tidiness here: a hook firing with no `lib/` fails closed, so
    an unrecovered gap locks the operator out of their own tooling."""
    root = Path(tempfile.mkdtemp(prefix="ulthar-install-swap-"))
    stack = contextlib.ExitStack()
    stack.enter_context(redirected_home(root))
    try:
        reason = deploy_skip_reason()
        if reason:
            p.skip("failed tree swap restores the previous tree", reason)
            return
        deploy_dir = root / "deploy"
        deploy_dir.mkdir()
        install_mod.main(["install.py", "--deploy-dir", str(deploy_dir)])
        before = sorted(q.name for q in (deploy_dir / "lib").iterdir())

        real_replace = os.replace

        def flaky(src, dst, *a, **kw):
            if Path(src).name == "lib.tmp-deploy":
                raise OSError("simulated failure of the second rename")
            return real_replace(src, dst, *a, **kw)

        os.replace = flaky
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = install_mod.main(["install.py", "--deploy-dir", str(deploy_dir)])
            out = buf.getvalue()
        finally:
            os.replace = real_replace

        p.check("failed swap: reported as a failure, not silent success",
                code == 1 and "DEPLOY FAILED" in out, out)
        p.check("failed swap: lib/ is BACK, not missing",
                (deploy_dir / "lib" / "ctx.py").is_file(),
                f"lib exists={(deploy_dir / 'lib').exists()}")
        p.check("failed swap: the restored lib/ is the complete previous tree",
                (deploy_dir / "lib").exists()
                and sorted(q.name for q in (deploy_dir / "lib").iterdir()) == before)
        p.check("failed swap: no .old-deploy left stranded",
                not list(deploy_dir.glob("*.old-deploy")),
                str(list(deploy_dir.glob("*.old-deploy"))))
    finally:
        stack.close()
        shutil.rmtree(root, ignore_errors=True)


def test_register_settings_migrates_the_legacy_four_event_state(p: Probe) -> None:
    """The helper exists to migrate a PRE-cutover settings.json, and the real one
    has FOUR dispatch.ps1 registrations: the three current `dispatch.events` plus
    the `PostCompact` one PE-20 removed from the routing table while deliberately
    LEAVING the settings.json entry alone (an owner-gated file).

    The old check required `changed == len(expected_events)`, so that exact input
    produced four replacements, failed `4 != 3`, and was refused -- the helper
    could not handle the only state it was written for, and its own regression
    fabricated the post-migration three-event shape instead.

    Send-back (round 2, MEDIUM): the extra registration used to be REWRITTEN to
    dispatch.py form too, called "a harmless no-op", and the command exited 0
    VERIFIED -- but an unrouted dispatch.py registration is exactly what
    --verify's reverse check now fails on, so a "successful" migration handed
    back a state --verify immediately failed. Now it is left COMPLETELY
    untouched (still dispatch.ps1, byte-identical), reported via `LEGACY
    EVENT:`, and the command itself exits non-zero -- the same verdict --verify
    reaches on this exact state."""
    import subprocess

    root = Path(tempfile.mkdtemp(prefix="ulthar-install-legacy-"))
    try:
        deploy_dir = root / "deploy"
        home_dir = root / "home"
        (home_dir / ".claude").mkdir(parents=True)

        def run(args: list[str]) -> tuple[int, str]:
            env = dict(os.environ)
            env["USERPROFILE"] = str(home_dir)
            env["HOME"] = str(home_dir)
            proc = subprocess.run([sys.executable, str(HOOKS_DIR / "install.py")] + args,
                                  capture_output=True, timeout=60, env=env)
            return proc.returncode, (proc.stdout + proc.stderr).decode("utf-8", "replace")

        # Detected in the SAME redirected env the subprocesses below run in --
        # detecting outside it could disagree with the deploy it predicts (on
        # POSIX a redirected HOME hides user-site packages).
        with redirected_home(root):
            reason = deploy_skip_reason()
        if reason:
            p.skip("legacy four-event settings.json is migrated", reason)
            return
        rc, out = run(["--deploy-dir", str(deploy_dir)])
        p.check("legacy migration: deploy succeeds first (precondition)", rc == 0, out)
        if rc != 0:
            return

        with open(install_mod.SKILL_ROOT / "config.json", "r", encoding="utf-8-sig") as fh:
            cfg = json.load(fh)
        expected_events = sorted(cfg.get("dispatch", {}).get("events", {}).keys())
        legacy_events = expected_events + ["PostCompact"]
        original_postcompact_cmd = f'powershell -File "dispatch.ps1" -Event PostCompact'

        settings_path = root / "settings.json"
        settings_path.write_text(json.dumps({"hooks": {
            ev: [{"matcher": "*", "hooks": [
                {"type": "command", "command": (
                    original_postcompact_cmd if ev == "PostCompact"
                    else f'powershell -File "dispatch.ps1" -Event {ev}')}
            ]}] for ev in legacy_events
        }}), encoding="utf-8")

        rc, out = run(["--register-settings", "--confirm-parity-passed",
                       "--settings-path", str(settings_path), "--deploy-dir", str(deploy_dir)])
        p.check("legacy migration: the four-event pre-cutover state exits NON-ZERO "
                "(a leftover legacy event is never a clean cutover)",
                rc != 0, out)
        p.check("legacy migration: the extra registration is reported, not silent",
                "LEGACY EVENT: PostCompact is registered in settings.json but "
                "config.dispatch.events does not route it; ULTHAR will never run it. "
                "Remove the entry (this tool does not edit entries it did not write)."
                in out, out)

        written = json.loads(settings_path.read_text(encoding="utf-8"))
        cmds = {ev: written["hooks"][ev][0]["hooks"][0]["command"] for ev in legacy_events}
        p.check("legacy migration: every EXPECTED event is now a dispatch.py registration",
                all("dispatch.py" in cmds[ev] and "dispatch.ps1" not in cmds[ev]
                    for ev in expected_events),
                str(cmds))
        p.check("legacy migration: PostCompact is left COMPLETELY untouched (byte-identical)",
                cmds["PostCompact"] == original_postcompact_cmd, cmds["PostCompact"])
        # NOTE: PostCompact stays dispatch.ps1-form here, which --verify's
        # reverse check (deliberately, per round-1 scope) never flags -- only
        # a py registration resolving to THIS deployment's dispatch.py counts.
        # The LEGACY EVENT message above is worded to be true regardless of
        # form, so it never claims --verify will independently agree; the
        # ALREADY-py orphan case where it in fact does agree is covered by
        # test_register_settings_leaves_a_py_orphan_untouched below.
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_register_settings_leaves_a_py_orphan_untouched(p: Probe) -> None:
    """Send-back (round 2): pre-state where the three expected events are still
    dispatch.ps1 (need migrating) but PostCompact is ALREADY a dispatch.py
    registration pointing at THIS deployment (e.g. a prior manual edit or an
    older register-settings run, from before a route was removed). This
    registration was never a candidate for rewriting either before or after
    the round-2 fix (it does not contain "dispatch.ps1"), so
    `--register-settings` leaves it byte-identical either way -- what changes
    is that it is now DETECTED and REPORTED, and the command exits non-zero
    instead of silently claiming VERIFIED.

    Because this orphan already resolves to THIS deployment's dispatch.py,
    it is exactly the shape --verify's reverse check flags: running --verify
    against the resulting settings.json reports the SAME line, and removing
    the orphaned entry makes --verify (and --register-settings, on a fresh
    ps1-only settings.json) PASS."""
    import subprocess

    root = Path(tempfile.mkdtemp(prefix="ulthar-install-py-orphan-"))
    try:
        deploy_dir = root / "deploy"
        home_dir = root / "home"
        (home_dir / ".claude").mkdir(parents=True)

        def run(args: list[str]) -> tuple[int, str]:
            env = dict(os.environ)
            env["USERPROFILE"] = str(home_dir)
            env["HOME"] = str(home_dir)
            proc = subprocess.run([sys.executable, str(HOOKS_DIR / "install.py")] + args,
                                  capture_output=True, timeout=60, env=env)
            return proc.returncode, (proc.stdout + proc.stderr).decode("utf-8", "replace")

        with redirected_home(root):
            reason = deploy_skip_reason()
        if reason:
            p.skip("register-settings leaves a py orphan untouched", reason)
            return
        rc, out = run(["--deploy-dir", str(deploy_dir)])
        p.check("py orphan: deploy succeeds first (precondition)", rc == 0, out)
        if rc != 0:
            return

        with open(install_mod.SKILL_ROOT / "config.json", "r", encoding="utf-8-sig") as fh:
            cfg = json.load(fh)
        expected_events = sorted(cfg.get("dispatch", {}).get("events", {}).keys())
        dispatch_py = deploy_dir / "dispatch.py"
        orphan_cmd = f'"{sys.executable}" "{dispatch_py}" --event PostCompact'

        # --verify hardcodes <home>/.claude/settings.json (no --settings-path
        # seam) -- write there so both commands see the SAME file.
        settings_path = home_dir / ".claude" / "settings.json"

        def write_settings(include_orphan: bool) -> None:
            hooks = {ev: [{"matcher": "*", "hooks": [
                {"type": "command", "command": f'powershell -File "dispatch.ps1" -Event {ev}'}
            ]}] for ev in expected_events}
            if include_orphan:
                hooks["PostCompact"] = [{"matcher": "*", "hooks": [
                    {"type": "command", "command": orphan_cmd}]}]
            settings_path.write_text(json.dumps({"hooks": hooks}), encoding="utf-8")

        write_settings(include_orphan=True)
        rc, out = run(["--register-settings", "--confirm-parity-passed",
                       "--settings-path", str(settings_path), "--deploy-dir", str(deploy_dir)])
        p.check("py orphan: --register-settings exits NON-ZERO", rc != 0, out)
        p.check("py orphan: the LEGACY EVENT line is printed",
                "LEGACY EVENT: PostCompact is registered in settings.json but "
                "config.dispatch.events does not route it; ULTHAR will never run it. "
                "Remove the entry (this tool does not edit entries it did not write)."
                in out, out)

        written = json.loads(settings_path.read_text(encoding="utf-8"))
        p.check("py orphan: left byte-identical (never rewritten, never touched)",
                written["hooks"]["PostCompact"][0]["hooks"][0]["command"] == orphan_cmd,
                written["hooks"]["PostCompact"])
        p.check("py orphan: the three expected events are still migrated to dispatch.py",
                all("dispatch.py" in written["hooks"][ev][0]["hooks"][0]["command"]
                    for ev in expected_events),
                str(written["hooks"]))

        # --verify (same settings.json, via the redirected home) reports the
        # SAME reverse-check line and FAILs -- register-settings' non-zero
        # exit was not a quieter verdict than --verify's own.
        rc, out = run(["--verify", "--deploy-dir", str(deploy_dir)])
        p.check("py orphan: --verify FAILs on the resulting state",
                rc != 0 and "VERIFY: FAIL" in out, out)
        p.check("py orphan: --verify reports the exact reverse-check line",
                "SETTINGS: PostCompact is registered in settings.json but "
                "config.dispatch.events does not route it" in out, out)

        # Removing the orphan (from the ALREADY-migrated file, not resetting
        # the three now-correctly-py-form events back to ps1): --verify PASSes.
        del written["hooks"]["PostCompact"]
        settings_path.write_text(json.dumps(written), encoding="utf-8")
        rc, out = run(["--verify", "--deploy-dir", str(deploy_dir)])
        p.check("py orphan removed: --verify now PASSes", rc == 0 and "VERIFY: PASS" in out, out)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_register_settings_clean_state_and_foreign_hook_still_pass(p: Probe) -> None:
    """Round-2 send-back review caught two gaps in the round-2 fix itself:

    (1) Flipping the four-event legacy test to assert non-zero removed the
    ONLY `rc == 0` / VERIFIED assertion `--register-settings` had -- the
    ordinary, no-legacy-leftover cutover must still exit 0 and print VERIFIED.

    (2) `legacy_found`'s first cut used DISPATCH_TOKENS (substring match) --
    broader than the reverse check it is meant to agree with. A FOREIGN hook
    under an unrouted event whose command merely happens to contain the
    substring "dispatch.py" (someone else's tool, or a DIFFERENT deployment)
    would have made `--register-settings` print LEGACY EVENT and exit 1 while
    `--verify` stays silent on it -- the exact class of disagreement this
    whole send-back exists to close, just pointed the other way. Fixed to
    match the reverse check's own criterion (ps1, or py resolving to THIS
    deploy_dir's dispatch.py); this test pins a foreign hook under
    PostCompact pointing at a DIFFERENT deployment's dispatch.py and confirms
    it is silently ignored, same as `_check_extra_dispatcher_registrations`
    itself ignores it."""
    import subprocess

    root = Path(tempfile.mkdtemp(prefix="ulthar-install-clean-state-"))
    try:
        deploy_dir = root / "deploy"
        home_dir = root / "home"
        (home_dir / ".claude").mkdir(parents=True)

        def run(args: list[str]) -> tuple[int, str]:
            env = dict(os.environ)
            env["USERPROFILE"] = str(home_dir)
            env["HOME"] = str(home_dir)
            proc = subprocess.run([sys.executable, str(HOOKS_DIR / "install.py")] + args,
                                  capture_output=True, timeout=60, env=env)
            return proc.returncode, (proc.stdout + proc.stderr).decode("utf-8", "replace")

        with redirected_home(root):
            reason = deploy_skip_reason()
        if reason:
            p.skip("register-settings clean state and foreign hook still pass", reason)
            return
        rc, out = run(["--deploy-dir", str(deploy_dir)])
        p.check("clean state: deploy succeeds first (precondition)", rc == 0, out)
        if rc != 0:
            return

        with open(install_mod.SKILL_ROOT / "config.json", "r", encoding="utf-8-sig") as fh:
            cfg = json.load(fh)
        expected_events = sorted(cfg.get("dispatch", {}).get("events", {}).keys())
        other_dispatch_py = root / "other-deploy" / "dispatch.py"
        foreign_cmd = f'"{sys.executable}" "{other_dispatch_py}" --event PostCompact'

        settings_path = root / "settings.json"
        settings_path.write_text(json.dumps({"hooks": {
            **{ev: [{"matcher": "*", "hooks": [
                {"type": "command", "command": f'powershell -File "dispatch.ps1" -Event {ev}'}
            ]}] for ev in expected_events},
            "PostCompact": [{"matcher": "*", "hooks": [
                {"type": "command", "command": foreign_cmd}]}],
        }}), encoding="utf-8")

        rc, out = run(["--register-settings", "--confirm-parity-passed",
                       "--settings-path", str(settings_path), "--deploy-dir", str(deploy_dir)])
        p.check("clean state + foreign hook: --register-settings still exits 0",
                rc == 0, out)
        p.check("clean state + foreign hook: still prints VERIFIED",
                "VERIFIED" in out, out)
        p.check("clean state + foreign hook: NO LEGACY EVENT line "
                "(a foreign/other-deployment hook is not ULTHAR's to report)",
                "LEGACY EVENT" not in out, out)

        written = json.loads(settings_path.read_text(encoding="utf-8"))
        p.check("clean state + foreign hook: the foreign PostCompact hook is untouched",
                written["hooks"]["PostCompact"][0]["hooks"][0]["command"] == foreign_cmd,
                written["hooks"]["PostCompact"])
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_reverse_check_unit(p: Probe) -> None:
    """Unit-level cover for `install_mod._check_extra_dispatcher_registrations`,
    the reverse half of F-5's registration check: only entries whose command
    resolves to THIS deployment's dispatch.py count -- someone else's hook
    under the same event name, or a hook pointing at a DIFFERENT deployment's
    dispatch.py, is not ULTHAR's to report."""
    deploy_dir = Path(tempfile.mkdtemp(prefix="ulthar-reverse-unit-"))
    try:
        dispatch_py = deploy_dir / "dispatch.py"
        dispatch_py.write_text("# stub\n", encoding="utf-8")
        other_dispatch_py = deploy_dir.parent / "other-deploy" / "dispatch.py"
        python_exe = "C:/pinned-python/python.exe"
        cfg = {"paths": {"python_exe": python_exe},
               "dispatch": {"events": {"PreToolUse": []}}}
        good_cmd = f'"{python_exe}" "{dispatch_py}" --event PostCompact'

        # An event NOT in dispatch.events, registered to THIS deployment's
        # dispatch.py -> reported, with the wording --verify prints.
        extra = {"hooks": {"PostCompact": [{"matcher": "*", "hooks": [
            {"type": "command", "command": good_cmd}
        ]}]}}
        problems = install_mod._check_extra_dispatcher_registrations(extra, cfg, deploy_dir)
        p.check(
            "reverse unit: an undeclared event routed to THIS deploy's dispatch.py is reported",
            problems == ["PostCompact is registered in settings.json but "
                         "config.dispatch.events does not route it"],
            str(problems),
        )

        # A foreign hook under that SAME event, not pointing at dispatch.py at
        # all -- must not be reported. It is not ULTHAR's hook to comment on.
        foreign_only = {"hooks": {"PostCompact": [{"matcher": "*", "hooks": [
            {"type": "command", "command": "echo ok"}
        ]}]}}
        p.check(
            "reverse unit: a foreign hook under an undeclared event is NOT reported",
            install_mod._check_extra_dispatcher_registrations(foreign_only, cfg, deploy_dir) == [],
        )

        # A hook pointing at ANOTHER deployment's dispatch.py under an
        # undeclared event -- also not this deployment's to report.
        other_deploy_cmd = f'"{python_exe}" "{other_dispatch_py}" --event PostCompact'
        other_deploy = {"hooks": {"PostCompact": [{"matcher": "*", "hooks": [
            {"type": "command", "command": other_deploy_cmd}
        ]}]}}
        p.check(
            "reverse unit: a hook routed to a DIFFERENT deployment's dispatch.py is NOT reported",
            install_mod._check_extra_dispatcher_registrations(other_deploy, cfg, deploy_dir) == [],
        )

        # An event that IS in dispatch.events is the forward check's job, never
        # this one's -- even if additionally malformed, it must not duplicate.
        declared = {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
            {"type": "command", "command": f'"{python_exe}" "{dispatch_py}" --event PreToolUse'}
        ]}]}}
        p.check(
            "reverse unit: a DECLARED event is never reported by the reverse check",
            install_mod._check_extra_dispatcher_registrations(declared, cfg, deploy_dir) == [],
        )

        # Send-back (round 2, LOW): an empty dispatch.events must not make the
        # reverse check list every registered event on top of the forward
        # check's own early "events is empty" failure -- pure noise.
        empty_cfg = {"paths": {"python_exe": python_exe}, "dispatch": {"events": {}}}
        p.check(
            "reverse unit: an EMPTY dispatch.events skips the reverse check entirely "
            "(no noise on top of the forward check's early failure)",
            install_mod._check_extra_dispatcher_registrations(extra, empty_cfg, deploy_dir) == [],
        )

        # Send-back (round 2, MEDIUM fix 2): `_registration_problems` is the ONE
        # shared call point cmd_verify and cmd_register_settings's post-write
        # self-check both use -- it must be the concatenation of forward AND
        # reverse, not either alone (that truthfulness is the whole point of
        # extracting it).
        missing_forward = {"hooks": {}}  # PreToolUse (expected) has NO registration at all
        combined = install_mod._registration_problems(missing_forward, cfg, deploy_dir)
        p.check(
            "registration_problems: includes a FORWARD-only problem (missing PreToolUse)",
            any("PreToolUse" in c and "NO dispatcher registered" in c for c in combined),
            str(combined),
        )
        combined = install_mod._registration_problems(extra, cfg, deploy_dir)
        p.check(
            "registration_problems: includes a REVERSE-only problem (undeclared PostCompact)",
            any("PostCompact is registered in settings.json but "
                "config.dispatch.events does not route it" in c for c in combined),
            str(combined),
        )
    finally:
        shutil.rmtree(deploy_dir, ignore_errors=True)


def test_verify_reports_undeclared_settings_registration(p: Probe) -> None:
    """F-5's registration check was one-directional: every `config.dispatch.events`
    entry needed a settings.json hook, but not the reverse. This is exactly the
    owner's real cleanup scenario -- a `PostCompact` route removed from
    config.json's dispatch.events while the matching settings.json entry
    (still pointed at THIS deployment's dispatch.py) was left behind by hand --
    and `--verify` used to say nothing and print PASS.

    End-to-end against a real deploy and a real settings.json (via
    redirected_home, exactly test_register_settings_migrates_the_legacy_four_event_state's
    technique): a settings.json with every expected event correctly registered
    PLUS one undeclared `PostCompact` entry pointing at the deployed dispatch.py
    must FAIL with the exact message; the same file with a FOREIGN `PostCompact`
    hook (not pointing at dispatch.py at all) must still PASS -- it is not
    ULTHAR's hook to comment on."""
    import subprocess

    root = Path(tempfile.mkdtemp(prefix="ulthar-verify-reverse-"))
    try:
        deploy_dir = root / "deploy"
        home_dir = root / "home"
        (home_dir / ".claude").mkdir(parents=True)

        def run(args: list[str]) -> tuple[int, str]:
            env = dict(os.environ)
            env["USERPROFILE"] = str(home_dir)
            env["HOME"] = str(home_dir)
            proc = subprocess.run([sys.executable, str(HOOKS_DIR / "install.py")] + args,
                                  capture_output=True, timeout=60, env=env)
            return proc.returncode, (proc.stdout + proc.stderr).decode("utf-8", "replace")

        with redirected_home(root):
            reason = deploy_skip_reason()
        if reason:
            p.skip("verify reports an undeclared settings.json registration", reason)
            return

        rc, out = run(["--deploy-dir", str(deploy_dir)])
        p.check("reverse e2e: deploy succeeds first (precondition)", rc == 0, out)
        if rc != 0:
            return

        with open(install_mod.SKILL_ROOT / "config.json", "r", encoding="utf-8-sig") as fh:
            cfg = json.load(fh)
        expected_events = sorted(cfg.get("dispatch", {}).get("events", {}).keys())
        dispatch_py = deploy_dir / "dispatch.py"
        good_cmd = lambda ev: f'"{sys.executable}" "{dispatch_py}" --event {ev}'  # noqa: E731

        settings_path = home_dir / ".claude" / "settings.json"

        # (a) every expected event correctly registered, PLUS an undeclared
        # PostCompact entry pointing at THIS deployment's dispatch.py.
        settings_path.write_text(json.dumps({"hooks": {
            **{ev: [{"matcher": "*", "hooks": [
                {"type": "command", "command": good_cmd(ev)}]}] for ev in expected_events},
            "PostCompact": [{"matcher": "*", "hooks": [
                {"type": "command", "command": good_cmd("PostCompact")}]}],
        }}), encoding="utf-8")
        rc, out = run(["--verify", "--deploy-dir", str(deploy_dir)])
        p.check("reverse e2e: an undeclared PostCompact registration -> VERIFY: FAIL",
                rc != 0 and "VERIFY: FAIL" in out, out)
        p.check("reverse e2e: the exact message is printed",
                "SETTINGS: PostCompact is registered in settings.json but "
                "config.dispatch.events does not route it" in out, out)

        # (b) same expected-event registrations, but PostCompact is a FOREIGN
        # hook that does not point at dispatch.py at all -- must still PASS.
        settings_path.write_text(json.dumps({"hooks": {
            **{ev: [{"matcher": "*", "hooks": [
                {"type": "command", "command": good_cmd(ev)}]}] for ev in expected_events},
            "PostCompact": [{"matcher": "*", "hooks": [
                {"type": "command", "command": "echo ok"}]}],
        }}), encoding="utf-8")
        rc, out = run(["--verify", "--deploy-dir", str(deploy_dir)])
        p.check("reverse e2e: a foreign PostCompact hook -> still VERIFY: PASS",
                rc == 0 and "VERIFY: PASS" in out, out)
        p.check("reverse e2e: no SETTINGS line names the foreign PostCompact hook",
                not any("SETTINGS:" in ln and "PostCompact" in ln for ln in out.splitlines()),
                out)
    finally:
        shutil.rmtree(root, ignore_errors=True)


SKILL_ROOT_LIB_CTX = HOOKS_DIR / "lib" / "ctx.py"


def main() -> int:
    p = Probe()
    # First: each owns its temp tree, so order is free -- and running them here
    # means a SKIP of the preserve block below (a deploy this machine refuses)
    # still reports THEIR outcome instead of returning early past three tests.
    test_link_at_an_owned_path_is_refused(p)
    test_failed_tree_swap_restores_the_previous_tree(p)
    test_register_settings_migrates_the_legacy_four_event_state(p)
    test_register_settings_leaves_a_py_orphan_untouched(p)
    test_register_settings_clean_state_and_foreign_hook_still_pass(p)
    test_reverse_check_unit(p)
    test_verify_reports_undeclared_settings_registration(p)

    root = Path(tempfile.mkdtemp(prefix="ulthar-install-preserve-"))
    stack = contextlib.ExitStack()
    stack.enter_context(redirected_home(root))
    try:
        reason = deploy_skip_reason()
        if reason:
            p.skip("deploy/verify/uninstall preserve foreign files", reason)
            return _report(p)
        deploy_dir = root / "deploy"
        deploy_dir.mkdir()
        _seed(deploy_dir)

        # --- default deploy -------------------------------------------------
        # Exit code is deliberately not asserted: cmd_default also runs the
        # guard-import/smoke checks, whose outcome depends on the machine, not on
        # the property under test. (The one machine dependence that would make
        # every check below vacuous -- a REFUSED deploy -- is reported as a SKIP
        # above rather than left to look like a pass or a defect.)
        install_mod.main(["install.py", "--deploy-dir", str(deploy_dir)])

        _check_foreign_intact(p, deploy_dir, "deploy")
        p.check(
            "deploy: ULTHAR's own files land",
            (deploy_dir / "dispatch.py").is_file()
            and (deploy_dir / "lib" / "ctx.py").is_file()
            and (deploy_dir / "guards" / "tool_discipline.py").is_file(),
        )

        # --- verify reports the survivors instead of failing on them --------
        # Exit code is deliberately not asserted: cmd_verify compares the
        # dispatch.py path registered in <home>/.claude/settings.json against
        # --deploy-dir, and under redirected_home() that file does not exist at
        # all, so SETTINGS can never pass here. Deliberate -- rewriting the live
        # settings.json is not this test's subject, and the EXTRA/DRIFT lines
        # asserted below are produced either way.
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            install_mod.main(["install.py", "--verify", "--deploy-dir", str(deploy_dir)])
        out = buf.getvalue()
        p.check(
            "verify: foreign file is reported as EXTRA",
            any("EXTRA:" in ln and "vendor_state.py" in ln for ln in out.splitlines()),
            out,
        )
        p.check(
            "verify: no DRIFT line names a foreign file",
            not any(
                ln.startswith("DRIFT:") and any(name in ln for name in ("vendor_state", "acme_guard", "notes.txt"))
                for ln in out.splitlines()
            ),
            out,
        )

        # --- uninstall ------------------------------------------------------
        # Re-seeded so this phase fails for its OWN reason, not merely because
        # the deploy phase above already destroyed the fixtures.
        _seed(deploy_dir)
        install_mod.main(["install.py", "--uninstall", "--deploy-dir", str(deploy_dir)])

        _check_foreign_intact(p, deploy_dir, "uninstall")
        leftovers = [
            deploy_dir / "dispatch.py",
            deploy_dir / "config.json",
            deploy_dir / "lib" / "ctx.py",
            deploy_dir / "guards" / "tool_discipline.py",
        ]
        p.check(
            "uninstall: ULTHAR's own files are gone",
            not any(f.exists() for f in leftovers),
            f"still present: {[str(f) for f in leftovers if f.exists()]}",
        )

        # --- uninstall on a clean tree removes the managed dirs entirely -----
        # (nothing foreign left behind => no empty stub directories)
        clean = root / "clean"
        clean.mkdir()
        install_mod.main(["install.py", "--deploy-dir", str(clean)])
        install_mod.main(["install.py", "--uninstall", "--deploy-dir", str(clean)])
        p.check(
            "uninstall: managed dirs removed when nothing foreign remains",
            not (clean / "lib").exists() and not (clean / "guards").exists(),
            f"lib={(clean / 'lib').exists()} guards={(clean / 'guards').exists()}",
        )
    finally:
        stack.close()  # restores cwd BEFORE rmtree -- Windows will not delete the cwd
        shutil.rmtree(root, ignore_errors=True)

    return _report(p)


def _report(p: Probe) -> int:
    print(f"\n{p.passed} passed, {p.failed} failed, {p.skipped} skipped")
    return 1 if p.failed else 0


if __name__ == "__main__":
    sys.exit(main())
