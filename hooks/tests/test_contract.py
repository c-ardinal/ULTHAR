#!/usr/bin/env python3
"""hooks/tests/test_contract.py -- U-0's contract self-enforcement suite.

CONTRACT.md #5 'Contract self-enforcement' (checks 1-8) and #12 (folds an
internal pre-promotion config probe's 74 checks in as the config-level layer). Run via
`python tests/run_all.py` or directly:

    python tests/test_contract.py

Exit 0 = all pass, 1 = at least one failure. Printed as plain OK/FAIL lines (no
pytest dependency -- the runtime is stdlib + psutil only, and this suite must run
with the pinned interpreter alone).

NOTE on checks 5 and 7: before the guard fan-out lands, no guard modules, guard
tests or parity fixtures exist yet except the tests/-only stub. Those two checks
are EXPECTED to report gaps until fan-out completes -- they are the mechanism
that will catch a missed unit, not evidence of an incomplete suite.
"""
from __future__ import annotations

import ast
import io
import json
import os
import re
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
HOOKS_DIR = TESTS_DIR.parent
SKILL_ROOT = HOOKS_DIR.parent
CONFIG_PATH = SKILL_ROOT / "config.json"

sys.path.insert(0, str(HOOKS_DIR))


class Probe:
    def __init__(self) -> None:
        self.failed = 0
        self.passed = 0
        self.fail_names: list[str] = []

    def check(self, name: str, cond: bool, detail: str = "") -> None:
        if cond:
            self.passed += 1
            print(f"OK   {name}")
        else:
            self.failed += 1
            self.fail_names.append(name)
            print(f"FAIL {name}" + (f"\n       {detail}" if detail else ""))


# ------------------------------------------------------------------ probe.py, folded


def parse_path(spec: str) -> list[str]:
    out: list[str] = []
    for seg in spec.split("."):
        base, brackets = seg, []
        while base.endswith("]"):
            i = base.rindex("[")
            inner = base[i + 1 : -1]
            brackets.insert(0, "[]" if inner == "" else inner)
            base = base[:i]
        out.append(base)
        out += brackets
    return out


def walk(node, parts: list[str]) -> tuple[list, bool]:
    """Returns (values, hit_empty_list). Accumulates across `*`/`[]` fan-out
    exactly like the pre-round1 version -- a fan-out branch that does not match
    (e.g. a dispatch rule using `tools_named` instead of `tools`) simply
    contributes nothing, same as always. `hit_empty_list` is the ONE addition
    (the config-generalisation round): True when a `[]` step lands on a list that is
    legitimately EMPTY (command_rules.dev_server / git_policy.deploy_refs now
    ship empty by default) -- the path still counts as RESOLVED in that case
    even though it yields zero values. Mirrors install.py's own `walk()`; kept
    as a separate copy per this file's existing style (it is a from-scratch
    reimplementation of that config probe's checks, not an import)."""
    if not parts:
        return [node], False
    head, rest = parts[0], parts[1:]
    out: list = []
    hit_empty = False
    if head == "*":
        if isinstance(node, dict):
            for v in node.values():
                vals, he = walk(v, rest)
                out += vals
                hit_empty = hit_empty or he
    elif head == "[]":
        if isinstance(node, list):
            if not node:
                hit_empty = True
            for v in node:
                vals, he = walk(v, rest)
                out += vals
                hit_empty = hit_empty or he
    elif head.isdigit():
        if isinstance(node, list) and int(head) < len(node):
            out, hit_empty = walk(node[int(head)], rest)
    else:
        if isinstance(node, dict) and head in node:
            out, hit_empty = walk(node[head], rest)
    return out, hit_empty


def walk_values(node, parts: list[str]) -> list:
    """Convenience wrapper for call sites that only need the values, not the
    hit_empty_list flag (offender/duplication scans below, which never need to
    distinguish an empty list from a missing path)."""
    return walk(node, parts)[0]


INLINE_FLAG = re.compile(r"\(\?[aimsuxL]+[):]")


def flags_for(kind: str, windows: bool) -> int:
    if kind == "text":
        return re.IGNORECASE
    if kind == "path":
        return re.IGNORECASE if windows else 0
    raise AssertionError(f"unknown kind {kind!r}")


