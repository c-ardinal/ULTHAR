"""Runs the v1 guard tests (hooks/tests at 971632f) against the v2 dispatcher.

Imported first by every ported test module. It makes the v1 import names resolve to
v2 code, unchanged test bodies included:
  * `lib`, `lib.<m>`   -> ulthar.lib (the ported library, same objects);
  * `guards.<id>`      -> a proxy of ulthar.guards.<id> whose `run(payload, cfg, ctx)`
    goes through the production entry, ulthar.hook.process (journal replay,
    transcript ingestion, overlay merge, grant validation, the dispatcher's routing
    table, v1's exception/result rules, the events row), in the dispatcher's
    single-guard mode (`--guard <id>`, as v1 did) with the test's cfg and Ctx (frozen
    clock, run_cmd stub) passed through the `loaded` seam. Only when the routing
    table does not send that payload to that guard at all (e.g. a payload with no
    tool_name) does it fall back to calling the guard directly -- and that call is
    counted as unit-level, never as the production entry;
  * DISPATCH_ARGV      -> the v2 hook entry, for the end-to-end subprocess cases.
It also points ULTHAR_RUNTIME_DB at a temp file (unless already set) so no test run
writes the real ~/.claude/ulthar/runtime.db.
"""
from __future__ import annotations

import functools
import importlib
import inspect
import os
from collections import Counter, defaultdict
import sys
import tempfile
import types
from pathlib import Path

V1_TESTS_DIR = Path(__file__).resolve().parent
V2_ROOT = V1_TESTS_DIR.parents[1]
REPO_ROOT = V2_ROOT.parent
if str(V2_ROOT) not in sys.path:
    sys.path.insert(0, str(V2_ROOT))

if not os.environ.get("ULTHAR_RUNTIME_DB"):
    os.environ["ULTHAR_RUNTIME_DB"] = str(
        Path(tempfile.mkdtemp(prefix="ulthar-v1tests-")) / "runtime.db")

import ulthar.lib  # noqa: E402
from ulthar import hook, v1dispatch  # noqa: E402

PACKAGE_DIR = V2_ROOT / "ulthar"
CONFIG_PATH = PACKAGE_DIR / "config.json"
DISPATCH_ARGV = [str(PACKAGE_DIR), "hook"]


def entry_argv() -> list[str]:
    """DISPATCH_ARGV for a subprocess case, counted as a production-entry call."""
    _SPLIT.note_call("entry")
    return list(DISPATCH_ARGV)
PORTED_GUARDS = ("git_discipline", "tool_discipline", "commander", "grant_files")
LIB_MODULES = ("ctx", "gitcmd", "grant", "io", "paths", "procs")


def _alias_lib() -> None:
    sys.modules["lib"] = ulthar.lib
    for name in LIB_MODULES:
        mod = importlib.import_module(f"ulthar.lib.{name}")
        sys.modules[f"lib.{name}"] = mod
        setattr(ulthar.lib, name, mod)


class _GuardProxy(types.ModuleType):
    def __init__(self, guard_id: str):
        super().__init__(f"guards.{guard_id}")
        self._guard_id = guard_id
        self._real = v1dispatch.import_guard(guard_id)

    def run(self, payload, cfg, ctx):
        processed = hook.process(ctx.event, payload, single_guard=self._guard_id,
                                 loaded=(cfg, ctx))
        if self._guard_id in processed.guards_run:
            _SPLIT.note_call("entry")
            return processed.result
        # Not decided by this guard through the entry: either the routing table does
        # not send this payload to it, or the dispatcher blocked before any guard ran
        # (e.g. grant validation). Evaluate the guard itself, and count it as unit.
        _SPLIT.note_call("unit:blocked-before-guard" if processed.result.exit_code == 2
                         else "unit:not-routed")
        return v1dispatch.invoke_guard(self._guard_id, self._real, payload, cfg, ctx, ctx.event)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _alias_guards() -> None:
    pkg = types.ModuleType("guards")
    pkg.__path__ = []
    sys.modules["guards"] = pkg
    for guard_id in PORTED_GUARDS:
        proxy = _GuardProxy(guard_id)
        sys.modules[f"guards.{guard_id}"] = proxy
        setattr(pkg, guard_id, proxy)


_alias_lib()
_alias_guards()


class _Split:
    """Attributes every counted check of the v1 suites to how its guard decision was
    produced: "entry" (hook.process routed the payload to the guard), "unit:not-routed"
    (the guard was called directly because the routing table does not route that
    payload to it), or "unit:no-guard-call" (the check evaluates a lib helper or the
    config, with no hook payload). A check takes the kind of the guard calls made
    since the previous check in the same test function, or the previous check's kind
    when it re-examines the same result. Attribution is per outermost test function."""

    def __init__(self) -> None:
        self.tally: dict[tuple[str, str], Counter] = defaultdict(Counter)
        self.stack: list[tuple[str, str]] = []
        self.pending: str | None = None
        self.last = "unit:no-guard-call"

    def note_call(self, kind: str) -> None:
        if self.pending is None or self.pending == "entry":
            self.pending = kind  # a unit-level call in the same check is sticky

    def note_check(self, module: str) -> None:
        kind = self.pending or self.last
        self.last, self.pending = kind, None
        where = self.stack[0] if self.stack else (module, "main (inline)")
        self.tally[where][kind] += 1

    def wrap(self, module_name: str, name: str, fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            outer = not self.stack
            if outer:
                self.pending, self.last = None, "unit:no-guard-call"
            self.stack.append((module_name, name))
            try:
                return fn(*args, **kwargs)
            finally:
                self.stack.pop()
        return wrapper


_SPLIT = _Split()


def instrument(module) -> None:
    """Called by v2/tests/run_all.py before module.main(): counts each check."""
    name = module.__name__
    probe = getattr(module, "Probe", None)
    if probe is not None:
        original = probe.check

        def check(self, *args, **kwargs):
            _SPLIT.note_check(name)
            return original(self, *args, **kwargs)
        probe.check = check
    if name == "test_git_discipline":  # one counted case per run_case() call, no Probe
        original_case = module.run_case

        def run_case(case):
            try:
                return original_case(case)
            finally:
                _SPLIT.note_check(name)
        module.run_case = _SPLIT.wrap(name, "run_case", run_case)
    for attr, fn in list(vars(module).items()):
        if (inspect.isfunction(fn) and getattr(fn, "__module__", "") == name
                and attr != "main" and not attr.startswith("_")):
            setattr(module, attr, _SPLIT.wrap(name, attr, fn))


def split_report() -> dict:
    by_kind: Counter = Counter()
    functions = []
    for (module, func), kinds in sorted(_SPLIT.tally.items()):
        by_kind.update(kinds)
        functions.append({"module": module, "function": func, **dict(kinds)})
    return {"totals": dict(by_kind), "functions": functions}
