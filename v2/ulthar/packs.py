"""Skill packs (SKL-01..06; R1-d AC-1, AC-3, AC-4, AC-5) -- hook side, also read by the CLI.

A pack is defined in `<repo>/.claude/ulthar.json` (JSON, so hooks can read it):
    "packs": [{"name": "ui", "path_globs": ["src/ui/**"], "skills": ["frontend-design"],
               "default_acs": [<R1-c AC>, ...], "evaluator_checklist": ["...", ...]}]
`load` is the one validator (the CLI and the hooks alike): every element present and
typed, a name usable in a file name, every skill a SKILL.md in <repo>/.claude/skills/
or ~/.claude/skills/, and no two packs whose path_globs can match one path (spec 事前
決定 3; decided by intersecting the globs, `globs_meet`). It raises PackError with every
reason; a hook that gets one blocks every product write (any could be a pack's).

`ulthar pack install` (ulthar.packinstall) generates `ulthar-creator@<pack>` and
`ulthar-evaluator@<pack>` and registers each file's sha256 and its SKILL.md files'
sha256 in runtime.db `pack_agents`. Hook side:
  * AC-3 `dispatch`: an Agent launch for a unit with packs whose subagent_type (absent =
    general-purpose, P-17) is not one of that unit's pack agents is rewritten to
    `ulthar-creator@<first pack>` through `updatedInput` (answered by the dispatcher,
    CORE-02); an unregistered target, or a launch whose unit cannot be told (the prompt
    names no unit id or several, and not exactly one unit is in progress), is blocked.
  * AC-4 `receipt`: SubagentStart of a registered pack agent whose definition file and
    every SKILL.md still hash as registered -> a receipts row; any `ulthar-*@*` agent
    that fails -> an agent_blocks row: every write of that agent_id is blocked.
  * AC-5 `check_write`: a product write whose path a pack's path_globs match is allowed
    only to an agent_id holding that pack's receipt; the window (no agent_id) is blocked
    (spec 事前決定 2). This comes on top of the unit-scope check (R1-c AC-11).
"""
from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath

from . import product
from . import store as store_mod
from .acshape import check_ac, is_str_list

PACK_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
AGENT_TYPE = re.compile(r"^ulthar-(creator|evaluator)@(.+)$")
UNIT_ID = re.compile(r"\bU-[0-9a-f]{12}\b")
ROLES = ("creator", "evaluator")
DEFAULT_AGENT = "general-purpose"  # an Agent call without subagent_type (P-17)
DISPATCH_STATES = ("contract_draft", "contract_confirmed", "build", "quant_verified",
                   "owner_review")
GUARD_ID = "skill_pack"
LAUNCH = frozenset({"Agent", "Task"})


class PackError(Exception):
    pass


def agent_type(role: str, pack: str) -> str:
    return f"ulthar-{role}@{pack}"


def skill_file(name: str, repo: Path | None) -> Path | None:
    """The SKILL.md of skill `name`: the project's .claude/skills, then the user's."""
    bases = ([repo / ".claude" / "skills"] if repo else []) + [Path.home() / ".claude" / "skills"]
    for base in bases:
        path = base / name / "SKILL.md"
        if path.is_file():
            return path
    return None


# ---------------------------------------------------------------------------- globs

def _seg_tokens(seg: str) -> list[str]:
    """A path segment pattern as tokens: '*', '?' (a `[...]` class counts as '?', which
    can only widen what is judged to overlap) or one literal character."""
    out, i = [], 0
    while i < len(seg):
        ch = seg[i]
        end = seg.find("]", i + 1) if ch == "[" else -1
        if end > 0:
            out.append("?")
            i = end + 1
            continue
        out.append(ch)
        i += 1
    return out


