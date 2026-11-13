"""Goal contracts (GOAL-01..05, R1-c AC-2/AC-3), CLI only: the only module that loads
YAML (`yaml.safe_load`, imported when a contract is read -- never on the hook path).

A contract is a YAML mapping (design §6.1, R1-c AC-2):
    intent:      {raw: <owner's words>, interpretation: <one of the alternatives>,
                  alternatives: [<text> | {id: <id>, text: <text>}, ...]}
    non_goals:   [<text>, ...]
    scope_globs: [<repo-relative glob>, ...]
    acs:         - {id, statement, kind: quant|qual, verify_cmd, threshold: {lo, hi,
                    lo_incl, hi_incl} | null, evidence_kind, rubric: {levels, good, bad}
                    (qual), sources: [...], artifacts: [<repo path>, ...] (optional)}
    budget:      {loops: <positive integer> | null}
    packs:       [<pack name>, ...]                 (optional; R1-d SKL-02)
    write_cmds:  {<name>: <command>, ...}           (optional; R1-d AC-7, `ulthar run`)
Structure errors (a missing element, a wrong type, YAML that does not parse) refuse the
submission; content rules are the lint's (AC-3):
    LINT-A  no vague word (U-05, case-insensitive) in a statement, threshold, rubric or
            intent.interpretation
    LINT-B  a quantitative AC's threshold has lo and/or hi, each with its *_incl flag
    LINT-C  every AC has a verify_cmd whose first word is an executable on PATH or a file
            inside the repository (dry check: nothing is run)
    LINT-D  every AC has at least one source
    LINT-E  at least one non-goal
    LINT-F  qualitative ACs are at most 30 % (30 % passes) and each has a rubric with
            levels, good examples and bad examples
    LINT-G  at least 2 intent alternatives, and the interpretation is one of them
    LINT-H  (R1-d, SKL-02) the declared packs are exactly the packs whose path_globs meet
            a scope glob (a pack definition that cannot be read is a violation too)
"""
from __future__ import annotations

import shutil
from pathlib import Path

from . import shellwords
from .acshape import check_ac, is_str_list as _is_str_list

VAGUE_WORDS = ("適切に", "いい感じ", "なるべく", "十分", "必要に応じて", "適宜", "可能な限り",
               "など", "等", "appropriate", "as needed", "reasonable", "properly")  # U-05
QUAL_RATIO_MAX = 0.30
RUBRIC_KEYS = ("levels", "good", "bad")


class ContractError(Exception):
    """A contract that cannot be submitted or read; the message lists the reasons."""


def load_yaml():
    try:
        import yaml  # noqa: PLC0415 -- CLI only; the hook path never reaches this module
    except ImportError as exc:
        raise ContractError(f"PyYAML is not importable ({exc}); contracts are YAML: install "
                            "PyYAML (`pip install PyYAML`) to use the contract commands. "
                            "Nothing was written.") from exc
    return yaml


