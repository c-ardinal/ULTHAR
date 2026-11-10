"""hooks/lib/ctx.py -- one Ctx, one is_main_chain, config load + cache.

CONTRACT.md #1 (Ctx) and #2 (lib/ctx.py). Ctx is the injection seam: every guard is a
pure function of (payload, cfg, ctx), and every source of non-determinism (clock,
filesystem, subprocesses, process table) enters through ctx so tests can stub it.

IMPORTANT: lib/procs.py (and therefore psutil) is imported LAZILY inside
Ctx.list_processes, never at module level -- importing it eagerly here would put
psutil on the PreToolUse hot path for every guard, which I-13.2 forbids.
"""
from __future__ import annotations

import copy
import os
import re
import shutil
import signal
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from collections import namedtuple

from . import io as io_mod
from . import paths as paths_mod


class CmdResult(namedtuple("CmdResult", ("rc", "out", "err", "timed_out"))):
    __slots__ = ()


class ProcInfo(namedtuple("ProcInfo", ("pid", "ppid", "name", "cmdline"))):
    __slots__ = ()


# ---------------------------------------------------------------- is_main_chain


def is_main_chain(payload: dict) -> bool:
    """True only when BOTH signals agree it's main chain (CONTRACT.md PE-1/PE-2):
    an absent agent_id and an empty-string agent_id both mean "no id"."""
    agent_id = str(payload.get("agent_id") or "").strip()
    transcript = str(payload.get("transcript_path") or "")
    looks_sub_by_id = bool(agent_id)
    looks_sub_by_path = re.search(r"[\\/]subagents[\\/]", transcript, re.I) is not None
    return not looks_sub_by_id and not looks_sub_by_path


# ---------------------------------------------------------------- config load + cache

_config_cache: dict[tuple[str, float, int], dict] = {}


def load_config(path: Path | None = None) -> dict:
    """Cached by (path, mtime, size). `ULTHAR_CONFIG` env override is honoured by the
    caller (dispatch.py) when it resolves `path` before calling this -- this
    function itself does not read the environment (guards must not; keeping the env
    read out of lib/ctx.py's cache key logic keeps it testable with an explicit path)."""
    import json

    resolved = str(path)
    st = os.stat(resolved)
    key = (resolved, st.st_mtime, st.st_size)
    cached = _config_cache.get(key)
    if cached is not None:
        return cached
    # utf-8-sig, not utf-8: config.json is a state-file-class read (I-4), and a
    # BOM'd config.json (as written by PowerShell's Set-Content -Encoding UTF8)
    # would otherwise make json.load raise and silently disable every guard.
    with open(resolved, "r", encoding="utf-8-sig") as fh:
        cfg = json.load(fh)
    _resolve_named_alternations(cfg)
    _config_cache.clear()  # only the newest (path, mtime, size) is worth keeping
    _config_cache[key] = cfg
    return cfg


def _resolve_named_alternations(cfg: dict) -> None:
    """Resolve every `*_named` pointer against regex.named_alternations ONCE, at load
    time, before per-guard compilation (CONTRACT.md N-4). A dangling name is a
    config error -- raised here so dispatch.py's config-load try/except can log it
    to config_error_log and, for the DEFAULT deploy path, install.py can refuse to
    deploy. This function does not mutate call sites; it only validates that every
    `*_named` value resolves, which is the load-bearing half of N-4 for U-0's scope."""
    named = cfg.get("regex", {}).get("named_alternations", {})

    def _walk(node) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if k.startswith("_"):
                    continue
                if k.endswith("_named") and isinstance(v, str):
                    if v not in named:
                        raise ConfigError(
                            f"dangling *_named reference: {k} -> {v!r}"
                        )
                    literal = k[: -len("_named")]
                    if literal in node:
                        raise ConfigError(
                            f"both {literal!r} and {k!r} present at one site"
                        )
                _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)

    _walk(cfg)


# ---------------------------------------------------------------- overlay merge


_MISSING = object()


def _get_dotted(d: dict, path: str):
    node = d
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return _MISSING
        node = node[part]
    return node


def _set_dotted(d: dict, path: str, value) -> None:
    parts = path.split(".")
    node = d
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    node[parts[-1]] = value


