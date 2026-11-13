"""Trusted verification runs (GOAL-10, R1-c AC-7/AC-16), CLI only.

`ulthar verify` runs only the confirmed contract's verify_cmd of each AC (never a
command given on the command line), each windowless and with its process tree
contained (ulthar.worker.run_contained). Pass/fail per AC:
  * an AC with a threshold (lo and/or hi): the last non-empty stdout line read as a
    number and compared with the threshold (not a number: fail);
  * an AC without one: exit code 0 passes.
The CLI writes one manifest per run to `<knowledge_root>/.ulthar/evidence/<unit>/<run>.json`
(command, stdout/stderr sha256, Python version and platform, exit code, declared
artifacts' sha256, worktree state, tree hash, base commit, contract sha, runner
agent_id) and one runtime.db `evidence` row holding the manifest's sha256. Acceptance
reads only those rows, and only with a manifest whose sha256 still matches: a file
anyone else wrote into the evidence directory, or a rewritten manifest, never counts.

Tree hash: every file of the working tree, untracked ones included (.gitignore
honoured), staged into a temporary index (a copy of the real one, so git's stat cache
applies) and `git write-tree` -- minus the knowledge store itself when it lives in the
repository (the store changes with every record, the verify run's own included). In
its place every transition checks that the confirmed contract file's current sha256 is
the confirmed (and evidence) contract sha (ulthar.flow). Known limit (spec): a change
to a product file .gitignore ignores does not show in the tree hash.
"""
from __future__ import annotations

import json
import math
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import shellwords
from . import store as store_mod
from . import worker

VERIFY_TIMEOUT_SEC = 600
GIT_TIMEOUT_SEC = 120
WINDOWLESS = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


class EvidenceError(Exception):
    pass


def sha256_bytes(data: bytes) -> str:
    return store_mod.sha256(data).hexdigest()


def sha256_file(path: Path) -> str | None:
    try:
        return sha256_bytes(path.read_bytes())
    except OSError:
        return None


# ---------------------------------------------------------------------------- git

def git(repo: Path, *args: str, env: dict | None = None) -> str:
    try:
        proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                              timeout=GIT_TIMEOUT_SEC, env=env, stdin=subprocess.DEVNULL,
                              **WINDOWLESS)
    except (OSError, subprocess.SubprocessError) as exc:
        raise EvidenceError(f"git {args[0]}: {type(exc).__name__}: {exc}") from exc
    if proc.returncode != 0:
        raise EvidenceError(f"git {' '.join(args[:2])} exit {proc.returncode}: "
                            f"{proc.stderr.decode('utf-8', 'replace').strip()[:300]}")
    return proc.stdout.decode("utf-8", "replace")


def _exclude(store: store_mod.Store, repo: Path) -> list[str]:
    try:
        rel = store.dir.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        return []
    return [f":(exclude){rel}"]


def tree_state(store: store_mod.Store) -> dict:
    """{tree_hash, base_commit, modified, untracked} of the repository's working tree."""
    repo = store.repo
    if repo is None or not (repo / ".git").exists():
        raise EvidenceError(f"{repo}: not a git repository (the tree hash needs git)")
    top = Path(git(repo, "rev-parse", "--show-toplevel").strip())
    git_dir = Path(git(repo, "rev-parse", "--absolute-git-dir").strip())
    try:
        base = git(repo, "rev-parse", "--verify", "-q", "HEAD").strip()
    except EvidenceError:
        base = None  # a repository without commits
    spec = ["--", ".", *_exclude(store, top)]
    status = git(top, "status", "--porcelain=v1", "-z", "--untracked-files=all", *spec)
    modified, untracked = [], []
    for entry in filter(None, status.split("\0")):
        code, path = entry[:2], entry[3:]
        (untracked if code == "??" else modified).append(path)
    with tempfile.TemporaryDirectory(prefix="ulthar-tree-") as tmp:
        index = Path(tmp) / "index"
        if (git_dir / "index").is_file():
            shutil.copyfile(git_dir / "index", index)
        env = dict(os.environ, GIT_INDEX_FILE=str(index))
        git(top, "add", "-A", *spec, env=env)
        for exclude in _exclude(store, top):  # the store is no part of the product tree
            git(top, "rm", "-r", "-q", "--cached", "--ignore-unmatch", "--",
                exclude.removeprefix(":(exclude)"), env=env)
        tree = git(top, "write-tree", env=env).strip()
    return {"tree_hash": tree, "base_commit": base, "modified": sorted(modified),
            "untracked": sorted(untracked)}