def run_config_probe(p: Probe, cfg: dict) -> None:
    """The full body of the config probe's checks (config.json parses; pattern_fields
    resolve and compile under both platform flag sets; N-1; reverse undeclared-field
    scan; single-home exact + substring; semantic cases; structural cases; PE-12;
    J1/J2/J3; B-1; B-4; *_named dangling/twin). Folded in verbatim per CONTRACT.md
    #5's instruction that U-0 folds probe's checks into this file at fan-out."""
    fields = cfg["regex"]["pattern_fields"]

    compiles = 0
    unresolved, uncompilable = [], []
    for f in fields:
        vals, hit_empty = walk(cfg, parse_path(f["path"]))
        if not vals and not hit_empty:
            unresolved.append(f["path"])
            continue
        for v in vals:
            if not isinstance(v, str) or v == f.get("sentinel"):
                continue
            src = v.replace("{ref}", "X") if f.get("template") else v
            for windows in (True, False):
                try:
                    re.compile(src, flags_for(f["kind"], windows))
                    compiles += 1
                except re.error as exc:
                    uncompilable.append((f["path"], str(exc)))
    p.check(f"config: all {len(fields)} pattern_fields resolve", not unresolved, str(unresolved))
    p.check(f"config: all declared patterns compile ({compiles} compilations)",
            not uncompilable, str(uncompilable))

    offenders = []
    for f in fields:
        if f["kind"] != "path":
            continue
        for v in walk_values(cfg, parse_path(f["path"])):
            if isinstance(v, str) and INLINE_FLAG.search(v):
                offenders.append((f["path"], v[:60]))
    p.check("config: N-1 no inline flag in any kind:'path' pattern", not offenders, str(offenders))

    declared = {f["path"] for f in fields} | set(cfg["regex"]["non_pattern_fields"])
    HINT = ("pattern", "patterns", "requires", "match", "suffix", "prefix",
            "detect", "rx", "template")
    leaves: list[tuple[str, frozenset]] = []

    def scan(node, paths):
        if isinstance(node, dict):
            for k, v in node.items():
                if not k.startswith("_"):
                    scan(v, tuple(f"{pp}.{k}" if pp else k for pp in paths))
        elif isinstance(node, list):
            for i, v in enumerate(node):
                nxt = tuple(pp + s for pp in paths for s in ("[]", f"[{i}]"))
                scan(v, nxt)
        elif isinstance(node, str):
            primary = paths[0]
            last = primary.split(".")[-1].split("[")[0]
            if any(h == last or last.endswith("_" + h) or last.startswith(h + "_") for h in HINT):
                leaves.append((primary, frozenset(paths)))

    scan(cfg, ("",))
    undeclared = sorted({primary for primary, spellings in leaves if not (spellings & declared)})
    p.check("config: reverse -- no undeclared regex-bearing field", not undeclared, str(undeclared))

    seen: dict = {}

    def scan_values(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                if k.startswith("_"):
                    continue
                if isinstance(v, (str, int, float)) and not isinstance(v, bool):
                    seen.setdefault((k, v), []).append(f"{path}/{k}")
                else:
                    scan_values(v, f"{path}/{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                scan_values(v, f"{path}[{i}]")

    scan_values(cfg, "")
    ENUMS = {"*", "raw", "probe_redirect", "probe_quotes", "require_pattern",
             "regex_block", "regex_block_any", "delete_allowlist",
             "IGNORECASE", "text", "path"}
    exact_dups = [(k, v) for k, v in seen.items() if len(v) > 1 and k[1] not in ENUMS
                  and not k[0].endswith("_named")]
    p.check("config: single-home -- no exact mirror values", not exact_dups,
            str([(k[0], v) for k, v in exact_dups]))

    composed = {f["path"]: f.get("composed_with") for f in fields}
    pats = []
    for f in fields:
        for v in walk_values(cfg, parse_path(f["path"])):
            if isinstance(v, str) and len(v) >= 24 and v != f.get("sentinel"):
                pats.append((f["path"], v))
    substring_dups = []
    for i, (pa, va) in enumerate(pats):
        for pb, vb in pats[i + 1:]:
            if pa == pb:
                continue
            if composed.get(pa) == pb or composed.get(pb) == pa:
                continue
            if va in vb or vb in va:
                substring_dups.append((pa, pb))
    p.check("config: single-home -- no substring-level pattern duplication (N-2)",
            not substring_dups, str(substring_dups))

    W, P = re.IGNORECASE, 0
    T = re.IGNORECASE
    td = {r["id"]: r for r in cfg["command_rules"]["tool_discipline"]}
    dele = td["delete-outside-disposable"]
    allowed = dele["allowed_path_pattern"]
    exempt = cfg["paths"]["exempt_patterns"]["patterns"]
    gp = cfg["git_policy"]
    dc = {c["id"]: c for c in cfg["design_cases"]["cases"]}
    to_posix = lambda s: s.replace("\\", "/")
    cases = [
        ("segment filter matches lower-case remove-item",
         bool(re.search(dele["segment_start_pattern"], "remove-item foo.txt", T))),
        ("segment filter matches Remove-Item",
         bool(re.search(dele["segment_start_pattern"], "Remove-Item foo.txt", T))),
        ("delete detect matches remove-item",
         bool(re.search(dele["detect_pattern"], "remove-item foo.txt", T))),
        ("exempt matches real Windows scratchpad path",
         any(re.search(e, to_posix(r"C:\Users\A\AppData\Local\Temp\claude\x\scratchpad\n.md"), W)
             for e in exempt)),
        ("exempt matches HANDOFF",
         any(re.search(e, to_posix(r"C:\repo\HANDOFF.md"), W) for e in exempt)),
        ("exempt matches docs/plans",
         any(re.search(e, to_posix(r"C:\repo\docs\plans\c.md"), W) for e in exempt)),
        ("exempt does NOT match src/App.tsx",
         not any(re.search(e, to_posix(r"C:\repo\src\App.tsx"), W) for e in exempt)),
        ("allowlist matches dist/", bool(re.search(allowed, "dist/x.js", W))),
        ("allowlist matches Temp/claude",
         bool(re.search(allowed, to_posix(r"C:\Users\a\AppData\Local\Temp\claude\x"), W))),
        ("allowlist rejects src/App.tsx", not re.search(allowed, "src/App.tsx", W)),
        ("N-1 Windows: /home/x/Dist IS disposable (case-insensitive)",
         bool(re.search(allowed, "/home/x/Dist", W))),
        ("N-1 POSIX: /home/x/Dist is NOT disposable (case-sensitive)",
         not re.search(allowed, "/home/x/Dist", P)),
        ("N-1 POSIX: /home/x/dist IS still disposable",
         bool(re.search(allowed, "/home/x/dist", P))),
        ("ui design case triggers on upper-case LAYOUT",
         any(re.search(t, "This brief covers the LAYOUT of the panel", T)
             for t in dc["ui-design-skill"]["triggers"])),
        ("ui design case satisfied by /UI-UX-PRO-MAX",
         any(re.search(s, "/UI-UX-PRO-MAX", T) for s in dc["ui-design-skill"]["satisfied_by"])),
        ("responsive design case triggers",
         any(re.search(t, "RESPONSIVE sweep", T)
             for t in dc["responsive-viewport-matrix"]["triggers"])),
        ("dev-process pattern matches upper-case VITE",
         bool(re.search(cfg["dev_processes"]["pattern"], "node VITE --port", T))),
        ("git blanket staging matches GIT ADD -A",
         bool(re.search(gp["global_option_prefix"] + gp["blanket_staging"][0]["suffix"],
                        "GIT ADD -A", T))),
        ("git blanket staging matches quoted -C (PE-4)",
         bool(re.search(gp["global_option_prefix"] + gp["blanket_staging"][0]["suffix"],
                        'git -C "C:/my repo" add -A', T))),
        ("pr_state composed detects quoted -C push",
         bool(re.search(gp["global_option_prefix"] + gp["pr_state"]["push_detect_suffix"],
                        'git -C "C:/my repo" push origin x', T))),
        ("pr_state composed detects --exec-path push (PE-4)",
         bool(re.search(gp["global_option_prefix"] + gp["pr_state"]["push_detect_suffix"],
                        "git --exec-path=/x push origin merged", T))),
        ("brief detector matches [Creator Brief]",
         bool(re.search(cfg["brief_rules"]["detect"]["brief_pattern"],
                        "[Creator Brief - x]", T))),
        ("gate1 matches Japanese \u5b8c\u6210\u306e\u5b9a\u7fa9",
         bool(re.search(
             [r for r in cfg["brief_rules"]["kinds"][3]["rules"] if r["id"] == "gate1-acceptance-table"][0]["requires"],
             "STATE.md \u00a72 \u5b8c\u6210\u306e\u5b9a\u7fa9", T))),
        ("M-6 ports render as 8788/5173",
         cfg["dev_processes"]["ports_join"].join(str(x) for x in cfg["dev_processes"]["ports"]) == "8788/5173"),
    ]
    for name, cond in cases:
        p.check("config semantic: " + name, cond)

    struct = [
        ("C-1 stop_hook_active covers both Stop guards",
         set(cfg["dispatch"]["honour_stop_hook_active_for"]) == {"artifact_hygiene", "dev_process_hygiene"}),
        ("H-2 config_error_log distinct from paths.error_log",
         cfg["logging"]["config_error_log"] != cfg["paths"]["error_log"]),
        ("H-2 refuse_deploy_on_invalid_config", cfg["logging"]["refuse_deploy_on_invalid_config"]),
        ("H-3 compile_scope is per_guard", cfg["regex"]["compile_scope"] == "per_guard"),
        ("H-6 path kind is case-sensitive on POSIX", cfg["regex"]["kinds"]["path"]["flags_posix"] == []),
        ("H-6 text kind is always IGNORECASE", cfg["regex"]["kinds"]["text"]["flags"] == ["IGNORECASE"]),
        ("PE-7 split timeouts present",
         {"subprocess_timeout_local_sec", "subprocess_timeout_remote_sec"} <= set(cfg["thresholds"])),
        ("S-7 phase_order carries per-phase match_target",
         all(isinstance(x, dict) and "match_target" in x for x in gp["phase_order"])),
        ("S-7 git_command_detect is the only raw phase",
         [x["phase"] for x in gp["phase_order"] if x["match_target"] == "raw"] == ["git_command_detect"]),
        ("S-7 pr_state match_target is raw", gp["pr_state"]["match_target"] == "raw"),
        ("N-2 pr_state.push_detect removed", "push_detect" not in gp["pr_state"]),
        ("M-5 skill_anchor.outside_cwd_is_exempt", cfg["skill_anchor"]["outside_cwd_is_exempt"] is True),
        ("M-7 artifact marker ordering",
         cfg["artifact_dirs"]["marker_written_only_when_found"]),
        ("M-9b dotdot_base is process_cwd", dele["dotdot_base"] == "process_cwd"),
        ("M-2 deploy_guard recursive, no subdir allowlist",
         cfg["deploy_guard"]["recursive"] and "guarded_subdirs" not in cfg["deploy_guard"]),
        ("M-4 skill_anchor off requires single entry", cfg["skill_anchor"]["off_token_requires_single_entry"]),
        ("M-18 enabled present on git rule lists",
         all("enabled" in r for r in gp["blanket_staging"] + gp["destroyers"])),
        ("I-11 viewport_matrix consumed keys intact",
         {"widths", "heights", "aspectRange", "breakpointDeltaPx", "projectBreakpointsNote",
          "quickCheckPresets", "invariants"} <= set(cfg["viewport_matrix"])),
        ("psutil: discovery is a single psutil path", cfg["dev_processes"]["discovery"]["strategy"] == "psutil"),
        ("psutil: OS-branch argv lists deleted",
         not {"windows_argv", "posix_argv", "windows_strategy"} & set(cfg["dev_processes"]["discovery"])),
        ("psutil: names is ONE base-name list", isinstance(cfg["dev_processes"]["names"], list)),
        ("psutil: kill + port strategies are psutil",
         cfg["dev_processes"]["stop_orphans"]["kill_strategy"] == "psutil"
         and cfg["dev_processes"]["stop_orphans"]["port_check_strategy"] == "psutil_net_connections"),
        ("psutil: degraded port message defined (AccessDenied)",
         "port_check_degraded_message" in cfg["dev_processes"]["stop_orphans"]),
        ("psutil: declared as a required dependency",
         any(d["module"] == "psutil" for d in cfg["dependencies"]["required"])),
        ("psutil: import confined to the Stop path",
         cfg["dependencies"]["required"][0]["imported_by"] == ["lib/procs.py"]),
    ]
    for name, cond in struct:
        p.check("config structural: " + name, cond)

    master_windows_names = ["bash.exe", "sh.exe", "node.exe", "tail.exe", "workerd.exe",
                            "python.exe", "draw.io.exe"]
    declared_names = {n.casefold() for n in cfg["dev_processes"]["names"]}

    def normalise(reported: str) -> str:
        base = reported[:-4] if reported.casefold().endswith(".exe") else reported
        return base.casefold()

    missed = [n for n in master_windows_names if normalise(n) not in declared_names]
    p.check("config PE-12: every master Windows name still matches after .exe stripping",
            not missed, str(missed))
    p.check("config PE-12: draw.io.exe keeps its internal dot",
            normalise("draw.io.exe") == "draw.io")
    extra = sorted(declared_names - {normalise(n) for n in master_windows_names})
    p.check("config PE-12: the only effective-set addition is python3", extra == ["python3"], str(extra))

    disp = cfg["dispatch"]
    stop_guards = [g for rule in disp["events"]["Stop"] for g in rule["guards"]]
    honoured = set(disp["honour_stop_hook_active_for"])
    exempt_j1 = {e["guard"] if isinstance(e, dict) else e for e in disp.get("stop_hook_active_exempt", [])}
    unaccounted = [g for g in stop_guards if g not in honoured and g not in exempt_j1]
    p.check("config J1: every Stop guard honours stop_hook_active (or is exempt with a reason)",
            not unaccounted, str(unaccounted))

    modes = cfg["commander"]["modes"]
    default_id = cfg["commander"]["default_mode_when_unrecognised"]
    p.check("config J2: default_mode_when_unrecognised is a mode ID", default_id in modes)
    p.check("config J2: it still resolves to 'observe'", modes.get(default_id) == "observe")

    g6 = [r for r in cfg["brief_rules"]["kinds"][3]["rules"] if r["id"] == "gate6-approval-handshake"][0]
    pr = g6["placeholder_resolution"]
    p.check("config B-1: placeholder_resolution declares all three placeholders",
            {"approval_dir", "approval_path", "approval_name"} <= set(pr))
    p.check("config B-1: the three forms are distinct",
            len({pr[k]["form"] for k in ("approval_dir", "approval_path", "approval_name")}) == 3)

    declared_lists = {
        "regex.non_pattern_fields": cfg["regex"]["non_pattern_fields"],
        "regex.string_compare_casefold": cfg["regex"]["string_compare_casefold"],
    }
    unresolvable = []
    for owner, paths in declared_lists.items():
        for spec in paths:
            vals, hit_empty = walk(cfg, parse_path(spec))
            if not vals and not hit_empty:
                unresolvable.append(f"{owner}: {spec}")
    p.check("config B-4: every declared list path resolves", not unresolvable, str(unresolvable))

    named = cfg["regex"]["named_alternations"]
    dangling, twins = [], []

    def scan_named(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                if k.startswith("_"):
                    continue
                if k.endswith("_named") and isinstance(v, str):
                    if v not in named:
                        dangling.append(f"{path}.{k} -> {v!r}")
                    literal = k[: -len("_named")]
                    if literal in node:
                        twins.append(f"{path}: both {literal!r} and {k!r}")
                scan_named(v, f"{path}.{k}" if path else k)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                scan_named(v, f"{path}[{i}]")

    scan_named(cfg, "")
    p.check("config *_named: no dangling reference", not dangling, str(dangling))
    p.check("config *_named: no literal twin alongside a _named key", not twins, str(twins))


# ------------------------------------------------------------------ check 1 / 1b / 1c: guard purity

FORBIDDEN_CALLS = {"exit", "print"}
FORBIDDEN_ATTRS = {("sys", "exit"), ("sys", "stdin"), ("os", "environ")}


class GuardPurityVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.violations: list[str] = []
        self.imports: set[str] = set()

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.imports.add(alias.name)
            if alias.name == "subprocess":
                self.violations.append("imports subprocess")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        mod = node.module or ""
        if mod == "subprocess":
            self.violations.append("imports subprocess")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        fn = node.func
        if isinstance(fn, ast.Name) and fn.id in ("exit", "print"):
            self.violations.append(f"calls {fn.id}(...)")
        if isinstance(fn, ast.Attribute):
            if fn.attr == "exit" and isinstance(fn.value, ast.Name) and fn.value.id == "sys":
                self.violations.append("calls sys.exit(...)")
            if fn.attr == "compile" and isinstance(fn.value, ast.Name) and fn.value.id == "re":
                self.violations.append("calls re.compile(...)")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.value, ast.Name):
            if node.value.id == "sys" and node.attr in ("stdin", "stdout", "stderr"):
                self.violations.append(f"references sys.{node.attr}")
            if node.value.id == "os" and node.attr == "environ":
                self.violations.append("references os.environ")
        self.generic_visit(node)


def check_guard_purity(p: Probe) -> None:
    guards_dir = HOOKS_DIR / "guards"
    guard_files = sorted(f for f in guards_dir.glob("*.py") if f.name != "__init__.py")
    if not guard_files:
        p.check("check1: guard purity AST scan (no guards/*.py yet -- pre-fan-out)", True)
        return
    all_clean = True
    psutil_offenders = []
    for f in guard_files:
        tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
        visitor = GuardPurityVisitor()
        visitor.visit(tree)
        if visitor.violations:
            all_clean = False
            p.check(f"check1: guard purity -- {f.name}", False, str(visitor.violations))
        if f.name != "dev_process_hygiene.py" and "psutil" in visitor.imports:
            psutil_offenders.append(f.name)
    if all_clean:
        p.check(f"check1: guard purity AST scan ({len(guard_files)} guards clean)", True)
    p.check("check1b: psutil confined to dev_process_hygiene.py", not psutil_offenders, str(psutil_offenders))


# ------------------------------------------------------------------ check 5: ownership completeness

OWNERSHIP_TABLE = {
    "tool_discipline": ("guards/tool_discipline.py", "tests/fixtures/tool_discipline/", "U-1"),
    "dev_server": ("guards/dev_server.py", "tests/fixtures/dev_server/", "U-1"),
    "git_discipline": ("guards/git_discipline.py", "tests/fixtures/git/", "U-2"),
    "pr_state": ("guards/pr_state.py", "tests/fixtures/pr_state/", "U-2"),
    "grant_files": ("guards/grant_files.py", "tests/test_grants.py", "owner-grants"),
    "deploy": ("guards/deploy.py", "tests/fixtures/write_edit/", "U-3"),
    "commander": ("guards/commander.py", "tests/fixtures/write_edit/", "U-3"),
    "skill_anchor": ("guards/skill_anchor.py", "tests/fixtures/write_edit/", "U-3"),
    "brief_gate": ("guards/brief_gate.py", "tests/fixtures/briefs/", "U-4"),
    "artifact_hygiene": ("guards/artifact_hygiene.py", "tests/fixtures/stop/", "U-5"),
    "dev_process_hygiene": ("guards/dev_process_hygiene.py", "tests/fixtures/stop/", "U-5"),
}


# ------------------------------------------------------------------ B2: routing table protection
#
# Pins the routing table itself: deleting a guard from dispatch.events, or
# reversing guard order within a rule, was otherwise invisible to the suite
# (every guards/*.py module is routed SOMEWHERE, per-event guard ORDER matches
# a golden snapshot) plus an end-to-end, non-guard chain case.

EXPECTED_DISPATCH_EVENTS_ORDER = {
    "PreToolUse": [
        {"tools": "Bash|PowerShell", "guards": ["tool_discipline", "git_discipline", "pr_state", "dev_server"]},
        # grant_files runs FIRST on this chain: it protects the files that carry the
        # owner's permission (grant dir, pm-delete-allow, both overlays), and a guard
        # that can be reordered behind one which might ALLOW first would be a guard
        # that can be skipped.
        {"tools_named": "write_edit_tools",
         "guards": ["grant_files", "deploy", "commander", "skill_anchor"]},
        {"tools": "Agent|SendMessage", "guards": ["skill_anchor", "brief_gate"]},
    ],
    "Stop": [{"tools": "*", "guards": ["artifact_hygiene", "dev_process_hygiene"]}],
    "SessionStart": [{"tools": "*", "guards": ["skill_anchor"]}],
}


def check_single_config_json(p: Probe) -> None:
    """Acceptance row 7: two config.json files -- one at the tree root, one in the
    nested build directory -- once drifted silently: different tools read
    different copies and nothing could tell them apart. The root copy is DELETED; this asserts it stays gone
    (excludes tests/fixtures/, which legitimately contain synthetic config.json
    files for isolated test scenarios, and CONFIG_PATH itself).

    Pins a depth-counting defect: scanning from `HOOKS_DIR.parent.parent` (TWO
    levels up from `hooks/`) is correct only in an earlier, more deeply nested
    build layout. After promotion to
    `~/.claude/skills/ulthar/hooks/`, that same depth-count lands on
    `~/.claude/skills/` -- the PARENT of every OTHER skill on the machine -- so
    the check permanently failed on unrelated skills' own, legitimate
    config.json files. `HOOKS_DIR.parent` (ONE level up from `hooks/`) is the
    skill root by construction in BOTH layouts -- the same invariant
    install.py's `SKILL_ROOT = HERE.parent` and conftest.py's
    `HOOKS_DIR.parent` already rely on. A permanently-red check for a benign
    reason is worse than no check (it gets ignored, and a REAL second
    config.json slips through unnoticed): scoped to THIS skill, not the
    ~/.claude/skills/ directory that contains it."""
    skill_root = HOOKS_DIR.parent  # the skill's own root, one level up from hooks/
    found = [
        f for f in skill_root.rglob("config.json")
        if "fixtures" not in f.parts and f.resolve() != CONFIG_PATH.resolve()
    ]
    p.check("row7: exactly ONE config.json belonging to THIS skill (outside test fixtures)",
            not found, str(found))


def check_shipped_python_exe_empty(p: Probe, cfg: dict) -> None:
    """Regression guard for the published-repo defect:
    `paths.python_exe` must ship EMPTY so a fresh clone resolves the interpreter
    via lib.paths.resolve_python_exe() (-> sys.executable) instead of failing on
    this machine's hardcoded pin. Invisible on the machine that pinned it (its
    own sys.executable happens to match), which is exactly why it survived
    until now -- so this asserts the SHIPPED VALUE, not the resolved one."""
    python_exe = cfg.get("paths", {}).get("python_exe")
    p.check("config.json: paths.python_exe ships empty (machine pin belongs in "
            "the user overlay, never the shipped default)",
            python_exe == "", repr(python_exe))


def check_routing_table_pinned(p: Probe, cfg: dict) -> None:
    events = cfg["dispatch"]["events"]
    p.check("B2: dispatch.events has exactly the expected event keys",
            set(events) == set(EXPECTED_DISPATCH_EVENTS_ORDER),
            f"{sorted(events)} != {sorted(EXPECTED_DISPATCH_EVENTS_ORDER)}")
    for event, expected_rules in EXPECTED_DISPATCH_EVENTS_ORDER.items():
        actual_rules = events.get(event, [])
        actual = [{k: r[k] for k in ("tools", "tools_named", "guards") if k in r} for r in actual_rules]
        p.check(f"B2: dispatch.events[{event}] rules AND per-rule guard ORDER pinned",
                actual == expected_rules, f"actual={actual}\nexpected={expected_rules}")

    guard_files = sorted(f.stem for f in (HOOKS_DIR / "guards").glob("*.py") if f.stem != "__init__")
    routed = set()
    for rules in events.values():
        for r in rules:
            routed |= set(r.get("guards", []))
    unrouted = sorted(set(guard_files) - routed)
    p.check("B2: every guards/*.py module is routed by SOME dispatch.events rule",
            not unrouted, f"unrouted: {unrouted}")


def check_chain_order_and_stdout_survival(p: Probe) -> None:
    """B2's third ask: an end-to-end chain (NOT --guard) where guard 1 emits
    stdout and ALLOWs, guard 2 BLOCKs -- the block must survive, and per I-9 the
    stdout already produced by guard 1 is written (accumulated), not discarded,
    even though the chain then terminates on guard 2's block. Also proves chain
    ORDER is live: reversing the two guards' declared order would flip which one
    decides."""
    import subprocess as _sp
    import os as _os
    import tempfile as _tmp

    two_guard_cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    # Guard 1 MUST be named "skill_anchor" -- I-9 / _is_stdout_emitter only
    # forwards stdout from the ONE declared emitter (today: skill_anchor); a
    # stub with any other name would have its stdout correctly DROPPED, which
    # would silently defeat this test's own premise rather than exercise it.
    two_guard_cfg["dispatch"]["events"]["PreToolUse"] = [
        {"tools": "Bash|PowerShell", "guards": ["skill_anchor", "always_block"]}
    ]
    with _tmp.TemporaryDirectory(prefix="ulthar-chain-") as td:
        tdp = Path(td)
        (tdp / "skill_anchor.py").write_text(
            "from lib.io import Result\n"
            "def run(payload, cfg, ctx):\n"
            "    return Result(0, 'FIRST-GUARD-STDOUT', '')\n",
            encoding="utf-8",
        )
        (tdp / "always_block.py").write_text(
            "from lib.io import Result\n"
            "def run(payload, cfg, ctx):\n"
            "    return Result(2, '', 'SECOND-GUARD-BLOCKED')\n",
            encoding="utf-8",
        )
        cfg_path = tdp / "config.json"
        cfg_path.write_text(json.dumps(two_guard_cfg), encoding="utf-8")

        env = dict(_os.environ)
        env["ULTHAR_CONFIG"] = str(cfg_path)
        env["ULTHAR_GUARDS_DIR"] = str(tdp)
        env["USERPROFILE"] = td
        env["HOME"] = td

        payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                   "tool_input": {"command": "echo hi"}}
        # NO --guard: exercises the FULL chain (routing + I-9 order), the thing
        # a --guard-only run (parity.py's hardcoded single-guard invocation) never touches.
        proc = _sp.run(
            [sys.executable, str(HOOKS_DIR / "dispatch.py"), "--event", "PreToolUse"],
            input=json.dumps(payload).encode("utf-8"), capture_output=True, timeout=15, env=env,
        )
        p.check("B2 chain: guard-1's ALLOW does not stop the chain (guard-2 still runs)",
                proc.returncode == 2, f"rc={proc.returncode}")
        p.check("B2 chain: guard-2's block reason is the one that surfaces",
                proc.stderr.decode("utf-8", "replace") == "SECOND-GUARD-BLOCKED",
                repr(proc.stderr))
        p.check("B2 chain: guard-1's stdout (produced before the block) is NOT discarded (I-9)",
                b"FIRST-GUARD-STDOUT" in proc.stdout, repr(proc.stdout))


def check_ownership(p: Probe, cfg: dict) -> None:
    ids = set()
    for rules in cfg["dispatch"]["events"].values():
        for rule in rules:
            ids |= set(rule.get("guards", []))
    missing_table = sorted(ids - set(OWNERSHIP_TABLE))
    p.check("check5: every dispatch.events guard id has a §5 ownership row",
            not missing_table, str(missing_table))
    missing_module = [gid for gid, (mod, _fx, _own) in OWNERSHIP_TABLE.items()
                       if gid in ids and not (HOOKS_DIR / mod).exists()]
    # HARD assertion: a guard id with no module on disk is a real gap now, not an
    # expected pre-fan-out state.
    p.check("check5: every owned guard has its module on disk",
            not missing_module, str(missing_module))


# ------------------------------------------------------------------ check 6: one stdout emitter/chain

STDOUT_EMITTERS = {"skill_anchor"}


def check_one_emitter(p: Probe, cfg: dict) -> None:
    bad = []
    for event, rules in cfg["dispatch"]["events"].items():
        for rule in rules:
            emitters = [g for g in rule.get("guards", []) if g in STDOUT_EMITTERS]
            if len(emitters) > 1:
                bad.append((event, rule.get("guards")))
    p.check("check6: at most one stdout emitter per dispatch chain", not bad, str(bad))


# ------------------------------------------------------------------ check 7: every PE has a fixture


def check_pe_fixtures(p: Probe) -> None:
    # No hand-maintained literal here either -- single source of truth is
    # parity.py's own PE_IDS, itself derived from CONTRACT.md §7 (see
    # parity._derive_pe_ids). Pins against a hand-maintained duplicate: re-stating
    # the range here was exactly what let PE-17 ship unnoticed for a while.
    sys.path.insert(0, str(TESTS_DIR))
    import parity  # noqa: E402
    PE_IDS = sorted(parity.PE_IDS, key=lambda s: int(s.split("-")[1]))

    fixtures_dir = HOOKS_DIR / "tests" / "fixtures"
    cited: set[str] = set()
    if fixtures_dir.exists():
        for f in fixtures_dir.rglob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            entries = data if isinstance(data, list) else [data]
            for entry in entries:
                if isinstance(entry, dict) and entry.get("parity") is False:
                    exc = entry.get("parity_exception")
                    if exc:
                        cited.add(exc)
    missing = [pe for pe in PE_IDS if pe not in cited]
    # HARD assertion. PE-13 is EXPECTED to still show here if its fixture has not
    # landed as of this run -- a real, correctly-reported gap, not a bug in this
    # check.
    p.check("check7: every §7 parity exception is cited by a parity:false fixture",
            not missing, f"not cited: {missing}")


def check_pe_citations_not_undeclared(p: Probe) -> None:
    """check7 above only ever inspects fixture `parity_exception` fields --
    a PE id cited in a docstring, a code comment, or a config.json `_note`
    is invisible to it, which is exactly how a stray undeclared PE id shipped in this
    tree without ever being declared in CONTRACT-ARCHIVE.md (2026-09-05
    send-back). This scans the WHOLE skill tree (parity.scan_source_pe_citations)
    for any `PE-<n>` mention and fails if its id exceeds the highest one
    CONTRACT-ARCHIVE.md actually declares -- closing the channel, not just
    the one instance."""
    sys.path.insert(0, str(TESTS_DIR))
    import parity  # noqa: E402
    bad = parity.undeclared_source_pe_citations(SKILL_ROOT)
    p.check("check7b: no PE-<n> citation anywhere in the tree exceeds the "
            "declared maximum in CONTRACT-ARCHIVE.md",
            not bad, {n: [str(f) for f in fs] for n, fs in bad.items()})


# ------------------------------------------------------------------ dispatch.py smoke, via the stub guard


def check_run_cmd_kills_whole_process_tree_on_timeout(p: Probe, cfg: dict) -> None:
    """Pins process-tree kill on timeout: plain `subprocess.run(timeout=...)` on
    Windows only kills the IMMEDIATE child. A `.cmd` executable that spawns a further
    child (exactly the shape shutil.which() now makes reachable) leaves that
    grandchild running as an orphan holding the same stdout/stderr pipe
    handles -- measured over 60s against a grandchild sleeping 120s, when
    the configured timeout was 20s. Fixed with Popen + `taskkill /F /T` tree-
    kill on timeout (Ctx._kill_process_tree). Proves a `.cmd` wrapping a
    LONG-sleeping grandchild is bounded by the configured timeout (a few
    seconds' margin, not the grandchild's full sleep duration)."""
    import tempfile as _tmp
    import time as _time

    sys.path.insert(0, str(HOOKS_DIR))
    from lib import ctx as ctx_mod  # noqa: E402

    with _tmp.TemporaryDirectory(prefix="ulthar-treekill-") as td:
        tdp = Path(td)
        cmd_path = tdp / "ulthartreekilltool7q2m.cmd"
        # Same shape as the real gh.cmd shim: a .cmd relay calling python.exe
        # SYNCHRONOUSLY, which sleeps far longer than the configured timeout
        # -- the exact case that hung before the fix (measured 60s+ against a
        # 120s sleep at a 20s timeout in the live sweep).
        py_script = tdp / "sleeper.py"
        py_script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
        cmd_path.write_text(
            f'@echo off\r\n"{sys.executable}" "{py_script}"\r\n',
            encoding="utf-8",
        )

        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = str(tdp) + os.pathsep + old_path
        try:
            class _FakeCtx(ctx_mod.Ctx):
                pass
            fc = object.__new__(_FakeCtx)
            fc.cfg = {"thresholds": {"subprocess_timeout_remote_sec": 2, "subprocess_timeout_local_sec": 2}}
            fc._run_cmd_fn = None
            fc.log_error = lambda *a, **k: None
            t0 = _time.time()
            result = fc.run_cmd(["ulthartreekilltool7q2m", "arg"], "local")
            elapsed = _time.time() - t0
            p.check("run_cmd: a .cmd wrapping a long-sleeping grandchild is bounded "
                    "by the configured timeout (not the grandchild's full sleep)",
                    result.timed_out and elapsed < 15,
                    f"timed_out={result.timed_out} elapsed={elapsed:.1f}s")
        finally:
            os.environ["PATH"] = old_path


def check_install_register_settings_regression(p: Probe) -> None:
    """Mutation testing against install.py found six surviving revert-mutations,
    all in register_settings, copy_deploy, and check_dependencies -- because
    nothing in tests/ exercised those functions at all. This kills those six
    mutants: pre-state validation, strict replacement count, post-write
    self-verify + automatic rollback, and backup-name uniqueness.

    ponytail: a small, hand-picked set of revert-mutations, not an exhaustive
    mutation-testing pass; a real mutation-testing tool (mutmut/cosmic-ray)
    over install.py is the upgrade path if broader coverage is ever needed."""
    import tempfile as _tmp
    import subprocess as _sp

    sys.path.insert(0, str(HOOKS_DIR))
    import install as install_mod  # noqa: E402

    # Derived from config.dispatch.events, not restated by hand -- a hardcoded
    # event list here is the exact defect class that left a dead PostCompact
    # registration in settings.json outliving its own dispatch.events removal
    # (PE-20). install.py's own cmd_register_settings already derives
    # expected_events the same way; mirroring that here means this regression
    # test tracks the real registered event set instead of a stale snapshot.
    with open(install_mod.SKILL_ROOT / "config.json", "r", encoding="utf-8-sig") as _fh:
        _cfg = json.load(_fh)
    expected_events = sorted(_cfg.get("dispatch", {}).get("events", {}).keys())

    with _tmp.TemporaryDirectory(prefix="ulthar-regsettings-") as td:
        tdp = Path(td)
        deploy_dir = tdp / "deploy"

        # Hermeticity: install.py's _merge_user_overlay reads Path.home() (via
        # ctx_mod.build_ctx({}, ...) -- install.py has no payload to carry the
        # dispatch.py-style _home_override test seam). Left uncontrolled, this
        # subprocess would merge the REAL ~/.claude/ulthar-overlay.json, so the
        # result depends on whatever machine runs the suite -- the exact defect
        # class under review. Point HOME/USERPROFILE at a fresh, overlay-free
        # directory instead (same technique dispatch.py subprocess tests already
        # use elsewhere in this file via env["HOME"]/env["USERPROFILE"]).
        home_dir = tdp / "home"
        (home_dir / ".claude").mkdir(parents=True)

        def _run(args: list[str], cwd_deploy: Path) -> tuple[int, str]:
            env = dict(os.environ)
            env["USERPROFILE"] = str(home_dir)
            env["HOME"] = str(home_dir)
            proc = _sp.run(
                [sys.executable, str(HOOKS_DIR / "install.py")] + args,
                capture_output=True, timeout=30, env=env,
            )
            return proc.returncode, (proc.stdout + proc.stderr).decode("utf-8", "replace")

        rc, out = _run(["--deploy-dir", str(deploy_dir)], deploy_dir)
        p.check("register_settings regression: deploy succeeds first (precondition)",
                rc == 0, out)
        if rc != 0:
            # Precondition failed: deploy_dir was never populated. Everything
            # below assumes a real deploy (e.g. sabotaging deploy_dir/dispatch.py
            # in step 5), so continuing would crash on a file that was never
            # created and report THAT crash instead of this real failure.
            return

        settings_path = tdp / "settings.json"
        good_settings = {
            "hooks": {
                ev: [{"matcher": "*", "hooks": [
                    {"type": "command",
                     "command": f'powershell -File "dispatch.ps1" -Event {ev}'}
                ]}]
                for ev in expected_events
            }
        }
        settings_path.write_text(json.dumps(good_settings), encoding="utf-8")

        # (1) Normal registration succeeds, writes exactly len(expected_events), self-verifies.
        rc, out = _run(["--register-settings", "--confirm-parity-passed",
                         "--settings-path", str(settings_path), "--deploy-dir", str(deploy_dir)],
                        deploy_dir)
        p.check("register_settings regression: normal run succeeds and self-verifies",
                rc == 0 and f"Rewrote {len(expected_events)}" in out and "VERIFIED" in out, out)

        # (2) Pre-state validation: running it AGAIN (now .py, not .ps1) must
        # be REFUSED, not silently re-rewritten (mutant: skip pre-state check).
        rc2, out2 = _run(["--register-settings", "--confirm-parity-passed",
                           "--settings-path", str(settings_path), "--deploy-dir", str(deploy_dir)],
                          deploy_dir)
        p.check("register_settings regression: re-running against an already-cutover "
                "file is REFUSED (pre-state validation)",
                rc2 == 1 and "REFUSING" in out2, out2)

        # (3) Backup uniqueness: two backups must both exist (not one overwriting
        # the other) even though only one register actually happened above --
        # verify by forcing a second backup path collision directly.
        b1 = install_mod._unique_backup_path(settings_path)
        b1.write_text("x", encoding="utf-8")
        b2 = install_mod._unique_backup_path(settings_path)
        p.check("register_settings regression: _unique_backup_path never returns "
                "a colliding path", b1 != b2 and not b2.exists(), f"b1={b1} b2={b2}")

        # (4) Strict replacement count: settings with one of expected_events
        # MISSING from the expected pre-cutover shape must be REFUSED, not
        # partially rewritten. Drops the last (alphabetically) expected event
        # -- whichever one that is -- so this stays a real negative case no
        # matter how dispatch.events is reshaped later.
        missing_event = expected_events[-1]
        partial_events = expected_events[:-1]
        partial_settings_path = tdp / "settings-partial.json"
        partial = {
            "hooks": {
                ev: [{"matcher": "*", "hooks": [
                    {"type": "command",
                     "command": f'powershell -File "dispatch.ps1" -Event {ev}'}
                ]}]
                for ev in partial_events  # missing_event deliberately absent
            }
        }
        partial_settings_path.write_text(json.dumps(partial), encoding="utf-8")
        rc3, out3 = _run(["--register-settings", "--confirm-parity-passed",
                           "--settings-path", str(partial_settings_path), "--deploy-dir", str(deploy_dir)],
                          deploy_dir)
        p.check("register_settings regression: a settings.json missing an expected "
                "event is REFUSED before any write (pre-state validation)",
                rc3 == 1 and "REFUSING" in out3, out3)
        still_original = json.loads(partial_settings_path.read_text(encoding="utf-8")) == partial
        p.check("register_settings regression: the refused file is UNCHANGED on disk",
                still_original)

        # (4b) Pre-state validation SPECIFICALLY (isolated from the
        # replacement-count check, which would also catch this downstream --
        # a DOUBLE .ps1 registration for one event is a pre-state problem in
        # its own right and must be caught before any replacement is even
        # attempted).
        double_ps1_settings_path = tdp / "settings-doublepre.json"
        double_pre = dict(good_settings)
        double_pre = json.loads(json.dumps(good_settings))  # deep copy
        double_pre["hooks"]["Stop"].append({"matcher": "*", "hooks": [
            {"type": "command", "command": 'powershell -File "dispatch.ps1" -Event Stop'}
        ]})
        double_ps1_settings_path.write_text(json.dumps(double_pre), encoding="utf-8")
        rc3b, out3b = _run(["--register-settings", "--confirm-parity-passed",
                             "--settings-path", str(double_ps1_settings_path),
                             "--deploy-dir", str(deploy_dir)], deploy_dir)
        p.check("register_settings regression: a DOUBLE dispatch.ps1 registration "
                "pre-rewrite is REFUSED by pre-state validation specifically",
                rc3b == 1 and "REFUSING" in out3b and "Stop" in out3b, out3b)

        # (5) Post-write self-verify + automatic rollback: corrupt the deployed
        # dispatch.py so the post-write check (dispatch.py path must exist) fails,
        # then confirm register-settings rolls back rather than leaving a broken file.
        fresh_settings_path = tdp / "settings-fresh.json"
        fresh_settings_path.write_text(json.dumps(good_settings), encoding="utf-8")
        (deploy_dir / "dispatch.py").unlink()  # sabotage: deployed path now missing
        rc4, out4 = _run(["--register-settings", "--confirm-parity-passed",
                           "--settings-path", str(fresh_settings_path), "--deploy-dir", str(deploy_dir)],
                          deploy_dir)
        p.check("register_settings regression: a broken deploy (missing dispatch.py) "
                "fails post-write verification and ROLLS BACK",
                rc4 == 1 and "VERIFICATION FAILED" in out4 and "rolling back" in out4, out4)
        rolled_back = json.loads(fresh_settings_path.read_text(encoding="utf-8"))
        p.check("register_settings regression: after rollback, settings.json is back "
                "to dispatch.ps1 (not left half-rewritten)",
                all("dispatch.ps1" in rules[0]["hooks"][0]["command"]
                    for rules in rolled_back["hooks"].values()), rolled_back)


def check_install_copy_deploy_regression(p: Probe) -> None:
    """Pins that copy_deploy stages into a sibling temp directory and swaps via
    os.replace rather than rmtree-then-copytree in place. (a) a normal deploy
    leaves no `.tmp-deploy`/`.old-deploy` artefacts behind and a repeated
    (swap-path) deploy still produces a complete tree. (b) the point actually
    exercised: if staging the NEW tree fails partway (simulated by
    monkeypatching shutil.copytree to raise), the OLD deployed tree must still
    be intact and importable afterwards -- the old rmtree-then-copytree-in-place
    implementation would have already deleted the old tree before this failure,
    leaving guards unable to load. This is the mutant `revert to rmtree(dst);
    copytree(src, dst)` would NOT catch on (a) alone."""
    import tempfile as _tmp
    from unittest import mock

    sys.path.insert(0, str(HOOKS_DIR))
    import install as install_mod  # noqa: E402

    with _tmp.TemporaryDirectory(prefix="ulthar-copydeploy-") as td:
        deploy_dir = Path(td) / "deploy"
        install_mod.copy_deploy(deploy_dir)
        install_mod.copy_deploy(deploy_dir)  # second call exercises the SWAP path
        leftovers = [p2 for p2 in deploy_dir.iterdir()
                     if p2.name.endswith(".tmp-deploy") or p2.name.endswith(".old-deploy")]
        p.check("copy_deploy regression: no staging/backup artefacts left behind "
                "after a swap-path deploy", leftovers == [], leftovers)
        p.check("copy_deploy regression: lib/ and guards/ both landed completely",
                (deploy_dir / "lib" / "ctx.py").exists()
                and (deploy_dir / "guards" / "__init__.py").exists())

        # (b) Simulate an interruption while staging the REPLACEMENT tree
        # (a third deploy, swap path). The old tree at deploy_dir/lib must
        # survive completely untouched.
        real_copytree = install_mod.shutil.copytree
        call_count = {"n": 0}

        def _flaky_copytree(src, dst, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1 and Path(dst).name.startswith("lib"):
                raise OSError("simulated interruption while staging the new lib/ tree")
            return real_copytree(src, dst, **kwargs)

        with mock.patch.object(install_mod.shutil, "copytree", side_effect=_flaky_copytree):
            try:
                install_mod.copy_deploy(deploy_dir)
                raised = False
            except OSError:
                raised = True
        p.check("copy_deploy regression: a staging failure DOES propagate "
                "(the test's own simulated interruption actually fired)", raised)
        p.check("copy_deploy regression: after a staging failure, the OLD "
                "deployed lib/ tree is STILL INTACT and importable (not "
                "deleted before the replacement was ready)",
                (deploy_dir / "lib" / "ctx.py").exists()
                and (deploy_dir / "lib" / "gitcmd.py").exists())


def check_install_dependency_check_pinned_interpreter(p: Probe) -> None:
    """Pins that check_dependencies must run under the PINNED interpreter
    (cfg.paths.python_exe), not whichever
    interpreter happens to run install.py, and must actually detect an
    unimportable module (proving it is a real subprocess-based check, not a
    call that always trivially succeeds)."""
    sys.path.insert(0, str(HOOKS_DIR))
    import install as install_mod  # noqa: E402

    bogus = {"paths": {"python_exe": "C:/ULTHAR-Regression-No-Such-Interpreter/python.exe"},
             "dependencies": {"required": [{"module": "psutil", "verified_version": "x",
                                             "install_location": "pip"}]}}
    problems = install_mod.check_dependencies(bogus)
    p.check("dependency check regression: a bogus PINNED interpreter path is "
            "detected (not silently ignored)",
            bool(problems) and "psutil" in problems[0], problems)

    real = {"paths": {"python_exe": sys.executable},
            "dependencies": {"required": [{"module": "psutil", "verified_version": "x",
                                            "install_location": "pip"}]}}
    p.check("dependency check regression: the real pinned interpreter with psutil "
            "installed reports no problems",
            install_mod.check_dependencies(real) == [])

    missing_module = {"paths": {"python_exe": sys.executable},
                       "dependencies": {"required": [{"module": "ulthar_no_such_module_xyz",
                                                       "verified_version": "x",
                                                       "install_location": "pip"}]}}
    problems2 = install_mod.check_dependencies(missing_module)
    p.check("dependency check regression: a genuinely missing module is detected "
            "under the pinned interpreter",
            bool(problems2) and "ulthar_no_such_module_xyz" in problems2[0], problems2)


def check_run_cmd_resolves_pathext_only_executable(p: Probe, cfg: dict) -> None:
    """Pins that `Ctx.run_cmd` resolves `argv[0]` through `shutil.which()` before
    invoking `subprocess.run` -- a FIDELITY fix, not a test-only one. The masters
    invoke `gh`/`git` from PowerShell, which
    resolves PATHEXT (.exe/.com/.bat/.cmd/...); `subprocess.run([name, ...],
    shell=False)` on Windows does NOT (Windows' CreateProcess only tries
    .exe/.com for a bare command name), so a user whose `gh` is a `.cmd` (a
    common shape for several installers) was served by the master and would
    NOT have been served by the port before this fix. Proves a bare command
    name resolvable ONLY via a `.cmd` extension (no `.exe` variant present)
    is found and actually invoked."""
    import tempfile as _tmp

    sys.path.insert(0, str(HOOKS_DIR))
    from lib import ctx as ctx_mod  # noqa: E402

    with _tmp.TemporaryDirectory(prefix="ulthar-pathext-") as td:
        tdp = Path(td)
        # A distinctive name so it cannot collide with anything already on PATH.
        cmd_path = tdp / "ultharpathexttool9f3k.cmd"
        cmd_path.write_text("@echo off\r\necho PATHEXT_OK\r\n", encoding="utf-8")

        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = str(tdp) + os.pathsep + old_path
        try:
            class _FakeCtx:
                pass
            fc = _FakeCtx()
            fc.cfg = cfg
            fc._run_cmd_fn = None
            fc.log_error = lambda *a, **k: None
            result = ctx_mod.Ctx.run_cmd(fc, ["ultharpathexttool9f3k", "arg1"], "local")
            p.check("run_cmd: a bare name resolvable ONLY via a .cmd (PATHEXT) "
                    "extension is found and invoked",
                    result.rc == 0 and "PATHEXT_OK" in result.out,
                    f"rc={result.rc} out={result.out!r} err={result.err!r}")
        finally:
            os.environ["PATH"] = old_path


def check_dispatch_smoke(p: Probe, cfg: dict) -> None:
    """End-to-end exercise of dispatch.py against the tests-only stub guard, proving
    the spine (config load, Ctx construction, routing, I-3/I-3b/I-9) works before any
    real guard exists."""
    import json as _json
    import os as _os
    import subprocess as _sp
    import tempfile

    stub_dir = TESTS_DIR / "fixtures" / "stub"
    stub_cfg = _json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    # Route Bash -> a single guard id "stub_guard" so --guard stub_guard exercises it.
    stub_cfg["dispatch"]["events"]["PreToolUse"] = [
        {"tools": "Bash|PowerShell", "guards": ["stub_guard"]}
    ]

    with tempfile.TemporaryDirectory(prefix="ulthar-dispatch-smoke-") as td:
        cfg_path = Path(td) / "config.json"
        cfg_path.write_text(_json.dumps(stub_cfg), encoding="utf-8")

        env = dict(_os.environ)
        env["ULTHAR_CONFIG"] = str(cfg_path)
        env["ULTHAR_GUARDS_DIR"] = str(stub_dir)
        env["USERPROFILE"] = td
        env["HOME"] = td

        def run(command: str) -> tuple[int, str]:
            payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                       "tool_input": {"command": command}}
            proc = _sp.run(
                [sys.executable, str(HOOKS_DIR / "dispatch.py"), "--event", "PreToolUse",
                 "--guard", "stub_guard"],
                input=_json.dumps(payload).encode("utf-8"),
                capture_output=True, timeout=15, env=env,
            )
            return proc.returncode, proc.stderr.decode("utf-8", "replace")

        rc, err = run("ALLOW")
        p.check("dispatch smoke: ALLOW -> exit 0", rc == 0, f"rc={rc} err={err!r}")

        rc, err = run("BLOCK:nope")
        p.check("dispatch smoke: BLOCK:<msg> -> exit 2 with reason", rc == 2 and err == "nope",
                f"rc={rc} err={err!r}")

        rc, err = run("BLOCK_NO_REASON")
        p.check("dispatch smoke: I-3b row3 (exit2, empty stderr) KEEPS the block, synthesises reason",
                rc == 2 and "no reason supplied" in err, f"rc={rc} err={err!r}")

        rc, err = run("RAISE")
        p.check("dispatch smoke: I-3 guard exception fails open (exit 0)", rc == 0, f"rc={rc} err={err!r}")

        rc, err = run("BAD_TYPE")
        p.check("dispatch smoke: I-3b row1 (non-Result return) fails open", rc == 0, f"rc={rc} err={err!r}")

        rc, err = run("BAD_EXIT")
        p.check("dispatch smoke: I-3b row2 (exit_code=1) fails open", rc == 0, f"rc={rc} err={err!r}")

        rc, err = run("STDOUT_LEAK")
        p.check("dispatch smoke: I-3b row4 (unexpected stdout, exit0) still allows",
                rc == 0, f"rc={rc} err={err!r}")

        jp_msg = "完成の定義が不明です"
        rc, err = run("BLOCK:" + jp_msg)
        p.check("dispatch smoke: acceptance row 5 -- Japanese stderr round-trips as UTF-8",
                rc == 2 and err == jp_msg, f"rc={rc} err={err!r}")


# ------------------------------------------------------------------ REAL dispatch.py, REAL guards/
#
# Pins _import_guard's production branch
# (`importlib.import_module(f"guards.{guard_id}")`), which the smoke test above
# never exercises since it routes through ULTHAR_GUARDS_DIR. That branch once had an
# UnboundLocalError caused by a locally-scoped `import importlib.util`, which
# _run_guard's fail-open catch silently turned into ALLOW -- so every real guard
# was unreachable through dispatch.py while every ULTHAR_GUARDS_DIR-routed test (and
# every test that calls guards.*.run() directly) stayed green. This check uses no
# override, the REAL guards/ directory, and a payload known to block, asserting
# exit 2.


def check_install_refuses_invalid_config(p: Probe) -> None:
    """PE-11's actual automated evidence (the fixture in tests/fixtures/dispatch/
    pe8_pe11.json is a citation marker only, since install.py is a CLI, not a
    dispatch.py guard). Builds a throwaway skill root whose config.json has one
    UNRESOLVABLE pattern_fields path, runs `install.py` against a temp deploy
    dir, and asserts it refuses (non-zero exit, 'REFUSING TO DEPLOY' on stdout)
    without creating anything in the deploy dir."""
    import subprocess as _sp
    import tempfile as _tmp
    import shutil as _shutil

    with _tmp.TemporaryDirectory(prefix="ulthar-pe11-") as td:
        tdp = Path(td)
        skill_root = tdp / "skill"
        (skill_root / "hooks" / "lib").mkdir(parents=True)
        (skill_root / "hooks" / "guards").mkdir(parents=True)
        for f in ["io.py", "ctx.py", "paths.py", "procs.py", "gitcmd.py", "grant.py"]:
            _shutil.copy2(HOOKS_DIR / "lib" / f, skill_root / "hooks" / "lib" / f)
        (skill_root / "hooks" / "lib" / "__init__.py").write_text("", encoding="utf-8")
        (skill_root / "hooks" / "guards" / "__init__.py").write_text("", encoding="utf-8")
        _shutil.copy2(HOOKS_DIR / "dispatch.py", skill_root / "hooks" / "dispatch.py")
        _shutil.copy2(HOOKS_DIR / "install.py", skill_root / "hooks" / "install.py")
        (skill_root / "hooks" / "stop_dev_orphans.py").write_text(
            "def main(argv):\n    return 0\n", encoding="utf-8")

        bad_cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        # Break the declared pattern_fields path `command_rules.tool_discipline[].pattern`
        # (rename the key so the declared JSON path no longer resolves) without
        # touching anything else. EVERY entry carrying that key has to be renamed,
        # not just [0]: a `[]` path resolves if ANY element still carries it, so
        # once a second regex_block rule existed (authority-file-in-shell) breaking
        # one element left the path resolvable and this check silently stopped
        # exercising a refusal at all.
        for rule in bad_cfg["command_rules"]["tool_discipline"]:
            if "pattern" in rule:
                rule["pattern_RENAMED"] = rule.pop("pattern")
        (skill_root / "config.json").write_text(json.dumps(bad_cfg), encoding="utf-8")

        deploy_dir = tdp / "deploy"
        proc = _sp.run(
            [sys.executable, str(skill_root / "hooks" / "install.py"), "--deploy-dir", str(deploy_dir)],
            capture_output=True, timeout=30,
        )
        out = proc.stdout.decode("utf-8", "replace")
        p.check("PE-11: install.py refuses to deploy an invalid config (non-zero exit)",
                proc.returncode != 0, f"rc={proc.returncode}")
        p.check("PE-11: install.py prints REFUSING TO DEPLOY", "REFUSING TO DEPLOY" in out,
                out[:300])
        p.check("PE-11: the deploy dir was never populated", not deploy_dir.exists())


def check_pretooluse_zero_subprocess_spawns(p: Probe) -> None:
    """Acceptance row 9, MEASURED not asserted: a PreToolUse dispatch through the
    FULL in-process chain must spawn ZERO child processes for a payload that
    never needs one (a plain, non-git Bash command past every guard's early
    ALLOW gates). Monkeypatches subprocess.Popen (what subprocess.run ultimately
    calls) for the duration of one in-process dispatch() call and counts
    invocations."""
    import subprocess as _sp

    sys.path.insert(0, str(HOOKS_DIR))
    sys.path.insert(0, str(TESTS_DIR))
    import dispatch as dispatch_mod  # noqa: E402
    from lib import ctx as ctx_mod  # noqa: E402
    import conftest  # noqa: E402

    sandbox = conftest.SandboxCtx()
    try:
        payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                   "tool_input": {"command": "echo hello world"}}
        ctx = sandbox.build(payload=payload, event="PreToolUse")

        spawn_count = 0
        real_popen = _sp.Popen

        def _counting_popen(*a, **kw):
            nonlocal spawn_count
            spawn_count += 1
            return real_popen(*a, **kw)

        _sp.Popen = _counting_popen
        try:
            dispatch_mod.dispatch("PreToolUse", payload, sandbox.cfg, ctx, single_guard=None)
        finally:
            _sp.Popen = real_popen

        p.check("row9: a plain, non-git PreToolUse dispatch spawns ZERO child processes",
                spawn_count == 0, f"spawn_count={spawn_count}")
    finally:
        sandbox.cleanup()