def _meet(a: list, b: list, wild: str, same) -> bool:
    """Whether some string matches both token lists; `wild` matches any run of items."""
    memo: dict = {}

    def f(i: int, j: int) -> bool:
        key = (i, j)
        if key not in memo:
            if i == len(a) and j == len(b):
                memo[key] = True
            elif i < len(a) and a[i] == wild:
                memo[key] = f(i + 1, j) or (j < len(b) and f(i, j + 1))
            elif j < len(b) and b[j] == wild:
                memo[key] = f(i, j + 1) or (i < len(a) and f(i + 1, j))
            else:
                memo[key] = i < len(a) and j < len(b) and same(a[i], b[j]) and f(i + 1, j + 1)
        return memo[key]
    return f(0, 0)


def _chars_meet(x: str, y: str) -> bool:
    return x == y or "?" in (x, y)


def _segs_meet(x: str, y: str) -> bool:
    return _meet(_seg_tokens(x), _seg_tokens(y), "*", _chars_meet)


def globs_meet(a: str, b: str) -> bool:
    """Whether one repository path can match both globs (pathlib full_match syntax:
    `**` any number of segments, `*` / `?` / `[...]` inside one); folded first."""
    return _meet(product.fold(a).split("/"), product.fold(b).split("/"), "**", _segs_meet)


def _matches(rel: str, globs: list[str]) -> bool:
    return any(PurePosixPath(rel).full_match(g) for g in globs)


def needed(rels: list[str], tree: bool, packs: list[dict]) -> set[str]:
    """The packs a write needs: each pack a path form matches -- for a directory written
    as a whole (tree), each pack any path under it can match."""
    return {p["name"] for p in packs for rel in rels
            if _matches(rel, p["path_globs"]) or (tree and _tree_meets(rel, p["path_globs"]))}


def _tree_meets(rel: str, globs: list[str]) -> bool:
    return any(globs_meet(f"{rel}/**" if rel else "**", g) for g in globs)


def inferred(scope_globs: list[str], packs) -> list[str]:
    """SKL-02 / LINT-H: the packs whose path_globs meet a scope glob."""
    return sorted(p["name"] for p in packs
                  if any(globs_meet(s, g) for s in scope_globs for g in p["path_globs"]))


# ---------------------------------------------------------------------------- AC-1

def _check_pack(i: int, p, repo: Path | None) -> list[str]:
    where = f"packs[{i}]"
    if not isinstance(p, dict):
        return [f"{where} must be an object"]
    out = [f"{where}.{k} is missing" for k in ("name", "path_globs", "skills", "default_acs",
                                               "evaluator_checklist") if k not in p]
    name = p.get("name")
    if "name" in p and not (isinstance(name, str) and PACK_NAME.match(name)):
        out.append(f"{where}.name must match {PACK_NAME.pattern} (it names agent files)")
    for k in ("path_globs", "skills", "evaluator_checklist"):
        if k in p and not (is_str_list(p[k]) and p[k] and all(x.strip() for x in p[k])):
            out.append(f"{where}.{k} must be a non-empty list of non-empty strings")
    if is_str_list(p.get("skills")):
        out += [f"{where}: unknown skill {s!r} (no .claude/skills/{s}/SKILL.md in the project "
                "or the user's ~/.claude)" for s in p["skills"] if s.strip() and not skill_file(s, repo)]
    acs = p.get("default_acs")
    if "default_acs" in p:
        if not isinstance(acs, list):
            out.append(f"{where}.default_acs must be a list of ACs (R1-c AC-2 shape)")
        else:
            for j, ac in enumerate(acs):
                out += check_ac(f"{where}.default_acs[{j}]", ac)
    return out