# ---------------------------------------------------------------------------- one AC

def judge(ac: dict, rc: int, stdout: bytes) -> tuple[bool, float | None]:
    """(passed, measured value) by the AC-7 rules."""
    th = ac.get("threshold") or {}
    lo, hi = th.get("lo"), th.get("hi")
    if lo is None and hi is None:
        return rc == 0, None
    lines = [ln.strip() for ln in stdout.decode("utf-8", "replace").splitlines() if ln.strip()]
    try:
        value = float(lines[-1])
    except (IndexError, ValueError):
        return False, None
    if not math.isfinite(value):
        return False, None
    ok = True
    if lo is not None:
        ok = ok and (value >= lo if th.get("lo_incl") else value > lo)
    if hi is not None:
        ok = ok and (value <= hi if th.get("hi_incl") else value < hi)
    return ok, value


def run_ac(ac: dict, repo: Path) -> dict:
    from . import contract  # noqa: PLC0415 -- resolve_command; contract imports nothing heavy at load

    argv = shellwords.tokenize(ac["verify_cmd"], None)
    exe = contract.resolve_command(argv[0] if argv else "", repo)
    entry = {"id": ac["id"], "kind": ac["kind"], "command": ac["verify_cmd"]}
    if exe is None:
        return dict(entry, exit_code=None, error="the command's first word does not resolve",
                    stdout_sha256=None, stderr_sha256=None, value=None, passed=False)
    try:
        rc, out, err = worker.run_contained([exe, *argv[1:]], b"", VERIFY_TIMEOUT_SEC,
                                            str(repo), dict(os.environ), what="verify_cmd")
    except (OSError, RuntimeError) as exc:
        return dict(entry, exit_code=None, error=f"{type(exc).__name__}: {exc}",
                    stdout_sha256=None, stderr_sha256=None, value=None, passed=False)
    passed, value = judge(ac, rc, out)
    return dict(entry, exit_code=rc, stdout_sha256=sha256_bytes(out),
                stderr_sha256=sha256_bytes(err), value=value, passed=passed,
                stdout_tail=out.decode("utf-8", "replace")[-500:],
                artifacts={a: sha256_file(repo / a) for a in ac.get("artifacts") or []})


# ---------------------------------------------------------------------------- one run

def run(store: store_mod.Store, unit: dict, doc: dict, runner_agent_id: str,
        self_check: bool) -> tuple[dict, Path, str]:
    """Runs every AC; writes the manifest. Returns (manifest, its path, its sha256)."""
    repo = store.repo
    state = tree_state(store)  # before the commands: the tree they were run on
    results = [run_ac(ac, repo) for ac in doc["acs"]]
    run_id = os.urandom(8).hex()
    confirmed = unit.get("confirmed") or {}
    manifest = {
        "unit_id": unit["id"], "run_id": run_id, "self": self_check,
        "runner_agent_id": runner_agent_id, "contract_sha": confirmed.get("sha"),
        "contract_version": confirmed.get("version"), "ts": store_mod.now_iso(),
        "python": {"version": sys.version.split()[0], "implementation": platform.python_implementation()},
        "platform": platform.platform(), **state, "acs": results,
        "all_quant_passed": all(r["passed"] for r in results if r["kind"] == "quant"),
    }
    data = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    path = store.dir / "evidence" / unit["id"] / f"{run_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "xb") as fh:
        fh.write(data)
    return manifest, path, sha256_bytes(data)


def intact(row_result: dict, manifest_path: str) -> str | None:
    """Why an evidence row's manifest cannot be used (None: it can)."""
    want = row_result.get("manifest_sha256")
    got = sha256_file(Path(manifest_path))
    if got is None:
        return f"manifest {manifest_path} is missing"
    if got != want:
        return f"manifest {manifest_path} was changed after ulthar wrote it (sha256 mismatch)"
    return None
