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
    checkout -B / switch -C of it, rebase naming it;
  * ref-moving plumbing: symbolic-ref naming it, fast-import (its updates come from
    stdin: always), filter-branch / replay / history while it is checked out, naming it,
    or (filter-branch) with --all / --branches;
  * indirection, fail-closed: a git alias defined on the command line (`-c alias.*=`,
    `--config-env alias.*=`); a subcommand that is no git builtin (a configured alias or
    an external git-<name>) while the branch is checked out or when the command names it;
    a segment whose command is itself indirect (`$var ...`, PowerShell `& $var ...`,
    eval, Invoke-Expression / iex, Invoke-Command) and that names the branch or update-ref.
`git commit --dry-run` stays allowed. The ulthar CLI's own git runs (`ulthar unit
advance <u> integrated`) never pass a hook.
Known limit: a spelling that names neither git nor the branch nor update-ref in the
command text -- a PowerShell alias or Bash alias / function defined earlier (`g
update-ref ...` with `g` = git), a script file that runs git -- is not seen here (as R1-c's
shell-write limit); such a move does not integrate any unit: its record stays accepted.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from . import shellwords
from . import store as store_mod

GUARD_ID = "git_refs"
SHELL_TOOLS = frozenset({"Bash", "PowerShell"})
DEFAULT_INTEGRATION = "develop"
DEFAULT_PROTECTED = ("main", "master")
GIT_EXES = frozenset({"git", "git.exe"})
VALUE_OPTIONS = frozenset({"--work-tree", "--namespace", "--exec-path", "--super-prefix",
                           "--attr-source", "--list-cmds"})
CONFIG_OPTIONS = frozenset({"-c", "--config-env"})
INDIRECT_COMMANDS = frozenset({"eval", "iex", "invoke-expression", "invoke-command", "icm"})
NAMING = frozenset({"symbolic-ref"})  # blocked when the command names a guarded branch
REWRITERS = frozenset({"filter-branch", "replay", "history"})  # ... or while it is checked out
# git's own commands (`git help -a`, 2.56): anything else is an alias or an external command
BUILTINS = frozenset("""
add am annotate apply archive backfill bisect blame branch bugreport bundle cat-file
check-attr check-ignore check-mailmap check-ref-format checkout checkout-index cherry
cherry-pick citool clean clone column commit commit-graph commit-tree config count-objects
credential credential-cache credential-store daemon describe diagnose diff diff-files
diff-index diff-pairs diff-tree difftool fast-export fast-import fetch fetch-pack
filter-branch fmt-merge-msg for-each-ref for-each-repo format-patch format-rev fsck gc
get-tar-commit-id grep gui hash-object help history hook http-backend index-pack init
instaweb interpret-trailers last-modified log ls-files ls-remote ls-tree mailinfo mailsplit
maintenance merge merge-base merge-file merge-index merge-one-file merge-tree mergetool mktag
mktree multi-pack-index mv name-rev notes pack-objects pack-redundant pack-refs patch-id
prune prune-packed pull push range-diff read-tree rebase reflog refs remote repack replace
replay repo request-pull rerere reset restore rev-list rev-parse revert rm scalar send-pack
shortlog show show-branch show-index show-ref sparse-checkout stash status stripspace
submodule survey switch symbolic-ref tag unpack-file unpack-objects update-index update-ref
update-server-info var verify-commit verify-pack verify-tag version whatchanged worktree
write-tree""".split())
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


def _branch_at(where: Path, git_dir: Path | None) -> str | None:
    if git_dir is None:
        return head_branch(where)
    head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    return head[len("ref: refs/heads/"):] if head.startswith("ref: refs/heads/") else None


def _ref(word: str) -> str:
    """A refspec side or ref as a folded branch name."""
    w = word.lstrip("+").casefold()
    for prefix in ("refs/heads/", "heads/"):
        if w.startswith(prefix):
            return w[len(prefix):]
    return w


def _git_call(seg: list[str]) -> dict | None:
    """{cdir, git_dir, configs, args} of the first git word of the segment: the -C dir,
    the --git-dir, the names given to -c / --config-env, and subcommand + args."""
    for i, word in enumerate(seg):
        bare = word
        for w in WRAPPERS:
            bare = bare.removeprefix(w)
        if shellwords.basename(bare) not in GIT_EXES:
            continue
        rest, j = seg[i + 1:], 0
        call: dict = {"cdir": None, "git_dir": None, "configs": []}
        while j < len(rest) and rest[j].startswith("-"):
            opt, has_value = rest[j], j + 1 < len(rest)
            name, eq, value = opt.partition("=")
            if opt == "-C" and has_value:
                call["cdir"], j = rest[j + 1], j + 2
            elif name == "--git-dir":
                call["git_dir"], j = (value, j + 1) if eq else (rest[j + 1] if has_value else "", j + 2)
            elif name in CONFIG_OPTIONS:
                call["configs"].append(value if eq else (rest[j + 1] if has_value else ""))
                j += 1 if eq else 2
            elif opt.startswith("-c") and len(opt) > 2:
                call["configs"].append(opt[2:])
                j += 1
            elif opt in VALUE_OPTIONS and has_value:
                j += 2
            else:
                j += 1
        call["args"] = [a.rstrip(")") for a in rest[j:]]
        return call
    return None


def _mentions(words: list[str], names: set[str]) -> str | None:
    """A guarded branch (or update-ref) named anywhere in `words` -- also inside a quoted
    string (eval, iex) and either side of a refspec."""
    for word in words:
        for piece in re.split(r"[\s:=;]+", word):
            piece = piece.strip("'\"(){}$`")
            if piece and (_ref(piece) in names or piece.casefold() == "update-ref"):
                return piece
    return None