def check_dispatch_real_guards_end_to_end(p: Probe) -> None:
    import subprocess as _sp
    import os as _os

    if not (HOOKS_DIR / "guards" / "tool_discipline.py").exists():
        p.check("dispatch REAL guards: cd-compound blocks via tool_discipline "
                 "(guards/tool_discipline.py not deployed yet)", True)
        return

    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "cd /tmp && rm -rf foo"},
    }
    env = dict(_os.environ)
    env.pop("ULTHAR_GUARDS_DIR", None)  # the whole point: NO override, real guards/
    env["ULTHAR_CONFIG"] = str(CONFIG_PATH)  # config sits at the skill root,
    # not next to hooks/dispatch.py the way a real deploy has it -- ULTHAR_CONFIG is
    # the documented test-only seam for exactly this (CONTRACT.md #0).

    proc = _sp.run(
        [sys.executable, str(HOOKS_DIR / "dispatch.py"), "--event", "PreToolUse",
         "--guard", "tool_discipline"],
        input=json.dumps(payload).encode("utf-8"), capture_output=True, timeout=15, env=env,
    )
    p.check(
        "dispatch REAL guards (no ULTHAR_GUARDS_DIR override): cd-compound blocks via "
        "the actual guards/tool_discipline.py",
        proc.returncode == 2,
        f"rc={proc.returncode} stderr={proc.stderr.decode('utf-8', 'replace')!r} "
        f"stdout={proc.stdout.decode('utf-8', 'replace')!r}",
    )

    # A second real-guard case, routed through the FULL chain (no --guard), so the
    # dispatch.events routing + _tool_matches path is exercised end to end too, not
    # only the --guard single-mode shortcut.
    proc2 = _sp.run(
        [sys.executable, str(HOOKS_DIR / "dispatch.py"), "--event", "PreToolUse"],
        input=json.dumps(payload).encode("utf-8"), capture_output=True, timeout=15, env=env,
    )
    p.check(
        "dispatch REAL guards, FULL chain (no --guard): cd-compound still blocks",
        proc2.returncode == 2,
        f"rc={proc2.returncode} stderr={proc2.stderr.decode('utf-8', 'replace')!r}",
    )