def load(cfg: dict, repo: Path | None) -> list[dict]:
    """The project's packs, validated (path_globs folded); PackError with every reason."""
    raw = cfg.get("packs", [])
    if not isinstance(raw, list):
        raise PackError(".claude/ulthar.json: packs must be a list of pack objects")
    problems = [m for i, p in enumerate(raw) for m in _check_pack(i, p, repo)]
    if not problems:
        names = [p["name"] for p in raw]
        problems += [f"pack name {n!r} is defined twice" for n in sorted(set(names))
                     if names.count(n) > 1]
        for i, a in enumerate(raw):
            for b in raw[i + 1:]:
                both = [(x, y) for x in a["path_globs"] for y in b["path_globs"] if globs_meet(x, y)]
                if both:
                    problems.append(f"packs {a['name']!r} and {b['name']!r} overlap: path_globs "
                                    f"{both[0][0]!r} and {both[0][1]!r} can match the same path "
                                    "(one path belongs to one pack)")
    if problems:
        raise PackError(".claude/ulthar.json packs are invalid:\n  " + "\n  ".join(problems))
    return [dict(p, path_globs=[product.fold(g) for g in p["path_globs"]]) for p in raw]


def for_store(store: store_mod.Store) -> list[dict]:
    """The packs of the store's project ([] without `.claude/ulthar.json`)."""
    if store.repo is None or not (store.repo / store_mod.PROJECT_CONFIG).is_file():
        return []
    return load(store.config, store.repo)


# ---------------------------------------------------------------------------- AC-3

def _block(why: str) -> str:
    return f"BLOCKED (ulthar skill pack, SKL-03): {why}"


def registered(rt, agent: str) -> bool:
    return rt.execute("SELECT 1 FROM pack_agents WHERE agent_type=?", (agent,)).fetchone() is not None


def unit_packs(unit: dict) -> list[str]:
    ref = unit.get("contract") if unit.get("state") == "contract_draft" else unit.get("confirmed")
    return list((ref or {}).get("packs") or [])


def _dispatch_unit(payload: dict, store: store_mod.Store, rt) -> tuple[dict | None, str | None]:
    ti = payload.get("tool_input") or {}
    named = set(UNIT_ID.findall(f"{ti.get('description') or ''}\n{ti.get('prompt') or ''}"))
    with store.reader(rt) as conn:
        units = store_mod.current(conn, "unit")
    if named:
        found = [u for u in units if u["id"] in named]
        if len(named) != 1 or not found:
            return None, (f"the launch names {sorted(named)}: name exactly one existing unit id in "
                          "the prompt")
        return found[0], None
    active = [u for u in units if u.get("state") in DISPATCH_STATES]
    if len(active) != 1:
        return None, (f"the launch names no unit id and {len(active)} units are in progress: "
                      "write the unit id (U-...) the subagent works on into its prompt")
    return active[0], None


def dispatch(payload: dict, store: store_mod.Store, rt) -> tuple[str | None, dict | None, str | None]:
    """AC-3 for an Agent / Task PreToolUse: (block message, rewritten tool_input, note)."""
    try:
        packs = for_store(store)
    except PackError as exc:
        return _block(f"the pack definitions cannot be read, so the subagent's type cannot be "
                      f"checked: {exc}"), None, None
    if not packs:
        return None, None, None
    unit, why = _dispatch_unit(payload, store, rt)
    if unit is None:
        return _block(why), None, None
    wanted = unit_packs(unit)
    if not wanted:
        return None, None, f"unit {unit['id']} declares no pack"
    ti = payload.get("tool_input") or {}
    requested = ti.get("subagent_type") or DEFAULT_AGENT
    allowed = {agent_type(role, p) for p in wanted for role in ROLES}
    target = requested if requested in allowed else agent_type("creator", wanted[0])
    if not registered(rt, target):
        return _block(f"unit {unit['id']} needs {target}, which is not installed: run `ulthar pack "
                      "install --agents-dir <dir>` before the session starts (P-13)"), None, None
    if target == requested:
        return None, None, f"unit {unit['id']}: {target}"
    return None, dict(ti, subagent_type=target), f"unit {unit['id']}: {requested} -> {target}"


# ---------------------------------------------------------------------------- AC-4

