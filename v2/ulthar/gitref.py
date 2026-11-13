"""Git ref updates of the integration and protected branches are the CLI's (TRUST-05,
R1-d AC-11) -- hook side, after the ported v1 git_discipline (which stays as it is).

In a project with `.claude/ulthar.json`, a Bash / PowerShell command (also inside
`bash -c` / `powershell -Command`, and a `git` word anywhere in a segment, e.g. after
xargs or `$(`) is blocked when it would move a guarded branch -- the config's
`integration_branch` (default develop) and `protected_branches` (default main, master):
  * merge / pull / commit / rebase / cherry-pick / revert / am / reset while that branch
    is checked out (the HEAD of the repository the command runs in, read from .git);
  * update-ref naming it (or reading its updates from --stdin), the usual tail of a
    commit-tree advance; a commit-tree in a command that names the branch;
  * push of it: a refspec whose destination is it, --all / --mirror, or a push without a
    refspec (or of HEAD) while it is checked out;
  * fetch into it (`<src>:<branch>`), branch -f / -m / -M / -c / -C / -d / -D of it,
    checkout -B / switch -C of it, rebase naming it.
The ulthar CLI's own git runs (`ulthar unit advance <u> integrated`) never pass a hook.
"""
from __future__ import annotations

import os
from pathlib import Path

from . import shellwords
from . import store as store_mod

GUARD_ID = "git_refs"
SHELL_TOOLS = frozenset({"Bash", "PowerShell"})
DEFAULT_INTEGRATION = "develop"
DEFAULT_PROTECTED = ("main", "master")
GIT_EXES = frozenset({"git", "git.exe"})
VALUE_OPTIONS = frozenset({"-c", "--git-dir", "--work-tree", "--namespace", "--exec-path",
                           "--config-env", "--super-prefix"})
ON_BRANCH = frozenset({"merge", "pull", "commit", "rebase", "cherry-pick", "revert", "am",
                       "reset"})
BRANCH_MOVES = frozenset({"-f", "--force", "-m", "-M", "--move", "-c", "-C", "--copy", "-d",
                          "-D", "--delete"})
CREATE_FORCE = {"checkout": frozenset({"-B"}), "switch": frozenset({"-C", "--force-create"})}
WRAPPERS = ("$(", "`", "(", "{")
NESTED_DEPTH = 2


def guarded(cfg: dict) -> set[str]:
    integ = cfg.get("integration_branch") or DEFAULT_INTEGRATION
    protected = cfg.get("protected_branches", list(DEFAULT_PROTECTED))
    if not isinstance(integ, str) or not isinstance(protected, list) or not all(
            isinstance(b, str) and b for b in protected):
        raise store_mod.StoreError(".claude/ulthar.json: integration_branch must be a branch "
                                   "name and protected_branches a list of branch names")
    return {b.casefold() for b in [integ, *protected]}


def head_branch(start: Path) -> str | None:
    """The branch checked out in the repository holding `start` (worktrees included);
    None when detached or not a repository. Raises OSError when .git cannot be read."""
    for d in (start, *start.parents):
        dot = d / ".git"
        if dot.is_dir():
            git_dir = dot
        elif dot.is_file():
            text = dot.read_text(encoding="utf-8").strip()
            raw = text.split(":", 1)[1].strip() if text.startswith("gitdir:") else ""
            git_dir = Path(raw) if Path(raw).is_absolute() else d / raw
        else:
            continue
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        return head[len("ref: refs/heads/"):] if head.startswith("ref: refs/heads/") else None
    return None


def _ref(word: str) -> str:
    """A refspec side or ref as a folded branch name."""
    w = word.lstrip("+").casefold()
    for prefix in ("refs/heads/", "heads/"):
        if w.startswith(prefix):
            return w[len(prefix):]
    return w


