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

    The extra registration is REWRITTEN, not deleted: dispatch.py resolves an
    event with no config route to zero rules and allows with no output, so the
    entry becomes a harmless no-op, whereas deleting it would edit an owner-gated
    file beyond what was asked. It is reported so it is not invisible."""
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

        settings_path = root / "settings.json"
        settings_path.write_text(json.dumps({"hooks": {
            ev: [{"matcher": "*", "hooks": [
                {"type": "command", "command": f'powershell -File "dispatch.ps1" -Event {ev}'}
            ]}] for ev in legacy_events
        }}), encoding="utf-8")

        rc, out = run(["--register-settings", "--confirm-parity-passed",
                       "--settings-path", str(settings_path), "--deploy-dir", str(deploy_dir)])
        p.check("legacy migration: the four-event pre-cutover state is ACCEPTED",
                rc == 0 and "VERIFIED" in out, out)
        p.check("legacy migration: the extra registration is reported, not silent",
                "LEGACY EVENT: PostCompact" in out, out)

        written = json.loads(settings_path.read_text(encoding="utf-8"))
        cmds = {ev: written["hooks"][ev][0]["hooks"][0]["command"] for ev in legacy_events}
        p.check("legacy migration: every event is now a dispatch.py registration",
                all("dispatch.py" in c and "dispatch.ps1" not in c for c in cmds.values()),
                str(cmds))
        p.check("legacy migration: PostCompact keeps its own --event argument",
                cmds["PostCompact"].endswith("--event PostCompact"), cmds["PostCompact"])
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