def parse(data: bytes) -> dict:
    """The contract in `data`, structurally validated; ContractError with every reason."""
    yaml = load_yaml()
    try:
        doc = yaml.safe_load(data.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ContractError(f"the contract is not UTF-8: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ContractError(f"YAML syntax error: {exc}") from exc
    problems = validate(doc)
    if problems:
        raise ContractError("the contract is missing or mistypes required elements:\n  "
                            + "\n  ".join(problems))
    return doc


def validate(doc) -> list[str]:
    if not isinstance(doc, dict):
        return ["the contract must be a YAML mapping"]
    out = [f"{k} is missing" for k in ("intent", "non_goals", "scope_globs", "acs", "budget")
           if k not in doc]
    intent = doc.get("intent")
    if "intent" in doc:
        if not isinstance(intent, dict):
            out.append("intent must be a mapping of raw, interpretation, alternatives")
        else:
            out += [f"intent.{k} is missing" for k in ("raw", "interpretation", "alternatives")
                    if k not in intent]
            out += [f"intent.{k} must be a string" for k in ("raw", "interpretation")
                    if k in intent and not isinstance(intent[k], str)]
            alts = intent.get("alternatives")
            if "alternatives" in intent and not (isinstance(alts, list) and all(
                    isinstance(a, str) or (isinstance(a, dict) and isinstance(a.get("text"), str))
                    for a in alts)):
                out.append("intent.alternatives must be a list of texts or {id, text} mappings")
    for k in ("non_goals", "scope_globs"):
        if k in doc and not _is_str_list(doc[k]):
            out.append(f"{k} must be a list of strings")
    acs = doc.get("acs")
    if "acs" in doc:
        if not isinstance(acs, list) or not acs:
            out.append("acs must be a non-empty list")
        else:
            for i, ac in enumerate(acs):
                out += check_ac(f"acs[{i}]", ac)
            ids = [ac.get("id") for ac in acs if isinstance(ac, dict)]
            if len(ids) != len(set(ids)):
                out.append("acs ids must be unique")
    if "packs" in doc and not _is_str_list(doc["packs"]):
        out.append("packs must be a list of pack names")
    cmds = doc.get("write_cmds")
    if "write_cmds" in doc and not (isinstance(cmds, dict) and all(
            isinstance(k, str) and k and isinstance(v, str) and v.strip() for k, v in cmds.items())):
        out.append("write_cmds must be a mapping of name -> command")
    budget = doc.get("budget")
    if "budget" in doc:
        if not isinstance(budget, dict) or "loops" not in budget:
            out.append("budget must be a mapping with loops (a positive integer, or null = unset)")
        elif budget["loops"] is not None and not (isinstance(budget["loops"], int)
                                                  and not isinstance(budget["loops"], bool)
                                                  and budget["loops"] > 0):
            out.append("budget.loops must be a positive integer or null")
    return out


# ---------------------------------------------------------------------------- lint

def _texts(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [t for v in value.values() for t in _texts(v)]
    if isinstance(value, list):
        return [t for v in value for t in _texts(v)]
    return [] if value is None else [str(value)]


def _vague(text: str) -> list[str]:
    folded = text.casefold()
    return [w for w in VAGUE_WORDS if w.casefold() in folded]


def first_word(cmd: str) -> str:
    toks = shellwords.tokenize(cmd, None)
    return toks[0] if toks else ""


def resolve_command(word: str, repo: Path) -> str | None:
    """The executable a verify_cmd's first word names: on PATH (a bare name), or a file
    inside the repository (a path). None when neither."""
    if not word:
        return None
    if "/" not in word and "\\" not in word:
        return shutil.which(word)
    path = Path(word) if Path(word).is_absolute() else repo / word
    try:
        path = path.resolve()
        path.relative_to(repo.resolve())
    except (OSError, ValueError):
        return None
    return str(path) if path.is_file() else None


def _alt_names(alts: list) -> set[str]:
    out = set()
    for a in alts:
        if isinstance(a, str):
            out.add(a.strip())
        else:
            out |= {str(a.get("text") or "").strip(), str(a.get("id") or "").strip()} - {""}
    return out


def _lint_packs(doc: dict, packs) -> list[tuple[str, str]]:
    """LINT-H: `packs` is the project's pack list, or the PackError text."""
    if isinstance(packs, str):
        return [("LINT-H", f"the pack definitions cannot be read: {packs}")]
    from .packs import inferred  # noqa: PLC0415 -- CLI only; packs is hook-safe, nothing heavy

    declared = sorted(set(doc.get("packs") or []))
    unknown = sorted(set(declared) - {p["name"] for p in packs})
    want = inferred(doc["scope_globs"], packs)
    out = [("LINT-H", f"packs {unknown} are not defined in .claude/ulthar.json")] if unknown else []
    if declared != want:
        out.append(("LINT-H", f"packs declares {declared}, but scope_globs meet the path_globs of "
                              f"{want}: declare exactly the packs the scope touches"))
    return out


def lint(doc: dict, repo: Path, packs=()) -> list[tuple[str, str]]:
    """[(rule id, message)] for a validated contract; [] = it passes. `packs`: the
    project's skill packs (ulthar.packs.load), or the text of the error reading them."""
    out: list[tuple[str, str]] = _lint_packs(doc, packs)
    intent = doc["intent"]
    for word in _vague(intent["interpretation"]):
        out.append(("LINT-A", f"intent.interpretation contains the vague word 「{word}」"))
    acs = doc["acs"]
    for ac in acs:
        name = ac["id"]
        for field in ("statement", "threshold", "rubric"):
            for word in sorted({w for t in _texts(ac.get(field)) for w in _vague(t)}):
                out.append(("LINT-A", f"{name}.{field} contains the vague word 「{word}」"))
        th = ac.get("threshold")
        if ac["kind"] == "quant":
            ends = [k for k in ("lo", "hi") if isinstance(th, dict) and th.get(k) is not None]
            if not ends:
                out.append(("LINT-B", f"{name}: a quantitative AC needs a threshold with lo "
                                      "and/or hi"))
            for k in ends:
                if not isinstance(th.get(f"{k}_incl"), bool):
                    out.append(("LINT-B", f"{name}: threshold.{k} has no explicit {k}_incl "
                                          "(true = inclusive)"))
        cmd = ac["verify_cmd"].strip()
        if not cmd:
            out.append(("LINT-C", f"{name}: verify_cmd is empty"))
        elif resolve_command(first_word(cmd), repo) is None:
            out.append(("LINT-C", f"{name}: verify_cmd's first word `{first_word(cmd)}` is "
                                  "neither an executable on PATH nor a file in the repository"))
        if not [s for s in ac["sources"] if s.strip()]:
            out.append(("LINT-D", f"{name}: no source"))
    if not [g for g in doc["non_goals"] if g.strip()]:
        out.append(("LINT-E", "non_goals is empty (at least one is required)"))
    qual = [ac for ac in acs if ac["kind"] == "qual"]
    if len(qual) > QUAL_RATIO_MAX * len(acs):
        out.append(("LINT-F", f"qualitative ACs are {len(qual)}/{len(acs)} "
                              f"({100 * len(qual) / len(acs):.0f} %), over 30 %"))
    for ac in qual:
        rubric = ac.get("rubric")
        missing = [k for k in RUBRIC_KEYS
                   if not isinstance(rubric, dict) or not _texts(rubric.get(k))]
        if missing:
            out.append(("LINT-F", f"{ac['id']}: a qualitative AC needs a rubric with "
                                  f"{', '.join(missing)}"))
    alts = intent["alternatives"]
    if len(alts) < 2:
        out.append(("LINT-G", f"intent.alternatives has {len(alts)} (at least 2 are required)"))
    if intent["interpretation"].strip() not in _alt_names(alts):
        out.append(("LINT-G", "intent.interpretation is not one of intent.alternatives "
                              "(give its id or its text)"))
    return out


def summary(doc: dict) -> dict:
    """What the unit's record keeps of a confirmed contract (hooks read it from index.db)."""
    return {"scope_globs": list(doc["scope_globs"]), "packs": list(doc.get("packs") or []),
            "acs": [{"id": ac["id"], "kind": ac["kind"]} for ac in doc["acs"]],
            "loops": doc["budget"]["loops"]}