def _read_overlay_json(path: Path) -> dict:
    import json

    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except Exception as exc:
        raise ConfigError(f"overlay {path} does not parse as JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"overlay {path} must be a JSON object at the top level, got {type(data).__name__}")
    return data


def _collect_container_ids(node, out: set) -> None:
    """Records id() of every dict/list inside an overlay-contributed item, so
    dispatch.py can tell whether a crashing guard actually had one of those
    objects in hand. CONTAINERS ONLY: small strings and ints are interned and
    shared, so an id() match on a scalar would be a coin flip, not evidence --
    scalars are covered by merge-time type validation instead, which is where
    the refusal of unreadable content lives."""
    if isinstance(node, dict):
        out.add(id(node))
        for value in node.values():
            _collect_container_ids(value, out)
    elif isinstance(node, list):
        out.add(id(node))
        for value in node:
            _collect_container_ids(value, out)


_SCALAR_TYPES = {"str": str, "int": int, "float": float, "bool": bool}


def _strategy_and_shape(spec) -> tuple:
    """One `overlays.list_merge` value. A bare string is the strategy with the
    element shape DERIVED from the base list; an object is the same strategy
    plus a DECLARED shape, which is what a list shipping empty needs (there are
    no base entries to derive from). No other reader parses this value --
    install.py hands its `list_merge` straight to _merge_walk -- so the two
    encodings cannot drift apart."""
    if isinstance(spec, dict):
        return spec.get("strategy"), spec
    return spec, {}


def _no_schema(path: str, source: str, what: str) -> "ConfigError":
    return ConfigError(
        f"overlay path {path!r} ({source}): this list ships EMPTY in config.json and "
        f"declares no {what} in overlays.list_merge, so there is nothing to validate an "
        f"appended entry against. Refusing the append: unvalidated content reaching a "
        f"guard is how an append becomes a fail-open. Declare the shape at this path in "
        f"overlays.list_merge as an object, e.g. "
        f'{{"strategy": "append", "element": "str"}} or '
        f'{{"strategy": "append_by_id", "required_keys": ["id", "..."]}}.'
    )


def _check_replacement_conforms(cfg: dict, path: str, value: list, source: str) -> None:
    """The USER layer may replace a list that declares no merge strategy -- the
    ones that are not rule lists at all: `command_rules.redirect_noise_strips`,
    `quote_strip_patterns`, and their siblings. That replacement went in with NO
    shape check of any kind, and it is the one hole none of the other defences
    could see: the path is not pinned (it is inside `command_rules`), the
    container is a list replacing a list, and every rule still exists afterwards,
    so `_refuse_pinned_paths`, `_refuse_container_swap` and
    `_refuse_authority_erasure` all pass it. Measured:
    `{"command_rules": {"redirect_noise_strips": [["x"]]}}` made tool_discipline
    raise inside `_build_probes` -- BEFORE it reached a single rule -- and the
    dispatcher's fail-open turned that into rc=0 with empty stderr.

    So a replacement is held to the shape the BASE entries already have, which is
    the same schema-from-config.json idea `_check_scalar_conforms` and
    `_object_schema` use for appends, and it cannot rot for the same reason. Per
    element: the base element type; for a list of lists, the same arity; for a
    list of objects, at least the keys every base object carries. A base list
    that ships EMPTY declares nothing to derive from, so a non-empty replacement
    is refused outright, exactly as an append to such a list already is.

    This is the merge-time half. The structural half is in dispatch.py: with an
    overlay merged, a guard exception blocks instead of failing open, whatever
    shape slipped past here."""
    base = _get_dotted(cfg, path)
    if not isinstance(base, list):
        return
    if not base:
        if value:
            raise ConfigError(
                f"overlay path {path!r} ({source}): this list ships EMPTY and declares no "
                "shape in overlays.list_merge, so there is nothing to check a replacement "
                "against. Refused rather than merged unvalidated -- the same rule an "
                "append to an empty undeclared list already gets."
            )
        return
    base_types = {type(b) for b in base}
    if len(base_types) != 1:
        return  # heterogeneous base: nothing single to hold a replacement to
    expected = next(iter(base_types))
    for item in value:
        if type(item) is not expected:
            raise ConfigError(
                f"overlay path {path!r} ({source}): this list holds "
                f"{expected.__name__} entries, got {type(item).__name__}: {item!r}. "
                "A guard reads these positionally and would raise on the wrong type, "
                "which the dispatcher cannot tell from a bug in ULTHAR."
            )
        if expected is list:
            arities = {len(b) for b in base}
            if len(arities) == 1 and len(item) not in arities:
                raise ConfigError(
                    f"overlay path {path!r} ({source}): every entry here is a list of "
                    f"{next(iter(arities))} items, got {len(item)}: {item!r}."
                )
        elif expected is dict:
            required = set.intersection(*(set(b) for b in base)) if base else set()
            missing = sorted(required - set(item))
            if missing:
                raise ConfigError(
                    f"overlay path {path!r} ({source}): every entry here carries "
                    f"{sorted(required)}; this one is missing {missing}: {item!r}."
                )


def _check_scalar_conforms(base: list, item, shape: dict, path: str, source: str) -> None:
    """An 'append' path is a HOMOGENEOUS list of scalars and every consumer does
    one type's operations on it (`n.casefold()` for dev_processes.names,
    `re.escape()` for git_policy.deploy_refs, set membership against ints for
    .ports). A type-legal but wrong-typed item merges cleanly and then raises
    inside the guard -- a fail-open. So the type is enforced HERE, before any
    guard sees it: against the base list's own entries where there are any (a
    schema that cannot rot, because it IS config.json), and against the
    `element` declared next to the strategy where the base ships empty."""
    types = {type(b) for b in base}
    if base:
        if len(types) != 1 or type(item) in types:
            return
        expected = next(iter(types)).__name__
    else:
        expected = shape.get("element")
        expected_type = _SCALAR_TYPES.get(expected)
        if expected_type is None:
            raise _no_schema(path, source, "'element' type")
        if type(item) is expected_type:
            return
    raise ConfigError(
        f"overlay path {path!r} ({source}): this list holds {expected} entries, "
        f"got {type(item).__name__}: {item!r}"
    )


def _object_schema(base: list, shape: dict, path: str, source: str) -> tuple:
    """(required keys, per-key type) for an 'append_by_id' path -- the shape an
    appended entry must have for the consuming guard to be able to READ it.

    Derived from the base list's existing entries: the keys EVERY base entry
    carries (`_`-prefixed notes excluded), plus, for each of those keys, the
    type every base entry stores there when they agree. That is a schema which
    cannot drift from config.json, because it is computed from config.json.
    Where the base ships empty there is nothing to derive from, so the shape
    must be DECLARED (`required_keys`) beside the strategy; an empty base with
    no declaration refuses appends outright."""
    entries = [b for b in base if isinstance(b, dict)]
    if entries:
        required = {k for k in set.intersection(*(set(e) for e in entries))
                    if not k.startswith("_")}
        key_types = {}
        for key in required:
            types = {type(e[key]) for e in entries}
            if len(types) == 1:
                key_types[key] = next(iter(types))
        return required, key_types
    declared = shape.get("required_keys")
    if not isinstance(declared, list) or not declared:
        raise _no_schema(path, source, "'required_keys'")
    return {str(k) for k in declared}, {}


def _check_object_conforms(item: dict, required: set, key_types: dict,
                           path: str, source: str) -> None:
    missing = sorted(required - set(item))
    if missing:
        raise ConfigError(
            f"overlay path {path!r} ({source}): entry {item.get('id')!r} is missing "
            f"{', '.join(repr(k) for k in missing)}. Every entry this list already holds "
            f"carries them, and a guard reading this list raises on an entry that does "
            f"not -- which the dispatcher's fail-open would turn into ALLOW. "
            f"Refused at merge time, so no guard ever sees it."
        )
    for key, expected in key_types.items():
        if type(item[key]) is not expected:
            raise ConfigError(
                f"overlay path {path!r} ({source}): entry {item.get('id')!r} key {key!r} "
                f"holds {expected.__name__} in every existing entry at this path, got "
                f"{type(item[key]).__name__}: {item[key]!r}"
            )


_DISABLE_KEY = "disable"


def _entry_id(entry) -> str:
    """The identifier a `disable` entry names. Rule lists hold objects with an
    `id`; policy lists hold bare scalars (a branch name, a port), which are
    their own identifier."""
    return str(entry.get("id")) if isinstance(entry, dict) else str(entry)


def rule_ids_at(cfg: dict, path: str) -> list[str] | None:
    """Every rule identifier at a dotted config path, or None when that path is
    not a list of rules. THE one place "what does the right half of a
    <path>/<rule id> identifier match" is answered, so the overlay `disable`
    operation and lib/grant.py's owner grants cannot drift apart on it --
    _apply_disables below resolves the same two halves the same way, against the
    same _entry_id (object lists carry an `id`; scalar lists are their own
    identifier)."""
    entries = _get_dotted(cfg, path)
    if not isinstance(entries, list):
        return None
    return [_entry_id(e) for e in entries]


def _collect_disables(doc: dict, restrict_to, source: str) -> list[tuple[str, str]]:
    """Validates one overlay's top-level `disable` list SYNTACTICALLY and returns
    [(identifier, source)]. Whether each identifier actually matches a rule is
    decided later, by _apply_disables, once every layer's appends have merged --
    so either layer may disable a rule the other one added, and both layers
    naming the same rule is agreement rather than a typo.

    Identifier syntax: "<dotted config path>/<rule id>", e.g.
    "git_policy.destroyers/clean". The left half is exactly the dotted path
    overlays.list_merge and overlays.project_allowed_paths already use, and a
    dotted path never contains '/', so the FIRST '/' splits unambiguously and a
    ref-shaped id ("release/1.0") stays addressable."""
    spec = doc.get(_DISABLE_KEY)
    if spec is None:
        return []
    if not isinstance(spec, list) or not all(isinstance(s, str) for s in spec):
        raise ConfigError(
            f"overlay {source}: 'disable' must be a list of \"<config path>/<rule id>\" "
            f'strings, e.g. ["git_policy.destroyers/clean"] -- got {spec!r}'
        )
    out = []
    for ident in spec:
        path, sep, rule_id = ident.partition("/")
        if not sep or not path or not rule_id:
            raise ConfigError(
                f"overlay {source}: disable entry {ident!r} is not "
                '"<config path>/<rule id>", e.g. "git_policy.destroyers/clean"'
            )
        if restrict_to is not None and path not in restrict_to:
            raise ConfigError(
                f"overlay {source}: disable entry {ident!r} names {path!r}, which is not in "
                "overlays.project_allowed_paths -- a project overlay states policy about the "
                "RULE lists listed there (it may add to them and switch entries off); the rest "
                "of config.json is ULTHAR's own wiring, not project policy"
            )
        out.append((ident, source))
    return out


def _refuse_authority_erasure(base_cfg: dict, merged: dict, sources: tuple[str, ...]) -> None:
    """THE CLASS CLOSE, as an invariant on the merge OUTPUT rather than on any
    particular spelling of the input.

    Every fix before this one named a shape: a list where a dict belongs, a dict
    where a list belongs, a scalar where a section belongs. Each was found by
    someone trying that spelling. This asks the question the shapes were proxies
    for -- after everything has merged, are the rules that may never be switched
    off still THERE, and do the files no agent may write still RESOLVE? -- so a
    future hole in the shape checks is a loud refusal instead of a silent
    fail-open. It is deliberately cheap: two lists of identifiers, resolved once
    per dispatch, only when an overlay exists at all.

    Both lists are read from the BASE cfg. They are inside the pinned `grants`
    section so an overlay cannot rewrite them anyway; reading base is belt and
    braces and costs nothing.

    `resolve_ref` is imported lazily because lib/grant.py imports THIS module.
    It is the same resolver grant_files goes through (protected_targets), which
    is the point: if the protection resolves to nothing here, it resolves to
    nothing there."""
    from . import grant as grant_mod  # noqa: PLC0415 -- circular at module scope

    gcfg = base_cfg.get("grants") or {}
    where = f" ({' / '.join(sources)})" if sources else ""
    for ident in gcfg.get("never_exempt") or ():
        path, _, rule_id = str(ident).partition("/")
        ids = rule_ids_at(merged, path)
        if ids is None or rule_id not in ids:
            raise ConfigError(
                f"overlay{where}: after merging, the rule {ident!r} named in "
                "grants.never_exempt no longer exists in the configuration. That rule is "
                "one of the two that protect the authority mechanism itself, so a merge "
                "result without it is refused whatever produced it -- most likely a "
                f"section or list at {path!r} replaced rather than added to."
            )
    for entry in gcfg.get("protected") or ():
        if not isinstance(entry, dict):
            continue
        ref = str(entry.get("ref", ""))
        values = [v for v in grant_mod.resolve_ref(merged, ref)
                  if isinstance(v, str) and v.strip()]
        if not values:
            raise ConfigError(
                f"overlay{where}: after merging, grants.protected entry "
                f"{entry.get('id', ref)!r} resolves to nothing ({ref!r} is gone), so "
                "guards/grant_files.py would stop protecting "
                f"{entry.get('why', 'that file')}. Refused."
            )


def _refuse_pinned_paths(base_cfg: dict, merged: dict,
                         pending: list[tuple[str, str]],
                         sources: tuple[str, ...] = ()) -> None:
    """The SUBTREES no overlay may change, in EITHER layer --
    `overlays.pinned_paths`, read from the BASE cfg so an overlay cannot empty
    the list on its way past (`overlays` being one of the four is what pins the
    list itself).

    The user layer is otherwise unrestricted and stays that way: it is the
    owner's own file, and the owner's standing decision that an overlay may
    weaken ORDINARY rules is untouched. Every rule list stays appendable and
    disableable from both layers; logging.* and paths.* stay overlayable.

    SUBTREES, NOT LEAVES, and that is the round-2 correction. The first version
    pinned four leaf paths and the review then measured five more single-overlay
    switch-offs it did not reach -- each rc=0 on a Write of a grant file that is
    rc=2 with no overlay:

      {"disable": ["dispatch.events.PreToolUse/None"]}  -> a CHILD of the pinned
          path. The routing entries carry no `id`, so _entry_id yields the
          string "None", _apply_disables matches one, and the whole PreToolUse
          chain is removed. Equality on the left half never saw it.
      {"regex": {"named_alternations": {"write_edit_tools": ...}}} -> the
          Write/Edit chain is routed through that NAME, so pointing it at
          something no tool matches unroutes every guard on it.
      {"grants": {"enabled": false}}   -> guards/grant_files.py returns ALLOW at
          its first line, so all four protected files become writable INCLUDING
          the user overlay, which makes the change permanent.
      {"grants": {"dir": ...}}         -> the Write half follows grants.dir
          while the shell rule holds the literal, so a repointed directory was
          reachable through the shell and the grant honoured end-to-end.
      {"grants": {"protected_message": 5}} -> grant_files formats it with
          .replace(); a non-string is an AttributeError, the dispatcher's
          fail-open turns that into ALLOW. Shape validation does not cover it:
          it validates LIST entries, and every scalar a guard reads with a type
          assumption is the same shape of bug.

    Enumerating leaves is chasing instances of a class. The class is: a path
    that is NOT A RULE but decides what a rule can do. Those live in four
    sections -- dispatch, grants, regex, overlays -- and pinning the section
    closes the instances nobody has thought of yet.

    THE ARGUMENT for pinning the user layer at all is the one that moved
    grants.never_exempt to both layers, and it is about materialisation rather
    than about trust: `cp x ~/.claude/`, `tar -xf -C ~/.claude`, `git apply`,
    `git clone` all reach the user overlay without naming it, so "it is the
    owner's own file" is not a structural property. What the owner gives up is
    doing any of the above FROM AN OVERLAY -- including extending
    regex.named_alternations, which is a legitimate use and is now refused;
    what they keep is doing it in config.json itself, which no overlay can reach
    and which install.py re-registers from.

    Refused loudly rather than ignored: an overlay that quietly did nothing is
    the invisible failure this whole operation exists to replace.

    `sources` is every overlay FILE merged this dispatch, in layer order. The
    rewrite branch below compares a merged VALUE against the base and so cannot
    attribute the change to one layer the way a `disable` identifier can -- and
    the dispatcher's overlay recovery text tells the reader to delete the file
    named in the detail line, which must never be a pointer to nothing
    (_merge_layer's whole reason for existing). Naming every file that was
    merged is the honest form of that: with one overlay present, which is the
    common case, it is exact."""
    for entry in (base_cfg.get("overlays") or {}).get("pinned_paths") or ():
        ref = entry["ref"]
        if _get_dotted(merged, ref) != _get_dotted(base_cfg, ref):
            where = f" ({' / '.join(sources)})" if sources else ""
            raise ConfigError(
                f"overlay{where}: {ref} cannot be changed by an overlay (either layer) -- "
                f"{entry['why']}. It is pinned in overlays.pinned_paths; change it in "
                "config.json itself, which is the owner's own file and is not "
                "overlay-writable."
            )
        for ident, source in pending:
            left = ident.partition("/")[0]
            # PREFIX, not equality. `{"disable": ["dispatch.events.PreToolUse/None"]}`
            # named a CHILD of the pinned path and emptied the whole PreToolUse
            # chain: the routing entries carry no `id`, so _entry_id yields the
            # string "None" and matches one. Measured rc=0 on a grant-file Write.
            if left == ref or left.startswith(ref + "."):
                raise ConfigError(
                    f"overlay {source}: disable entry {ident!r} names {left!r}, which is "
                    f"inside the pinned subtree {ref!r} (overlays.pinned_paths) -- "
                    f"{entry['why']}. A `disable` removes an entry from that list exactly "
                    "as a rewrite would."
                )


def _refuse_never_exempt(base_cfg: dict, merged: dict,
                         pending: list[tuple[str, str]]) -> None:
    """`grants.never_exempt` names the rules that protect the authority mechanism
    itself. It was enforced against grant files (lib/grant.py) and NOT against an
    overlay `disable`, which is the same power with a longer lifetime -- and
    `command_rules.tool_discipline` is in overlays.project_allowed_paths, so a
    repo could switch off `authority-file-in-shell` permanently just by shipping
    an overlay. config.json's own `_note_never_exempt` argues that a grant able to
    switch that rule off would be granting the power to write grants; the argument
    is about the rule, not about the lifetime, so it applies verbatim here.

    BOTH LAYERS, including the user's own overlay. The tempting exception -- "the
    user overlay is the owner's file" -- does not survive the pattern:
    authority-file-in-shell matches a command that NAMES the file, so one
    `cp x ~/.claude/` or `tar -xf -C ~/.claude` (destination directory only, no
    file name in the command) materialises a user overlay exactly as easily as a
    project one. The owner keeps a way to switch these two rules off: they are
    `enabled: false` in config.json, which is the owner's own file and is not
    overlay-writable. Read from the BASE cfg, before any layer has walked it, so a
    user overlay cannot empty this list on its way past.

    An ordinary rule is untouched: the owner's decision that a project overlay may
    weaken ordinary rules stands.

    THE LIST ITSELF IS ALSO PINNED. `grants.never_exempt` is not in
    overlays.list_merge, so it merges by REPLACEMENT -- and the user layer has no
    path restriction, so `{"grants": {"never_exempt": []}}` in a user overlay
    would hand back both the disable above and (lib/grant.py:validate reads the
    MERGED cfg) the grant route as well. An overlay able to rewrite the carve-out
    has the carve-out for free, which is the same closure argument one level up.
    Refused loudly rather than silently ignored: an overlay that quietly did
    nothing is the invisible failure this whole operation exists to replace.
    Every RULE list stays overlay-appendable; only this one short list of rule
    IDENTIFIERS is frozen. Shadowing by id needs no code here: _merge_list already
    refuses an appended entry that reuses an existing id, so a rule can only leave
    a list through `disable` (pinned in test_overlay.py)."""
    reason = (base_cfg.get("grants") or {}).get(
        "_note_never_exempt", "see grants.never_exempt in config.json")
    base_never = list((base_cfg.get("grants") or {}).get("never_exempt") or ())
    if list((merged.get("grants") or {}).get("never_exempt") or ()) != base_never:
        raise ConfigError(
            "overlay: grants.never_exempt cannot be changed by an overlay -- it names "
            "the rules that protect the mechanism deciding what an overlay may do, so "
            f"rewriting it would waive itself: {reason}"
        )
    never = set(base_never)
    if not never:
        return
    for ident, source in pending:
        if ident in never:
            raise ConfigError(
                f"overlay {source}: disable entry {ident!r} is in grants.never_exempt "
                f"and cannot be switched off by an overlay -- it protects the mechanism "
                f"that decides what an overlay may do: {reason}"
            )


def _apply_disables(cfg: dict, pending: list[tuple[str, str]]) -> list[str]:
    """Removes each named rule from its list. Applied AFTER every layer's
    appends, so an identifier is judged against the config that will actually
    run. An identifier matching nothing is a ConfigError naming the file, the
    path and the ids that ARE there: a typo that silently switched nothing off
    would be the same invisible failure this operation exists to replace."""
    applied = []
    for ident, source in pending:
        if ident in applied:
            continue  # both layers switched the same rule off; agreement, not a typo
        path, _, rule_id = ident.partition("/")
        entries = _get_dotted(cfg, path)
        if not isinstance(entries, list):
            raise ConfigError(
                f"overlay {source}: disable entry {ident!r} names {path!r}, which is not a "
                "list of rules in config.json"
            )
        kept = [e for e in entries if _entry_id(e) != rule_id]
        if len(kept) == len(entries):
            raise ConfigError(
                f"overlay {source}: disable entry {ident!r} matches no rule. {path!r} holds: "
                f"{', '.join(repr(_entry_id(e)) for e in entries) or '(nothing)'}"
            )
        _set_dotted(cfg, path, kept)
        applied.append(ident)
    return applied


def _merge_list(cfg: dict, path: str, incoming: list, strategy: str, source: str,
                taint: set | None = None, shape: dict | None = None) -> None:
    """Merges one overlay list into `cfg`, VALIDATING every incoming entry first.

    Validation is what keeps a disable EXPLICIT: an entry whose shape a guard
    cannot read is refused HERE, so no guard ever sees it. Without it, shipping
    one malformed entry would switch off every rule the consuming guard
    enforces, silently.

    SCOPE OF "a rule is switched off by naming it or not at all", because it is
    narrower than it used to read: it is true of the DECLARED list paths this
    function merges -- those are append-only, a duplicate id is refused, and
    removal happens only through _apply_disables. It is NOT a statement about
    every path. A user-layer replacement of an UNDECLARED list -- the ones that
    hold no rules, `command_rules.redirect_noise_strips` and
    `quote_strip_patterns` -- changes what ORDINARY rules see: those lists feed
    probe construction, so replacing them with a shape-VALID value alters which
    text the rules match against. That is the owner-permitted equivalent of a
    `disable`, in the same class as the standing decision that an overlay may
    switch ordinary rules off, and it is shape-checked
    (_check_replacement_conforms) so it cannot do it by crashing. It never
    reaches the two authority rules: those match `target: raw`, the unprocessed
    command text, which no probe transform touches.

    The schema is not a hand-written table that rots: it is derived from the
    base list's own entries (see _object_schema / _check_scalar_conforms), or,
    for a list that ships empty, declared beside the strategy in
    overlays.list_merge."""
    shape = shape or {}
    base = _get_dotted(cfg, path)
    if base is _MISSING:
        base = []
    if not isinstance(base, list):
        raise ConfigError(f"overlay path {path!r} ({source}): base config value at this path is not a list")
    merged = list(base)
    if strategy == "append":
        for item in incoming:
            # 'append' paths hold PLAIN SCALARS (overlays.list_merge says so, and
            # every consumer does string/int/set operations on them). A dict or a
            # list here is not a rule the guard can read -- it is an AttributeError
            # or an unhashable-type TypeError inside a guard, i.e. a fail-open.
            if not isinstance(item, (str, int, float, bool)):
                raise ConfigError(
                    f"overlay path {path!r} ({source}): 'append' path holds plain scalars, "
                    f"got {type(item).__name__}: {item!r}"
                )
            _check_scalar_conforms(base, item, shape, path, source)
            if item not in merged:
                merged.append(item)
    elif strategy == "append_by_id":
        required, key_types = _object_schema(base, shape, path, source)
        existing_ids = {it.get("id") for it in merged if isinstance(it, dict)}
        for item in incoming:
            if not isinstance(item, dict) or "id" not in item:
                raise ConfigError(f"overlay path {path!r} ({source}): append_by_id item missing 'id': {item!r}")
            # A non-string id is not merely wrong-typed: an unhashable one (list,
            # dict) raises a bare TypeError out of the set operations below, which
            # then reaches the user with no overlay path in it -- exactly when they
            # are locked out and need to know which file to delete.
            if not isinstance(item["id"], str):
                raise ConfigError(
                    f"overlay path {path!r} ({source}): append_by_id 'id' must be a string, "
                    f"got {type(item['id']).__name__}: {item['id']!r}"
                )
            if item["id"] in existing_ids:
                raise ConfigError(
                    f"overlay {source} adds id {item['id']!r} at {path!r}, which already exists "
                    "in the base/user configuration -- ids identify rules and must stay unique, "
                    "so a redefinition is ambiguous rather than forbidden. To switch the "
                    f"existing rule off, say so explicitly: "
                    f'"disable": ["{path}/{item["id"]}"] (and give the replacement its own id)'
                )
            # AFTER the id checks, so a shadowing attempt keeps its own message.
            _check_object_conforms(item, required, key_types, path, source)
            merged.append(item)
            existing_ids.add(item["id"])
            if taint is not None:
                _collect_container_ids(item, taint)
    else:
        raise ConfigError(f"overlay path {path!r} ({source}): unknown list_merge strategy {strategy!r}")
    _set_dotted(cfg, path, merged)


def _refuse_container_swap(cfg: dict, path: str, value, source: str) -> None:
    """A RULE LIST may only ever be merged AS A LIST. _merge_list already refuses
    the mismatch one way round ("base config value at this path is not a list");
    this is the missing mirror, and it was a hole, not a tidiness point.

    `{"command_rules": {"tool_discipline": {"zz": 1}}}` in a user overlay put a
    DICT where the base holds a rule list, so _merge_walk recursed into it as a
    dict and _set_dotted -- finding a list where it wanted to descend -- replaced
    the whole list with `{}`. Every tool_discipline rule vanished, including
    `authority-file-in-shell`, which grants.never_exempt says nothing may switch
    off: measured rc=0 on a shell `touch` of a grant path that is rc=2 with no
    overlay. A scalar (`"tool_discipline": 0`) did the same. Neither spelling
    ever reached _merge_list, so neither hit the check that exists for it.

    Refused rather than coerced: an overlay that meant to append wrote a list,
    and an overlay that wrote something else has a bug the author must see."""
    base_here = _get_dotted(cfg, path)
    if base_here is _MISSING:
        return
    if isinstance(base_here, list) and not isinstance(value, list):
        raise ConfigError(
            f"overlay {source} puts a {type(value).__name__} at {path!r}, where "
            "config.json holds a LIST of rules -- a rule list can only be merged as a "
            "list (see overlays.list_merge). Replacing it with anything else removes "
            'every rule in it. To switch ONE rule off, name it: "disable": '
            f'["{path}/<rule id>"].'
        )
    if isinstance(base_here, dict) and not isinstance(value, dict):
        # THE HALF THAT WAS STILL MISSING (round 3). The list checks above and a
        # dict->list check were in place; dict -> SCALAR was not, and it is the
        # cheapest erasure of all: `{"command_rules": 0}` -- also `null`, `""`,
        # `false` -- put a scalar where a whole section lives, the walk sent it
        # to the leaf branch, and _set_dotted replaced the section. Every
        # tool_discipline rule went, both grants.never_exempt rules with them,
        # and the consuming guard's KeyError became ALLOW through the
        # dispatcher's fail-open. Measured rc=0 for a shell write to a grant
        # path and for a delete outside the disposable allowlist.
        raise ConfigError(
            f"overlay {source} puts a {type(value).__name__} at {path!r}, where config.json "
            "holds a section (an object) -- replacing it would remove every key in that "
            "section, and a guard reading one of them would crash into the dispatcher's "
            "fail-open rather than block. Merge a section with a section."
        )
    if isinstance(base_here, (str, int, float, bool)) and isinstance(value, (dict, list)):
        # THE THIRD SIDE OF THE MIRROR, and the one that is not a fail-open but a
        # CRASH OUT of the dispatcher: `{"skill_anchor": {"envelope_key": {"x": 1}}}`
        # put a container where a scalar lives, and dispatch.py's _notice_stdout
        # -- which runs OUTSIDE the guard try/except, so the round-4 block cannot
        # catch it -- raised on it. rc=1 with a traceback, and worse, the
        # once-per-session marker for the project overlay's disable notice had
        # already been consumed, so the operator lost the notice as well.
        raise ConfigError(
            f"overlay {source} puts a {type(value).__name__} at {path!r}, where config.json "
            f"holds a single {type(base_here).__name__} value -- a consumer reads it as one "
            "value and a container there is a crash, not a setting. Merge a scalar with a "
            "scalar."
        )


def _merge_walk(cfg: dict, node: dict, list_merge: dict, prefix: str, restrict_to, source: str,
                taint: set | None = None) -> None:
    """Recursively merges one overlay document into `cfg`, in place. `restrict_to`
    is None for the USER layer (any path may be touched; a list path with no
    declared `list_merge` strategy is replaced wholesale, everything else is
    overridden directly) or a set of dotted RULE paths for the PROJECT layer
    (any path -- list, dict, or scalar leaf -- outside this set is a ConfigError).
    That set is not a security boundary any more (a project overlay may switch
    rules off, see _collect_disables); it is the line between the rule lists a
    project states policy about and ULTHAR's own wiring -- paths.python_exe,
    dispatch.events, logging.*, overlays.* -- which a cloned repo repointing
    would not be policy at all. Keys starting with '_' are notes/comments and
    are never merged; the top-level `disable` key is handled by _collect_disables
    and never reaches this walk."""
    for key, value in node.items():
        if key.startswith("_"):
            continue
        path = f"{prefix}.{key}" if prefix else key
        _refuse_container_swap(cfg, path, value, source)
        if isinstance(value, list):
            if restrict_to is not None and path not in restrict_to:
                raise ConfigError(
                    f"overlay {source} touches {path!r}, which is not in "
                    "overlays.project_allowed_paths -- a project overlay states policy about "
                    "the rule lists named there, not about ULTHAR's own wiring"
                )
            strategy, shape = _strategy_and_shape(list_merge.get(path))
            if strategy not in ("append", "append_by_id"):
                if restrict_to is not None:
                    raise ConfigError(
                        f"overlay {source} touches {path!r}, which has no 'append'/'append_by_id' "
                        "strategy declared in overlays.list_merge -- a project overlay may only "
                        "touch a path declared that way"
                    )
                _check_replacement_conforms(cfg, path, value, source)
                _set_dotted(cfg, path, value)  # user layer, undeclared list path: wholesale replace
                continue
            _merge_list(cfg, path, value, strategy, source, taint, shape)
        elif isinstance(value, dict):
            # `and value` was here, which sent an EMPTY dict to the leaf branch
            # below -- so `{"command_rules": {}}` replaced the whole section with
            # {} instead of expressing nothing. An empty object IS nothing:
            # recursing over no keys is the honest reading and changes no value.
            _merge_walk(cfg, value, list_merge, path, restrict_to, source, taint)
        else:
            if restrict_to is not None:
                raise ConfigError(
                    f"overlay {source} touches {path!r}, which is not in "
                    "overlays.project_allowed_paths -- a project overlay may add to and disable "
                    "entries in the rule lists named there; it may not set arbitrary scalars. "
                    'To switch a rule off, use the explicit form: "disable": '
                    '["<config path>/<rule id>"]'
                )
            _set_dotted(cfg, path, value)  # user layer: direct override


def _merge_layer(result: dict, path: Path, list_merge: dict, restrict_to, layer: str,
                 taint: set | None = None) -> list[tuple[str, str]]:
    """One overlay layer, with the guarantee that NO exception can leave this
    function without naming the overlay file it came from. A ConfigError raised
    below already carries `source` (the path) in its message; anything else --
    any exception type at all, present or future -- is wrapped so the dispatcher's
    "delete the overlay file named in the detail line above" recovery text is
    never a pointer to nothing (the case that turned a malformed overlay into a
    total lockout with no way to tell which of the two files to remove).

    Returns this layer's PENDING disables (see _collect_disables); the caller
    applies them once every layer has merged."""
    try:
        doc = _read_overlay_json(path)
        body = {k: v for k, v in doc.items() if k != _DISABLE_KEY}
        _merge_walk(result, body, list_merge, "", restrict_to, str(path), taint)
        return _collect_disables(doc, restrict_to, str(path))
    except ConfigError:
        raise
    except Exception as exc:  # noqa: BLE001 -- the point is that no type escapes unlabelled
        raise ConfigError(f"{layer} overlay {path}: {type(exc).__name__}: {exc}") from exc


def merge_overlays(cfg: dict, ctx: "Ctx", layers: tuple[str, ...] = ("user", "project")) -> dict:
    """Merges the user- and project-level overlay files declared in cfg['overlays']
    into `cfg` and returns the RESULT (never mutates the cached base `cfg` in
    place -- see load_config's module-level cache). Absent overlays are the common
    case and stay cheap: with `overlays.enabled` false, or with neither file
    present, this returns `cfg` unchanged after at most two os.stat()-class calls
    -- no json.load(), no copy, no behaviour change (the config-generalisation round).

    Layer model (owner decision, 2026-09-06): PROJECT CONFIGURATION TAKES
    PRECEDENCE OVER PERSONAL CONFIGURATION, as it does for .editorconfig, linter
    configs and CI settings. A project overlay may ADD rules at the rule paths in
    overlays.project_allowed_paths and may DISABLE named ones there
    (_collect_disables / _apply_disables), even where that weakens a guard: that
    is the repo's policy and not this tool's business. What it may not do is
    weaken anything ACCIDENTALLY or INVISIBLY -- hence merge-time shape
    validation below (a malformed entry must not reach a guard, crash it, and let
    the dispatcher's fail-open remove everything that guard enforced), a loud
    failure for a `disable` that matches no rule, and the operator-facing notice
    dispatch.py raises from ctx.project_overlay_disabled. What it also may not do
    is repoint ULTHAR's own wiring (paths.python_exe, dispatch.events, logging.*,
    overlays.*): that is not policy, and it is what project_allowed_paths still
    draws the line around. The user layer (overlays.user_path, under the user's
    own home) has no path restriction at all -- see _merge_walk -- and gets the
    same `disable` operation.

    Any violation raises ConfigError, which propagates to dispatch.py's config-load
    try/except exactly like a broken config.json -- BLOCK (exit 2), logged to
    logging.config_error_log -- never a silent per-guard fail-open, because an
    overlay that silently degraded to 'ignored' on error is the hollowing-out this
    mechanism exists to prevent.

    Explicit disabling is safe; disabling by accident is not. An appended entry
    that makes a guard RAISE reaches dispatch.py's fail-open and becomes ALLOW --
    which removes EVERY rule that guard enforced, silently, rather than the one
    the author meant. Closing it takes TWO steps, and the split matters because
    four review rounds each found one more shape the first step did not know
    about. STEP ONE, here, at merge time: refuse any entry whose SHAPE the
    consuming guard could not read -- declared lists via _merge_list ->
    _object_schema / _check_scalar_conforms, sections via
    _refuse_container_swap, and (round 4) an UNDECLARED list replaced wholesale
    by the user layer via _check_replacement_conforms, which was the hole that
    made a guard raise in its own setup before it reached a single rule. This
    step is precise and early but it can only refuse what it can derive a shape
    for. STEP TWO, in dispatch.py: while ANY overlay is merged -- which
    `ctx.overlay_sources`, set below, is what records -- a guard that raises
    BLOCKS instead of failing open. So what step one misses cannot end as a
    silent ALLOW; it ends as a refusal naming the overlay file. Shape means
    structure only, so a shape-valid entry with a semantically wrong value still
    reaches its guard, and step two is what catches that too. This function also
    records WHICH OBJECTS the project layer contributed (`project_overlay_ids`),
    which no longer decides anything -- it only lets the block message say the
    guard died holding overlay content when it did. `layers` is kept because a caller may legitimately want a single
    layer; nothing in the dispatcher re-merges."""
    ov = cfg.get("overlays", {})
    if not ov.get("enabled"):
        return cfg

    user_path = ctx.expand(ov["user_path"]) if ov.get("user_path") else None
    project_path = ctx.expand(ov["project_path"]) if ov.get("project_path") else None
    user_exists = bool(user_path and user_path.exists()) and "user" in layers
    project_exists = bool(project_path and project_path.exists()) and "project" in layers
    if "project" in layers:
        # Recorded so dispatch.py can tell, on a guard exception, whether any
        # PROJECT-SUPPLIED content was in the cfg that guard just ran against (see
        # dispatch._attribute_to_project_overlay). Set here rather than re-stat'ed
        # there: this is the one place that already knows.
        ctx.project_overlay_path = project_path if project_exists else None
        ctx.project_overlay_ids = frozenset()
        ctx.project_overlay_disabled = ()
    if not user_exists and not project_exists:
        return cfg

    list_merge = ov.get("list_merge", {})
    result = copy.deepcopy(cfg)
    pending: list[tuple[str, str]] = []
    if user_exists:
        pending += _merge_layer(result, user_path, list_merge, None, "user")
    project_pending: list[tuple[str, str]] = []
    if project_exists:
        allowed = set(ov.get("project_allowed_paths", []))
        taint: set[int] = set()
        project_pending = _merge_layer(result, project_path, list_merge, allowed, "project", taint)
        pending += project_pending
        # Valid for the life of this dispatch: `result` becomes ctx.cfg and holds
        # every one of these objects alive, so no id can be recycled underneath us.
        ctx.project_overlay_ids = frozenset(taint)
    # AFTER both walks, so an identifier is matched against the rule set that will
    # actually run -- a rule one layer appends is disableable by the other, and a
    # list left empty by a disable is never mistaken for one that ships empty.
    merged_from = tuple(str(p) for p, present in
                        ((user_path, user_exists), (project_path, project_exists)) if present)
    ctx.overlay_sources = merged_from
    _refuse_pinned_paths(cfg, result, pending, merged_from)  # raises before any removal
    _refuse_never_exempt(cfg, result, pending)  # raises before anything is removed
    _apply_disables(result, pending)  # raises unless every identifier matched
    # LAST, and AFTER the disables, because it judges the RESULT: "the authority
    # rules survived the merge" has to mean the cfg the guards will actually run
    # against, not an intermediate one. `_refuse_never_exempt` above has already
    # refused a disable naming one of them, so this cannot be the thing that
    # reports such a disable -- it is the backstop for every other way a rule
    # could go missing, whichever step removed it.
    _refuse_authority_erasure(cfg, result, merged_from)
    if project_pending and "project" in layers:
        # Read by dispatch.py, which is the only place that can put it in front of
        # the operator (see _overlay_disable_notice). Project layer only: the user
        # layer is the operator's own file, and announcing their own choice back to
        # them every session is noise.
        ctx.project_overlay_disabled = tuple(ident for ident, _ in project_pending)
    return result


class ConfigError(Exception):
    """Raised on a structural config defect (dangling *_named, invalid JSON, etc.).
    dispatch.py catches this at config-load time; install.py treats it as a reason
    to refuse to deploy (logging.refuse_deploy_on_invalid_config)."""


class PatternCompileError(Exception):
    """Raised by compile_pattern() when a config-declared regex fails to compile.
    dispatch.py distinguishes this from a generic guard exception so the failure
    routes to logging.config_error_log (a deployment defect) rather than
    paths.error_log / dispatch-errors.jsonl (a transient operational event) --
    CONTRACT.md 'Regex dialect' / 'compile_scope: per_guard'.

    This module is the sole owner of re.compile() for guard-declared patterns
    (guards are AST-forbidden from calling re.compile directly); guard modules
    call compile_pattern()/compile_patterns() instead."""

    def __init__(self, json_path: str, message: str):
        super().__init__(message)
        self.json_path = json_path


_SENTINEL = "*"


def _flags_for(kind: str, is_windows: bool) -> int:
    if kind == "text":
        return re.IGNORECASE
    if kind == "path":
        return re.IGNORECASE if is_windows else 0
    raise PatternCompileError("regex.kinds", f"unknown pattern kind {kind!r}")


def compile_pattern(pattern: str, kind: str, ctx: "Ctx", json_path: str = "") -> re.Pattern | None:
    """Compiles one pattern under the flags for its declared `kind` (regex.kinds),
    on the CURRENT platform only. Returns None for the `sentinel` value '*' (R-b) --
    callers must check identity against the sentinel BEFORE calling this, but this
    is a defensive second line since re.compile('*') raises."""
    if pattern == _SENTINEL:
        return None
    try:
        return re.compile(pattern, _flags_for(kind, ctx.is_windows))
    except re.error as exc:
        raise PatternCompileError(json_path or pattern, str(exc)) from exc


def compile_patterns(patterns: list[str], kind: str, ctx: "Ctx", json_path: str = "") -> list[re.Pattern]:
    out = []
    for p in patterns:
        compiled = compile_pattern(p, kind, ctx, json_path)
        if compiled is not None:
            out.append(compiled)
    return out


def compile_text_pattern(pattern: str, json_path: str = "") -> re.Pattern | None:
    """`kind: "text"` is ALWAYS IGNORECASE on every platform (regex.kinds.text) and
    needs no `ctx` -- unlike kind: "path", is_windows never enters the decision.
    lib/gitcmd.py's public functions take (cmd, cfg) / (cfg, suffix) with no `ctx`
    parameter, so this is the one authority for what 'text kind' means without
    widening that signature. Relying on a pattern's own inline (?i) instead is
    forbidden by N-1: several config strings carry one today, but that is not
    something this function may depend on."""
    if pattern == _SENTINEL:
        return None
    try:
        return re.compile(pattern, _flags_for("text", is_windows=True))  # is_windows irrelevant for kind:text
    except re.error as exc:
        raise PatternCompileError(json_path or pattern, str(exc)) from exc


# ---------------------------------------------------------------- Ctx


class Ctx:
    """Per-dispatch context. A plain class with the former dataclass's fields, order
    and defaults (v2: dataclasses pulls in inspect, ~20 ms of every hook start-up).

    `project_overlay_path` / `project_overlay_ids`: set by merge_overlays, read only
    on the guard-exception path (attribution to the project overlay).
    `overlay_sources`: EVERY overlay file merged into this dispatch's cfg, in layer
    order -- with any overlay present a guard crash may not fail open.
    `project_overlay_disabled`: rule ids the PROJECT overlay switched off."""

    def __init__(self, home: Path, state_dir: Path, deploy_dir: Path, skills_root: Path,
                 logs_dir: Path, temp_dir: Path, cwd: Path, is_windows: bool, event: str,
                 cfg: dict, project_overlay_path: Path | None = None,
                 project_overlay_ids: frozenset | None = None,
                 overlay_sources: tuple[str, ...] = (),
                 project_overlay_disabled: tuple | None = None,
                 _now_fn: Callable[[], datetime] = datetime.now,
                 _run_cmd_fn: Callable | None = None,
                 _list_processes_fn: Callable | None = None) -> None:
        self.home = home
        self.state_dir = state_dir
        self.deploy_dir = deploy_dir
        self.skills_root = skills_root
        self.logs_dir = logs_dir
        self.temp_dir = temp_dir
        self.cwd = cwd
        self.is_windows = is_windows
        self.event = event
        self.cfg = cfg
        self.project_overlay_path = project_overlay_path
        self.project_overlay_ids = frozenset() if project_overlay_ids is None else project_overlay_ids
        self.overlay_sources = overlay_sources
        self.project_overlay_disabled = (
            tuple() if project_overlay_disabled is None else project_overlay_disabled)
        self._now_fn = _now_fn
        self._run_cmd_fn = _run_cmd_fn
        self._list_processes_fn = _list_processes_fn

    def __repr__(self) -> str:
        return (f"Ctx(home={self.home!r}, state_dir={self.state_dir!r}, "
                f"deploy_dir={self.deploy_dir!r}, skills_root={self.skills_root!r}, "
                f"logs_dir={self.logs_dir!r}, temp_dir={self.temp_dir!r}, cwd={self.cwd!r}, "
                f"is_windows={self.is_windows!r}, event={self.event!r})")

    def now(self) -> datetime:
        return self._now_fn()

    def run_cmd(
        self,
        argv: list[str],
        kind: Literal["local", "remote"],
        cwd: Path | None = None,
    ) -> CmdResult:
        """`kind` selects the timeout budget and is REQUIRED (S-5): "local" ->
        thresholds.subprocess_timeout_local_sec (5), "remote" ->
        subprocess_timeout_remote_sec (20). There is no caller-supplied timeout and
        no default kind -- a guard must state which it is.

        `argv[0]` is resolved through `shutil.which()` first: subprocess.run hands
        a bare command straight to CreateProcess on Windows, which does not do the
        PATHEXT search a shell would -- so a `gh`/`git` that resolves to a `.cmd`/
        `.bat` installer would otherwise raise FileNotFoundError even though it
        runs fine from PowerShell. shutil.which honours PATHEXT and falls back to
        the unresolved name if nothing is found, so a genuinely missing executable
        still fails the same way as before."""
        if self._run_cmd_fn is not None:
            return self._run_cmd_fn(argv, kind, cwd)
        if kind == "local":
            timeout = self.cfg["thresholds"]["subprocess_timeout_local_sec"]
        elif kind == "remote":
            timeout = self.cfg["thresholds"]["subprocess_timeout_remote_sec"]
        else:
            raise ValueError(f"run_cmd: kind must be 'local' or 'remote', got {kind!r}")
        resolved_argv = list(argv)
        if resolved_argv:
            resolved = shutil.which(resolved_argv[0])
            if resolved:
                resolved_argv[0] = resolved
        try:
            # Popen directly (not subprocess.run(timeout=...)): on timeout, a
            # `.cmd`/`.bat` or shell-wrapped child can itself have spawned a
            # further child that survives Popen.kill()/taskkill of the immediate
            # process, holding the inherited stdout/stderr pipes open and
            # blocking the post-kill drain read far past `timeout` -- PE-7 exists
            # to close exactly this unbounded-hang risk. On POSIX,
            # start_new_session=True makes this child the leader of its own
            # process group so os.killpg can signal the whole tree in one call
            # (plain Popen.kill() only reaches the immediate child); Windows uses
            # `taskkill /F /T` in _kill_process_tree.
            popen_kwargs = {}
            if not sys.platform.startswith("win"):
                popen_kwargs["start_new_session"] = True
            proc = subprocess.Popen(
                resolved_argv,
                cwd=str(cwd) if cwd else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding="utf-8",
                errors="replace",
                **popen_kwargs,
            )
            try:
                out, err = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                self._kill_process_tree(proc)
                try:
                    proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    # Tree-kill didn't free the pipes within 5s -- log it, but
                    # still fail open (I-3): the TimeoutExpired handler below
                    # returns CmdResult(..., timed_out=True) regardless.
                    self.log_error(
                        "run_cmd",
                        TimeoutError(f"process tree kill did not free stdout/stderr "
                                     f"within 5s (kind={kind}): {argv!r}"),
                        [],
                    )
                except Exception:
                    pass
                raise
            return CmdResult(proc.returncode, out or "", err or "", False)
        except subprocess.TimeoutExpired:
            # Logged here, not by the caller: guards other than
            # tool_discipline/commander/artifact_hygiene must not write
            # filesystem side effects (§1), so run_cmd (which already owns
            # log_error/log_config_error I/O) is the one place a timeout can be
            # observed and logged, keeping every caller a pure function of
            # (payload, cfg, ctx).
            self.log_error(
                "run_cmd",
                TimeoutError(f"subprocess timed out (kind={kind}, timeout={timeout}s): {argv!r}"),
                [],
            )
            return CmdResult(-1, "", "", True)
        except OSError as exc:
            self.log_error("run_cmd", exc, [])
            return CmdResult(-1, "", str(exc), False)

    @staticmethod
    def _kill_process_tree(proc: "subprocess.Popen") -> None:
        """Kills `proc` AND its descendants -- Popen.kill() alone only signals the
        immediate process, leaving a `.cmd`/`.bat` (Windows) or shell (POSIX)
        wrapper's own children to survive and hold the stdout/stderr pipes open
        (see run_cmd). Windows: `taskkill /F /T /PID <pid>` (stock utility, no
        psutil, per I-13). POSIX: run_cmd starts the child with
        start_new_session=True so it leads its own process group, and
        `os.killpg` on that group id is the equivalent of `taskkill /T`.

        No psutil import here either way -- a grace-terminate/kill fallback
        belongs to lib/procs.py (Stop-only); this PreToolUse-reachable path goes
        straight to SIGKILL/TerminateProcess, since a deliberately-hung external
        command gets no grace period."""
        if sys.platform.startswith("win"):
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                    timeout=5,
                )
            except Exception:
                proc.kill()
        else:
            try:
                pgid = os.getpgid(proc.pid)
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                proc.kill()

    def list_processes(self) -> list[ProcInfo]:
        """Lazily imports lib.procs (and therefore psutil) -- see module docstring.
        Only reachable from dev_process_hygiene (Stop) and stop_dev_orphans (CLI)."""
        if self._list_processes_fn is not None:
            return self._list_processes_fn()
        from . import procs as procs_mod  # lazy: keep psutil off the PreToolUse path

        return procs_mod.list_processes(self, self.cfg)

    def log_error(self, guard: str, exc: BaseException, payload_keys: list[str]) -> None:
        """Appends one line to paths.error_log. The payload itself is NEVER logged --
        only its top-level key names (I-3). Wrapped so a logging failure never raises
        past the caller."""
        import traceback

        entry = {
            "ts": self.now().isoformat(),
            "event": self.event,
            "guard": guard,
            "error": type(exc).__name__,
            "message": str(exc),
            "traceback": "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            ),
            "payload_keys": payload_keys,
        }
        try:
            # Must go through paths.expand(), not Path(cfg[...]) directly -- a
            # literal '~' won't resolve otherwise, and this log silently lands
            # under the process cwd instead of the real log file. Every config
            # PATH value read anywhere in lib/ must go through expand().
            io_mod.append_jsonl(
                paths_mod.expand(self.cfg["paths"]["error_log"], self), entry,
                max_bytes=self.cfg.get("logging", {}).get("max_error_log_bytes"),
            )
        except Exception:
            pass

    def log_config_error(self, phase: str, json_path: str, error: str, guard_scope: str = "") -> None:
        """Appends one line to logging.config_error_log -- a DISTINCT stream from
        paths.error_log (a config defect is a deployment defect, not a transient
        runtime event)."""
        entry = {
            "ts": self.now().isoformat(),
            "phase": phase,
            "json_path": json_path,
            "guard_scope": guard_scope,
            "error": error,
        }
        try:
            io_mod.append_jsonl(
                paths_mod.expand(self.cfg["logging"]["config_error_log"], self), entry,
                max_bytes=self.cfg.get("logging", {}).get("max_error_log_bytes"),
            )
        except Exception:
            pass

    def expand(self, value: str) -> Path:
        return paths_mod.expand(value, self)


