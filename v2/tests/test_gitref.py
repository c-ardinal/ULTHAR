"""R1-d AC-11 (TRUST-05): git ref updates of the integration branch (develop) and the
protected branches are blocked at PreToolUse, through the production hook entry, in a
project with `.claude/ulthar.json`; the same operation on the work branch is allowed.
The ported v1 git_discipline runs first, unchanged (its 464 checks stay in run_all)."""
from __future__ import annotations

import sys

from helpers import Probe
from r1d_helpers import WORK_BRANCH, bash, git, new_r1d_sandbox, payload

GUARD = "ulthar git refs"


def check_git_refs(p: Probe) -> None:
    sb = new_r1d_sandbox(install=False)
    try:
        on_work = [
            ("update-ref of develop", "git update-ref refs/heads/develop HEAD"),
            ("commit-tree advanced into develop",
             "git update-ref refs/heads/develop $(git commit-tree HEAD^{tree} -p develop -m x)"),
            ("push to develop", "git push origin HEAD:develop"),
            ("branch -f develop", "git branch -f develop HEAD"),
            ("checkout -B develop", "git checkout -B develop"),
        ]
        blocked = 0
        for name, command in on_work:
            res = bash(sb, None, command)
            ok = res.returncode == 2 and GUARD in res.stderr
            blocked += ok
            p.check(f"AC-11: blocked on the work branch -- {name}", ok, res.stderr[-300:])
        git(sb, "checkout", "-q", "develop")
        for name, command in (("merge while develop is checked out", "git merge feature/unit"),
                              ("reset while develop is checked out", "git reset --soft HEAD~1")):
            res = bash(sb, None, command)
            ok = res.returncode == 2 and GUARD in res.stderr
            blocked += ok
            p.check(f"AC-11: blocked -- {name}", ok, res.stderr[-300:])
        git(sb, "checkout", "-q", WORK_BRANCH)
        allowed = bash(sb, None, "git merge develop")
        p.check("AC-11: a merge into the work branch is allowed", allowed.returncode == 0,
                allowed.stderr[-300:])
        nested = bash(sb, None, "bash -c \"git -C . update-ref refs/heads/main HEAD\"")
        p.check("AC-11: a protected branch inside `bash -c` with `-C` is blocked too",
                nested.returncode == 2 and GUARD in nested.stderr, nested.stderr[-300:])
        p.check(f"AC-11: blocked {blocked}/7, allowed {int(allowed.returncode == 0)}/1",
                blocked == 7 and allowed.returncode == 0)
        _indirection(p, sb)
    finally:
        sb.cleanup()


def _shell(sb, tool: str, command: str):
    return sb.hook("PreToolUse", payload(tool, {"command": command}))


def _indirection(p: Probe, sb) -> None:
    """Review r1d-1 #3: indirection and ref-moving plumbing fail closed; read-only stays."""
    on_work = [
        ("Bash", "an alias defined with -c", "git -c alias.x='update-ref refs/heads/develop HEAD' x"),
        ("Bash", "an alias defined with --config-env", "git --config-env=alias.y=ULTHAR_T y"),
        ("Bash", "an unknown subcommand naming develop", "git sync-to develop"),
        ("Bash", "symbolic-ref naming develop", "git symbolic-ref refs/heads/develop refs/heads/x"),
        ("Bash", "fast-import", "git fast-import --quiet"),
        ("Bash", "filter-branch --all", "git filter-branch --all"),
        ("Bash", "a variable as the command", "g=git; $g update-ref refs/heads/develop HEAD"),
        ("PowerShell", "PowerShell & $var", "$g = 'git'; & $g update-ref refs/heads/develop HEAD"),
        ("PowerShell", "PowerShell Invoke-Expression",
         "Invoke-Expression 'git update-ref refs/heads/develop HEAD'"),
    ]
    for tool, name, command in on_work:
        res = _shell(sb, tool, command)
        p.check(f"AC-11 (review #3): blocked -- {name}", res.returncode == 2 and GUARD in res.stderr,
                res.stderr[-300:])
    git(sb, "checkout", "-q", "develop")
    res = bash(sb, None, "git st")
    p.check("AC-11 (review #3): blocked -- an unknown subcommand (an alias) while develop is "
            "checked out", res.returncode == 2 and GUARD in res.stderr, res.stderr[-300:])
    allowed = [("git log --oneline -3 develop", "a read-only log naming develop"),
               ("git status", "status while develop is checked out"),
               ("git commit --dry-run", "commit --dry-run while develop is checked out")]
    for command, name in allowed:
        res = bash(sb, None, command)
        p.check(f"AC-11 (review #3): allowed -- {name}", res.returncode == 0, res.stderr[-300:])
    _command_position(p, sb)
    git(sb, "checkout", "-q", WORK_BRANCH)
    _config_aliases(p, sb)


def _command_position(p: Probe, sb) -> None:
    """Review r1d-2 A (develop is checked out): git counts only in command position."""
    for command in ("echo git commit", 'grep "git reset" notes.txt', "printf 'git push'"):
        res = bash(sb, None, command)
        p.check(f"AC-11 (review r1d-2 A): allowed on develop -- `{command}`", res.returncode == 0,
                res.stderr[-300:])
    for command in ("xargs git merge x", "x=$(git update-ref refs/heads/develop HEAD)",
                    "echo $(git update-ref refs/heads/develop HEAD)", "env A=1 git reset"):
        res = bash(sb, None, command)
        p.check(f"AC-11 (review r1d-2 A): blocked on develop -- `{command}`",
                res.returncode == 2 and GUARD in res.stderr, res.stderr[-300:])


def _config_aliases(p: Probe, sb) -> None:
    """Review r1d-2 B (on the work branch): configured aliases are resolved and judged."""
    git(sb, "config", "alias.x", "update-ref refs/heads/develop HEAD")
    git(sb, "config", "alias.y", "!git update-ref refs/heads/develop HEAD")
    git(sb, "config", "alias.st", "status")
    for command, name in (("git x", "a config alias expanding to update-ref of develop"),
                          ("git y", "a `!` shell alias doing the same"),
                          ("g update-ref refs/heads/develop HEAD", "a non-git spelling of update-ref")):
        res = bash(sb, None, command)
        p.check(f"AC-11 (review r1d-2 B): blocked -- {name}", res.returncode == 2 and GUARD in res.stderr,
                res.stderr[-300:])
    res = bash(sb, None, "git st")
    p.check("AC-11 (review r1d-2 B): allowed -- a harmless alias (st = status)", res.returncode == 0,
            res.stderr[-300:])
    inc = sb.root / "inc.gitconfig"
    inc.write_text("[core]\n", encoding="utf-8")
    git(sb, "config", "include.path", inc.as_posix())
    res = bash(sb, None, "git st")
    p.check("AC-11 (review r1d-2 B): blocked -- an unknown subcommand when the config uses "
            "include (fail-closed)", res.returncode == 2 and "certainty" in res.stderr,
            res.stderr[-300:])
    git(sb, "config", "--unset", "include.path")
    for key in ("alias.x", "alias.y", "alias.st"):
        git(sb, "config", "--unset", key)


def main() -> int:
    p = Probe()
    print("\n== R1-d AC-11: git ref updates of the integration / protected branches ==")
    check_git_refs(p)
    return p.result()


if __name__ == "__main__":
    sys.exit(main())
