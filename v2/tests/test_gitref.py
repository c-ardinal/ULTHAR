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
    _round3_on_develop(p, sb)
    git(sb, "checkout", "-q", WORK_BRANCH)
    _config_aliases(p, sb)
    _child_shells(p, sb)
    _round4(p, sb)


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


def _round3_on_develop(p: Probe, sb) -> None:
    """Review r1d-3 #3 / #4, develop checked out: heredoc bodies are no commands; a non-git
    spelling of a ref-moving operation is blocked."""
    bodies = ["cat <<'EOF'\ngit push\ngit commit -m x\ngit reset HEAD~1\nEOF",
              "cat <<EOF\ngit push\nEOF", "cat <<-\"END\"\n\tgit commit\n\tEND\necho done"]
    for command in bodies:
        res = bash(sb, None, command)
        p.check(f"AC-11 (review r1d-3 #3): allowed on develop -- heredoc body {command.split()[1]}",
                res.returncode == 0, res.stderr[-300:])
    for command, name in (("git commit -F - <<EOF\nmessage\nEOF", "git commit with a heredoc message"),
                          ("g merge x", "`g merge x` (a shell alias of git)"),
                          ("g reset --hard", "`g reset --hard`")):
        res = bash(sb, None, command)
        p.check(f"AC-11 (review r1d-3 #3/#4): blocked on develop -- {name}",
                res.returncode == 2 and GUARD in res.stderr, res.stderr[-300:])
    for command in ("npm run build", "echo merge", "docker ps"):
        res = bash(sb, None, command)
        p.check(f"AC-11 (review r1d-3 #4): allowed on develop -- `{command}`", res.returncode == 0,
                res.stderr[-300:])


def _child_shells(p: Probe, sb) -> None:
    """Review r1d-3 #2, work branch: cmd /c and powershell -Command run the whole tail."""
    for tool, command in (("Bash", "cmd /c git update-ref refs/heads/develop HEAD"),
                          ("Bash", 'cmd /c "git update-ref refs/heads/develop HEAD"'),
                          ("Bash", "powershell -Command git update-ref refs/heads/develop HEAD"),
                          ("PowerShell", 'powershell -Command "git update-ref refs/heads/develop HEAD"'),
                          ("PowerShell", "pwsh -c git update-ref refs/heads/develop HEAD")):
        res = _shell(sb, tool, command)
        p.check(f"AC-11 (review r1d-3 #2): blocked -- {tool}: `{command}`",
                res.returncode == 2 and GUARD in res.stderr, res.stderr[-300:])


UPDATE = "git update-ref refs/heads/develop HEAD"


def _round4(p: Probe, sb) -> None:
    """Review r1d-4, work branch: input that gets executed is a command; child-shell text
    given to a display command is not."""
    executed = [
        ("Bash", f"bash <<EOF\n{UPDATE}\nEOF", "bash <<EOF"),
        ("Bash", f"sh -s <<EOF\n{UPDATE}\nEOF", "sh -s <<EOF"),
        ("Bash", f"zsh <<'EOF'\n{UPDATE}\nEOF", "zsh <<'EOF'"),
        ("Bash", f"cat <<EOF | bash\n{UPDATE}\nEOF", "cat <<EOF | bash"),
        ("Bash", "cat <<EOF | xargs git update-ref\nrefs/heads/develop HEAD\nEOF", "cat <<EOF | xargs"),
        ("Bash", f'bash <<< "{UPDATE}"', "bash <<< here-string"),
        ("Bash", f"echo {UPDATE} | bash", "echo ... | bash"),
        ("Bash", f"powershell -Command - <<EOF\n{UPDATE}\nEOF", "powershell -Command - <<EOF"),
        ("Bash", f"echo {UPDATE} | powershell -Command -", "echo ... | powershell -Command -"),
        ("PowerShell", f"@'\n{UPDATE}\n'@ | iex", "PowerShell @'...'@ | iex"),
        ("PowerShell", f"@'\n{UPDATE}\n'@ | Invoke-Expression", "PowerShell @'...'@ | Invoke-Expression"),
        ("Bash", f"python - <<EOF\nimport os\nos.system('{UPDATE}')\nEOF",
         "an unclassified reader (python) of a body mentioning git"),
        ("Bash", f"printf '%s\\n' '{UPDATE}' | bash", "printf '%s\\n' ... | bash (review r1d-5)"),
        ("Bash", f'echo -e "{UPDATE}" | bash', "echo -e ... | bash (review r1d-5)"),
        ("PowerShell", f'Write-Output -InputObject "{UPDATE}" | iex',
         "Write-Output -InputObject ... | iex (review r1d-5)"),
    ]
    for tool, command, name in executed:
        res = _shell(sb, tool, command)
        p.check(f"AC-11 (review r1d-4 #1): blocked -- {name}", res.returncode == 2 and GUARD in res.stderr,
                res.stderr[-300:])
    inert = [f"cat <<'EOF'\n{UPDATE}\nEOF", f"cat <<EOF | grep develop\n{UPDATE}\nEOF",
             "python - <<EOF\nprint(1)\nEOF", f"echo cmd /c {UPDATE}",
             f"echo powershell -Command {UPDATE}", f"grep cmd /c {UPDATE}",
             f"bash -c 'cat >/dev/null' <<EOF\n{UPDATE}\nEOF", "git log --oneline -3 | less"]
    for command in inert:
        res = bash(sb, None, command)
        p.check(f"AC-11 (review r1d-4 #1/#2): allowed -- `{command.splitlines()[0]}`",
                res.returncode == 0, res.stderr[-300:])


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
