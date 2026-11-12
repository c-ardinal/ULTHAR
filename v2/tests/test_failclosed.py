"""Fail-closed regressions from review round 5:
  * no cross-process config cache: a planted cache file beside the package changes
    nothing, config.json is parsed and validated on every call;
  * subprocess/signal are imported at module level, as in v1: when they cannot be
    imported, the failure surfaces at guard/runtime load and blocks a PreToolUse
    (AC-4), never mid-run where v1's fail-open rule applies;
  * the one owner-approved exception (hook latency, R1-c): lib/ctx.py imports shutil
    where run_cmd uses it; a guard whose run_cmd cannot import it BLOCKS the PreToolUse
    (ctx.RuntimeImportError), and lib/ + guards/ have no other function-level stdlib
    import that v1 (hooks/) does not have;
  * blob_hash records hash_failed when subprocess cannot be imported.
"""
from __future__ import annotations

import ast
import json
import marshal
import os
import subprocess
import sys
import zlib
from pathlib import Path

from helpers import V2_ROOT, Probe, Sandbox

from ulthar import hook

PACKAGE = V2_ROOT / "ulthar"
CONFIG = PACKAGE / "config.json"

STUB_ALLOW = "from ulthar.lib.io import ALLOW\n\ndef run(payload, cfg, ctx):\n    return ALLOW\n"
BASH = {"session_id": "fc", "tool_name": "Bash", "tool_input": {"command": "ls"}}


class _Env:
    """In-process hook.process with a sandbox db, optional guard stubs and config, and
    one stdlib module made unimportable (sys.modules[name] = None)."""

    def __init__(self, sb: Sandbox, guard_src: str | None = None, block: str | None = None,
                 config: dict | None = None):
        self.sb, self.block = sb, block
        self.env = {"ULTHAR_RUNTIME_DB": str(sb.db), "HOME": str(sb.home),
                    "USERPROFILE": str(sb.home)}
        if guard_src is not None:
            stubs = sb.root / "guards"
            stubs.mkdir(exist_ok=True)
            (stubs / "tool_discipline.py").write_text(guard_src, encoding="utf-8")
            (stubs / "git_discipline.py").write_text(STUB_ALLOW, encoding="utf-8")
            self.env["ULTHAR_GUARDS_DIR"] = str(stubs)
        if config is not None:
            path = sb.root / "config.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            self.env["ULTHAR_CONFIG"] = str(path)

    def __enter__(self):
        self.saved_env = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        self.saved_mod = sys.modules.get(self.block, False) if self.block else False
        if self.block:
            sys.modules[self.block] = None
        return self

    def __exit__(self, *exc):
        if self.block:
            if self.saved_mod is False:
                sys.modules.pop(self.block, None)
            else:
                sys.modules[self.block] = self.saved_mod
        for k, v in self.saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _import_error(text: str) -> bool:
    return "ImportError" in text or "ModuleNotFoundError" in text


def _last_event(sb: Sandbox, session: str) -> tuple:
    rows = sb.query("SELECT decision, guard_id, detail, post_hashes FROM events "
                    "WHERE session_id=? ORDER BY id", (session,))
    return rows[-1] if rows else (None, None, "{}", None)


def check_planted_config_cache_has_no_effect(p: Probe) -> None:
    sb = Sandbox()
    planted = []
    try:
        weak = json.loads(CONFIG.read_text(encoding="utf-8"))
        weak["dispatch"]["events"] = {}  # a policy with no guards at all
        st = os.stat(CONFIG)
        cache_dir = PACKAGE / "__pycache__"
        cache_dir.mkdir(exist_ok=True)
        for spelling in {str(CONFIG), str(CONFIG.resolve())}:
            key = (spelling, st.st_mtime_ns, st.st_size)
            f = cache_dir / f"config-{zlib.crc32(spelling.encode('utf-8')):08x}.marshal"
            f.write_bytes(marshal.dumps((key, weak)))
            planted.append(f)
        proc = sb.hook("PreToolUse", {"session_id": "fc-cache", "tool_name": "Bash",
                                      "tool_input": {"command": "git push --force origin main"}})
        p.check("P0: a forged config cache beside the package does not weaken the policy "
                "(force-push still blocked by git_discipline)",
                proc.returncode == 2 and "git discipline" in proc.stderr,
                f"rc={proc.returncode} {proc.stderr[:200]}")
        sources = [f.relative_to(V2_ROOT) for f in PACKAGE.rglob("*.py")
                   if "marshal" in f.read_text(encoding="utf-8")]
        p.check("P0: no runtime module reads or writes a marshal config cache", not sources,
                str(sources))
    finally:
        for f in planted:
            f.unlink(missing_ok=True)
        sb.cleanup()