def check_b3_gitcmd_case_insensitive(p: Probe, cfg: dict) -> None:
    """B3 red-then-green: refs_heads_strip_pattern (kind:"text", always
    IGNORECASE) must strip 'refs/Heads/' case-insensitively -- it was compiled
    with a bare re.compile() and no flags, so 'refs/Heads/feat' was never
    stripped and pr_state's branch lookup missed the PR entirely."""
    sys.path.insert(0, str(HOOKS_DIR))
    from lib import gitcmd  # noqa: E402

    branch = gitcmd.push_target_branch("git push origin refs/Heads/feat", cfg)
    p.check("B3: push_target_branch strips 'refs/Heads/' case-insensitively",
            branch == "feat", repr(branch))


def check_strip_quotes_unterminated_quote(p: Probe, cfg: dict) -> None:
    """Pins addendum 2: an UNTERMINATED quote must be left untouched, not
    swallowed to end-of-string.
    The master's `-replace '"[^"]*"'` requires a CLOSING quote, so
    `git commit -m "x -a` has NOTHING stripped and `-a` stays visible for the
    blanket-staging rule to see. The old hand-rolled scanner's `if j < n or
    j == n` was always true and replaced the entire unterminated remainder
    (including `-a`) with `""` -- a real loosening (ALLOWED what the master
    BLOCKED)."""
    sys.path.insert(0, str(HOOKS_DIR))
    from lib import gitcmd  # noqa: E402

    unterminated = 'git commit -m "x -a'
    out = gitcmd.strip_quotes(unterminated, cfg)
    p.check("strip_quotes: an UNTERMINATED quote is left alone, '-a' stays visible",
            "-a" in out, repr(out))

    terminated = 'git commit -m "drop the -a flag"'
    out2 = gitcmd.strip_quotes(terminated, cfg)
    p.check("strip_quotes: a properly closed quote IS stripped, '-a' disappears",
            "-a" not in out2, repr(out2))


