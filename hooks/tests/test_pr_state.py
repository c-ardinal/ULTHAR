"""hooks/tests/test_pr_state.py -- unit tests for guards/pr_state.py.

CONTRACT.md #1 (Ctx), #5 (U-2), #6 (fixture inventory: pr_state >=9, PE-4, PE-7).
Drives guards.pr_state.run() directly through a SandboxCtx (tests/conftest.py) --
no subprocess, no real `gh`/`git`, no pytest dependency (exposes main() -> int for
tests/run_all.py's discovery, exactly like tests/test_contract.py).

Fixture schema: the SAME shape tests/parity.py consumes (`id`, `guard`, `ps1`,
`payload`, `expect_exit`, `expect_stderr_contains`?, `parity`, `parity_exception`?)
plus test-only extension keys this module reads and tests/parity.py ignores:

  `cwd_kind`  -- "main" | "feature" | "none" | any other string -- same convention
                 as test_git_discipline.py; resolves `payload.cwd`'s placeholder and
                 what a FALLBACK `git rev-parse --abbrev-ref HEAD` answers (this
                 guard only shells out to it when the command's own tokens carry no
                 parseable branch). A value outside the three well-known kinds
                 (e.g. "my repo", deliberately containing a space) is used
                 verbatim as the seeded directory's name -- mirrors parity.py's
                 `_repo_path_for`.
  `${CWD}`    -- a literal placeholder token usable inside `payload.tool_input
                 .command` and `expect_gh_cwd` (the only two fields that need it),
                 substituted with this case's resolved cwd -- mirrors the ONE
                 placeholder of parity.py's ten-name `_placeholder_map` that this
                 module's fixtures actually use.
  `gh_stub`   -- {"rc": int, "timeout": bool, "response": list|None} -- controls
                 what the stubbed `gh pr list --json number,state,url` answers.
                 `response: None` means "no stdout" (matches the master's `if (-not
                 $out) { exit 0 }` empty-output fail-open path); `rc` non-zero or
                 `timeout: true` exercise the fail-open-on-error / fail-open-on-
                 timeout paths (thresholds.subprocess_timeout_remote_sec, PE-7).
  `expect_gh_cwd`          -- if present, asserts the `cwd` ctx.run_cmd received for
                               the `gh` call equals this (proves `-C <dir>` /
                               `dir_option` extraction actually reaches the call).
  `expect_gh_argv_contains` -- list of tokens that must all appear (in order, as a
                               contiguous or scattered subset) in the `gh` argv --
                               proves `{branch}`/`{limit}` substitution actually
                               happened rather than leaving the literal placeholders.
  `expect_no_gh_call`       -- if true, asserts `gh` was never invoked (proves an
                               ALLOW fired before the network call -- non-push,
                               branch-delete, sha-ref, HEAD sentinel).
  `gh_stub_by_head`         -- {branch: {"rc","timeout","response"}} -- when the gh
                               call's `--head <value>` argument is a key of this
                               dict, its sub-object is used INSTEAD of `gh_stub`.
                               Exists for the F-14 regression fixture below: a
                               command with multiple refspecs (`git push origin a
                               b`) must resolve branch 'a' (tokens[1], NOT tokens[-1]
                               = 'b') -- giving 'a' and 'b' DIFFERENT PR states is
                               what makes a tokens[1]->tokens[-1] mutation actually
                               change the fixture's outcome instead of merely
                               changing which branch name appears in a message.

The stub also asserts `kind` is exactly "remote" for `gh` and exactly "local" for
`git` -- CONTRACT #1 S-5 requires ctx.run_cmd's `kind` to be stated explicitly by
the guard, never defaulted, and a guard that silently swapped the two budgets is
precisely the PE-7 bug class this fixture set exists to catch.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
HOOKS_DIR = TESTS_DIR.parent
sys.path.insert(0, str(HOOKS_DIR))
sys.path.insert(0, str(TESTS_DIR))

import conftest  # noqa: E402
from guards import pr_state  # noqa: E402
from lib.ctx import CmdResult  # noqa: E402

FIXTURES_DIR = TESTS_DIR / "fixtures" / "pr_state"

_BRANCH_FOR_KIND = {
    "main": "main",
    "feature": "feature/x",
}


def load_fixtures() -> list[dict]:
    cases = []
    for f in sorted(FIXTURES_DIR.glob("*.json")):
        data = json.loads(f.read_text(encoding="utf-8"))
        entries = data if isinstance(data, list) else [data]
        for entry in entries:
            if entry.get("guard") == "pr_state":
                cases.append(entry)
    return cases


def _resolve_cwd(sandbox: "conftest.SandboxCtx", kind: str) -> Path:
    """"main"/"feature"/"none" keep their readable directory names; any other
    `cwd_kind` (e.g. "my repo", deliberately containing a space to exercise
    gitcmd.repo_dir's quoted `-C "<dir>"` parsing) is used verbatim as the
    directory name -- mirrors parity.py's _repo_path_for(name, sandbox_root),
    which accepts any string, not just the three well-known kinds."""
    name = {"main": "on-main", "feature": "on-feature", "none": "no-repo"}.get(kind, kind)
    p = sandbox.root / name
    p.mkdir(parents=True, exist_ok=True)
    return p


def _substitute_cwd_placeholder(value: str, resolved_cwd: Path) -> str:
    """Minimal, LOCAL echo of parity.py's `${CWD}` placeholder (the only one this
    module's fixtures use) -- so a fixture written for the live sweep (which
    substitutes `${CWD}` in the whole payload before running either side) behaves
    identically under this unit test's own direct-call harness."""
    return value.replace("${CWD}", str(resolved_cwd))