STUB_NEEDS_SUBPROCESS = ("import subprocess", "from ulthar.lib.io import ALLOW", "",
                         "def run(payload, cfg, ctx):", "    return ALLOW", "")
FRESH_HOOK = (
    "import runpy, sys",
    "sys.modules[sys.argv[1]] = None  # this stdlib module cannot be imported",
    "main = sys.argv[2]",
    "sys.argv = [main, 'hook', '--event', sys.argv[3]]",
    "runpy.run_path(main, run_name='__main__')", "")
NL = chr(10)



def check_import_failure_at_guard_load_blocks(p: Probe) -> None:
    """subprocess/signal are imported at module level (as in v1), so their failure
    surfaces when the guard (or the package it needs) loads -- the AC-4 fail-closed
    path -- never mid-run, where v1's fail-open rule would turn it into ALLOW."""
    sb = Sandbox()
    try:
        with _Env(sb, NL.join(STUB_NEEDS_SUBPROCESS), block="subprocess"):
            out = hook.process("PreToolUse", dict(BASH, session_id="fc-load", cwd=str(sb.work)))
        decision, guard_id, _, _ = _last_event(sb, "fc-load")
        p.check("AC-4: a guard whose module-level `import subprocess` fails -> PreToolUse exits 2",
                out.result.exit_code == 2 and "failed to import" in out.result.stderr
                and "To recover" in out.result.stderr, out.result.stderr[:300])
        p.check("  ... and is recorded as a deny", decision == "deny", f"{decision} {guard_id}")
        payload = json.dumps(dict(BASH, hook_event_name="PreToolUse", cwd=str(sb.work)))
        for module in ("subprocess", "signal"):
            proc = subprocess.run(
                [sys.executable, "-S", "-c", NL.join(FRESH_HOOK), module, str(PACKAGE / "__main__.py"),
                 "PreToolUse"], input=payload, capture_output=True, text=True,
                encoding="utf-8", errors="replace", env=sb.env())
            p.check(f"AC-4: `{module}` unimportable when the runtime loads (fresh process) -> "
                    f"PreToolUse exits 2 naming the import failure",
                    proc.returncode == 2 and "could not be imported" in proc.stderr
                    and module in proc.stderr, f"rc={proc.returncode} {proc.stderr[:300]}")
    finally:
        sb.cleanup()


STUB_RUNS_CMD = ("from ulthar.lib.io import ALLOW", "",
                 "def run(payload, cfg, ctx):",
                 "    ctx.run_cmd(['git', '--version'], 'local')",
                 "    return ALLOW", "")
LAZY_STDLIB_EXCEPTIONS = {("lib/ctx.py", "shutil")}  # owner-approved, R1-c hook latency


def check_shutil_import_failure_blocks(p: Probe) -> None:
    """shutil is imported where Ctx.run_cmd uses it: its import failure, raised while a
    guard runs, must block the PreToolUse -- not reach v1's fail-open ALLOW."""
    sb = Sandbox()
    try:
        with _Env(sb, NL.join(STUB_RUNS_CMD)):
            ok = hook.process("PreToolUse", dict(BASH, session_id="fc-sh-ok", cwd=str(sb.work)))
        p.check("shutil importable: a guard calling ctx.run_cmd is allowed (control)",
                ok.result.exit_code == 0, ok.result.stderr[:300])
        with _Env(sb, NL.join(STUB_RUNS_CMD), block="shutil"):
            out = hook.process("PreToolUse", dict(BASH, session_id="fc-sh", cwd=str(sb.work)))
        decision, guard_id, _, _ = _last_event(sb, "fc-sh")
        p.check("owner exception: `shutil` unimportable inside ctx.run_cmd -> PreToolUse exits 2 "
                "naming the import failure",
                out.result.exit_code == 2 and "could not import a runtime module" in out.result.stderr
                and "shutil" in out.result.stderr and "To recover" in out.result.stderr,
                out.result.stderr[:300])
        p.check("  ... and is recorded as a deny", decision == "deny", f"{decision} {guard_id}")
    finally:
        sb.cleanup()


