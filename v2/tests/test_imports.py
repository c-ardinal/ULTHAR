"""R1-a AC-11 / R1-b AC-17 / R1-c AC-15 / R1-d AC-15: every import in v2/ulthar/ is the standard
library, psutil, PyYAML (`yaml`) or ulthar itself (static AST scan: module-level,
function-level and relative imports alike); and the hook path -- every ulthar module
reachable from `__main__.py hook` (the dispatcher, its guards and helpers) -- never
imports PyYAML: statically (no `yaml` import in any of those modules, at any level) and
at run time (`sys.modules` after hook calls of every kind R1-c and R1-d add work to).
R1-d AC-15 also (review r1d-1 #6): no module of the whole computed hook closure
(ulthar.hook included) imports a standard-library module inside a function, except the
entries of LAZY_STDLIB_ALLOWED, each with its reason -- every other one fails."""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

from helpers import V2_ROOT, Probe, Sandbox

PACKAGE = V2_ROOT / "ulthar"
ALLOWED_THIRD_PARTY = {"psutil", "yaml"}
CLI_ONLY = {"ulthar.contract", "ulthar.flow", "ulthar.evidence", "ulthar.cli", "ulthar.stages",
            "ulthar.integrate", "ulthar.runcmd", "ulthar.packinstall"}
R1D_HOOK_MODULES = ("ulthar.packs", "ulthar.lease", "ulthar.gitref", "ulthar.acshape",
                    "ulthar.posthash")
PRE_R1D = "in develop before R1-d, not on the guard decision path"
AS_V1 = "ported v1 code, where v1 has it (test_failclosed compares lib/ and guards/ with v1)"
LAZY_STDLIB_ALLOWED = {
    ("ulthar.hook", "subprocess"): "PostToolUse post-hash only (git hash-object); " + PRE_R1D,
    ("ulthar.hook", "traceback"): "the pipeline-error path only; " + PRE_R1D,
    ("ulthar.journal", "fcntl"): "platform-specific lock (POSIX); " + PRE_R1D,
    ("ulthar.journal", "msvcrt"): "platform-specific lock (Windows); " + PRE_R1D,
    ("ulthar.store", "winreg"): "platform-specific machine id (Windows); " + PRE_R1D,
    ("ulthar.v1dispatch", "hashlib"): "the overlay notice marker only; " + PRE_R1D,
    ("ulthar.lib.ctx", "shutil"): "the owner's single exception (Ctx.run_cmd, fail-closed)",
    ("ulthar.lib.ctx", "json"): AS_V1,
    ("ulthar.lib.ctx", "traceback"): AS_V1,
    ("ulthar.lib.io", "ctypes"): AS_V1,
    ("ulthar.guards.tool_discipline", "ctypes"): AS_V1,
}


def imported_roots(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [(node.lineno, a.name.split(".")[0]) for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            out.append((node.lineno, "." if node.level else (node.module or "").split(".")[0]))
    return out


def check_imports(p: Probe) -> None:
    allowed = set(sys.stdlib_module_names) | ALLOWED_THIRD_PARTY | {"ulthar", "."}
    files = sorted(PACKAGE.rglob("*.py"))
    violations = []
    for f in files:
        for lineno, root in imported_roots(f):
            if root not in allowed:
                violations.append(f"{f.relative_to(V2_ROOT)}:{lineno}: {root}")
    p.check(f"scanned {len(files)} modules under v2/ulthar", len(files) >= 10, str(len(files)))
    p.check(f"AC-15: imports other than stdlib / psutil / PyYAML: {len(violations)}",
            not violations, "\n       ".join(violations))


def _module_name(path: Path) -> str:
    rel = path.relative_to(V2_ROOT).with_suffix("")
    parts = list(rel.parts)
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _ulthar_imports(path: Path) -> set[str]:
    """ulthar modules `path` imports (any level, relative or absolute)."""
    name = _module_name(path)
    package = name if path.name == "__init__.py" else name.rsplit(".", 1)[0]
    out = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names if a.name.split(".")[0] == "ulthar"}
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".")
                base = base[:len(base) - node.level + 1]
                mod = ".".join(base + ([node.module] if node.module else []))
            else:
                mod = node.module or ""
            if mod.split(".")[0] != "ulthar":
                continue
            out.add(mod)
            out |= {f"{mod}.{a.name}" for a in node.names}
    return out


def hook_closure() -> dict[str, Path]:
    files = {_module_name(f): f for f in PACKAGE.rglob("*.py")}
    # `__main__.py hook` imports ulthar.hook and exits; its CLI import below that line is
    # never reached on the hook path. The guards are imported by name (v1dispatch).
    todo = ["ulthar.hook"] + [n for n in files if n.startswith("ulthar.guards.")]
    seen: dict[str, Path] = {}
    while todo:
        name = todo.pop()
        if name in seen or name not in files:
            continue
        seen[name] = files[name]
        todo += sorted(_ulthar_imports(files[name]))
    return seen