def _run_cmd_stub(cwd_kind: str, gh_stub: dict | None, calls: list, gh_stub_by_head: dict | None = None):
    gh_stub = gh_stub or {"rc": 0, "timeout": False, "response": None}
    gh_stub_by_head = gh_stub_by_head or {}

    def _stub(argv, kind, cwd=None):
        calls.append((list(argv), kind, str(cwd) if cwd is not None else None))
        if argv and argv[0] == "gh":
            assert kind == "remote", f"gh call used kind={kind!r}, want 'remote' (S-5)"
            head_value = argv[argv.index("--head") + 1] if "--head" in argv else None
            effective = gh_stub_by_head.get(head_value, gh_stub)
            if effective.get("timeout"):
                return CmdResult(-1, "", "", True)
            rc = effective.get("rc", 0)
            response = effective.get("response")
            out = "" if response is None else json.dumps(response)
            return CmdResult(rc, out, "" if rc == 0 else "gh: error", False)
        if argv and argv[0] == "git":
            assert kind == "local", f"git call used kind={kind!r}, want 'local' (S-5)"
            if cwd_kind in _BRANCH_FOR_KIND:
                return CmdResult(0, _BRANCH_FOR_KIND[cwd_kind] + "\n", "", False)
            return CmdResult(128, "", "fatal: not a git repository", False)
        raise AssertionError(f"unexpected argv[0]: {argv!r}")

    return _stub


