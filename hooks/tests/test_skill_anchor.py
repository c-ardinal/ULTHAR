#!/usr/bin/env python3
"""hooks/tests/test_skill_anchor.py -- U-3's guard-level test suite for
guards/skill_anchor.py.

CONTRACT.md #5 (U-3 row), #6 (the 13 test-skill-anchor.ps1 checks, the Write/Edit
branch >=6 minimum, the max_chars truncation fixture), #7 (PE-2, PE-3), I-2 (this
guard must NEVER block -- every case here asserts exit_code == 0; there is no block
assertion anywhere in this file, on purpose).

Two of the original 13 golden checks ("ANCHOR.md exists" / "is small (<=1200 chars)")
inspected the REAL installed skill's ANCHOR.md on disk. Per this
unit's instructions ("tests must not read or write the real ~/.claude/state or the
real skills tree"), those two are NOT ported as-is; their intent -- "the size ceiling
is enforced" -- is covered instead by the dedicated max_chars truncation fixture
below, against a synthetic sandboxed skill, which is a strictly stronger check (it
proves truncation actually fires, not just that one file happens to be short today).

Runs guards/skill_anchor.py DIRECTLY against a sandboxed Ctx and asserts on
Result.stdout content -- this is the "explicit evidence that skill_anchor's two
stdout shapes are both correct" the done-report requires. NOTE (flagged, not fixed
here): tests/parity.py's run_case() only compares exit code and stderr; since this
guard's exit code is invariantly 0, parity.py as currently written CANNOT detect a
regression in PE-2 or PE-3 for this guard (the divergence is stdout-content-only).
That gap is called out in the done-report; parity.py is owned by U-0.

    python tests/test_skill_anchor.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
HOOKS_DIR = TESTS_DIR.parent
FIXTURES = TESTS_DIR / "fixtures" / "write_edit" / "skill_anchor.json"

sys.path.insert(0, str(HOOKS_DIR))
sys.path.insert(0, str(TESTS_DIR))

from guards import skill_anchor  # noqa: E402
from conftest import SandboxCtx  # noqa: E402
import dispatch as dispatch_mod  # noqa: E402


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
        "${MAIN_TRANSCRIPT}": str(sandbox.home / ".claude" / "projects" / "P" / "abc.jsonl"),
        "${SUB_TRANSCRIPT}": str(sandbox.home / ".claude" / "projects" / "P" / "abc" / "subagents" / "def.jsonl"),
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


def _apply_setup(sandbox: SandboxCtx, setup: dict) -> None:
    if "active_skills" in setup:
        list_path = sandbox.cwd / ".claude" / "state" / "active-skills"
        list_path.parent.mkdir(parents=True, exist_ok=True)
        list_path.write_text(setup["active_skills"], encoding="utf-8")
    for name, spec in setup.get("anchors", {}).items():
        if "repeat" in spec:
            text = spec["repeat"] * spec["length"]
        else:
            text = spec["text"]
        anchor_dir = sandbox.home / ".claude" / "skills" / name
        anchor_dir.mkdir(parents=True, exist_ok=True)
        (anchor_dir / "ANCHOR.md").write_text(text, encoding="utf-8")


def run_fixture(p: Probe, case: dict) -> None:
    sandbox = SandboxCtx()
    try:
        tokens = _tokens(sandbox)
        _apply_setup(sandbox, case.get("setup", {}))
        payload = _resolve(case["payload"], tokens)
        assert "tool_name" in payload, f"{case['id']}: fixture payload missing tool_name (misreading #9)"

        ctx = sandbox.build(payload, event=case.get("event", "PreToolUse"))
        if case.get("via") == "dispatch_route":
            # PE-20: proves the EVENT ROUTING (config.dispatch.events), not the
            # guard function, produces no output -- skill_anchor.run() would
            # still process a PostCompact payload if called directly, so a
            # case meant to pin "the registration was deleted" must go
            # through dispatch() itself, exactly as dispatch.py's main() does.
            result = dispatch_mod.dispatch(case["event"], payload, sandbox.cfg, ctx, None)
        else:
            result = skill_anchor.run(payload, sandbox.cfg, ctx)

        p.check(
            f"{case['id']}: exit_code == {case['expect_exit']}",
            result.exit_code == case["expect_exit"],
            f"got exit_code={result.exit_code!r}",
        )
        p.check(f"{case['id']}: I-2 never blocks (stderr empty)", result.stderr == "", repr(result.stderr))

        if "expect_stdout" in case:
            p.check(
                f"{case['id']}: stdout == {case['expect_stdout']!r}",
                result.stdout == case["expect_stdout"],
                f"got stdout={result.stdout!r}",
            )
        for needle in case.get("expect_stdout_contains", []):
            p.check(
                f"{case['id']}: stdout contains {needle!r}",
                needle in result.stdout,
                f"stdout={result.stdout!r}",
            )
        for needle in case.get("expect_stdout_not_contains", []):
            p.check(
                f"{case['id']}: stdout does NOT contain {needle!r}",
                needle not in result.stdout,
                f"stdout={result.stdout!r}",
            )
    finally:
        sandbox.cleanup()


def test_json_envelope_shape_and_key_order(p: Probe) -> None:
    """I-2's exact envelope shape: {"hookSpecificOutput": {"hookEventName": ...,
    "additionalContext": ...}}, compact (no spaces), UTF-8, key order preserved --
    parsed back and checked field-by-field, not just substring-matched."""
    sandbox = SandboxCtx()
    try:
        (sandbox.home / ".claude" / "state" / "active-skills").write_text("test-skill\n", encoding="utf-8")
        anchor_dir = sandbox.home / ".claude" / "skills" / "test-skill"
        anchor_dir.mkdir(parents=True, exist_ok=True)
        (anchor_dir / "ANCHOR.md").write_text("anchor body text", encoding="utf-8")

        payload = {"tool_name": "Agent", "cwd": str(sandbox.cwd), "hook_event_name": "PreToolUse"}
        ctx = sandbox.build(payload, event="PreToolUse")
        result = skill_anchor.run(payload, sandbox.cfg, ctx)

        p.check("envelope: no spaces (compact, matches ConvertTo-Json -Compress)",
                "  " not in result.stdout and ": " not in result.stdout and ", " not in result.stdout,
                result.stdout)
        p.check("envelope: no trailing newline (I-2 raw-bytes write)",
                not result.stdout.endswith("\n"), repr(result.stdout[-5:]))
        parsed = json.loads(result.stdout)
        p.check("envelope: top-level key order", list(parsed.keys()) == ["hookSpecificOutput"])
        inner = parsed["hookSpecificOutput"]
        p.check("envelope: inner key order",
                list(inner.keys()) == ["hookEventName", "additionalContext"], list(inner.keys()))
        p.check("envelope: hookEventName == PreToolUse", inner["hookEventName"] == "PreToolUse")
        p.check("envelope: additionalContext carries the anchor body", inner["additionalContext"] == "anchor body text")
    finally:
        sandbox.cleanup()


def test_sessionstart_is_raw_text_not_json(p: Probe) -> None:
    sandbox = SandboxCtx()
    try:
        (sandbox.home / ".claude" / "state" / "active-skills").write_text("test-skill\n", encoding="utf-8")
        anchor_dir = sandbox.home / ".claude" / "skills" / "test-skill"
        anchor_dir.mkdir(parents=True, exist_ok=True)
        (anchor_dir / "ANCHOR.md").write_text("plain anchor body", encoding="utf-8")

        payload = {"tool_name": "", "cwd": str(sandbox.cwd), "hook_event_name": "SessionStart"}
        ctx = sandbox.build(payload, event="SessionStart")
        result = skill_anchor.run(payload, sandbox.cfg, ctx)

        p.check("SessionStart: stdout is exactly the trimmed anchor body (raw text)",
                result.stdout == "plain anchor body", repr(result.stdout))
        try:
            json.loads(result.stdout)
            is_json = True
        except json.JSONDecodeError:
            is_json = False
        p.check("SessionStart: stdout does NOT parse as the envelope JSON", not is_json)
    finally:
        sandbox.cleanup()


def test_max_chars_truncation_exact(p: Probe) -> None:
    """Exact-length check for the max_chars truncation fixture: body is cut to
    EXACTLY max_chars characters, then truncation_suffix appended verbatim."""
    sandbox = SandboxCtx()
    try:
        (sandbox.home / ".claude" / "state" / "active-skills").write_text("big-skill\n", encoding="utf-8")
        anchor_dir = sandbox.home / ".claude" / "skills" / "big-skill"
        anchor_dir.mkdir(parents=True, exist_ok=True)
        (anchor_dir / "ANCHOR.md").write_text("B" * 1300, encoding="utf-8")

        payload = {"tool_name": "Agent", "cwd": str(sandbox.cwd), "hook_event_name": "PreToolUse"}
        ctx = sandbox.build(payload, event="PreToolUse")
        result = skill_anchor.run(payload, sandbox.cfg, ctx)

        max_chars = sandbox.cfg["skill_anchor"]["max_chars"]
        suffix = sandbox.cfg["skill_anchor"]["truncation_suffix"]
        parsed = json.loads(result.stdout)
        body = parsed["hookSpecificOutput"]["additionalContext"]
        p.check(f"truncation: body cut to exactly max_chars ({max_chars})",
                body == "B" * max_chars + suffix, f"len={len(body)}")
    finally:
        sandbox.cleanup()


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass  # this test's OWN console output only -- not the guard under test

    p = Probe()
    cases = json.loads(FIXTURES.read_text(encoding="utf-8"))
    print(f"== guards/skill_anchor.py -- {len(cases)} fixtures from {FIXTURES.name} ==")
    for case in cases:
        run_fixture(p, case)

    print("\n== additional sanity checks (I-2 stdout-shape evidence) ==")
    test_json_envelope_shape_and_key_order(p)
    test_sessionstart_is_raw_text_not_json(p)
    test_max_chars_truncation_exact(p)

    print(f"\nRESULT: {p.passed} passed, {p.failed} failed")
    return 1 if p.failed else 0


if __name__ == "__main__":
    sys.exit(main())