def _stdlib_imports(path: Path) -> set[tuple[str, str]]:
    """('top' | 'lazy', stdlib root) of every import in `path`; 'lazy' = inside a function."""
    found: set[tuple[str, str]] = set()

    def visit(node: ast.AST, in_fn: bool) -> None:
        for ch in ast.iter_child_nodes(node):
            names = []
            if isinstance(ch, ast.Import):
                names = [a.name for a in ch.names]
            elif isinstance(ch, ast.ImportFrom) and ch.level == 0 and ch.module:
                names = [ch.module]
            for name in names:
                root = name.split(".")[0]
                if root in sys.stdlib_module_names and root != "__future__":
                    found.add(("lazy" if in_fn else "top", root))
            visit(ch, in_fn or isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)))

    visit(ast.parse(path.read_text(encoding="utf-8")), False)
    return found


def check_lazy_stdlib_imports_match_v1(p: Probe) -> None:
    """Owner rule: the guard path (lib/, guards/) imports stdlib modules where v1 does
    (module level) -- the only function-level stdlib import v1 lacks is shutil in ctx."""
    v1_root = V2_ROOT.parent / "hooks"
    extra = set()
    for sub in ("lib", "guards"):
        for f in sorted((PACKAGE / sub).glob("*.py")):
            rel = f.relative_to(PACKAGE).as_posix()
            v1 = v1_root / rel
            v1_lazy = {m for k, m in _stdlib_imports(v1) if k == "lazy"} if v1.is_file() else set()
            extra |= {(rel, m) for k, m in _stdlib_imports(f) if k == "lazy" and m not in v1_lazy}
    p.check("owner rule: function-level stdlib imports in lib/ and guards/ beyond v1's are "
            f"exactly {sorted(LAZY_STDLIB_EXCEPTIONS)}", extra == LAZY_STDLIB_EXCEPTIONS,
            str(sorted(extra)))


def check_blob_hash_import_failure_marks_hash_failed(p: Probe) -> None:
    sb = Sandbox()
    try:
        target = sb.work / "w.txt"
        target.write_text("x\n", encoding="utf-8")
        with _Env(sb, block="subprocess"):
            direct = hook.blob_hash(str(target))
            out = hook.process("PostToolUse", {"session_id": "fc-hash", "cwd": str(sb.work),
                                               "tool_name": "Write", "tool_response": {},
                                               "tool_input": {"file_path": str(target)}})
        _, _, detail, post = _last_event(sb, "fc-hash")
        recorded = json.loads(post or "{}").get(str(target))
        p.check("P1: blob_hash with subprocess unimportable returns a hash_failed marker",
                isinstance(direct, dict) and _import_error(direct.get("hash_failed", "")),
                str(direct))
        p.check("  ... and PostToolUse completes (exit 0) recording hash_failed for the path",
                out.result.exit_code == 0 and isinstance(recorded, dict)
                and json.loads(detail).get("hash_failed") == [str(target)], str(recorded))
    finally:
        sb.cleanup()


def main() -> int:
    p = Probe()
    check_planted_config_cache_has_no_effect(p)
    check_import_failure_at_guard_load_blocks(p)
    check_shutil_import_failure_blocks(p)
    check_lazy_stdlib_imports_match_v1(p)
    check_blob_hash_import_failure_marks_hash_failed(p)
    return p.result()


if __name__ == "__main__":
    sys.exit(main())