def check_log_error_uses_expand(p: Probe) -> None:
    """Pins addendum 3, previously only exercised indirectly via
    log_config_error's guard_load path: Ctx.log_error (the OTHER logging method,
    hit when a guard raises
    at RUNTIME rather than failing to import) used to build Path(cfg[...])
    directly, skipping paths.expand(), so the literal '~' was never resolved and
    the runtime silently created a '~/.claude/hooks/logs/' tree under the
    process's cwd instead of the real log file under HOME. This drives a guard
    through dispatch's *generic* exception branch (ctx.log_error at
    dispatch.py's line ~185), not the guard_load/import-failure branch, so it
    is a distinct code path from check_guard_load_error_is_loud."""
    import subprocess as _sp
    import os as _os
    import tempfile as _tmp

    with _tmp.TemporaryDirectory(prefix="ulthar-logerror-") as td:
        tdp = Path(td)
        stub_dir = HOOKS_DIR / "tests" / "fixtures" / "stub"

        # single_guard mode only runs a guard that also appears in the real
        # routing table (dispatch()'s `_guards_to_run` filters against
        # rule["guards"]) -- "stub_guard" is not a production guard id, so it
        # must be wired into a throwaway config's routing table the same way
        # check_guard_load_error_is_loud does, or dispatch() silently never
        # calls _run_guard at all and this test would pass for the wrong reason
        # (no call made, therefore no log, therefore a FAIL that looks like a
        # missing expand() but is actually a routing miss).
        stub_cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        stub_cfg["dispatch"]["events"]["PreToolUse"] = [{"tools": "Bash", "guards": ["stub_guard"]}]
        cfg_path = tdp / "config.json"
        cfg_path.write_text(json.dumps(stub_cfg), encoding="utf-8")

        env = dict(_os.environ)
        env["ULTHAR_CONFIG"] = str(cfg_path)
        env["ULTHAR_GUARDS_DIR"] = str(stub_dir)
        env["USERPROFILE"] = td
        env["HOME"] = td

        payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                   "tool_input": {"command": "RAISE"}}
        proc = _sp.run(
            [sys.executable, str(HOOKS_DIR / "dispatch.py"), "--event", "PreToolUse",
             "--guard", "stub_guard"],
            input=json.dumps(payload).encode("utf-8"), capture_output=True, timeout=15, env=env,
        )
        p.check("log_error: a guard that raises at RUNTIME fails OPEN (exit 0)",
                proc.returncode == 0, f"rc={proc.returncode}")

        error_log = tdp / ".claude" / "hooks" / "logs" / "dispatch-errors.jsonl"
        p.check("log_error: dispatch-errors.jsonl is written under the resolved "
                "HOME (paths.expand honoured, not skipped)",
                error_log.exists(), str(error_log))
        if error_log.exists():
            p.check("log_error: the entry names the raising guard",
                    "stub_guard" in error_log.read_text(encoding="utf-8"))

        literal_tilde = tdp / "~"
        p.check("log_error: no literal '~' directory was created (expand() was "
                "actually applied, not skipped)", not literal_tilde.exists())


def check_addendum6_root_match_platform_case(p: Probe, cfg: dict) -> None:
    """Addendum 6 red-then-green: find_dev_processes' root-fragment match must be
    case-insensitive on Windows only (path-kind rule), never unconditionally --
    unconditional would reintroduce the POSIX loosening N-1 exists to prevent."""
    sys.path.insert(0, str(HOOKS_DIR))
    from lib import procs  # noqa: E402
    from unittest import mock

    fake_procs = [
        procs.ProcInfo(pid=1, ppid=1, name="node.exe", cmdline="node vite --root=/My/Repo"),
    ]

    class _Ctx:
        pass

    win_ctx, posix_ctx = _Ctx(), _Ctx()
    win_ctx.is_windows, posix_ctx.is_windows = True, False

    with mock.patch.object(procs, "list_processes", lambda ctx, cfg: fake_procs):
        win_hit = procs.find_dev_processes(cfg, win_ctx, orphans_only=False, root="/my/repo")
        posix_hit = procs.find_dev_processes(cfg, posix_ctx, orphans_only=False, root="/my/repo")
    p.check("addendum6: root match IS case-insensitive on Windows", bool(win_hit))
    p.check("addendum6: root match is NOT case-insensitive on POSIX (N-1)", not posix_hit)


def check_b1_bom_config_loads(p: Probe) -> None:
    """B1 red-then-green: a BOM'd config.json (PowerShell Set-Content -Encoding
    UTF8's default) must still load and dispatch correctly -- a plain 'utf-8'
    read raises on the BOM, hits main()'s sanctioned M-13 catch, and every guard
    goes silently disabled (exit 0 for a command that should BLOCK)."""
    import subprocess as _sp
    import os as _os
    import tempfile as _tmp

    with _tmp.TemporaryDirectory(prefix="ulthar-b1-bom-") as td:
        tdp = Path(td)
        cfg_text = CONFIG_PATH.read_text(encoding="utf-8")
        (tdp / "config.json").write_text(cfg_text, encoding="utf-8-sig")  # WRITES a BOM

        env = dict(_os.environ)
        env["ULTHAR_CONFIG"] = str(tdp / "config.json")
        payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                   "tool_input": {"command": "cd /tmp && rm -rf foo"}}
        proc = _sp.run(
            [sys.executable, str(HOOKS_DIR / "dispatch.py"), "--event", "PreToolUse",
             "--guard", "tool_discipline"],
            input=json.dumps(payload).encode("utf-8"), capture_output=True, timeout=15, env=env,
        )
        p.check("B1: a BOM'd config.json still loads and BLOCKS (not silently disabled)",
                proc.returncode == 2, f"rc={proc.returncode} stderr={proc.stderr!r}")