def _git_call(seg: list[str]) -> tuple[str | None, list[str]] | None:
    """(the -C dir, subcommand + args) of the first git word of the segment."""
    for i, word in enumerate(seg):
        bare = word
        for w in WRAPPERS:
            bare = bare.removeprefix(w)
        if shellwords.basename(bare) not in GIT_EXES:
            continue
        rest, cdir, j = seg[i + 1:], None, 0
        while j < len(rest) and rest[j].startswith("-"):
            opt = rest[j]
            if opt == "-C" and j + 1 < len(rest):
                cdir, j = rest[j + 1], j + 2
            elif opt in VALUE_OPTIONS and j + 1 < len(rest):
                j += 2
            else:
                j += 1
        return cdir, [a.rstrip(")") for a in rest[j:]]
    return None


def _positional(args: list[str]) -> list[str]:
    return [a for a in args if not a.startswith("-")]


def _why(sub: str, args: list[str], branch_of, names: set[str], command: str) -> str | None:
    pos = _positional(args)
    if sub in ON_BRANCH:
        here = branch_of()
        if here is not None and here.casefold() in names:
            return f"`git {sub}` while {here} is checked out moves {here}"
        if sub == "rebase" and any(_ref(p) in names for p in pos):
            return f"`git rebase` of {[p for p in pos if _ref(p) in names][0]}"
        return None
    if sub == "update-ref":
        if "--stdin" in args:
            return "`git update-ref --stdin` (its targets cannot be read here)"
        hit = [p for p in pos if _ref(p) in names]
        return f"`git update-ref {hit[0]}`" if hit else None
    if sub == "commit-tree":
        hit = [n for n in names if n in command.casefold()]
        return f"`git commit-tree` in a command naming {hit[0]}" if hit else None
    if sub == "push":
        if {"--all", "--mirror"} & set(args):
            return "`git push --all/--mirror` includes every branch"
        specs = pos[1:]
        here = branch_of()
        if (not specs or "head" in [s.casefold() for s in specs]) and here and here.casefold() in names:
            return f"`git push` of the checked-out {here}"
        hit = [s for s in specs if _ref(s.split(":")[-1]) in names]
        return f"`git push {hit[0]}`" if hit else None
    if sub == "fetch":
        hit = [s for s in pos[1:] if ":" in s and _ref(s.split(":")[-1]) in names]
        return f"`git fetch {hit[0]}` writes the branch" if hit else None
    if sub == "branch" and BRANCH_MOVES & set(args):
        hit = [p for p in pos if _ref(p) in names]
        return f"`git branch {' '.join(a for a in args if a in BRANCH_MOVES)} {hit[0]}`" if hit else None
    if sub in CREATE_FORCE:
        for k, a in enumerate(args[:-1]):
            if a in CREATE_FORCE[sub] and _ref(args[k + 1]) in names:
                return f"`git {sub} {a} {args[k + 1]}` resets the branch"
    return None


def _segments(command: str, tool: str, depth: int = 0):
    for toks in shellwords.variants(command, tool):
        for seg in shellwords.segments(toks):
            yield seg
            if depth < NESTED_DEPTH:
                for prev, tok in zip(seg, seg[1:]):
                    if prev.casefold() in shellwords.COMMAND_OPTIONS:
                        yield from _segments(tok, tool, depth + 1)


def check(payload: dict, store_of) -> str | None:
    """The block message, or None. Raises on an unreadable config or .git (fail-closed)."""
    if payload.get("tool_name") not in SHELL_TOOLS:
        return None
    command = str((payload.get("tool_input") or {}).get("command") or "")
    if "git" not in command.casefold():
        return None
    store = store_of()
    if store.repo is None or not (store.repo / store_mod.PROJECT_CONFIG).is_file():
        return None
    names = guarded(store.config)
    cwd = Path(str(payload.get("cwd") or os.getcwd()))
    for seg in _segments(command, payload["tool_name"]):
        call = _git_call(seg)
        if call is None or not call[1]:
            continue
        cdir, (sub, *args) = call
        where = cwd / cdir if cdir else cwd
        why = _why(sub.casefold(), args, lambda: head_branch(where), names, command)
        if why:
            return (f"BLOCKED (ulthar git refs, TRUST-05): {why}. The integration branch and the "
                    f"protected branches ({', '.join(sorted(names))}) move only through ulthar: "
                    "`ulthar unit advance <unit> integrated` checks the unit's changes (TRUST-04) "
                    "and merges it itself. Work on the unit's own branch.")
    return None