def file_sha(path: str | Path) -> str | None:
    try:
        return store_mod.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def receipt(rt, payload: dict) -> tuple[dict | None, str | None]:
    """(receipts row, None) for a verified pack agent, (None, why) for a pack-agent type
    that fails, (None, None) for any other agent type."""
    kind = str(payload.get("agent_type") or "")
    if not AGENT_TYPE.match(kind):
        return None, None
    reg = rt.execute("SELECT pack, def_path, def_sha, skills FROM pack_agents WHERE agent_type=?",
                     (kind,)).fetchone()
    if reg is None:
        return None, f"{kind} is not a registered pack agent (`ulthar pack install`)"
    pack, def_path, def_sha, skills = reg
    if file_sha(def_path) != def_sha:
        return None, f"{kind}: its definition {def_path} changed since `ulthar pack install`"
    hashes = {}
    for s in json.loads(skills):
        if file_sha(s["path"]) != s["sha"]:
            return None, f"{kind}: skill {s['name']} ({s['path']}) changed since `ulthar pack install`"
        hashes[s["name"]] = s["sha"]
    return {"agent_id": payload.get("agent_id"), "pack": pack, "agent_def_hash": def_sha,
            "skill_hashes": hashes, "agent_type": kind}, None


def write_receipt(rt, row: dict) -> None:
    rt.execute("INSERT INTO receipts (agent_id, pack, agent_def_hash, skill_hashes, ts, agent_type)"
               " VALUES (?,?,?,?,?,?)", (row["agent_id"], row["pack"], row["agent_def_hash"],
                                        json.dumps(row["skill_hashes"]), store_mod.now_iso(),
                                        row["agent_type"]))


def refuse_agent(rt, agent_id: str, kind: str, why: str) -> None:
    rt.execute("INSERT OR REPLACE INTO agent_blocks (agent_id, agent_type, reason, ts)"
               " VALUES (?,?,?,?)", (agent_id, kind, why, store_mod.now_iso()))


def refused(rt, agent_id: str) -> str | None:
    row = rt.execute("SELECT reason FROM agent_blocks WHERE agent_id=?", (agent_id,)).fetchone()
    if row is None:
        return None
    return _block(f"this subagent's receipt check failed at its start ({row[0]}): every write "
                  "of it is blocked (AC-4). Launch a fresh pack agent after `ulthar pack install`.")


def receipts_of(rt, agent_id: str, role: str | None = None) -> set[str]:
    sql = "SELECT pack FROM receipts WHERE agent_id=?"
    rows = rt.execute(sql, (agent_id,)).fetchall() if role is None else rt.execute(
        sql + " AND agent_type LIKE ?", (agent_id, f"ulthar-{role}@%")).fetchall()
    return {r[0] for r in rows}


# ---------------------------------------------------------------------------- AC-5

def check_write(payload: dict, entries: list, store: store_mod.Store, rt) -> str | None:
    """`entries`: product.check's (label, repository-relative forms, is_tree) of this
    call's product writes. The block message, or None."""
    if not entries:
        return None
    try:
        packs = for_store(store)
    except PackError as exc:
        return _block(f"the pack definitions cannot be read, so no product write can be shown "
                      f"to need no pack: {exc}")
    agent, have = payload.get("agent_id"), None
    for label, rels, tree in entries:
        need = needed(rels, tree, packs)
        if not need:
            continue
        if not agent:
            return _block(f"{label} belongs to pack {sorted(need)}: the window (main session) "
                          "never writes a pack's paths (spec 事前決定 2). Launch the unit's "
                          f"{agent_type('creator', sorted(need)[0])} to write it.")
        if have is None:
            have = receipts_of(rt, agent)
        if need - have:
            return _block(f"{label} belongs to pack {sorted(need)}, and this subagent holds no "
                          f"receipt for {sorted(need - have)} (it holds {sorted(have)}): only a "
                          f"verified {agent_type('creator', sorted(need)[0])} writes it (SKL-05).")
    return None
