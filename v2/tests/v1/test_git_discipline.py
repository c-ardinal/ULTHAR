"""hooks/tests/test_git_discipline.py -- unit tests for guards/git_discipline.py.

CONTRACT.md #1 (Ctx), #5 (U-2), #6 (fixture inventory: golden 24 + destroyers >=5 +
deploy_refs >=4 + PE-4). Drives guards.git_discipline.run() directly through a
SandboxCtx (tests/conftest.py) -- no subprocess, no real .ps1, no pytest dependency
(this module exposes main() -> int, exactly like tests/test_contract.py, so
tests/run_all.py can discover and run it).

Fixture schema: the SAME shape tests/parity.py consumes (`id`, `guard`, `ps1`,
`payload`, `expect_exit`, `expect_stderr_contains`?, `parity`, `parity_exception`?)
plus ONE test-only extension key this module reads and tests/parity.py ignores:

  `cwd_kind` -- "main" | "feature" | "none" -- selects which throwaway directory the
  fixture's `payload.cwd` placeholder ("${MAIN_REPO}" / "${FEATURE_REPO}" /
  "${NO_REPO}") is resolved against, and what `git rev-parse --abbrev-ref HEAD` the
  stubbed ctx.run_cmd answers for it ("main" -> "main", "feature" -> "feature/x",
  "none" -> a real, empty, non-repo directory that makes the stub answer a non-zero
  rc, matching git's own "not a git repository" fail-open path). This mirrors
  test-git-discipline-guard.ps1's own $mainRepo/$featRepo throwaway-repo setup,
  minus the real `git init` calls -- this module never shells out to real git; only
  the DIRECTORIES are real, so the guard's `dir_.exists()` check behaves correctly.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
HOOKS_DIR = TESTS_DIR.parent
sys.path.insert(0, str(HOOKS_DIR))
sys.path.insert(0, str(TESTS_DIR))
import v1_compat  # noqa: E402,F401 -- v2 port: v1 names resolve to v2 code

import copy  # noqa: E402

import conftest  # noqa: E402
from guards import git_discipline  # noqa: E402
from lib.ctx import CmdResult  # noqa: E402

SKILL_ROOT = v1_compat.REPO_ROOT

FIXTURES_DIRS = [
    TESTS_DIR / "fixtures" / "git",
]


def _golden_cfg() -> dict:
    """The config-generalisation round: git_policy.deploy_refs ships EMPTY by default (the two
    branch names were one owner's hosting-provider preview branches, moved to
    examples/user-overlay.example.json -- see config.json's
    _note_deploy_refs_empty). The deploy_refs-specific fixtures below still
    exercise the guard MECHANISM against those exact names, sourced from the
    shipped example so the two never drift apart."""
    example = json.loads(
        (SKILL_ROOT / "examples" / "user-overlay.example.json").read_text(encoding="utf-8")
    )
    cfg = copy.deepcopy(conftest.load_test_config())
    cfg["git_policy"]["deploy_refs"] = example["git_policy"]["deploy_refs"]
    return cfg


_DEPLOY_REF_CASE_IDS = {
    "git-phase-order-deploy-ref-wins-over-protected-branch",
}

_BRANCH_FOR_KIND = {
    "main": "main",
    "feature": "feature/x",
}


def load_fixtures() -> list[dict]:
    cases = []
    for d in FIXTURES_DIRS:
        for f in sorted(d.glob("*.json")):
            data = json.loads(f.read_text(encoding="utf-8"))
            entries = data if isinstance(data, list) else [data]
            for entry in entries:
                if entry.get("guard") == "git_discipline":
                    entry["_source_file"] = f.name
                    cases.append(entry)
    return cases


def _needs_deploy_refs(case: dict) -> bool:
    return case.get("_source_file") == "deploy_refs.json" or case["id"] in _DEPLOY_REF_CASE_IDS


def _resolve_cwd(sandbox: "conftest.SandboxCtx", kind: str) -> Path:
    name = {"main": "on-main", "feature": "on-feature", "none": "no-repo"}[kind]
    p = sandbox.root / name
    p.mkdir(parents=True, exist_ok=True)
    return p


def _run_cmd_stub(kind_name: str):
    """`git rev-parse --abbrev-ref HEAD` answers per cwd_kind; any other argv (there
    is none, on this guard's path) would fall through to the non-repo answer."""

    def _stub(argv, kind, cwd=None):
        if kind_name in _BRANCH_FOR_KIND:
            return CmdResult(0, _BRANCH_FOR_KIND[kind_name] + "\n", "", False)
        return CmdResult(128, "", "fatal: not a git repository", False)

    return _stub


def run_case(case: dict) -> str | None:
    sandbox = conftest.SandboxCtx(_golden_cfg() if _needs_deploy_refs(case) else None)
    try:
        cwd_kind = case.get("cwd_kind", "none")
        resolved_cwd = _resolve_cwd(sandbox, cwd_kind)
        sandbox.set_run_cmd(_run_cmd_stub(cwd_kind))
        payload = json.loads(json.dumps(case["payload"]))  # deep copy
        payload["cwd"] = str(resolved_cwd)
        ctx = sandbox.build(payload)

        result = git_discipline.run(payload, sandbox.cfg, ctx)

        if result.exit_code != case["expect_exit"]:
            return (
                f"{case['id']}: exit_code {result.exit_code} != "
                f"expect_exit {case['expect_exit']} (stderr={result.stderr!r})"
            )
        expect_contains = case.get("expect_stderr_contains")
        if expect_contains and expect_contains not in result.stderr:
            return f"{case['id']}: stderr missing {expect_contains!r} (got {result.stderr!r})"
        if not expect_contains and result.exit_code == 0 and result.stderr:
            return f"{case['id']}: unexpected stderr on ALLOW: {result.stderr!r}"
        return None
    finally:
        sandbox.cleanup()


def main() -> int:
    cases = load_fixtures()
    if len(cases) < 24 + 5 + 4 + 1:
        print(
            f"test_git_discipline: fixture count {len(cases)} below the CONTRACT #6 "
            "minimum (24 golden + >=5 destroyers + >=4 deploy_refs + PE-4)"
        )
        return 1
    failures = []
    for case in cases:
        failure = run_case(case)
        if failure:
            failures.append(failure)
    for f in failures:
        print("FAIL " + f)
    print(f"test_git_discipline: {len(cases) - len(failures)}/{len(cases)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
