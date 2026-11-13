"""R1-d AC-11 (TRUST-05): git ref updates of the integration branch (develop) and the
protected branches are blocked at PreToolUse, through the production hook entry, in a
project with `.claude/ulthar.json`; the same operation on the work branch is allowed.
The ported v1 git_discipline runs first, unchanged (its 464 checks stay in run_all)."""
from __future__ import annotations

import sys

from helpers import Probe
from r1d_helpers import WORK_BRANCH, bash, git, new_r1d_sandbox

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
    finally:
        sb.cleanup()


def main() -> int:
    p = Probe()
    print("\n== R1-d AC-11: git ref updates of the integration / protected branches ==")
    check_git_refs(p)
    return p.result()


if __name__ == "__main__":
    sys.exit(main())
