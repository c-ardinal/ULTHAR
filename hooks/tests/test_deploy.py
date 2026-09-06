#!/usr/bin/env python3
"""hooks/tests/test_deploy.py -- U-3's guard-level test suite for guards/deploy.py.

CONTRACT.md #5 (U-3 row), #6 (A: deploy needs >=6 fixtures -- .ps1, .py, nested
guards/ path, logs/x.ps1 recursion (M-2), master path, unrelated path), #7 (PE-5,
PE-10). Runs guards/deploy.py DIRECTLY against a sandboxed Ctx.

    python tests/test_deploy.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
HOOKS_DIR = TESTS_DIR.parent
FIXTURES = TESTS_DIR / "fixtures" / "write_edit" / "deploy.json"

sys.path.insert(0, str(HOOKS_DIR))
sys.path.insert(0, str(TESTS_DIR))

from guards import deploy  # noqa: E402
from conftest import SandboxCtx  # noqa: E402


class Probe:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0

    def check(self, name: str, cond: bool, detail: str = "") -> None:
        if cond:
            self.passed += 1
            print(f"OK   {name}")
        else:
            self.failed += 1
            print(f"FAIL {name}" + (f"\n       {detail}" if detail else ""))


def _tokens(sandbox: SandboxCtx) -> dict:
    return {
        "${CWD}": str(sandbox.cwd),
        "${DEPLOY_DIR}": str(sandbox.deploy_dir),
        "${SKILL_HOOKS_DIR}": str(sandbox.root / "skill-source" / "hooks"),
    }


def _resolve(obj, tokens: dict):
    if isinstance(obj, str):
        for k, v in tokens.items():
            obj = obj.replace(k, v)
        return obj
    if isinstance(obj, dict):
        return {k: _resolve(v, tokens) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve(v, tokens) for v in obj]
    return obj


def run_fixture(p: Probe, case: dict) -> None:
    sandbox = SandboxCtx()
    try:
        tokens = _tokens(sandbox)
        payload = _resolve(case["payload"], tokens)
        assert "tool_name" in payload, f"{case['id']}: fixture payload missing tool_name (misreading #9)"

        ctx = sandbox.build(payload, event=case.get("event", "PreToolUse"))
        result = deploy.run(payload, sandbox.cfg, ctx)

        p.check(
            f"{case['id']}: exit_code == {case['expect_exit']}",
            result.exit_code == case["expect_exit"],
            f"got exit_code={result.exit_code!r} stderr={result.stderr!r}",
        )
        expect_contains = case.get("expect_stderr_contains")
        if expect_contains:
            p.check(
                f"{case['id']}: stderr contains {expect_contains!r}",
                expect_contains in result.stderr,
                f"stderr={result.stderr!r}",
            )
    finally:
        sandbox.cleanup()


def test_pe5_recursive_at_any_depth(p: Probe) -> None:
    """M-2 sanity, beyond the fixture file: a file three levels deep under deploy_dir
    is still caught -- deploy_guard.recursive is not an enumerated subdirectory
    allowlist, matching -like's cross-separator '*' in the master."""
    sandbox = SandboxCtx()
    try:
        deep = sandbox.deploy_dir / "guards" / "sub" / "deeper" / "x.py"
        payload = {"tool_name": "Write", "tool_input": {"file_path": str(deep)}, "cwd": str(sandbox.cwd)}
        ctx = sandbox.build(payload)
        result = deploy.run(payload, sandbox.cfg, ctx)
        p.check(
            "M-2 sanity: three levels deep under deploy_dir is still blocked",
            result.exit_code == 2,
            f"got exit_code={result.exit_code!r}",
        )
    finally:
        sandbox.cleanup()


def test_windows_case_insensitive_extension(p: Probe) -> None:
    """regex.kinds.path parity: extension/name matching is case-insensitive on
    Windows (PowerShell -like's default), matching PS3 and every other path-kind
    comparison in this port."""
    sandbox = SandboxCtx()
    try:
        f = sandbox.deploy_dir / "dispatch.PS1"
        payload = {"tool_name": "Write", "tool_input": {"file_path": str(f)}, "cwd": str(sandbox.cwd)}
        ctx = sandbox.build(payload)
        result = deploy.run(payload, sandbox.cfg, ctx)
        expected = 2 if ctx.is_windows else 0
        p.check(
            f"extension case-insensitivity matches platform rule (is_windows={ctx.is_windows})",
            result.exit_code == expected,
            f"got exit_code={result.exit_code!r}",
        )
    finally:
        sandbox.cleanup()


def main() -> int:
    p = Probe()
    cases = json.loads(FIXTURES.read_text(encoding="utf-8"))
    print(f"== guards/deploy.py -- {len(cases)} fixtures from {FIXTURES.name} ==")
    for case in cases:
        run_fixture(p, case)

    print("\n== additional sanity checks ==")
    test_pe5_recursive_at_any_depth(p)
    test_windows_case_insensitive_extension(p)

    print(f"\nRESULT: {p.passed} passed, {p.failed} failed")
    return 1 if p.failed else 0


if __name__ == "__main__":
    sys.exit(main())