def check_b4_double_registration_detected(p: Probe) -> None:
    """B4 red-then-green, extended for finding 2 (CRITICAL): the OLD check,
    probed directly with `{}`, `{"hooks": {}}`, and a PreToolUse event whose
    only command is `echo ok`, reported NO problem in all three cases, because
    it only looked at events PRESENT in settings.json and substring-matched
    `dispatch.py` without validating the interpreter, the deployed path, the
    `--event` argument, or that ALL expected events (driven by
    cfg['dispatch']['events']) are covered. `--verify` would have reported the
    cutover succeeded when no hook fires at all. This test reproduces all three
    probes plus the original B4 double-registration/pre-cutover cases."""
    import tempfile as _tmp

    sys.path.insert(0, str(HOOKS_DIR))
    import install as install_mod  # noqa: E402

    with _tmp.TemporaryDirectory(prefix="ulthar-b4-") as td:
        deploy_dir = Path(td)
        dispatch_py = deploy_dir / "dispatch.py"
        dispatch_py.write_text("# stub\n", encoding="utf-8")
        python_exe = "C:/pinned-python/python.exe"
        cfg = {"paths": {"python_exe": python_exe},
               "dispatch": {"events": {"PreToolUse": []}}}
        good_cmd = f'"{python_exe}" "{dispatch_py}" --event PreToolUse'

        # --- direct probes: all three used to report NO problem. ---
        for label, settings in [
            ("{}", {}),
            ('{"hooks": {}}', {"hooks": {}}),
            ('PreToolUse -> echo ok', {"hooks": {"PreToolUse": [
                {"matcher": "*", "hooks": [{"type": "command", "command": "echo ok"}]}
            ]}}),
        ]:
            problems = install_mod._check_dispatcher_registration(settings, cfg, deploy_dir)
            p.check(f"B4/finding2: settings.json {label} -> reported as a problem "
                    f"(no dispatcher registered at all)",
                    bool(problems), str(problems))

        # A single .ps1-only registration is NOT the cutover-complete state --
        # `--verify` must FAIL it as "not yet cut over".
        single_ps1 = {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
            {"type": "command", "command": 'powershell -File "dispatch.ps1" -Event PreToolUse'}
        ]}]}}
        single_problems = install_mod._check_dispatcher_registration(single_ps1, cfg, deploy_dir)
        p.check("B4: exactly one .ps1-only registration -> reported as NOT YET CUT OVER",
                bool(single_problems) and "NOT YET CUT OVER" in single_problems[0]
                and "PreToolUse" in single_problems[0], str(single_problems))

        single_py = {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
            {"type": "command", "command": good_cmd}
        ]}]}}
        p.check("B4: exactly one CORRECT .py registration -> no problems reported "
                "(cutover complete)",
                install_mod._check_dispatcher_registration(single_py, cfg, deploy_dir) == [])

        double = {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
            {"type": "command", "command": 'powershell -File "dispatch.ps1" -Event PreToolUse'},
            {"type": "command", "command": good_cmd},
        ]}]}}
        problems = install_mod._check_dispatcher_registration(double, cfg, deploy_dir)
        p.check("B4: TWO dispatcher registrations for one event -> reported as a problem",
                bool(problems) and "PreToolUse" in problems[0]
                and "NOT YET CUT OVER" not in problems[0], str(problems))

        # --- finding 2's other explicit asks: interpreter / path / --event. ---
        wrong_interp = {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
            {"type": "command", "command": f'"C:/Some/Other/python.exe" "{dispatch_py}" --event PreToolUse'}
        ]}]}}
        p.check("finding2: a .py registration under the WRONG interpreter is reported",
                bool(install_mod._check_dispatcher_registration(wrong_interp, cfg, deploy_dir)))

        missing_dispatch_py = {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
            {"type": "command", "command": f'"{python_exe}" "{deploy_dir / "nope.py"}" --event PreToolUse'}
        ]}]}}
        p.check("finding2: a .py registration pointing at a NON-EXISTENT deployed "
                "path is reported",
                bool(install_mod._check_dispatcher_registration(missing_dispatch_py, cfg, deploy_dir)))

        wrong_event_arg = {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
            {"type": "command", "command": f'"{python_exe}" "{dispatch_py}" --event Stop'}
        ]}]}}
        p.check("finding2: a .py registration with the WRONG --event argument is reported",
                bool(install_mod._check_dispatcher_registration(wrong_event_arg, cfg, deploy_dir)))


def check_interpreter_comparison_pinned_vs_unpinned(p: Probe) -> None:
    """Send-back findings 3 (HIGH) and 4 (HIGH): the interpreter equality check in
    `_check_dispatcher_registration` used to compare raw bytes even though
    `lib.paths.norm_for_compare` already exists for exactly this (separator/case
    tolerant equality), AND it labelled a bare `sys.executable` fallback "the
    PINNED interpreter" when nothing was actually pinned.

    Finding 3: with a PIN configured, a registered spelling that differs only by
    separator style or drive-letter case must NOT be reported as drift.
    Finding 4: with NO pin configured (`paths.python_exe` empty), a registered
    interpreter that is a real, different-looking-but-existing path must NOT be
    reported as drift either -- only a genuinely nonexistent registered
    interpreter is a problem in that mode."""
    import tempfile as _tmp

    sys.path.insert(0, str(HOOKS_DIR))
    import install as install_mod  # noqa: E402

    with _tmp.TemporaryDirectory(prefix="ulthar-b4-interp-") as td:
        deploy_dir = Path(td)
        dispatch_py = deploy_dir / "dispatch.py"
        dispatch_py.write_text("# stub\n", encoding="utf-8")

        # --- finding 3: PINNED, separator- and case-only spelling difference. ---
        cfg_pinned = {"paths": {"python_exe": "c:/pinned-python/PYTHON.EXE"},
                      "dispatch": {"events": {"PreToolUse": []}}}
        settings_diff_spelling = {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
            {"type": "command",
             "command": f'"C:\\Pinned-Python\\python.exe" "{dispatch_py}" --event PreToolUse'}
        ]}]}}
        problems = install_mod._check_dispatcher_registration(settings_diff_spelling, cfg_pinned, deploy_dir)
        p.check("finding3: PINNED mode tolerates a separator/case-only spelling "
                "difference (norm_for_compare, not raw !=)",
                problems == [], str(problems))

        # A GENUINE identity mismatch under a pin must still be reported.
        settings_real_mismatch = {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
            {"type": "command", "command": f'"C:/Some/Other/python.exe" "{dispatch_py}" --event PreToolUse'}
        ]}]}}
        problems = install_mod._check_dispatcher_registration(settings_real_mismatch, cfg_pinned, deploy_dir)
        p.check("finding3: PINNED mode still reports a GENUINE interpreter mismatch",
                bool(problems), str(problems))

        # --- finding 4: UNPINNED, registered interpreter is real but a
        # different string than whatever sys.executable resolves to right now.
        cfg_unpinned = {"paths": {"python_exe": ""},
                         "dispatch": {"events": {"PreToolUse": []}}}
        settings_other_real_interp = {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
            {"type": "command", "command": f'"{sys.executable}" "{dispatch_py}" --event PreToolUse'}
        ]}]}}
        problems = install_mod._check_dispatcher_registration(settings_other_real_interp, cfg_unpinned, deploy_dir)
        p.check("finding4: UNPINNED mode does not assert identity against the "
                "currently-running interpreter (no false drift)",
                problems == [], str(problems))

        # A registered interpreter that does not exist on disk is STILL a
        # problem in unpinned mode -- existence, not identity, is the check.
        settings_nonexistent = {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
            {"type": "command", "command": f'"C:/definitely/does/not/exist/python.exe" "{dispatch_py}" --event PreToolUse'}
        ]}]}}
        problems = install_mod._check_dispatcher_registration(settings_nonexistent, cfg_unpinned, deploy_dir)
        p.check("finding4: UNPINNED mode still reports a registered interpreter "
                "that does not exist on disk",
                bool(problems), str(problems))


def check_stop_malformed_stdin_fails_open(p: Probe) -> None:
    """Pins that a corrupted/unreadable stdin payload must
    short-circuit to ALLOW (exit 0) BEFORE any guard runs, matching every
    master's `catch { exit 0 }` around ConvertFrom-Json -- the port blocking
    here would be a divergence in the DANGEROUS direction (a corrupted
    payload blocking the user's work with a message nobody can act on).
    Routes Stop -> a guard that UNCONDITIONALLY blocks (proving the routing
    genuinely CAN block, so the malformed-input ALLOW below is not an
    accident of the guard never being reachable), then sends malformed and
    empty stdin and asserts exit 0 with no guard invocation, plus a logged,
    non-silent config-errors.jsonl entry (fail-open-but-not-silent, I-3)."""
    import subprocess as _sp
    import os as _os
    import tempfile as _tmp

    with _tmp.TemporaryDirectory(prefix="ulthar-stopstdin-") as td:
        tdp = Path(td)
        guards_dir = tdp / "guards"
        guards_dir.mkdir()
        (guards_dir / "always_block.py").write_text(
            "from lib.io import Result\n"
            "def run(payload, cfg, ctx):\n"
            "    return Result(2, '', 'always blocks')\n",
            encoding="utf-8",
        )

        stub_cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        stub_cfg["dispatch"]["events"]["Stop"] = [{"tools": "*", "guards": ["always_block"]}]
        cfg_path = tdp / "config.json"
        cfg_path.write_text(json.dumps(stub_cfg), encoding="utf-8")

        env = dict(_os.environ)
        env["ULTHAR_CONFIG"] = str(cfg_path)
        env["ULTHAR_GUARDS_DIR"] = str(guards_dir)
        env["USERPROFILE"] = td
        env["HOME"] = td

        def run_dispatch(stdin_bytes: bytes) -> tuple[int, bytes]:
            proc = _sp.run(
                [sys.executable, str(HOOKS_DIR / "dispatch.py"), "--event", "Stop"],
                input=stdin_bytes, capture_output=True, timeout=15, env=env,
            )
            return proc.returncode, proc.stderr

        # Sanity check: the routing genuinely blocks on a WELL-FORMED payload,
        # so the malformed-input cases below prove fail-open, not an
        # unreachable guard.
        rc, err = run_dispatch(json.dumps({"hook_event_name": "Stop"}).encode("utf-8"))
        p.check("stop malformed-stdin sanity: well-formed Stop payload DOES block "
                "(routing is genuinely reachable)", rc == 2, f"rc={rc} err={err!r}")

        rc, err = run_dispatch(b"")
        p.check("stop malformed-stdin: EMPTY stdin fails OPEN (exit 0), matching "
                "every master's catch{exit 0}", rc == 0, f"rc={rc} err={err!r}")

        rc, err = run_dispatch(b"{not valid json at all")
        p.check("stop malformed-stdin: INVALID JSON stdin fails OPEN (exit 0)",
                rc == 0, f"rc={rc} err={err!r}")

        rc, err = run_dispatch(b"\xff\xfe\x00\x01not utf-8")
        p.check("stop malformed-stdin: UNDECODABLE bytes fail OPEN (exit 0)",
                rc == 0, f"rc={rc} err={err!r}")

        config_error_log = tdp / ".claude" / "hooks" / "logs" / "config-errors.jsonl"
        logged = config_error_log.exists() and "stdin_parse" in config_error_log.read_text(encoding="utf-8")
        p.check("stop malformed-stdin: fail-open is LOGGED (config-errors.jsonl, "
                "phase='stdin_parse') -- not silently indistinguishable from a "
                "legitimate ALLOW", logged)


def check_guard_load_error_is_loud(p: Probe) -> None:
    """Overturns the earlier M-13 approval: a guard that FAILS TO IMPORT is an
    INFRASTRUCTURE failure (the
    runtime could not even load), not a per-rule bug -- it now BLOCKS
    (exit 2) with an actionable message, rather than failing open. Still
    LOGGED distinctly (config_error_log, phase=guard_load). The asymmetry
    that changed the ruling: an unguarded session can delete the owner's
    untracked files; a blocked one cannot."""
    import subprocess as _sp
    import os as _os
    import tempfile as _tmp

    with _tmp.TemporaryDirectory(prefix="ulthar-guardload-") as td:
        tdp = Path(td)
        guards_dir = tdp / "guards"
        guards_dir.mkdir()
        (guards_dir / "broken.py").write_text("raise ImportError('deliberately broken')\n",
                                                encoding="utf-8")

        stub_cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        stub_cfg["dispatch"]["events"]["PreToolUse"] = [{"tools": "Bash", "guards": ["broken"]}]
        cfg_path = tdp / "config.json"
        cfg_path.write_text(json.dumps(stub_cfg), encoding="utf-8")

        env = dict(_os.environ)
        env["ULTHAR_CONFIG"] = str(cfg_path)
        env["ULTHAR_GUARDS_DIR"] = str(guards_dir)
        env["USERPROFILE"] = td
        env["HOME"] = td

        payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                   "tool_input": {"command": "echo hi"}}
        proc = _sp.run(
            [sys.executable, str(HOOKS_DIR / "dispatch.py"), "--event", "PreToolUse",
             "--guard", "broken"],
            input=json.dumps(payload).encode("utf-8"), capture_output=True, timeout=15, env=env,
        )
        p.check("guard_load: an unimportable guard BLOCKS (exit 2) -- infrastructure "
                "failure, round-13 ruling",
                proc.returncode == 2, f"rc={proc.returncode}")
        stderr_text = proc.stderr.decode("utf-8", "replace")
        p.check("guard_load: the block message is ACTIONABLE -- names the guard, "
                "and gives a rollback that actually works (PE-19: no dead .ps1 "
                "reference; settings.json / git recovery instead)",
                "broken" in stderr_text and "install.py" in stderr_text
                and "settings.json" in stderr_text
                and "dispatch.ps1" not in stderr_text, stderr_text)

        config_error_log = tdp / ".claude" / "hooks" / "logs" / "config-errors.jsonl"
        logged = config_error_log.exists() and "guard_load" in config_error_log.read_text(encoding="utf-8")
        p.check("guard_load: logged to config_error_log with phase='guard_load' "
                "(not silently indistinguishable from a legitimate ALLOW)",
                logged, f"log exists={config_error_log.exists()}")