def check_hook_path(p: Probe) -> None:
    closure = hook_closure()
    with_yaml = [n for n, f in closure.items() if "yaml" in {r for _, r in imported_roots(f)}]
    p.check(f"AC-15: the hook path ({len(closure)} modules) imports PyYAML nowhere (static)",
            not with_yaml and not (CLI_ONLY & set(closure)) and "ulthar.product" in closure
            and all(m in closure for m in R1D_HOOK_MODULES),
            f"yaml in {with_yaml}; CLI-only modules reached: {sorted(CLI_ONLY & set(closure))}")
    sb = Sandbox()
    try:
        sb.configure(["none"], non_product_globs=[])
        events = [
            ("PreToolUse", {"tool_name": "Edit", "tool_input": {"file_path": str(sb.work / "a.py")}}),
            ("PreToolUse", {"tool_name": "Bash", "agent_id": "a-1", "tool_use_id": "t1",
                            "tool_input": {"command": "ulthar verify U-000000000000 --self"}}),
            ("PostToolUse", {"tool_name": "AskUserQuestion", "tool_use_id": "t2",
                             "tool_input": {"questions": [{"question": "CONTRACT-CONFIRM:U-0 v1 sha:000000000000"}]},
                             "tool_response": {"answers": {}}}),
            ("SubagentStart", {"agent_id": "a-2", "agent_type": "t"}),
            ("SubagentStart", {"agent_id": "a-3", "agent_type": "ulthar-creator@ui"}),
            ("PreToolUse", {"tool_name": "Agent", "tool_use_id": "t3",
                            "tool_input": {"prompt": "work", "description": "w"}}),
            ("PreToolUse", {"tool_name": "Bash", "tool_use_id": "t4",
                            "tool_input": {"command": "git update-ref refs/heads/develop HEAD"}}),
            ("PostToolUse", {"tool_name": "Edit", "tool_input": {"file_path": str(sb.work / "a.py")}}),
            ("UserPromptSubmit", {"prompt": "hello", "prompt_id": "p-1"}),
        ]
        script = (
            "import json, sys\n"
            f"sys.path.insert(0, {str(V2_ROOT)!r})\n"
            "from ulthar import hook\n"
            "codes = []\n"
            f"for event, payload in json.loads({json.dumps(json.dumps(events))}):\n"
            f"    payload = dict(payload, session_id='imp', cwd={str(sb.work)!r}, hook_event_name=event)\n"
            "    codes.append(hook.process(event, payload).result.exit_code)\n"
            "print(json.dumps({'codes': codes, 'yaml': sorted(m for m in sys.modules if m.split('.')[0] == 'yaml'),"
            " 'cli_only': sorted(m for m in sys.modules if m in %r)}))\n" % sorted(CLI_ONLY))
        proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                              encoding="utf-8", env=sb.env(), timeout=120)
        report = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.returncode == 0 else {}
        p.check("AC-15: after hook calls (Edit scope check, runner token, unit question answer, "
                "SubagentStart incl. a pack agent, Agent dispatch, a git ref command, PostToolUse "
                "Edit, UserPromptSubmit) with site-packages available, sys.modules holds "
                f"no yaml module ({report.get('yaml')}) and no CLI-only module",
                bool(report) and not report["yaml"] and not report["cli_only"],
                proc.stdout[-300:] + proc.stderr[-500:])
    finally:
        sb.cleanup()


def check_no_lazy_stdlib(p: Probe) -> None:
    """R1-d AC-15: no standard-library import inside a function anywhere on the hook path,
    the justified LAZY_STDLIB_ALLOWED entries excepted."""
    stdlib = set(sys.stdlib_module_names)
    found = []
    closure = hook_closure()
    for name, path in sorted(closure.items()):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            for node in ast.walk(fn):
                mods = ([a.name for a in node.names] if isinstance(node, ast.Import) else
                        [node.module or ""] if isinstance(node, ast.ImportFrom) and not node.level
                        else [])
                found += [f"{name}:{node.lineno} {m}" for m in mods if m.split(".")[0] in stdlib
                          and (name, m.split(".")[0]) not in LAZY_STDLIB_ALLOWED]
    p.check(f"AC-15 (R1-d, review #6): function-level stdlib imports on the hook path "
            f"({len(closure)} modules, ulthar.hook included) beyond the {len(LAZY_STDLIB_ALLOWED)} "
            f"justified exceptions: {len(found)}",
            not found and "ulthar.hook" in closure and all(m in closure for m in R1D_HOOK_MODULES),
            "\n       ".join(found))


def main() -> int:
    p = Probe()
    check_imports(p)
    check_hook_path(p)
    check_no_lazy_stdlib(p)
    return p.result()


if __name__ == "__main__":
    sys.exit(main())