def build_ctx(payload: dict, cfg: dict, event: str, deploy_dir: Path) -> Ctx:
    """Constructs the Ctx used for one dispatch. A failure HERE is a runtime-load
    (infrastructure) failure, not a per-guard one: dispatch.py wraps this call
    and, on exception, writes nothing to stdout, best-effort logs to
    config_error_log, and BLOCKS (exit 2) with an actionable message via
    `dispatch.py`'s `_infra_block_message` rather than failing open."""
    resolved_home = payload.get("_home_override")  # test seam only; never set by real payloads
    home = Path(resolved_home) if resolved_home else Path.home()
    is_windows = os.name == "nt" or sys.platform.startswith("win")

    partial_ctx_for_expand = _PartialCtxForExpand(
        home=home,
        deploy_dir=deploy_dir,
        cwd=Path(payload.get("cwd") or os.getcwd()),
        temp_dir=Path(os.environ.get("TEMP") or os.environ.get("TMPDIR") or "/tmp"),
        skills_root=home / ".claude" / "skills",
    )

    def _p(key: str, fallback: Path) -> Path:
        raw = cfg.get("paths", {}).get(key)
        if not raw:
            return fallback
        return paths_mod.expand(raw, partial_ctx_for_expand)

    state_dir = _p("state_dir", home / ".claude" / "state")
    skills_root = _p("skills_root", home / ".claude" / "skills")
    logs_dir = _p("logs_dir", deploy_dir / "logs")

    return Ctx(
        home=home,
        state_dir=state_dir,
        deploy_dir=deploy_dir,
        skills_root=skills_root,
        logs_dir=logs_dir,
        temp_dir=partial_ctx_for_expand.temp_dir,
        cwd=partial_ctx_for_expand.cwd,
        is_windows=is_windows,
        event=event,
        cfg=cfg,
    )


class _PartialCtxForExpand:
    """A minimal stand-in exposing only what paths.expand() reads (home, cwd,
    temp_dir, deploy_dir, skills_root), used while build_ctx is still resolving the
    real Ctx's own path fields from config (which themselves need expand())."""

    def __init__(self, home: Path, deploy_dir: Path, cwd: Path, temp_dir: Path,
                 skills_root: Path) -> None:
        self.home = home
        self.deploy_dir = deploy_dir
        self.cwd = cwd
        self.temp_dir = temp_dir
        self.skills_root = skills_root
