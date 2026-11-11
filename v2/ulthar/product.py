"""Product writes only inside a confirmed unit's scope (GOAL-08, R1-c AC-11), hook side.

Applies in a project that has `.claude/ulthar.json`. A product file is any file inside
the repository except `.claude/state/`, `docs/plans/` and the config's
`non_product_globs` (a path outside the repository -- the session's scratchpad among
them -- is not a product file). A PreToolUse that writes a product file is allowed only
when the path matches a `scope_globs` glob of a unit whose state is contract_confirmed ..
owner_review; the scope is the one the CLI wrote into the unit's record when the
contract was confirmed (index.db -- no YAML here). The allowed event's detail records
`unit_id`.

Write targets: Edit / Write / MultiEdit / NotebookEdit's path, and what a Bash /
PowerShell command names literally: redirections, Out-File / Set-Content /
Add-Content, and the operands of cp (destination), mv, rm, tee and touch (also inside
`bash -c` / `powershell -Command`). Paths are compared after the same resolution as the
store guard (R1-b AC-4 c): symlinks and junctions of existing components resolved,
separators unified, case folded; every form of a path must be allowed.
Known limit (spec): a target the command assembles at run time (variables, globs,
script bodies, a `cd` before a relative path) is not seen here; R1-d's integration
check finds such changes.
"""
from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

from . import shellwords
from . import store as store_mod

WRITE_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
SHELL_TOOLS = frozenset({"Bash", "PowerShell"})
PATH_KEYS = ("file_path", "notebook_path")
SCOPE_STATES = ("contract_confirmed", "build", "quant_verified", "owner_review")
NON_PRODUCT_PREFIXES = (".claude/state/", "docs/plans/")
GUARD_ID = "unit_scope"
SINKS = frozenset({"/dev/null", "nul", "$null"})
DEST_CMDS = frozenset({"cp", "copy", "copy-item", "cpi"})  # only the destination is written
ALL_OPERAND_CMDS = frozenset({"mv", "move", "move-item", "mi", "rm", "del", "erase",
                              "remove-item", "ri", "tee", "tee-object", "touch"})
CONTENT_CMDLETS = frozenset({"out-file", "set-content", "add-content", "sc", "ac"})
PATH_OPTIONS = frozenset({"-path", "-filepath", "-literalpath", "-lp", "-pspath",
                          "-destination", "-t", "--target-directory"})
VALUE_OPTIONS = frozenset({"-value", "-encoding", "-inputobject", "-width", "-delimiter",
                           "-stream", "-filter", "-include", "-exclude", "-itemtype", "-name",
                           "-credential", "-variable"})
UNRESOLVABLE = ("$", "`", "%", "*", "?", "~")
LAUNCHERS = {"bash": "Bash", "sh": "Bash", "powershell": "PowerShell", "pwsh": "PowerShell"}
NESTED_DEPTH = 2


# ---------------------------------------------------------------------------- paths

def fold(path: str) -> str:
    """The comparison form: separators unified, case-insensitive (R1-b AC-4 c)."""
    return path.replace("\\", "/").rstrip("/").casefold()


def resolved(path: str) -> str:
    """`path` with every existing component's symlinks and junctions resolved -- for a
    file not created yet, its deepest existing ancestor -- and the rest appended."""
    existing, rest = Path(path), []
    while not os.path.lexists(existing) and existing.parent != existing:
        rest.insert(0, existing.name)
        existing = existing.parent
    return os.path.join(os.path.realpath(existing), *rest)


def forms(path: str) -> set[str]:
    full = os.path.abspath(path)
    return {fold(full), fold(resolved(full))}


# ---------------------------------------------------------------------------- targets

def _operands(words: list[str]) -> tuple[list[str], list[str]]:
    """(named path values, positional operands) of one command's arguments."""
    named, positional, i = [], [], 0
    while i < len(words):
        w = words[i]
        low = w.casefold()
        if low.startswith("--target-directory="):
            named.append(w.split("=", 1)[1])
        elif low in PATH_OPTIONS and i + 1 < len(words):
            named.append(words[i + 1])
            i += 1
        elif low in VALUE_OPTIONS:
            i += 1
        elif not w.startswith("-") or w == "-":
            positional.append(w)
        i += 1
    return named, positional


def _segment_targets(seg: list[str]) -> list[str]:
    words = [w for w in seg if w not in (shellwords.REDIRECT, shellwords.INPUT)]
    while words and "=" in words[0] and words[0].split("=", 1)[0].isidentifier():
        words = words[1:]
    if not words:
        return []
    exe = shellwords.basename(words[0]).removesuffix(".exe")
    named, positional = _operands(words[1:])
    if exe in DEST_CMDS:
        if named:
            return named
        return positional[-1:] if len(positional) > 1 else []
    if exe in ALL_OPERAND_CMDS:
        return named + positional
    if exe in CONTENT_CMDLETS:
        return named or positional[:1]
    return []