def _indirect(seg: list[str], names: set[str]) -> str | None:
    words = [w for w in seg if w != shellwords.REDIRECT]
    while words and re.match(r"^\w+=", words[0]):
        words = words[1:]
    if not words:
        return None
    first = words[0]
    if not (first.startswith(("$", "`")) or first.casefold() in INDIRECT_COMMANDS):
        return None
    hit = _mentions(words[1:], names)
    return f"an indirect command (`{first} ...`) naming {hit}" if hit else None


def _positional(args: list[str]) -> list[str]:
    return [a for a in args if not a.startswith("-")]


def _on_guarded(branch_of, names: set[str]) -> str | None:
    here = branch_of()
    return here if here is not None and here.casefold() in names else None


def _why(sub: str, args: list[str], branch_of, names: set[str], command: str) -> str | None:
    pos = _positional(args)
    if sub not in BUILTINS:
        here, hit = _on_guarded(branch_of, names), _mentions(args, names)
        if here or hit:
            return (f"`git {sub}` is no git builtin (an alias or an external command) "
                    f"{'while ' + here + ' is checked out' if here else 'naming ' + hit}")
        return None
    if sub == "fast-import":
        return "`git fast-import` (its ref updates come from stdin and cannot be read here)"
    if sub in NAMING or sub in REWRITERS:
        here, hit = _on_guarded(branch_of, names), _mentions(args, names)
        if sub == "filter-branch" and {"--all", "--branches"} & set(args):
            hit = hit or "every branch (--all / --branches)"
        if hit or (sub in REWRITERS and here):
            return f"`git {sub}` {'naming ' + hit if hit else 'while ' + here + ' is checked out'}"
        return None
    if sub == "commit" and "--dry-run" in args:
        return None
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
    store = store_of()
    if store.repo is None or not (store.repo / store_mod.PROJECT_CONFIG).is_file():
        return None
    names = guarded(store.config)
    cwd = Path(str(payload.get("cwd") or os.getcwd()))
    for seg in _segments(command, payload["tool_name"]):
        why = _indirect(seg, names)
        call = _git_call(seg) if why is None else None
        if call is not None:
            aliases = [c for c in call["configs"] if c.casefold().startswith("alias.")]
            if aliases:
                why = f"a git alias defined on the command line ({aliases[0].split('=')[0]})"
            elif call["args"]:
                sub, *args = call["args"]
                where = cwd / call["cdir"] if call["cdir"] else cwd
                git_dir = Path(call["git_dir"]) if call["git_dir"] else None
                if git_dir is not None and not git_dir.is_absolute():
                    git_dir = where / git_dir
                why = _why(sub.casefold(), args, lambda: _branch_at(where, git_dir), names, command)
        if why:
            return (f"BLOCKED (ulthar git refs, TRUST-05): {why}. The integration branch and the "
                    f"protected branches ({', '.join(sorted(names))}) move only through ulthar: "
                    "`ulthar unit advance <unit> integrated` checks the unit's changes (TRUST-04) "
                    "and merges it itself. Work on the unit's own branch.")
    return None
