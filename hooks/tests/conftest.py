"""hooks/tests/conftest.py -- shared test fixtures: a Ctx factory pointed at a temp
tree, with a frozen `now()`, and stubbable `run_cmd` / `list_processes`.

CONTRACT.md #1 (Ctx): "Tests construct a Ctx pointing at a temp tree with a frozen
now(), a run_cmd stub and a list_processes stub -- which is what makes the
180-second approval-age check, the git rev-parse branch check, the gh pr list check
and the orphan predicate testable without touching the real machine."

No pytest dependency assumed (the runtime is stdlib + psutil only) -- this module is
plain, importable helper functions any test module (unittest or pytest) can use.
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
HOOKS_DIR = TESTS_DIR.parent
sys.path.insert(0, str(HOOKS_DIR))

from lib import ctx as ctx_mod  # noqa: E402
from lib import io as io_mod  # noqa: E402


@contextlib.contextmanager
def redirected_home(root: Path):
    """Runs a block with HOME/USERPROFILE and the process cwd pointed INSIDE
    `root`, yielding the fake home. Restores both, always.

    install.py has no payload and therefore no `_home_override` seam
    (lib/ctx.py:build_ctx): it resolves the user overlay, the skills root and the
    active-skills marker from `Path.home()` (which reads USERPROFILE on Windows,
    HOME on POSIX) and from `os.getcwd()` (every `${CWD}`-rooted config path).
    An in-process `install_mod.main(...)` left uncontrolled therefore reads the
    REAL ~/.claude/ulthar-overlay.json, ~/.claude/state/active-skills and
    ~/.claude/settings.json, which makes the result depend on the machine running
    the suite -- the same machine-dependence test_contract.py fixed for its own
    subprocess cases with env["HOME"]/env["USERPROFILE"].

    Restoring matters as much as setting: run_all.py imports every test module
    into ONE process, so a leaked env var or cwd would poison later modules. cwd
    is restored before any rmtree of `root` -- Windows will not delete the
    process's own working directory."""
    home = root / "home"
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    saved_env = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE")}
    saved_cwd = os.getcwd()
    os.environ["HOME"] = str(home)
    os.environ["USERPROFILE"] = str(home)
    os.chdir(root)
    try:
        yield home
    finally:
        os.chdir(saved_cwd)
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def deploy_skip_reason() -> str | None:
    """None when a deploy can actually run here; otherwise the reason it cannot.

    `cmd_default` refuses to deploy when `check_dependencies` fails, so on a
    machine where psutil is not importable under the PINNED interpreter every
    deploy-dependent test fails -- reporting "the behaviour under test is broken"
    when the truth is "this machine lacks a dependency". Callers skip instead,
    with this reason printed.

    Deliberately calls install.py's own check rather than `import psutil` here:
    the deploy runs it as a subprocess under `paths.python_exe`, which need not
    be the interpreter running the tests. Call this INSIDE the same
    `redirected_home()` block the deploy will run in -- on POSIX a redirected
    HOME hides user-site packages, so detecting outside it could disagree with
    the deploy it is meant to predict."""
    import install as install_mod  # noqa: PLC0415 -- test-only, keeps conftest import-light

    cfg = load_test_config()
    if not cfg.get("dependencies", {}).get("check_on_deploy"):
        return None
    problems = install_mod.check_dependencies(cfg)
    if not problems:
        return None
    return ("install.py REFUSES to deploy on this machine, so the behaviour under "
            "test never runs: " + "; ".join(problems))


def load_test_config() -> dict:
    """Loads hooks/../config.json (the skill-root master) fresh, bypassing the
    mtime/size cache so tests never see a stale cached copy from a prior case."""
    cfg_path = HOOKS_DIR.parent / "config.json"
    with open(cfg_path, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    ctx_mod._resolve_named_alternations(cfg)
    return cfg


class SandboxCtx:
    """Builds a temp tree matching the layout Ctx expects (state/logs/skills dirs
    under a fake home) and returns a real lib.ctx.Ctx pointed at it, with `now()`,
    `run_cmd` and `list_processes` all overridable."""

    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg if cfg is not None else load_test_config()
        self.root = Path(tempfile.mkdtemp(prefix="ulthar-test-"))
        self.home = self.root / "home"
        self.deploy_dir = self.root / "deploy"
        self.cwd = self.root / "cwd"
        self.temp_dir = self.root / "tmp"
        for d in (self.home, self.deploy_dir, self.cwd, self.temp_dir):
            d.mkdir(parents=True, exist_ok=True)
        (self.home / ".claude" / "state").mkdir(parents=True, exist_ok=True)
        (self.deploy_dir / "logs").mkdir(parents=True, exist_ok=True)
        (self.home / ".claude" / "skills").mkdir(parents=True, exist_ok=True)

        # pins: a fixed past timestamp goes stale relative to real file mtimes,
        # producing a NEGATIVE age for a freshly-created test file -- any check
        # computing `ctx.now() - file.mtime` (e.g. tool_discipline's
        # `_lock_is_stale` mtime-fallback branch) could then never observe
        # "stale", silently making that branch vacuous. Defaults to the real
        # current time; `set_now()` remains available for tests that
        # deliberately want a frozen/specific clock.
        self._frozen_now = datetime.now().astimezone()
        self._run_cmd_stub = lambda argv, kind, cwd=None: io_mod  # placeholder, replaced below
        self._run_cmd_stub = None
        self._list_processes_stub = None

    def set_now(self, dt: datetime) -> None:
        self._frozen_now = dt

    def set_run_cmd(self, fn) -> None:
        self._run_cmd_stub = fn

    def set_list_processes(self, fn) -> None:
        self._list_processes_stub = fn

    def build(self, payload: dict | None = None, event: str = "PreToolUse") -> ctx_mod.Ctx:
        # pins: cwd must come from payload["cwd"] when present, not a hardcoded
        # sandbox default -- cwd matters to commander's outside-cwd exemption,
        # artifact_hygiene's scan root, and Rule 3's `..` resolution
        # (dotdot_base: "process_cwd"), so ignoring it here could make a
        # cwd-dependent case silently test the wrong thing.
        payload = dict(payload or {})
        payload.setdefault("cwd", str(self.cwd))
        resolved_cwd = Path(payload["cwd"])
        ctx = ctx_mod.Ctx(
            home=self.home,
            state_dir=self.home / ".claude" / "state",
            deploy_dir=self.deploy_dir,
            skills_root=self.home / ".claude" / "skills",
            logs_dir=self.deploy_dir / "logs",
            temp_dir=self.temp_dir,
            cwd=resolved_cwd,
            is_windows=(sys.platform.startswith("win")),
            event=event,
            cfg=self.cfg,
            _now_fn=lambda: self._frozen_now,
            _run_cmd_fn=self._run_cmd_stub,
            _list_processes_fn=self._list_processes_stub,
        )
        return ctx

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


def make_ctx(payload: dict | None = None, cfg: dict | None = None, event: str = "PreToolUse"):
    """Convenience one-shot: returns (ctx, sandbox) so a test can still reach
    sandbox.set_run_cmd()/set_list_processes()/set_now() before or after building --
    note build() snapshots the stub at construction time, so set_* calls must happen
    BEFORE build() for a fresh ctx, or call sandbox.build() again."""
    sandbox = SandboxCtx(cfg)
    return sandbox
