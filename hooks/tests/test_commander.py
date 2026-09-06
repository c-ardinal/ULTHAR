#!/usr/bin/env python3
"""hooks/tests/test_commander.py -- U-3's guard-level test suite for
guards/commander.py.

CONTRACT.md #5 (U-3 row), #6 (fixture inventory: the 10 golden
test-commander-guard.ps1 cases, migrated with `tool_name` per misreading #9), #7
(PE-3). Runs guards/commander.py DIRECTLY against a sandboxed Ctx (no dispatch.py
subprocess, no real PowerShell) -- fast, and independent of tests/parity.py, which
is Windows-only and needs the real .ps1 originals. tests/fixtures/write_edit/*.json
remain the format parity.py consumes when it does run.

    python tests/test_commander.py

Exit 0 = all pass, 1 = at least one failure. No pytest dependency (project
convention, see test_contract.py's Probe).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
HOOKS_DIR = TESTS_DIR.parent
FIXTURES = TESTS_DIR / "fixtures" / "write_edit" / "commander.json"

sys.path.insert(0, str(HOOKS_DIR))
sys.path.insert(0, str(TESTS_DIR))

from guards import commander  # noqa: E402
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
        "${TEMP}": str(sandbox.temp_dir),
        "${HOME}": str(sandbox.home),
        "${DEPLOY_DIR}": str(sandbox.deploy_dir),
        "${MAIN_TRANSCRIPT}": str(sandbox.home / ".claude" / "projects" / "P" / "abc.jsonl"),
        "${SUB_TRANSCRIPT}": str(sandbox.home / ".claude" / "projects" / "P" / "abc" / "subagents" / "def.jsonl"),
        "${ELSEWHERE}": str(sandbox.root / "elsewhere"),
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


def _apply_marker(sandbox: SandboxCtx, marker: str | None) -> None:
    if marker is None:
        return
    marker_path = sandbox.cwd / ".claude" / "state" / "commander-mode"
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(marker, encoding="utf-8")


def run_fixture(p: Probe, case: dict) -> None:
    sandbox = SandboxCtx()
    try:
        tokens = _tokens(sandbox)
        _apply_marker(sandbox, case.get("marker"))
        payload = _resolve(case["payload"], tokens)
        assert "tool_name" in payload, f"{case['id']}: fixture payload missing tool_name (misreading #9)"

        ctx = sandbox.build(payload, event=case.get("event", "PreToolUse"))
        result = commander.run(payload, sandbox.cfg, ctx)

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
        p.check(f"{case['id']}: skill_anchor never a stdout emitter here", result.stdout == "")
    finally:
        sandbox.cleanup()


def test_pe3_call_site_is_commander(p: Probe) -> None:
    """Sanity check that the PE-3 fixture actually exercises paths.is_under() rather
    than accidentally passing for an unrelated reason (e.g. an exempt_patterns hit)."""
    sandbox = SandboxCtx()
    try:
        tokens = _tokens(sandbox)
        _apply_marker(sandbox, "enforce")
        file_path = str(sandbox.cwd) + "-backup/x.ts"
        payload = {
            "tool_name": "Edit",
            "tool_input": {"file_path": file_path},
            "cwd": str(sandbox.cwd),
            "transcript_path": tokens["${MAIN_TRANSCRIPT}"],
        }
        ctx = sandbox.build(payload)
        result = commander.run(payload, sandbox.cfg, ctx)
        p.check(
            "PE-3 sanity: repo-backup sibling is exempt (is_under, not StartsWith)",
            result.exit_code == 0,
            f"got exit_code={result.exit_code!r} stderr={result.stderr!r}",
        )
    finally:
        sandbox.cleanup()


def test_decision_log_is_bounded_and_keeps_the_newest(p: Probe) -> None:
    """The bounded-log mechanism (lib/io.append_jsonl_drop_oldest) landed for
    paths.error_log / logging.config_error_log this session but missed
    commander.decision_log -- the one writer that grows in NORMAL operation
    (one line per main-chain Write/Edit the guard evaluates), not only on
    faults. Real byte counts, on a real file, through commander.run() itself --
    not a direct lib/io call -- so this fails if the wiring regresses even if
    lib/io.append_jsonl_drop_oldest itself stays correct."""
    sandbox = SandboxCtx()
    try:
        _apply_marker(sandbox, "observe")  # log-only: never blocks, always logs
        cap = 2048
        sandbox.cfg["commander"]["max_decision_log_bytes"] = cap
        n = 100
        for i in range(n):
            payload = {
                "tool_name": "Edit",
                "tool_input": {"file_path": str(sandbox.cwd / f"file{i}.py")},
                "cwd": str(sandbox.cwd),
                "transcript_path": str(sandbox.home / ".claude" / "projects" / "P" / "abc.jsonl"),
            }
            ctx = sandbox.build(payload)
            result = commander.run(payload, sandbox.cfg, ctx)
            p.check(f"decision-log cap: call {i} still resolves (exit_code in {{0,2}})",
                    result.exit_code in (0, 2), f"exit_code={result.exit_code!r}") if i == 0 else None

        log_path = ctx.expand(sandbox.cfg["commander"]["decision_log"])
        size = log_path.stat().st_size
        p.check(f"decision_log: {n} calls under a {cap}B cap -> file is {size}B <= {cap}B",
                size <= cap, f"size={size}")

        lines = [json.loads(ln) for ln in log_path.read_text(encoding="utf-8").splitlines()]
        p.check("decision_log: entries kept are the LAST calls (newest), not the first",
                lines[-1]["file"].endswith(f"file{n - 1}.py"), str(lines[-1]))
        p.check("decision_log: no cap-footer marker (this cap never freezes the log)",
                not any(ln.get("_ulthar_log_capped") for ln in lines), str(lines[-1]))
    finally:
        sandbox.cleanup()


def main() -> int:
    p = Probe()
    cases = json.loads(FIXTURES.read_text(encoding="utf-8"))
    print(f"== guards/commander.py -- {len(cases)} fixtures from {FIXTURES.name} ==")
    for case in cases:
        run_fixture(p, case)

    print("\n== additional sanity checks ==")
    test_pe3_call_site_is_commander(p)
    test_decision_log_is_bounded_and_keeps_the_newest(p)

    print(f"\nRESULT: {p.passed} passed, {p.failed} failed")
    return 1 if p.failed else 0


if __name__ == "__main__":
    sys.exit(main())