def _redirect_targets(toks: list[str]) -> list[str]:
    out = []
    for prev, tok in zip(toks, toks[1:]):
        if prev == shellwords.REDIRECT and tok not in shellwords.SEPARATORS and not tok.isdigit():
            out.append(tok)
    return out


def shell_targets(command: str, tool: str | None, depth: int = 0) -> list[str]:
    """Every path the command writes, as written (unresolvable ones left out)."""
    escape = None if tool == "cmd" else shellwords.ESCAPES.get(tool or "", "\\")
    toks = shellwords.tokenize(command, escape, exact=True)
    found = _redirect_targets(toks)
    for seg in shellwords.segments(toks):
        found += _segment_targets(seg)
        if depth >= NESTED_DEPTH:
            continue
        launcher = shellwords.basename(seg[0]).removesuffix(".exe") if seg else ""
        for prev, tok in zip(seg, seg[1:]):
            if prev.casefold() in shellwords.COMMAND_OPTIONS:
                found += shell_targets(tok, LAUNCHERS.get(launcher, "cmd"), depth + 1)
    return [t for t in found if t and t.casefold() not in SINKS
            and not any(c in t for c in UNRESOLVABLE)]


def targets(payload: dict) -> list[str]:
    tool = payload.get("tool_name")
    tool_input = payload.get("tool_input") or {}
    if tool in WRITE_TOOLS:
        return [v for k in PATH_KEYS if isinstance(v := tool_input.get(k), str) and v]
    if tool in SHELL_TOOLS:
        return shell_targets(str(tool_input.get("command") or ""), tool)
    return []


# ---------------------------------------------------------------------------- scope

def _excluded(rel: str, globs: list[str]) -> bool:
    return (rel + "/").startswith(NON_PRODUCT_PREFIXES) or any(
        PurePosixPath(rel).full_match(g) for g in globs)


def _product_forms(raw: str, cwd: str, roots: set[str], globs: list[str]) -> list[str]:
    """The repository-relative forms of `raw` that are product files ([] = none is)."""
    p = Path(raw)
    rels = []
    for form in forms(str(p if p.is_absolute() else Path(cwd) / p)):
        for root in roots:
            if form.startswith(root + "/"):
                rel = form[len(root) + 1:]
                if not _excluded(rel, globs):
                    rels.append(rel)
    return rels


def _non_product_globs(cfg: dict) -> list[str]:
    raw = cfg.get("non_product_globs", [])
    if not isinstance(raw, list) or not all(isinstance(g, str) and g for g in raw):
        raise store_mod.StoreError(".claude/ulthar.json: non_product_globs must be a list of "
                                   "non-empty glob strings")
    return [fold(g) for g in raw]


def scoped_units(store: store_mod.Store, rt) -> list[tuple[str, list[str]]]:
    """(unit id, folded scope globs) of every unit whose state allows product writes."""
    with store.reader(rt) as conn:
        units = store_mod.current(conn, "unit", SCOPE_STATES)
    out = []
    for u in units:
        globs = (u.get("confirmed") or {}).get("scope_globs")
        if not isinstance(globs, list) or not all(isinstance(g, str) and g for g in globs):
            raise store_mod.StoreError(f"unit {u.get('id')}: its confirmed scope_globs are "
                                       "missing or malformed")
        out.append((u["id"], [fold(g) for g in globs]))
    return out


def check(payload: dict, store_of, rt, detail: dict) -> str | None:
    """The block message, or None. `store_of()` gives the project's store (opened only
    when a product file is written). Raises on an unreadable config or store."""
    raws = targets(payload)
    if not raws:
        return None
    store = store_of()
    if store.repo is None or not (store.repo / store_mod.PROJECT_CONFIG).is_file():
        return None
    globs = _non_product_globs(store.config)
    cwd = str(payload.get("cwd") or os.getcwd())
    roots = forms(str(store.repo))
    product = [(raw, rels) for raw in raws
               if (rels := _product_forms(raw, cwd, roots, globs))]
    if not product:
        return None
    units = scoped_units(store, rt)
    matched: list[str] = []
    for raw, rels in product:
        ids = [uid for uid, scope in units
               if all(any(PurePosixPath(rel).full_match(g) for g in scope) for rel in rels)]
        if not ids:
            return (f"BLOCKED (ulthar unit scope, GOAL-08): {payload.get('tool_name')} writes the "
                    f"product file {raw}, which is in no confirmed unit's scope_globs. Product "
                    "files may be written only while a unit is contract_confirmed .. owner_review "
                    "and the path matches its confirmed contract's scope_globs. Confirm the "
                    "contract first (`ulthar contract submit` -> lint -> review -> the owner's "
                    "CONTRACT-CONFIRM -> `ulthar unit advance <unit> contract_confirmed`), or "
                    "widen the scope in a new contract version.")
        matched += [i for i in ids if i not in matched]
    detail["unit_id"] = matched[0] if len(matched) == 1 else matched
    return None