def check_missing_lib_blocks_not_exit1(p: Probe) -> None:
    """Pins a MANDATORY cutover blocker: dispatch.py's top-level
    `from lib import ctx, io` used to sit OUTSIDE every try/except. If `lib/`
    is missing (reachable by accident from an interrupted deploy -- finding 4's
    residual window), Python
    aborts with an UNCAUGHT ImportError and exits 1 -- and Claude Code
    treats exit 1 as a non-blocking error, so the tool call RUNS. This was
    the last surviving infrastructure-failure-to-ALLOW path. Proves a
    dispatch.py copied WITHOUT a sibling lib/ directory exits 2 (BLOCK) with
    an actionable message, never exit 1."""
    import subprocess as _sp
    import tempfile as _tmp
    import shutil as _shutil

    with _tmp.TemporaryDirectory(prefix="ulthar-nolib-") as td:
        tdp = Path(td)
        _shutil.copy2(HOOKS_DIR / "dispatch.py", tdp / "dispatch.py")
        _shutil.copy2(CONFIG_PATH, tdp / "config.json")
        # Deliberately NO lib/ directory copied alongside.
        payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                   "tool_input": {"command": "echo hi"}}
        proc = _sp.run(
            [sys.executable, str(tdp / "dispatch.py"), "--event", "PreToolUse"],
            input=json.dumps(payload).encode("utf-8"), capture_output=True, timeout=15,
        )
        p.check("missing lib/: dispatch.py BLOCKS (exit 2), never exit 1 "
                "(a bare crash Claude Code would treat as non-blocking)",
                proc.returncode == 2, f"rc={proc.returncode}")
        stderr_text = proc.stderr.decode("utf-8", "replace")
        p.check("missing lib/: the block message is actionable (names lib, "
                "install.py --verify, and a working rollback -- PE-19: no dead "
                ".ps1 reference)",
                "lib" in stderr_text and "install.py" in stderr_text
                and "settings.json" in stderr_text
                and "dispatch.ps1" not in stderr_text, stderr_text)


def check_infra_failures_block_not_allow(p: Probe) -> None:
    """Pins that infrastructure failures (config.json
    unparseable, a routed guard's pattern failing to compile) BLOCK rather
    than fail open -- the failure to LOAD the runtime is a deployment
    failure, not a per-rule bug, and approving a destructive tool call
    because enforcement could not start is exactly the hollowing-out this port
    exists to remove. Each message must be actionable: name the failure and
    give a rollback that actually works from the CURRENT state (PE-19: the
    .ps1 masters are gone, so the message must not point at restoring them)."""
    import subprocess as _sp
    import os as _os
    import tempfile as _tmp

    def _run(cfg_path: Path, guards_dir: Path, guard: str, td: str) -> tuple[int, str]:
        env = dict(_os.environ)
        env["ULTHAR_CONFIG"] = str(cfg_path)
        env["ULTHAR_GUARDS_DIR"] = str(guards_dir)
        env["USERPROFILE"] = td
        env["HOME"] = td
        payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                   "tool_input": {"command": "echo hi"}}
        proc = _sp.run(
            [sys.executable, str(HOOKS_DIR / "dispatch.py"), "--event", "PreToolUse",
             "--guard", guard],
            input=json.dumps(payload).encode("utf-8"), capture_output=True, timeout=15, env=env,
        )
        return proc.returncode, proc.stderr.decode("utf-8", "replace")

    # (a) config.json does not parse at all.
    with _tmp.TemporaryDirectory(prefix="ulthar-infra-cfgparse-") as td:
        tdp = Path(td)
        guards_dir = tdp / "guards"
        guards_dir.mkdir()
        cfg_path = tdp / "config.json"
        cfg_path.write_text("{ this is not valid json", encoding="utf-8")
        rc, err = _run(cfg_path, guards_dir, "anything", td)
        p.check("infra failure: unparseable config.json BLOCKS (exit 2), not ALLOW",
                rc == 2, f"rc={rc}")
        p.check("infra failure: unparseable config.json message is actionable "
                "(names config.json and a working rollback -- PE-19: no dead "
                ".ps1 reference)",
                "config.json" in err and "install.py" in err
                and "settings.json" in err and "dispatch.ps1" not in err, err)
        p.check("infra failure: non-overlay cause (config.json) still gets the "
                "GENERIC redeploy-or-rollback recovery text -- proves the overlay "
                "override below does not leak into every infra cause",
                "git -C" in err, err)

    # (b) a routed guard's config-declared pattern fails to compile.
    with _tmp.TemporaryDirectory(prefix="ulthar-infra-pattern-") as td:
        tdp = Path(td)
        guards_dir = tdp / "guards"
        guards_dir.mkdir()
        (guards_dir / "badpattern.py").write_text(
            "from lib.io import ALLOW\n"
            "from lib import ctx as ctx_mod\n"
            "def run(payload, cfg, ctx):\n"
            "    ctx_mod.compile_pattern('[', 'path', ctx, 'test.bad_pattern')\n"
            "    return ALLOW\n",
            encoding="utf-8",
        )
        stub_cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        stub_cfg["dispatch"]["events"]["PreToolUse"] = [{"tools": "Bash", "guards": ["badpattern"]}]
        cfg_path = tdp / "config.json"
        cfg_path.write_text(json.dumps(stub_cfg), encoding="utf-8")
        rc, err = _run(cfg_path, guards_dir, "badpattern", td)
        p.check("infra failure: a guard's uncompilable pattern BLOCKS (exit 2), not ALLOW",
                rc == 2, f"rc={rc}")
        p.check("infra failure: pattern-compile message is actionable",
                "badpattern" in err and "settings.json" in err
                and "dispatch.ps1" not in err, err)

    # (c) a bad user overlay -- this cause's recovery text must differ
    # from (a)/(b) above. The generic block ("redeploy" / "git checkout" /
    # "disable this event") is WRONG here: the overlay is not under hooks/,
    # is not in git, and redeploying never touches it. Not a §7 parity
    # exception (no old .ps1 master, no exit-code change, no fixture) --
    # see check_pe_citations_not_undeclared below for why that matters. Uses
    # the REAL shipped
    # config.json (CONFIG_PATH) unmodified so overlays.enabled/user_path are
    # exactly what production ships, and stubs a real guards/ dir (the stub
    # guard is never reached -- the overlay merge fails before dispatch()).
    with _tmp.TemporaryDirectory(prefix="ulthar-infra-overlay-") as td:
        tdp = Path(td)
        (tdp / ".claude").mkdir(parents=True)
        (tdp / ".claude" / "ulthar-overlay.json").write_text("{not valid json", encoding="utf-8")
        rc, err = _run(CONFIG_PATH, HOOKS_DIR / "guards", "tool_discipline", td)
        p.check("infra failure: bad overlay BLOCKS (exit 2), not ALLOW",
                rc == 2, f"rc={rc}")
        p.check("infra failure: bad overlay message names the overlay cause and "
                "says overlays are OPTIONAL (the fact that makes deletion safe)",
                "overlay" in err and "OPTIONAL" in err, err)
        p.check("infra failure: bad overlay message does NOT reuse the generic "
                "redeploy/rollback/disable-the-event recovery -- that fix is wrong "
                "for a file that is not under hooks/ and not in git",
                "Fix the master under" not in err and "git -C" not in err
                and "disable this event" not in err.lower()
                and "no .ps1 fallback exists" not in err, err)
        p.check("infra failure: bad overlay message still carries the shared "
                "diagnosis framing (install.py --verify, 'DEPLOYMENT failure')",
                "install.py" in err and "--verify" in err and "DEPLOYMENT failure" in err, err)


def check_parity_gate6_elapsed_normalisation(p: Probe) -> None:
    """Pins gate6's 'approval window not elapsed ({age_sec}s < {min_age_sec}s)'
    message: it embeds a value computed from the
    LIVE clock at the moment each side's guard runs -- comparing it literally
    made the case flaky (pass/fail depending on real elapsed seconds between
    two subprocess launches). Fixed with ONE narrow, declared normalisation
    (`_GATE6_ELAPSED_RE`) that replaces ONLY the first number in the exact
    shape '(Ns < Ms)' with a fixed placeholder -- the threshold and every
    other digit in every other message must still compare exactly. Proves
    (a) two different elapsed values normalise to equal, (b) a different
    THRESHOLD still compares as different (not accidentally swallowed too),
    and (c) an unrelated message with digits elsewhere is untouched."""
    sys.path.insert(0, str(TESTS_DIR))
    import parity  # noqa: E402

    a = "BLOCKED (brief gate): approval window not elapsed (7s < 180s) and no OWNER-OK line in fresh-round1.md."
    b = "BLOCKED (brief gate): approval window not elapsed (142s < 180s) and no OWNER-OK line in fresh-round1.md."
    p.check("gate6 elapsed normalisation: two different elapsed-seconds values normalise EQUAL",
            parity.normalise_stderr(a.encode("utf-8")) == parity.normalise_stderr(b.encode("utf-8")))

    c = "BLOCKED (brief gate): approval window not elapsed (7s < 60s) and no OWNER-OK line in fresh-round1.md."
    p.check("gate6 elapsed normalisation: a DIFFERENT threshold (min_age_sec) still compares DIFFERENT",
            parity.normalise_stderr(a.encode("utf-8")) != parity.normalise_stderr(c.encode("utf-8")))

    d = "BLOCKED (PR state): branch 'a' has no OPEN pull request:\n  #40 MERGED https://github.com/o/r/pull/40"
    e = "BLOCKED (PR state): branch 'a' has no OPEN pull request:\n  #41 MERGED https://github.com/o/r/pull/41"
    p.check("gate6 elapsed normalisation: an UNRELATED message's digits (PR numbers) are NOT "
            "swallowed -- still compares DIFFERENT",
            parity.normalise_stderr(d.encode("utf-8")) != parity.normalise_stderr(e.encode("utf-8")))


# ------------------------------------------------------------------ parity.py sandbox_env (S-1)


def check_parity_sandbox_env(p: Probe) -> None:
    """S-1/S-1a/S-1b regression: build_sandbox_env() must redirect USERPROFILE/HOME/
    TEMP/TMP/HOMEDRIVE+HOMEPATH under the sandbox root, and the precondition check
    must FLAG an un-redirected (real) environment -- this is the exact defect class
    that would otherwise let a parity run consume the real pm-delete-allow."""
    import os as _os
    import tempfile

    sys.path.insert(0, str(TESTS_DIR))
    import parity  # noqa: E402  (tests/parity.py)

    with tempfile.TemporaryDirectory(prefix="ulthar-parity-unit-") as td:
        root = Path(td) / "sandboxroot"
        env = parity.build_sandbox_env(root)
        real_home = Path(_os.environ.get("USERPROFILE") or _os.environ.get("HOME") or Path.home())

        problems = parity.assert_sandbox_preconditions(env, root, real_home)
        p.check("parity: build_sandbox_env redirects USERPROFILE/HOME/TEMP/TMP under sandbox_root",
                not problems, str(problems))

        bad_env = dict(_os.environ)
        bad_problems = parity.assert_sandbox_preconditions(bad_env, root, real_home)
        p.check("parity: preconditions FLAG a real, non-redirected environment (S-1a)",
                bool(bad_problems), "should have been non-empty")

        norm = parity.normalise_stderr(b"line1\r\nline2  \r\n\r\n")
        p.check("parity: normalise_stderr strips CRLF/trailing-ws/blank lines",
                norm == "line1\nline2", repr(norm))


def check_parity_sandbox_not_under_exempt_patterns(p: Probe) -> None:
    """Pins the sandbox placement: it used to be placed under the
    session scratchpad ('...\\Temp\\claude\\<workspace>\\<session>\\scratchpad\\...'),
    which matches paths.exempt_patterns' '/Temp/claude/' and '/scratchpad/'
    blanket substrings -- so commander/skill_anchor exempted every fixture
    under it regardless of the is_under()/matches_any() logic actually under
    test (two PE-3 cases were passing for the wrong reason). Proves (a) the
    NEW placement (bare %TEMP%) does not collide, and (b) the OLD placement
    WOULD have been caught -- so the collision detector is not a no-op."""
    sys.path.insert(0, str(TESTS_DIR))
    import parity  # noqa: E402
    import tempfile as _tmp
    import shutil as _shutil

    with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)

    parent = parity._resolve_sandbox_parent()
    sandbox_root = Path(_tmp.mkdtemp(prefix="ulthar-exempt-selftest-", dir=str(parent)))
    try:
        try:
            parity._assert_sandbox_not_exempt(sandbox_root, cfg)
            collided = False
        except parity.SandboxExemptCollisionError:
            collided = True
        p.check("parity sandbox placement: bare %TEMP% does NOT collide with "
                "paths.exempt_patterns", not collided)
    finally:
        _shutil.rmtree(sandbox_root, ignore_errors=True)

    old_style_path = parent / "claude" / "some-workspace" / "some-session" / "scratchpad" / "ulthar-parity-x"
    try:
        parity._assert_sandbox_not_exempt(old_style_path, cfg)
        p.check("parity sandbox placement: the OLD session-scratchpad-style path "
                "IS caught by the collision detector (not a no-op)", False,
                "did not raise")
    except parity.SandboxExemptCollisionError:
        p.check("parity sandbox placement: the OLD session-scratchpad-style path "
                "IS caught by the collision detector (not a no-op)", True)


