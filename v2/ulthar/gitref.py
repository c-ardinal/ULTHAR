"""Git ref updates of the integration and protected branches are the CLI's (TRUST-05,
R1-d AC-11) -- hook side, after the ported v1 git_discipline (which stays as it is).

In a project with `.claude/ulthar.json`, every Bash / PowerShell command is read for
git in COMMAND POSITION: the first word of a segment (after VAR=x assignments), after
the wrappers xargs / env / command / exec / nice / time / timeout / sudo / nohup /
stdbuf (and their options), at a `$(` / backtick / `(` / `{` opening, and inside a
child shell: `bash -c` / `sh -c` take their one script argument, `cmd /c` / `cmd /k` and
`powershell -Command` / `-c` the whole rest of the line, quoted or not (PowerShell's `&`
call operator splits the segment, so `& git ...` is a segment of its own). `echo git
commit` or `grep "git reset" f` is no git call, nor is the body of a Bash here-document
(`<<EOF`, `<<-EOF`, `<<'EOF'`, `<<"EOF"`: shellwords.strip_heredocs) -- the line that
opens it is read as usual. A call is blocked when it would move a guarded branch -- the config's
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
    `--config-env alias.*=`); a subcommand that is no git builtin is resolved against
    `alias.<name>` in the repository / global / system config (the files
    ulthar.posthash.config_files locates) and its expansion judged by these same rules
    (a `!` alias as a shell command); when a config file cannot be read, uses
    include / includeIf, or config comes from the environment, such a subcommand is
    blocked; with no alias (an external git-<name>) it is blocked while the branch is
    checked out or when the command names it; a command whose executable is itself
    indirect (`$var ...`, eval, Invoke-Expression / iex, Invoke-Command) and that names the
    branch or update-ref; any command-position executable whose first argument is
    `update-ref` followed by a guarded ref (`g update-ref refs/heads/develop ...`); and,
    while a guarded branch is checked out, any command-position executable other than a
    display command whose first argument is a ref-moving git operation (merge, pull,
    commit, rebase, cherry-pick, revert, am, reset, update-ref, push) -- `g merge x` with
    a shell alias or function `g` = git.
`git commit --dry-run` stays allowed. The ulthar CLI's own git runs (`ulthar unit
advance <u> integrated`) never pass a hook.
Known limit: git run from inside a script file or program (`./sync.sh`, `make release`)
is not seen here (as R1-c's shell-write limit).
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from . import posthash
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
PRINTERS = frozenset({"echo", "printf", "write-output", "write-host", "grep", "rg", "findstr",
                      "select-string", "cat", "less", "more", "head", "tail", "man", "help",
                      "get-help", "which", "where", "type", "ls", "dir", "get-content"})
# their arguments are text or names, not a command
REF_MOVERS = frozenset({"merge", "pull", "commit", "rebase", "cherry-pick", "revert", "am",
                        "reset", "update-ref", "push"})
VALUED = frozenset({"-C", "-c", "--git-dir", "--work-tree"})
CHILD_SHELLS = {"cmd": ("cmd", frozenset({"/c", "/k"})),
                "powershell": ("PowerShell", frozenset({"-command", "-c", "-commandwithargs"})),
                "pwsh": ("PowerShell", frozenset({"-command", "-c", "-commandwithargs"}))}
WRAPPER_VALUE_OPTIONS = {
    "xargs": frozenset({"-I", "-n", "-L", "-P", "-d", "-E", "-s", "-a", "--max-args",
                        "--max-lines", "--max-procs", "--delimiter", "--arg-file"}),
    "env": frozenset({"-u", "--unset", "-C", "--chdir"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "timeout": frozenset({"-s", "--signal", "-k", "--kill-after"}),
    "sudo": frozenset({"-u", "-g", "-h", "-p", "-C", "-D", "-R", "-T", "-U"}),
    "stdbuf": frozenset({"-i", "-o", "-e"}),
    "command": frozenset(), "exec": frozenset({"-a"}), "time": frozenset(),
    "nohup": frozenset(),
}
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
OPENERS = ("$(", "`", "(", "{")
ASSIGNMENT = re.compile(r"^\w+=")
NESTED_DEPTH = 2
ALIAS_DEPTH = 5


class _Unsure(Exception):
    """A git config this module cannot read with certainty (include, continuation)."""


def guarded(cfg: dict) -> set[str]:
    integ = cfg.get("integration_branch") or DEFAULT_INTEGRATION
    protected = cfg.get("protected_branches", list(DEFAULT_PROTECTED))
    if not isinstance(integ, str) or not isinstance(protected, list) or not all(
            isinstance(b, str) and b for b in protected):
        raise store_mod.StoreError(".claude/ulthar.json: integration_branch must be a branch "
                                   "name and protected_branches a list of branch names")
    return {b.casefold() for b in [integ, *protected]}


# ---------------------------------------------------------------------------- repository

def find_git_dir(start: Path) -> Path | None:
    """The git directory of the repository holding `start` (a worktree's .git file
    followed); None when not a repository. Raises OSError when .git cannot be read."""
    for d in (start, *start.parents):
        dot = d / ".git"
        if dot.is_dir():
            return dot
        if dot.is_file():
            text = dot.read_text(encoding="utf-8").strip()
            raw = text.split(":", 1)[1].strip() if text.startswith("gitdir:") else ""
            return Path(raw) if Path(raw).is_absolute() else d / raw
    return None


def head_of(git_dir: Path | None) -> str | None:
    if git_dir is None:
        return None
    head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    return head[len("ref: refs/heads/"):] if head.startswith("ref: refs/heads/") else None


def head_branch(start: Path) -> str | None:
    """The branch checked out in the repository holding `start`; None when detached or
    not a repository."""
    return head_of(find_git_dir(start))


# ---------------------------------------------------------------------------- config aliases

def _value(raw: str) -> str:
    """A git config value: quotes removed, escapes applied, a comment cut off."""
    out, quoted, i = [], False, 0
    while i < len(raw):
        ch = raw[i]
        if ch == "\\" and i + 1 < len(raw):
            out.append({"n": "\n", "t": "\t", "b": "\b"}.get(raw[i + 1], raw[i + 1]))
            i += 2
            continue
        if ch == '"':
            quoted = not quoted
        elif ch in "#;" and not quoted:
            break
        else:
            out.append(ch)
        i += 1
    return "".join(out).strip()


def aliases_in(text: str) -> dict[str, str]:
    """`alias.<name>` values (names folded) of one config file's text; _Unsure for an
    include / includeIf section or a continued line."""
    found, section = {}, ""
    for line in text.splitlines():
        line = line.strip()
        if line.endswith("\\"):
            raise _Unsure("a continued line")
        while line.startswith("["):
            end = line.find("]")
            if end < 0:
                raise _Unsure("an unterminated section header")
            section = line[1:end].strip().casefold()
            if section.startswith("include"):
                raise _Unsure("include / includeIf")
            line = line[end + 1:].strip()
        if not line or line[0] in "#;" or section != "alias":
            continue
        key, eq, raw = line.partition("=")
        found[key.strip().casefold()] = _value(raw) if eq else "true"
    return found


def alias_of(git_dir: Path | None, name: str) -> tuple[str | None, str | None]:
    """(the alias's expansion or None, why the config cannot be trusted or None)."""
    files = posthash.config_files(git_dir) if git_dir is not None else None
    if files is None:
        return None, "the git config cannot be located or comes from the environment"
    value = None
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            continue
        except OSError as exc:
            return None, f"{path} cannot be read ({exc})"
        try:
            value = aliases_in(text).get(name.casefold(), value)
        except _Unsure as exc:
            return None, f"{path} uses {exc}"
    return value, None


# ---------------------------------------------------------------------------- words

def _ref(word: str) -> str:
    """A refspec side or ref as a folded branch name."""
    w = word.lstrip("+").casefold()
    for prefix in ("refs/heads/", "heads/"):
        if w.startswith(prefix):
            return w[len(prefix):]
    return w


def _opened(word: str) -> str | None:
    """`word` without its `$(` / backtick / `(` / `{` opening (an assignment's too), or
    None when it opens nothing."""
    w = ASSIGNMENT.sub("", word)
    if not w.startswith(OPENERS):
        return None
    while w.startswith(OPENERS):
        w = w[2:] if w.startswith("$(") else w[1:]
    return w


def _unwrap(words: list[str]) -> list[str]:
    """The words from the command position on: assignments and wrappers (with their
    options) skipped."""
    while True:
        words = [w for w in words if w]
        while words and ASSIGNMENT.match(words[0]):
            words = words[1:]
        if not words:
            return []
        exe = shellwords.basename(words[0]).removesuffix(".exe")
        if exe not in WRAPPER_VALUE_OPTIONS:
            return words
        opts, j = WRAPPER_VALUE_OPTIONS[exe], 1
        while j < len(words) and (words[j].startswith("-") or (exe == "env" and "=" in words[j])):
            j += 2 if words[j] in opts else 1
        if exe == "timeout" and j < len(words):
            j += 1  # the duration
        words = words[j:]


def commands(seg: list[str]) -> list[list[str]]:
    """Every command of the segment, from its command position: the segment's start and
    each `$(` / backtick / `(` / `{` opening."""
    seg = [w for w in seg if w not in (shellwords.REDIRECT, shellwords.INPUT)]
    out = [_unwrap(seg)]
    for i in range(len(seg)):  # the first word too: `x=$(git ...)`
        inner = _opened(seg[i])
        if inner is not None:
            out.append(_unwrap([inner, *seg[i + 1:]]))
    return [c for c in out if c]


def _git_call(words: list[str]) -> dict | None:
    """{cdir, git_dir, configs, args} when `words` (from the command position) is a git
    call: the -C dir, the --git-dir, the names given to -c / --config-env, and
    subcommand + args."""
    if not words or shellwords.basename(words[0]) not in GIT_EXES:
        return None
    rest, j = words[1:], 0
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


def _mentions(words: list[str], names: set[str]) -> str | None:
    """A guarded branch (or update-ref) named anywhere in `words` -- also inside a quoted
    string (eval, iex) and either side of a refspec."""
    for word in words:
        for piece in re.split(r"[\s:=;]+", word):
            piece = piece.strip("'\"(){}$`")
            if piece and (_ref(piece) in names or piece.casefold() == "update-ref"):
                return piece
    return None


def _guarded_ref(words: list[str], names: set[str]) -> str | None:
    for word in words:
        for piece in re.split(r"[\s:=;]+", word):
            piece = piece.strip("'\"(){}$`")
            if piece and _ref(piece) in names:
                return piece
    return None


def _first_operand(words: list[str]) -> str | None:
    j = 1
    while j < len(words) and words[j].startswith("-"):
        j += 2 if words[j] in VALUED else 1
    return words[j] if j < len(words) else None


def _not_git(words: list[str], names: set[str], ctx: "_Ctx") -> str | None:
    """Rules for a command-position executable that is not git."""
    first = words[0]
    if first.startswith(("$", "`")) or first.casefold() in INDIRECT_COMMANDS:
        hit = _mentions(words[1:], names)
        return f"an indirect command (`{first} ...`) naming {hit}" if hit else None
    exe = shellwords.basename(first).removesuffix(".exe")
    args = [w for w in words[1:] if not w.startswith("-")]
    if exe in PRINTERS:
        return None
    if args[:1] and args[0].casefold() == "update-ref":
        hit = _guarded_ref(args[1:], names)
        if hit:
            return f"`{first} update-ref {hit}` (update-ref of a guarded branch)"
    op = _first_operand(words)
    if op is not None and op.casefold() in REF_MOVERS:
        here = ctx.here()
        if here:
            return (f"`{first} {op} ...` while {here} is checked out (a ref-moving git operation "
                    "through a non-git spelling, e.g. a shell alias of git)")
    return None


# ---------------------------------------------------------------------------- judging

class _Ctx:
    """One command's judging context: guarded names, the repository the call runs in."""

    def __init__(self, names: set[str], where: Path, git_dir: Path | None, command: str):
        self.names, self.where, self.command = names, where, command
        self._git_dir, self._found = git_dir, git_dir is not None

    def git_dir(self) -> Path | None:
        if not self._found:
            self._git_dir, self._found = find_git_dir(self.where), True
        return self._git_dir

    def here(self) -> str | None:
        head = head_of(self.git_dir())
        return head if head is not None and head.casefold() in self.names else None


def _why_alias(sub: str, args: list[str], ctx: _Ctx, depth: int) -> str | None:
    value, unsure = alias_of(ctx.git_dir(), sub)
    if unsure:
        return f"`git {sub}` is no git builtin and the aliases cannot be read with certainty ({unsure})"
    if value is None:  # an external git-<name>
        here, hit = ctx.here(), _mentions(args, ctx.names)
        if here or hit:
            return (f"`git {sub}` is no git builtin (an external command) "
                    f"{'while ' + here + ' is checked out' if here else 'naming ' + hit}")
        return None
    if depth >= ALIAS_DEPTH:
        return f"`git {sub}`: aliases nested deeper than {ALIAS_DEPTH}"
    if value.startswith("!"):
        why = _why_text(" ".join([value[1:], *args]), "Bash", ctx, depth + 1)
        return f"`git {sub}` is the shell alias `{value}`: {why}" if why else None
    expanded = shellwords.tokenize(value, "\\") + args
    if not expanded:
        return None
    why = _why(expanded[0].casefold(), expanded[1:], ctx, depth + 1)
    return f"`git {sub}` is the alias `{value}`: {why}" if why else None


def _why(sub: str, args: list[str], ctx: _Ctx, depth: int = 0) -> str | None:
    names, pos = ctx.names, [a for a in args if not a.startswith("-")]
    if sub not in BUILTINS:
        return _why_alias(sub, args, ctx, depth)
    if sub == "fast-import":
        return "`git fast-import` (its ref updates come from stdin and cannot be read here)"
    if sub in NAMING or sub in REWRITERS:
        here, hit = ctx.here(), _mentions(args, names)
        if sub == "filter-branch" and {"--all", "--branches"} & set(args):
            hit = hit or "every branch (--all / --branches)"
        if hit or (sub in REWRITERS and here):
            return f"`git {sub}` {'naming ' + hit if hit else 'while ' + here + ' is checked out'}"
        return None
    if sub == "commit" and "--dry-run" in args:
        return None
    if sub in ON_BRANCH:
        here = ctx.here()
        if here:
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
        hit = [n for n in names if n in ctx.command.casefold()]
        return f"`git commit-tree` in a command naming {hit[0]}" if hit else None
    if sub == "push":
        if {"--all", "--mirror"} & set(args):
            return "`git push --all/--mirror` includes every branch"
        specs = pos[1:]
        if not specs or "head" in [s.casefold() for s in specs]:
            here = ctx.here()
            if here:
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


def _child(seg: list[str], k: int, tool: str) -> tuple[str, str]:
    """(command text, tool) a child shell runs for the option at seg[k]: cmd /c|/k and
    powershell -Command|-c take the whole rest of the segment, bash/sh -c its one argument."""
    opt = seg[k].casefold()
    for word in reversed(seg[:k]):
        launcher = shellwords.basename(word).removesuffix(".exe")
        if launcher in CHILD_SHELLS and opt in CHILD_SHELLS[launcher][1]:
            return " ".join(seg[k + 1:]), CHILD_SHELLS[launcher][0]
        if launcher in ("bash", "sh"):
            return seg[k + 1], "Bash"
    return seg[k + 1], tool


def _segments(command: str, tool: str, depth: int = 0):
    text = shellwords.strip_heredocs(command) if tool == "Bash" else command
    for toks in shellwords.variants(text, tool):
        for seg in shellwords.segments(toks):
            yield seg
            if depth < NESTED_DEPTH:
                for k in range(len(seg) - 1):
                    if seg[k].casefold() in shellwords.COMMAND_OPTIONS:
                        yield from _segments(*_child(seg, k, tool), depth + 1)


def _why_command(words: list[str], ctx: _Ctx, depth: int) -> str | None:
    call = _git_call(words)
    if call is None:
        return _not_git(words, ctx.names, ctx)
    aliases = [c for c in call["configs"] if c.casefold().startswith("alias.")]
    if aliases:
        return f"a git alias defined on the command line ({aliases[0].split('=')[0]})"
    if not call["args"]:
        return None
    where = ctx.where / call["cdir"] if call["cdir"] else ctx.where
    git_dir = Path(call["git_dir"]) if call["git_dir"] else None
    if git_dir is not None and not git_dir.is_absolute():
        git_dir = where / git_dir
    sub, *args = call["args"]
    return _why(sub.casefold(), args, _Ctx(ctx.names, where, git_dir, ctx.command), depth)


def _why_text(command: str, tool: str, ctx: _Ctx, depth: int = 0) -> str | None:
    for seg in _segments(command, tool):
        for words in commands(seg):
            why = _why_command(words, ctx, depth)
            if why:
                return why
    return None


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
    why = _why_text(command, payload["tool_name"], _Ctx(names, cwd, None, command))
    if not why:
        return None
    return (f"BLOCKED (ulthar git refs, TRUST-05): {why}. The integration branch and the "
            f"protected branches ({', '.join(sorted(names))}) move only through ulthar: "
            "`ulthar unit advance <unit> integrated` checks the unit's changes (TRUST-04) "
            "and merges it itself. Work on the unit's own branch.")
