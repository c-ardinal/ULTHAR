#!/usr/bin/env python3
"""hooks/tests/test_dev_server.py -- unit tests for guards/dev_server.py.

CONTRACT.md #6 'Fixture inventory (required coverage)' (A): dev_server had no
fixtures before the port; minimum >= 4. Calls the guard's `run()` DIRECTLY (no
subprocess -- that is the entire point of the in-process guard shape, acceptance
row 1) against every case in tests/fixtures/dev_server/*.json.

No pytest dependency; exposes `main() -> int` (0 pass / 1 fail) per run_all.py's
discovery contract.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
HOOKS_DIR = TESTS_DIR.parent
sys.path.insert(0, str(HOOKS_DIR))
sys.path.insert(0, str(TESTS_DIR))

from conftest import SandboxCtx, load_test_config  # noqa: E402
from guards import dev_server  # noqa: E402

FIXTURES_DIR = TESTS_DIR / "fixtures" / "dev_server"
SKILL_ROOT = HOOKS_DIR.parent


def _golden_cfg() -> dict:
    """The config-generalisation round: command_rules.dev_server ships EMPTY by default (the
    single 'dev-heavy-requires-heap' rule was one project's convention, moved to
    examples/user-overlay.example.json -- see config.json's
    _note_dev_server_empty). This fixture set still exercises the guard
    MECHANISM against that exact rule, sourced from the shipped example so the
    two never drift apart, rather than duplicating the rule text here."""
    example = json.loads(
        (SKILL_ROOT / "examples" / "user-overlay.example.json").read_text(encoding="utf-8")
    )
    cfg = copy.deepcopy(load_test_config())
    cfg["command_rules"]["dev_server"] = example["command_rules"]["dev_server"]
    return cfg


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


def load_cases() -> list[dict]:
    cases: list[dict] = []
    for f in sorted(FIXTURES_DIR.glob("*.json")):
        data = json.loads(f.read_text(encoding="utf-8"))
        entries = data if isinstance(data, list) else [data]
        cases += [e for e in entries if isinstance(e, dict) and "id" in e]
    return cases


def run_case(p: Probe, case: dict) -> None:
    sandbox = SandboxCtx(_golden_cfg())
    try:
        ctx = sandbox.build(payload=case["payload"])
        result = dev_server.run(case["payload"], sandbox.cfg, ctx)
        ok = result.exit_code == case["expect_exit"]
        detail = f"exit={result.exit_code} want={case['expect_exit']} stderr={result.stderr!r}"
        p.check(f"{case['id']}: exit_code", ok, detail)

        contains = case.get("expect_stderr_contains")
        if contains:
            p.check(
                f"{case['id']}: stderr contains {contains!r}",
                contains in result.stderr,
                repr(result.stderr),
            )
    finally:
        sandbox.cleanup()


# ------------------------------------------------------------------ regression checks


def check_target_field_is_honoured(p: Probe) -> None:
    """Pins `target` against being a dead ternary that always evaluates `raw`:
    it must actually select which probe of the command is tested. Proof: a synthetic
    rule with `target: "probe_quotes"` whose `requires` text appears ONLY inside a
    quoted string in the command. Under `raw` (the pre-fix behaviour, since both
    ternary branches returned `cmd`) the quoted mention is visible and the
    guard wrongly concludes the requirement is satisfied -> ALLOW. Under
    `probe_quotes` (post-fix) the quoted content is blanked first, so the
    mention is gone and the guard correctly BLOCKs."""
    cfg = copy.deepcopy(load_test_config())
    cfg["command_rules"]["dev_server"] = [
        {
            "id": "test-target-probe-quotes",
            "enabled": True,
            "type": "regex_require",
            "target": "probe_quotes",
            "pattern": "(?i)custom-dev",
            "requires": "(?i)max-old-space-size",
            "message": "BLOCKED (test): custom-dev needs max-old-space-size OUTSIDE quotes",
        }
    ]
    sandbox = SandboxCtx(cfg)
    try:
        cmd = 'npm run custom-dev --note "remember to set max-old-space-size later"'
        payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": cmd}}
        ctx = sandbox.build(payload=payload)
        result = dev_server.run(payload, cfg, ctx)
        p.check(
            "dev_server target: probe_quotes blanks the quoted mention, so "
            "'requires' correctly does NOT match -> BLOCK",
            result.exit_code == 2,
            str(result),
        )
    finally:
        sandbox.cleanup()


def check_unknown_rule_type_is_logged(p: Probe) -> None:
    """Pins an unrecognised `type` against being silently dropped: the loop's
    `continue` must log via `ctx.log_config_error`, naming the bad json_path,
    rather than skip with no trace."""
    cfg = copy.deepcopy(load_test_config())
    cfg["command_rules"]["dev_server"] = [
        {
            "id": "typo-rule",
            "enabled": True,
            "type": "regex_requrie",  # typo: should be regex_require
            "target": "raw",
            "pattern": "(?i)npm\\s+run\\s+dev:heavy",
            "requires": "(?i)max-old-space-size",
            "message": "should never fire",
        }
    ]
    sandbox = SandboxCtx(cfg)
    try:
        payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "npm run dev:heavy"}}
        ctx = sandbox.build(payload=payload)
        calls: list[tuple] = []
        ctx.log_config_error = lambda *a, **k: calls.append((a, k))
        dev_server.run(payload, cfg, ctx)
    finally:
        sandbox.cleanup()

    p.check(
        "dev_server: unrecognised rule type is logged via log_config_error",
        any("unrecognised rule type" in str(c) for c in calls),
        str(calls),
    )


def main() -> int:
    p = Probe()
    cases = load_cases()
    p.check(f"fixtures: at least 4 dev_server cases loaded ({len(cases)} found)", len(cases) >= 4)
    for case in cases:
        run_case(p, case)

    # A blank/whitespace command must never crash and must always allow (defensive
    # boundary check, not one of the .ps1's golden cases -- the guard's own
    # `if not cmd.strip(): return ALLOW` short-circuit).
    sandbox = SandboxCtx()
    try:
        ctx = sandbox.build()
        empty_payload = {"tool_input": {"command": "   "}}
        result = dev_server.run(empty_payload, sandbox.cfg, ctx)
        p.check("blank command -> ALLOW (no crash)", result.exit_code == 0)
    finally:
        sandbox.cleanup()

    print("\n== round-2 send-back regressions ==")
    check_target_field_is_honoured(p)
    check_unknown_rule_type_is_logged(p)

    print(f"\nRESULT: {p.passed} passed, {p.failed} failed")
    return 1 if p.failed else 0


if __name__ == "__main__":
    sys.exit(main())