def check_parity_case_wide_placeholder_substitution(p: Probe) -> None:
    """Pins a silent-degradation defect: `_check_gh_expectations` used to read
    `expect_gh_cwd`/`expect_gh_argv_contains` off the RAW case dict, which
    never went through `_substitute_placeholders` -- only `payload` did. A
    fixture writing `expect_gh_cwd: "${CWD}"` compared against the LITERAL
    string '${CWD}' and reported a false failure while both the fixture and
    the guard were correct. run_case() now substitutes the WHOLE case (not
    payload in isolation) and scans the WHOLE case for leftover placeholders.
    Proves (a) an assertion field with a placeholder resolves to a concrete
    value via a real run_case() call, and (b) an unresolved placeholder
    living ONLY in an assertion field (never in payload) is still a hard
    UnresolvedPlaceholderError, not a silently-wrong comparison."""
    sys.path.insert(0, str(TESTS_DIR))
    import parity  # noqa: E402
    import tempfile as _tmp

    with _tmp.TemporaryDirectory(prefix="ulthar-case-subst-") as td:
        sandbox_root = Path(td) / "sandbox"
        sandbox_root.mkdir()
        env = parity.build_sandbox_env(sandbox_root)
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)

        case = {
            "id": "self-test-case-wide-subst",
            "guard": "stub_guard",
            "guards_dir": "fixtures/stub",
            "event": "PreToolUse",
            "payload": {"tool_name": "Bash", "tool_input": {"command": "ALLOW"}, "cwd": "${CWD}"},
            "expect_exit": 0,
            # This field previously never went through substitution.
            "expect_gh_cwd": "${CWD}",
            "parity": False,
            "parity_exception": "PE-8",  # meta/dispatcher-plumbing self-test, same as PE-8's own fixture
        }
        failures = parity.run_case(case, ps1_dir=HOOKS_DIR, dispatch_py=HOOKS_DIR / "dispatch.py",
                                    sandbox_env=env, cfg_path=CONFIG_PATH, sandbox_root=sandbox_root,
                                    cfg=cfg)
        # expect_gh_cwd never matches here (stub_guard never calls gh), but the
        # POINT is that the failure message must show a resolved sandbox path,
        # never the literal, unresolved string '${CWD}'.
        gh_cwd_failures = [f for f in failures if "expect_gh_cwd" in f or "gh call's cwd" in f]
        p.check("parity case-wide substitution: an assertion field ('expect_gh_cwd') "
                "is resolved, not left as a literal '${CWD}' placeholder",
                gh_cwd_failures and all("${CWD}" not in f for f in gh_cwd_failures),
                gh_cwd_failures)

        bad_case = {
            "id": "self-test-unresolved-assertion",
            "guard": "stub_guard",
            "guards_dir": "fixtures/stub",
            "event": "PreToolUse",
            "payload": {"tool_name": "Bash", "tool_input": {"command": "ALLOW"}},
            "expect_exit": 0,
            "expect_gh_cwd": "${NOT_A_REAL_PLACEHOLDER}",
            "parity": False,
            "parity_exception": "PE-8",
        }
        try:
            parity.run_case(bad_case, ps1_dir=HOOKS_DIR, dispatch_py=HOOKS_DIR / "dispatch.py",
                             sandbox_env=dict(env), cfg_path=CONFIG_PATH,
                             sandbox_root=sandbox_root, cfg=cfg)
            p.check("parity case-wide substitution: an unresolved placeholder living "
                    "ONLY in an assertion field is a HARD error", False, "did not raise")
        except parity.UnresolvedPlaceholderError:
            p.check("parity case-wide substitution: an unresolved placeholder living "
                    "ONLY in an assertion field is a HARD error", True)


def check_parity_placeholder_resolution(p: Probe) -> None:
    """Pins that the harness once resolved only 8 of the 10
    placeholders real fixtures use, and ONLY inside payload.cwd -- every other
    payload field (file_path, notebook_path, transcript_path) carrying a
    placeholder was passed through literally, unresolved, making a guard's
    correct ALLOW on a literal '${DEPLOY_DIR}/x.ps1' string look like a guard
    defect. Verifies (a) all ten placeholders resolve to concrete, sandboxed
    paths inside a nested payload field (not just cwd), and (b) an unknown
    placeholder is a HARD error, never a silently-passed-through literal."""
    sys.path.insert(0, str(TESTS_DIR))
    import parity  # noqa: E402
    import tempfile as _tmp

    with _tmp.TemporaryDirectory(prefix="ulthar-placeholder-") as td:
        sandbox_root = Path(td) / "sandbox"
        sandbox_root.mkdir()
        cwd = sandbox_root
        env = parity.build_sandbox_env(sandbox_root)
        deploy_dir = sandbox_root / ".claude" / "hooks"
        skill_hooks_dir = sandbox_root / ".claude" / "skills" / "ulthar" / "hooks"
        mapping = parity._placeholder_map(sandbox_root, cwd, env, deploy_dir, skill_hooks_dir)

        expected_names = {"CWD", "SANDBOX", "MAIN_REPO", "FEATURE_REPO", "NO_REPO",
                           "TEMP", "DEPLOY_DIR", "SKILL_HOOKS_DIR", "MAIN_TRANSCRIPT",
                           "SUB_TRANSCRIPT", "ELSEWHERE"}
        p.check("parity placeholders: all 10+ names fixtures use are resolved",
                expected_names <= set(mapping), sorted(set(mapping)))

        payload = {
            "tool_name": "Write",
            "tool_input": {"file_path": "${DEPLOY_DIR}/dispatch.ps1"},
            "transcript_path": "${SUB_TRANSCRIPT}",
            "cwd": "${CWD}",
        }
        substituted = parity._substitute_placeholders(payload, mapping)
        p.check("parity placeholders: a NESTED field (tool_input.file_path) is "
                "substituted, not just payload.cwd",
                "${DEPLOY_DIR}" not in substituted["tool_input"]["file_path"],
                substituted["tool_input"]["file_path"])
        p.check("parity placeholders: ${SUB_TRANSCRIPT} resolves to a path "
                "containing a '/subagents/' segment (lib.ctx.is_main_chain's signal)",
                "subagents" in substituted["transcript_path"].replace("\\", "/"),
                substituted["transcript_path"])
        p.check("parity placeholders: ${DEPLOY_DIR} resolves under the SANDBOXED "
                "'.claude/hooks', matching what the old-side master computes from "
                "a redirected USERPROFILE",
                substituted["tool_input"]["file_path"].replace("\\", "/")
                .startswith(str(deploy_dir).replace("\\", "/")),
                substituted["tool_input"]["file_path"])
        p.check("parity placeholders: no unresolved token remains after substituting "
                "every known name",
                parity._find_unresolved_placeholders(substituted) == [])

        unknown_payload = {"tool_name": "Write", "tool_input": {"file_path": "${NOT_A_REAL_PLACEHOLDER}"}}
        unknown_substituted = parity._substitute_placeholders(unknown_payload, mapping)
        unresolved = parity._find_unresolved_placeholders(unknown_substituted)
        p.check("parity placeholders: an UNKNOWN placeholder survives substitution "
                "(so it CAN be detected) rather than silently vanishing",
                unresolved == ["${NOT_A_REAL_PLACEHOLDER}"], unresolved)


def check_parity_top_level_marker_seeds_commander_mode(p: Probe) -> None:
    """Pins that tests/fixtures/write_edit/commander.json
    expresses its commander-mode marker as a case-level `"marker"` field, not
    inside `setup` -- run_case previously only ever read case.get("setup"), so
    all 12 commander fixtures ran against an unseeded sandbox regardless of what
    "marker" said. Proves run_case() now actually writes the marker file the
    top-level field names."""
    sys.path.insert(0, str(TESTS_DIR))
    import parity  # noqa: E402
    import tempfile as _tmp

    with _tmp.TemporaryDirectory(prefix="ulthar-marker-") as td:
        sandbox_root = Path(td) / "sandbox"
        sandbox_root.mkdir()
        env = parity.build_sandbox_env(sandbox_root)
        cwd = sandbox_root / "cwd"
        cwd.mkdir()

        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)

        case = {
            "id": "self-test-top-level-marker",
            "guard": "commander",
            "marker": "enforce",
            "payload": {
                "tool_name": "Edit",
                "tool_input": {"file_path": str(cwd / "src" / "App.tsx")},
                "cwd": str(cwd),
                "transcript_path": str(sandbox_root / "transcripts" / "main.jsonl"),
            },
        }
        dispatch_py = HOOKS_DIR / "dispatch.py"
        failures = parity.run_case(case | {"parity": False, "parity_exception": "PE-1",
                                            "expect_exit": 2,
                                            "expect_stderr_contains": "BLOCKED (commander discipline)"},
                                    ps1_dir=Path("."), dispatch_py=dispatch_py, sandbox_env=env,
                                    cfg_path=CONFIG_PATH, sandbox_root=sandbox_root, cfg=cfg)
        p.check("parity top-level marker: 'enforce' + main-chain edit outside "
                "exemptions -> BLOCKS (marker file was actually written)",
                failures == [], failures)


def check_parity_setup_self_test(p: Probe) -> None:
    """Pins addendum 1: a class-B fixture whose expectation DEPENDS on seeded
    state must fail when the setup builder is disabled -- a harness that cannot
    detect its own absence is the same class of defect as a config typo that
    silently disables a guard.

    Uses the stub guard's CHECK_APPROVAL:<name> command (tests/fixtures/stub/
    stub_guard.py), which ALLOWs only if ctx.state_dir/brief-approvals/<name>
    exists -- a real, guard-observable decision that depends entirely on the
    approval_file setup builder having run, without needing the real
    brief_gate.py. Runs the SAME case twice against a FRESH sandbox each time:
    once with setup applied (must ALLOW) and once with setup deliberately
    disabled (must BLOCK) -- proving the outcome is not a coincidence."""
    import os as _os
    import tempfile as _tmp

    sys.path.insert(0, str(TESTS_DIR))
    import parity  # noqa: E402

    stub_dir = TESTS_DIR / "fixtures" / "stub"
    with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
        stub_cfg = json.load(fh)
    stub_cfg["dispatch"]["events"]["PreToolUse"] = [
        {"tools": "Bash|PowerShell", "guards": ["stub_guard"]}
    ]

    approval_name = "config-generalisation-round.md"
    case = {
        "id": "self-test-setup-dependency",
        "guard": "stub_guard",
        "payload": {
            "hook_event_name": "PreToolUse", "tool_name": "Bash",
            "tool_input": {"command": f"CHECK_APPROVAL:{approval_name}"},
        },
        "setup": {"kind": "approval_file", "name": approval_name},
    }

    def _run_new(sandbox_root: Path, apply_setup: bool) -> int:
        with _tmp.TemporaryDirectory(prefix="ulthar-cfg-", dir=str(sandbox_root.parent)) as cfgtd:
            cfg_path = Path(cfgtd) / "config.json"
            cfg_path.write_text(json.dumps(stub_cfg), encoding="utf-8")
            env = parity.build_sandbox_env(sandbox_root)
            env["ULTHAR_CONFIG"] = str(cfg_path)
            env["ULTHAR_GUARDS_DIR"] = str(stub_dir)
            payload = dict(case["payload"])
            cwd = sandbox_root / "cwd"
            cwd.mkdir(parents=True, exist_ok=True)
            payload["cwd"] = str(cwd)
            if apply_setup:
                parity.run_setup(case["setup"], sandbox_root, cwd, Path(env["TEMP"]), stub_cfg)
            rc, _err, _out = parity.run_new_side(
                HOOKS_DIR / "dispatch.py", "PreToolUse", "stub_guard", payload, env)
            return rc

    with _tmp.TemporaryDirectory(prefix="ulthar-setup-selftest-with-") as td_with:
        rc_with_setup = _run_new(Path(td_with) / "sandbox", apply_setup=True)
    p.check("parity setup self-test: WITH setup applied, seeded-state case ALLOWs",
            rc_with_setup == 0, f"rc={rc_with_setup}")

    with _tmp.TemporaryDirectory(prefix="ulthar-setup-selftest-without-") as td_without:
        rc_without_setup = _run_new(Path(td_without) / "sandbox", apply_setup=False)
    p.check("parity setup self-test: WITHOUT setup (disabled), the SAME case BLOCKS -- "
            "proves the outcome truly depends on the setup builder having run",
            rc_without_setup == 2, f"rc={rc_without_setup}")

    p.check("parity setup self-test: outcomes actually differ (harness can detect its own absence)",
            rc_with_setup != rc_without_setup,
            f"with={rc_with_setup} without={rc_without_setup} -- if equal, the harness "
            "cannot tell the difference between setup running and not running")

    # Unknown setup kind must be a HARD ERROR, never a silent no-op (a typo in a
    # fixture must not degrade that case into an unseeded pass).
    try:
        with _tmp.TemporaryDirectory(prefix="ulthar-setup-badkind-") as td:
            root = Path(td) / "sandbox"
            root.mkdir()
            parity.run_setup({"kind": "no_such_builder"}, root, root, root, stub_cfg)
        p.check("parity setup: unknown setup kind raises SetupError (not a silent no-op)",
                False, "run_setup did not raise")
    except parity.SetupError:
        p.check("parity setup: unknown setup kind raises SetupError (not a silent no-op)", True)


def main() -> int:
    p = Probe()
    with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)

    print("== config-level checks (folded from the config probe) ==")
    run_config_probe(p, cfg)

    print("\n== §5 contract self-enforcement checks ==")
    check_guard_purity(p)
    check_ownership(p, cfg)
    check_single_config_json(p)
    check_shipped_python_exe_empty(p, cfg)
    check_routing_table_pinned(p, cfg)
    check_chain_order_and_stdout_survival(p)
    check_one_emitter(p, cfg)
    check_pe_fixtures(p)
    check_pe_citations_not_undeclared(p)

    print("\n== dispatch.py end-to-end smoke (stub guard) ==")
    check_run_cmd_kills_whole_process_tree_on_timeout(p, cfg)
    check_install_register_settings_regression(p)
    check_install_copy_deploy_regression(p)
    check_install_dependency_check_pinned_interpreter(p)
    check_run_cmd_resolves_pathext_only_executable(p, cfg)
    check_dispatch_smoke(p, cfg)

    print("\n== dispatch.py end-to-end, REAL guards/ (addendum 3 regression) ==")
    check_dispatch_real_guards_end_to_end(p)
    check_pretooluse_zero_subprocess_spawns(p)
    check_install_refuses_invalid_config(p)
    check_guard_load_error_is_loud(p)
    check_missing_lib_blocks_not_exit1(p)
    check_infra_failures_block_not_allow(p)
    check_stop_malformed_stdin_fails_open(p)
    check_log_error_uses_expand(p)
    check_b1_bom_config_loads(p)
    check_b3_gitcmd_case_insensitive(p, cfg)
    check_strip_quotes_unterminated_quote(p, cfg)
    check_addendum6_root_match_platform_case(p, cfg)
    check_b4_double_registration_detected(p)
    check_interpreter_comparison_pinned_vs_unpinned(p)

    print("\n== parity.py mechanism unit checks (no powershell spawned) ==")
    check_parity_sandbox_env(p)
    check_parity_gate6_elapsed_normalisation(p)
    check_parity_sandbox_not_under_exempt_patterns(p)
    check_parity_placeholder_resolution(p)
    check_parity_case_wide_placeholder_substitution(p)
    check_parity_top_level_marker_seeds_commander_mode(p)
    check_parity_setup_self_test(p)

    print(f"\nRESULT: {p.passed} passed, {p.failed} failed")
    return 1 if p.failed else 0


if __name__ == "__main__":
    sys.exit(main())
