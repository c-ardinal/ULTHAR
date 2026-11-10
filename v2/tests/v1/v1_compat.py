"""Runs the v1 guard tests (hooks/tests at 971632f) against the v2 dispatcher.

Imported first by every ported test module. It makes the v1 import names resolve to
v2 code, unchanged test bodies included:
  * `lib`, `lib.<m>`   -> ulthar.lib (the ported library, same objects);
  * `guards.<id>`      -> a proxy of ulthar.guards.<id> whose `run(payload, cfg, ctx)`
    goes through the v2 dispatcher's guard invocation (ulthar.v1dispatch.invoke_guard:
    v1's exception / result-validation rules), every other attribute delegated;
  * DISPATCH_ARGV      -> the v2 hook entry, for the end-to-end subprocess cases.
It also points ULTHAR_RUNTIME_DB at a temp file (unless already set) so no test run
writes the real ~/.claude/ulthar/runtime.db.
"""
from __future__ import annotations

import importlib
import os
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
from ulthar import v1dispatch  # noqa: E402

PACKAGE_DIR = V2_ROOT / "ulthar"
CONFIG_PATH = PACKAGE_DIR / "config.json"
DISPATCH_ARGV = [str(PACKAGE_DIR), "hook"]
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
