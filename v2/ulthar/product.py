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
separators unified, case folded; every form of a path must be allowed. A cp/mv
destination that is an existing directory (the repository root `.` included) is judged
by the child each source lands as; a directory written as a whole (rm -r, a moved
directory, a copied directory's new subtree) must be covered by a `<dir>/**` scope; what
cannot be proved inside the scope is blocked.
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
MOVE_CMDS = frozenset({"mv", "move", "move-item", "mi"})  # the destination and the sources
ALL_OPERAND_CMDS = frozenset({"rm", "del", "erase", "remove-item", "ri", "tee", "tee-object",
                              "touch"})
CONTENT_CMDLETS = frozenset({"out-file", "set-content", "add-content", "sc", "ac"})
PATH_OPTIONS = frozenset({"-path", "-filepath", "-literalpath", "-lp", "-pspath"})
DEST_OPTIONS = frozenset({"-destination", "-t", "--target-directory"})
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

def _operands(words: list[str]) -> tuple[list[str], list[str], list[str]]:
    """(destination option values, path option values, positional operands)."""
    dests, paths, positional, i = [], [], [], 0
    while i < len(words):
        w = words[i]
        low = w.casefold()
        if low.startswith("--target-directory="):
            dests.append(w.split("=", 1)[1])
        elif low in DEST_OPTIONS and i + 1 < len(words):
            dests.append(words[i + 1])
            i += 1
        elif low in PATH_OPTIONS and i + 1 < len(words):
            paths.append(words[i + 1])
            i += 1
        elif low in VALUE_OPTIONS:
            i += 1
        elif not w.startswith("-") or w == "-":
            positional.append(w)
        i += 1
    return dests, paths, positional


def _segment_targets(seg: list[str]) -> list[tuple[str, list[str] | None]]:
    """(path, sources): sources is the list of what is copied/moved INTO path when path
    is a cp/mv destination, else None."""
    words = [w for w in seg if w not in (shellwords.REDIRECT, shellwords.INPUT)]
    while words and "=" in words[0] and words[0].split("=", 1)[0].isidentifier():
        words = words[1:]
    if not words:
        return []
    exe = shellwords.basename(words[0]).removesuffix(".exe")
    dests, paths, positional = _operands(words[1:])
    if exe in DEST_CMDS or exe in MOVE_CMDS:
        operands = paths + positional
        if dests:
            dest, sources = dests[0], operands
        elif len(operands) > 1:
            dest, sources = operands[-1], operands[:-1]
        else:
            return []
        moved = [(s, None) for s in sources] if exe in MOVE_CMDS else []
        return [(dest, sources)] + moved
    if exe in ALL_OPERAND_CMDS:
        return [(t, None) for t in dests + paths + positional]
    if exe in CONTENT_CMDLETS:
        return [(t, None) for t in (paths or positional[:1])]
    return []


def _redirect_targets(toks: list[str]) -> list[tuple[str, None]]:
    out = []
    for prev, tok in zip(toks, toks[1:]):
        if prev == shellwords.REDIRECT and tok not in shellwords.SEPARATORS and not tok.isdigit():
            out.append((tok, None))
    return out


def shell_targets(command: str, tool: str | None, depth: int = 0) -> list[tuple[str, list | None]]:
    """Every (path, sources) the command writes, as written; a path assembled at run
    time (variables, globs) is left out -- except a glob among the sources of a
    destination directory, which `check` treats as an unknown child name."""
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
    return [(t, src) for t, src in found if t and t.casefold() not in SINKS
            and not _unresolvable(t)]


def _unresolvable(path: str) -> bool:
    return any(c in path for c in UNRESOLVABLE)


def targets(payload: dict) -> list[tuple[str, list | None]]:
    tool = payload.get("tool_name")
    tool_input = payload.get("tool_input") or {}
    if tool in WRITE_TOOLS:
        return [(v, None) for k in PATH_KEYS if isinstance(v := tool_input.get(k), str) and v]
    if tool in SHELL_TOOLS:
        return shell_targets(str(tool_input.get("command") or ""), tool)
    return []


# ---------------------------------------------------------------------------- scope

def _excluded(rel: str, globs: list[str]) -> bool:
    return bool(rel) and ((rel + "/").startswith(NON_PRODUCT_PREFIXES) or any(
        PurePosixPath(rel).full_match(g) for g in globs))


def _absolute(raw: str, cwd: str) -> str:
    p = Path(raw)
    return str(p if p.is_absolute() else Path(cwd) / p)


def _product_forms(path: str, roots: set[str], globs: list[str]) -> list[str]:
    """The repository-relative forms of the absolute `path` that are product paths
    ("" = the repository root itself); [] = none is."""
    rels = []
    for form in forms(path):
        for root in roots:
            if form == root or form.startswith(root + "/"):
                rel = form[len(root) + 1:]
                if not _excluded(rel, globs):
                    rels.append(rel)
    return rels


def _covers_tree(rel: str, scope: list[str]) -> bool:
    """Every path under `rel` (and rel itself) is inside the scope: a glob `P/**` with a
    literal P that is rel or one of its ancestors, or `**`."""
    for g in scope:
        if g == "**":
            return True
        base = g[:-3] if g.endswith("/**") else None
        if base and not _unresolvable(base) and "[" not in base and (
                rel == base or rel.startswith(base + "/")):
            return True
    return False


def _fits(rels: list[str], tree: bool, scope: list[str]) -> bool:
    if tree:
        return all(_covers_tree(rel, scope) for rel in rels)
    return all(rel and any(PurePosixPath(rel).full_match(g) for g in scope) for rel in rels)


def _outputs(raw: str, sources, cwd: str) -> list[tuple[str, str, bool, bool]]:
    """(label, absolute path, is_tree, child_names_known) of what the target writes. A
    cp/mv destination that is an existing directory (or written as one) receives each
    source under its own name; a directory written to as such (rm -r, a moved source)
    is a whole tree."""
    path = _absolute(raw, cwd)
    if sources is not None and (os.path.isdir(path) or raw in (".", "..")
                                or raw.endswith(("/", "\\"))):
        out = []
        for src in sources:
            if _unresolvable(src):
                out.append((f"{raw} <- {src}", path, True, False))
                continue
            src_path = _absolute(src, cwd)
            child = os.path.join(path, os.path.basename(src_path.rstrip("\\/")))
            out.append((child, child, os.path.isdir(src_path), True))
        return out or [(raw, path, True, False)]
    tree = os.path.isdir(path) or any(  # `cp -r dir new` creates a whole tree at `new`
        _unresolvable(s) or os.path.isdir(_absolute(s, cwd)) for s in sources or [])
    return [(raw, path, tree, True)]


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
    found = targets(payload)
    if not found:
        return None
    store = store_of()
    if store.repo is None or not (store.repo / store_mod.PROJECT_CONFIG).is_file():
        return None
    globs = _non_product_globs(store.config)
    cwd = str(payload.get("cwd") or os.getcwd())
    roots = forms(str(store.repo))
    product = []
    for raw, sources in found:
        for label, path, tree, known in _outputs(raw, sources, cwd):
            rels = _product_forms(path, roots, globs)
            if rels:
                product.append((label, rels, tree or not known))
    if not product:
        return None
    units = scoped_units(store, rt)
    matched: list[str] = []
    for label, rels, tree in product:
        ids = [uid for uid, scope in units if _fits(rels, tree, scope)]
        if not ids:
            return (f"BLOCKED (ulthar unit scope, GOAL-08): {payload.get('tool_name')} writes the "
                    f"product path {label}, which is not provably inside a confirmed unit's "
                    "scope_globs (a directory needs a `<dir>/**` scope covering all of it). Product "
                    "files may be written only while a unit is contract_confirmed .. owner_review "
                    "and the path matches its confirmed contract's scope_globs. Confirm the "
                    "contract first (`ulthar contract submit` -> lint -> review -> the owner's "
                    "CONTRACT-CONFIRM -> `ulthar unit advance <unit> contract_confirmed`), or "
                    "widen the scope in a new contract version.")
        matched += [i for i in ids if i not in matched]
    detail["unit_id"] = matched[0] if len(matched) == 1 else matched
    return None