def run_case(case: dict) -> str | None:
    sandbox = conftest.SandboxCtx()
    try:
        cwd_kind = case.get("cwd_kind", "feature")
        resolved_cwd = _resolve_cwd(sandbox, cwd_kind)
        calls: list = []
        sandbox.set_run_cmd(
            _run_cmd_stub(cwd_kind, case.get("gh_stub"), calls, case.get("gh_stub_by_head"))
        )
        payload = json.loads(json.dumps(case["payload"]))  # deep copy
        cmd = payload.get("tool_input", {}).get("command")
        if cmd is not None:
            payload["tool_input"]["command"] = _substitute_cwd_placeholder(cmd, resolved_cwd)
        payload["cwd"] = str(resolved_cwd)
        ctx = sandbox.build(payload)

        try:
            result = pr_state.run(payload, sandbox.cfg, ctx)
        except AssertionError as exc:
            return f"{case['id']}: {exc}"

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

        gh_calls = [c for c in calls if c[0] and c[0][0] == "gh"]
        if case.get("expect_no_gh_call"):
            if gh_calls:
                return f"{case['id']}: expected no gh call, got {gh_calls}"
        if "expect_gh_cwd" in case:
            if not gh_calls:
                return f"{case['id']}: expect_gh_cwd set but gh was never called"
            got_cwd = gh_calls[0][2]
            wanted_cwd = _substitute_cwd_placeholder(case["expect_gh_cwd"], resolved_cwd)
            # normalise separators -- the fixture writes a forward-slash path
            if got_cwd.replace("\\", "/") != wanted_cwd.replace("\\", "/"):
                return f"{case['id']}: gh cwd {got_cwd!r} != expect_gh_cwd {wanted_cwd!r}"
        if "expect_gh_argv_contains" in case:
            if not gh_calls:
                return f"{case['id']}: expect_gh_argv_contains set but gh was never called"
            argv = gh_calls[0][0]
            for tok in case["expect_gh_argv_contains"]:
                if tok not in argv:
                    return f"{case['id']}: gh argv {argv} missing token {tok!r}"
        return None
    finally:
        sandbox.cleanup()


def test_match_target_is_enforced() -> str | None:
    """Pins git_policy.pr_state.match_target against being a decorative config
    key: a pre-fix guard ignored it entirely, so both runs below returned the
    identical BLOCK result. Proof of consumption: match_target="raw" blocks
    normally, while an unrecognised value must raise (turned into ALLOW by
    dispatch.py's fail-open) -- observably different from a silent no-op."""
    sandbox = conftest.SandboxCtx()
    try:
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": "git push origin feature/merged-out"},
            "cwd": str(_resolve_cwd(sandbox, "feature")),
        }
        gh_stub = {
            "rc": 0,
            "timeout": False,
            "response": [{"number": 5, "state": "MERGED", "url": "https://x/pull/5"}],
        }

        sandbox.set_run_cmd(_run_cmd_stub("feature", gh_stub, []))
        ctx_raw = sandbox.build(dict(payload))
        result_raw = pr_state.run(dict(payload), sandbox.cfg, ctx_raw)
        if result_raw.exit_code != 2:
            return (
                "test_match_target_is_enforced: baseline (match_target='raw') "
                f"did not BLOCK as expected -- exit_code={result_raw.exit_code}"
            )

        cfg_bogus = json.loads(json.dumps(sandbox.cfg))
        cfg_bogus["git_policy"]["pr_state"]["match_target"] = "bogus-unrecognised-value"
        sandbox2 = conftest.SandboxCtx(cfg=cfg_bogus)
        sandbox2.set_run_cmd(_run_cmd_stub("feature", gh_stub, []))
        ctx_bogus = sandbox2.build(dict(payload))
        try:
            pr_state.run(dict(payload), sandbox2.cfg, ctx_bogus)
        except ValueError:
            pass  # EXPECTED: consuming the key means an unrecognised value is loud
        else:
            return (
                "test_match_target_is_enforced: match_target='bogus-unrecognised-value' "
                "was silently ignored -- the config key is decorative (round-2 item 1 "
                "regression)"
            )
        return None
    finally:
        sandbox.cleanup()


def main() -> int:
    cases = load_fixtures()
    if len(cases) < 9 + 3:  # >=9 golden + PE-4 + PE-7 + F-14
        print(
            f"test_pr_state: fixture count {len(cases)} below the CONTRACT #6 "
            "minimum (>=9 golden + PE-4 + PE-7 + F-14)"
        )
        return 1
    failures = []
    for case in cases:
        failure = run_case(case)
        if failure:
            failures.append(failure)

    match_target_failure = test_match_target_is_enforced()
    if match_target_failure:
        failures.append(match_target_failure)

    for f in failures:
        print("FAIL " + f)
    total = len(cases) + 1  # +1 for test_match_target_is_enforced
    print(f"test_pr_state: {total - len(failures)}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
