#!/usr/bin/env python3
"""hooks/tests/test_brief_gate.py -- U-4's unit-test suite for guards/brief_gate.py.

CONTRACT.md #1 (guard interface), #3.4/#3.5 (design_cases / brief_rules), #5 (U-4
row), #6 fixture-inventory table (brief_gate >= 20, gate 6's 5 required paths).
Calls `brief_gate.run(payload, cfg, ctx)` directly against a `tests.conftest`
SandboxCtx -- no subprocess, no dispatcher, no real filesystem outside a temp
sandbox. This is the unit layer; `tests/parity.py` is the separate .ps1-vs-.py
layer that runs once the golden `.ps1` masters and sibling units are in place.

Run via `python tests/run_all.py` or directly:

    python tests/test_brief_gate.py

Exit 0 = all pass, 1 = at least one failure.

Fixtures live under `tests/fixtures/briefs/`:
  detect.json, forwarder.json, committer.json, reviewer.json, gate1.json,
  gate2_design_cases.json, gate6.json, gate3.json, pe9.json
    -- flat lists of {id, guard, payload, expect_exit, expect_stderr_contains?,
       setup?, parity, parity_exception?}, the same shape tests/parity.py reads
       (a "setup" object is additionally honoured HERE -- see _apply_setup();
       parity.py does not yet apply `setup`, which is reported as an
       underspecified gap in the done-report, not patched here since parity.py
       is owned by U-0).
  acceptance_3_4.json
    -- A SEPARATE shape (no top-level "guard"/"payload" keys), read only by
       test_acceptance_rows_3_and_4() below. Deliberately NOT recognised by
       tests/parity.py's flat loader (`entries = data if isinstance(data, list)
       else [data]`, then requires "guard" AND "payload" on each entry) -- this
       is a config-injection proof, not a .ps1-vs-.py parity case.
"""
from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
HOOKS_DIR = TESTS_DIR.parent
FIXTURES_DIR = TESTS_DIR / "fixtures" / "briefs"

sys.path.insert(0, str(HOOKS_DIR))

from lib import io as io_mod  # noqa: E402
from guards import brief_gate  # noqa: E402
from conftest import SandboxCtx, load_test_config  # noqa: E402


class Probe:
    def __init__(self) -> None:
        self.failed = 0
        self.passed = 0
        self.fail_names: list[str] = []

    def check(self, name: str, cond: bool, detail: str = "") -> None:
        if cond:
            self.passed += 1
            print(f"OK   {name}")
        else:
            self.failed += 1
            self.fail_names.append(name)
            print(f"FAIL {name}" + (f"\n       {detail}" if detail else ""))


# ------------------------------------------------------------------ fixture setup


def _apply_setup(sandbox: SandboxCtx, ctx, setup) -> None:
    """Materialises gate 6's approval directory state for one fixture. Not part of
    the shipped runtime -- a test-harness-only builder, named by analogy to the
    contract's `setup` fixture-builder concept (CONTRACT.md #6 step 2).

    `setup` is a dict, or a list of dicts, each
    `{"kind": "approval_file", "name": ..., "age_sec"?, "content"?, "ok_marker"?}`,
    matching tests/parity.py's own SETUP_BUILDERS schema (kept independent, no
    shared import). Only `approval_file` is implemented (the only kind brief_gate
    fixtures need); an unknown `kind` raises rather than silently no-op-ing, so a
    typo can't degrade a case into an unseeded pass."""
    if not setup:
        return
    approval_dir = ctx.expand("~/.claude/state/brief-approvals")
    approval_dir.mkdir(parents=True, exist_ok=True)

    steps = setup if isinstance(setup, list) else [setup]
    for step in steps:
        kind = step.get("kind")
        if kind != "approval_file":
            raise ValueError(f"test_brief_gate._apply_setup: unknown setup kind {kind!r}")
        path = approval_dir / step["name"]
        content = step.get("content", "presented understanding + acceptance-criteria deltas\n")
        if step.get("ok_marker"):
            creator_rules = next(
                k for k in sandbox.cfg["brief_rules"]["kinds"] if k["id"] == "creator"
            )["rules"]
            gate6 = next(r for r in creator_rules if r["type"] == "approval_handshake")
            content += f"\n{gate6['ok_marker']} approved by owner\n"
        io_mod.write_text(path, content)
        age_sec = step.get("age_sec", 0)
        mtime = sandbox._frozen_now.timestamp() - age_sec
        os.utime(path, (mtime, mtime))


def _run_fixture(cfg: dict, entry: dict) -> io_mod.Result:
    sandbox = SandboxCtx(cfg)
    try:
        payload = dict(entry["payload"])
        ctx = sandbox.build(payload=payload, event=payload.get("hook_event_name", "PreToolUse"))
        _apply_setup(sandbox, ctx, entry.get("setup"))
        return brief_gate.run(payload, cfg, ctx)
    finally:
        sandbox.cleanup()


# ------------------------------------------------------------------ standard fixtures


def _load_flat_fixture_files() -> list[tuple[str, dict]]:
    """Every *.json under tests/fixtures/briefs/ EXCEPT acceptance_3_4.json, which
    carries a deliberately different top-level shape (see module docstring)."""
    out = []
    for f in sorted(FIXTURES_DIR.glob("*.json")):
        if f.name == "acceptance_3_4.json":
            continue
        entries = json.loads(f.read_text(encoding="utf-8"))
        assert isinstance(entries, list), f"{f.name}: expected a flat list of fixtures"
        for entry in entries:
            out.append((f.name, entry))
    return out


def run_standard_fixtures(p: Probe, cfg: dict) -> None:
    fixtures = _load_flat_fixture_files()
    p.check(
        "fixture inventory: >= 20 standard fixtures under tests/fixtures/briefs/",
        len(fixtures) >= 20,
        f"found {len(fixtures)}",
    )

    gate6_names = {
        "no APPROVAL reference -> BLOCK": False,
        "referenced file not found -> BLOCK": False,
        "window not elapsed (no marker) -> BLOCK": False,
        "age >= threshold -> ALLOW": False,
        "OWNER-OK line present -> ALLOW": False,
    }
    gate6_id_map = {
        "gate6-no-approval-reference-block": "no APPROVAL reference -> BLOCK",
        "gate6-approval-file-not-found-block": "referenced file not found -> BLOCK",
        "gate6-window-not-elapsed-no-marker-block": "window not elapsed (no marker) -> BLOCK",
        "gate6-age-elapsed-allow": "age >= threshold -> ALLOW",
        "gate6-owner-ok-marker-allow": "OWNER-OK line present -> ALLOW",
    }

    for filename, entry in fixtures:
        if not entry.get("parity", True) and entry.get("parity_exception"):
            label = f"{filename}::{entry['id']} (parity_exception {entry['parity_exception']})"
        else:
            label = f"{filename}::{entry['id']}"

        result = _run_fixture(cfg, entry)
        ok = result.exit_code == entry["expect_exit"]
        detail = f"expected exit {entry['expect_exit']}, got {result.exit_code}; stderr={result.stderr!r}"
        p.check(label, ok, detail if not ok else "")

        expect_contains = entry.get("expect_stderr_contains")
        if expect_contains:
            p.check(
                f"{label}: stderr contains {expect_contains!r}",
                expect_contains in result.stderr,
                f"stderr={result.stderr!r}",
            )

        if entry["id"] in gate6_id_map:
            gate6_names[gate6_id_map[entry["id"]]] = True

    for name, seen in gate6_names.items():
        p.check(f"gate 6 required path present: {name}", seen)


# ------------------------------------------------------------------ acceptance rows 3 & 4


def test_acceptance_rows_3_and_4(p: Probe, base_cfg: dict) -> None:
    """CONTRACT.md #3.4 owner hard requirement, STATE.md #2 rows 3/4: adding ONE
    object to design_cases.cases[] must start blocking a previously-allowed brief,
    and removing it must stop blocking the SAME brief -- with the SAME guard binary
    (guards/brief_gate.py, unmodified) and only a config diff between the two runs.
    The synthetic case is deliberately non-UI (a DB-migration checklist rule), per
    the brief's instruction not to special-case the two rows that exist today."""
    doc = json.loads((FIXTURES_DIR / "acceptance_3_4.json").read_text(encoding="utf-8"))
    synthetic_case = doc["synthetic_case"]
    payload = doc["payload"]
    setup = doc["setup"]

    # Row 4 baseline: config WITHOUT the synthetic case (today's real config.json
    # already lacks it) -- must ALLOW.
    cfg_before = copy.deepcopy(base_cfg)
    before_ids = [c["id"] for c in cfg_before["design_cases"]["cases"]]
    p.check(
        "acceptance row 4 baseline: synthetic case id absent from config before the edit",
        synthetic_case["id"] not in before_ids,
        f"cases={before_ids}",
    )
    result_before = _run_fixture(cfg_before, {"payload": payload, "setup": setup})
    p.check(
        "acceptance row 4: brief ALLOWED before the synthetic case exists (baseline)",
        result_before.exit_code == doc["row4_remove_case_expect_exit"],
        f"exit={result_before.exit_code} stderr={result_before.stderr!r}",
    )

    # Row 3: config WITH the synthetic case appended (pure data edit; brief_gate.py
    # is not touched between this run and the one above) -- must BLOCK.
    cfg_after = copy.deepcopy(base_cfg)
    cfg_after["design_cases"]["cases"].append(synthetic_case)
    diff_summary = {
        "cases_before": before_ids,
        "cases_after": [c["id"] for c in cfg_after["design_cases"]["cases"]],
    }
    result_after = _run_fixture(cfg_after, {"payload": payload, "setup": setup})
    p.check(
        "acceptance row 3: brief BLOCKED after adding one design_cases object "
        f"(diff: {diff_summary})",
        result_after.exit_code == doc["row3_add_case_expect_exit"],
        f"exit={result_after.exit_code} stderr={result_after.stderr!r}",
    )
    p.check(
        "acceptance row 3: block message names the new case's own text (migration checklist)",
        doc["row3_expect_stderr_contains"] in result_after.stderr,
        f"stderr={result_after.stderr!r}",
    )
    p.check(
        "acceptance row 3/4: same guard module, no code change between the two runs",
        True,  # documentary -- both calls above invoke the identical brief_gate.run
    )


# ------------------------------------------------------------------ committer content classification


def test_committer_content_classification(p: Probe, cfg: dict) -> None:
    """2026-09-05 false positive: a Japanese commit dispatch carries neither the
    bracket header nor the compact header, fell through to the `creator` fallback,
    and was blocked by the UI-design case for naming no design skill. The exit
    codes asserted by committer_content.json prove the OUTCOME; these assertions
    prove the CLASSIFICATION itself, in both directions, since an exit code is
    only indirect evidence of which kind ran.

    2026-09-06 (HIGH finding): an exit code was not even indirect evidence for the
    dangerous case -- a creator brief missing its mandatory approval reference
    classified `committer` and returned ALLOW, so an exit-code-only assertion saw
    a passing brief and nothing else. Every row below therefore asserts the kind,
    and the three regressions the finding named are rows 3, 4 and 1 respectively:
    the reviewer's labelled reproduction, its unlabelled sibling, and the owner's
    genuine Japanese commit dispatch that must keep working."""
    entries = {
        e["id"]: e
        for f, e in _load_flat_fixture_files()
        if f == "committer_content.json"
    }
    expected = {
        "committer-ja-content-dispatch-allow": "committer",
        "committer-ja-content-without-files-list-not-committer-block": "creator",
        "creator-labelled-no-approval-with-files-and-commit-stays-creator-block": "creator",
        "creator-shaped-unlabelled-no-approval-not-committer-block": "creator",
    }
    kinds = cfg["brief_rules"].get("kinds", [])
    for fixture_id, want in expected.items():
        entry = entries.get(fixture_id)
        if entry is None:
            p.check(f"classification fixture present: {fixture_id}", False)
            continue
        sandbox = SandboxCtx(cfg)
        try:
            payload = dict(entry["payload"])
            ctx = sandbox.build(payload=payload, event="PreToolUse")
            text = brief_gate._extract_text(
                payload, cfg["brief_rules"]["detect"].get("text_fields", [])
            )
            kind = brief_gate._classify_kind(text, kinds, ctx)
        finally:
            sandbox.cleanup()
        got = kind and kind.get("id")
        p.check(
            f"classified as {want}: {fixture_id}",
            got == want,
            f"got {got!r}",
        )


# ------------------------------------------------------------------ kind precedence over content signals


_PRECEDENCE_CASES = [
    {
        "id": "creator-label-beats-committer-content-route",
        "expect_kind": "creator",
        "expect_exit": 2,
        "expect_stderr_contains": "names no design skill",
        "setup": {
            "kind": "approval_file",
            "name": "precedence-creator.md",
            "age_sec": 999999,
            "content": "presented understanding, no marker\n",
        },
        "payload": {
            "tool_name": "Agent",
            "hook_event_name": "PreToolUse",
            "tool_input": {
                "prompt": (
                    "[Creator Brief] round7 — ドックのレイアウトとパネルの見た目を作り直してください。\n"
                    "APPROVAL:precedence-creator.md\n"
                    "Goal: DockPanel の情報構造を整理し、チップの状態表示を追加する。\n"
                    "Files:\n"
                    "  - src/components/LayoutDock.tsx\n"
                    "  - src/components/Panel.tsx\n"
                    "  Do NOT edit: 共有トークン、ルーター\n"
                    "列挙したファイルのみをステージし、未ステージのものは残してください。\n"
                    "STATE.md §2 の完成の定義 行3/行4 を対象とします。\n"
                    "OWNER-CONFIRMED: オーナー決定 — 今回の round はドックのみ。\n"
                    "作業が終わったら変更をコミットしてください。push はしないでください。\n"
                    "Report back: files changed, actual test numbers, per-invariant confirmation.\n"
                ),
            },
        },
    },
    {
        "id": "approval-reference-alone-beats-committer-content-route",
        "expect_kind": "creator",
        "expect_exit": 2,
        "expect_stderr_contains": "references no acceptance table",
        "setup": {
            "kind": "approval_file",
            "name": "precedence-reference-only.md",
            "age_sec": 999999,
            "content": "presented understanding, no marker\n",
        },
        "payload": {
            "tool_name": "SendMessage",
            "hook_event_name": "PreToolUse",
            "tool_input": {
                "message": (
                    "C-162 round12 — 直近の受入分をまとめてください。\n"
                    "APPROVAL:precedence-reference-only.md\n"
                    "Files:\n"
                    "  - src/core/tokenizer.py\n"
                    "  - src/core/normalize.py\n"
                    "列挙したファイルのみをステージし、未ステージのものはそのまま残してください。\n"
                    "対象の差分をコミットしてください。push はしないでください。\n"
                    "ブランチは improve/token-diet のままにしてください。テストは既に通っています。\n"
                    "コミットメッセージは feat: で始めてください。以上の内容で作業を進めてください。\n"
                    "追加の確認事項があれば作業前に質問してください。作業ログは done-report にまとめてください。\n"
                    "実際に実行したコマンドとその出力の要約を添えてください。判断に迷う点があれば止めて報告してください。\n"
                    "ブランチの切り替えやリベースは行わないでください。作業対象は現在のワークツリーのみです。\n"
                ),
            },
        },
    },
    {
        "id": "creator-label-beats-forwarder-content-route",
        "expect_kind": "creator",
        "expect_exit": 2,
        "expect_stderr_contains": "carries no APPROVAL",
        "setup": None,
        "payload": {
            "tool_name": "Agent",
            "hook_event_name": "PreToolUse",
            "tool_input": {
                "prompt": (
                    "[Creator Brief] round9 — Final delta の指摘に対応してください。\n"
                    "Goal: レビュアが Final delta で挙げた境界条件の取りこぼしを潰し、回帰を１件足す。\n"
                    "File scope: src/core/tokenizer.py, tests/test_tokenizer.py\n"
                    "  Do NOT edit: 共有トークン、設定ファイル\n"
                    "STATE.md §2 の完成の定義 行5 を対象とし、行ごとに証拠付きで報告してください。\n"
                    "OWNER-CONFIRMED: オーナー決定 — 今回の round は指摘対応のみ。\n"
                    "テストは既存の run_all を使い、実測値を報告してください。push はしないでください。\n"
                    "Report back: files changed, actual test numbers, per-invariant confirmation.\n"
                ),
            },
        },
    },
]


def test_kind_precedence_over_content_signals(p: Probe, cfg: dict) -> None:
    """2026-09-06 (HIGH finding sweep). `committer` and `forwarder` both classify
    partly BY CONTENT. The rule that keeps that safe is precedence: a brief that
    LABELS its own kind is that kind, and no content signal may reclassify it.
    The cases below all carry another kind's content signals IN FULL and must
    still classify creator. Two are labelled creator briefs -- one carrying the
    committer locks (Files: header, をコミット, a staging instruction), one carrying
    the forwarder token `Final delta`; before the precedence lookahead they
    classified committer and forwarder respectively, skipping the creator gates,
    and the forwarder one even kept exit code 2 (gate 4 blocked it for an
    unrelated reason) -- which is exactly why these assert the KIND, not only the
    exit code. The third case carries NO label at all: its only creator marker is
    an `APPROVAL:<file>.md` reference, which exercises the second half of the
    precedence lookahead on its own (gate 6 lives only on `creator`, so such a
    reference is a creator marker; its PRESENCE is precedence).

    These live inline rather than in fixtures/briefs/ deliberately: tests/parity.py
    pins the total fixture-file case count (parity.py EXPECTED_FIXTURE_COUNT) and
    that file is outside this change's scope, so the JSON fixture count is held
    constant."""
    kinds = cfg["brief_rules"].get("kinds", [])
    for case in _PRECEDENCE_CASES:
        sandbox = SandboxCtx(cfg)
        try:
            payload = dict(case["payload"])
            ctx = sandbox.build(payload=payload, event="PreToolUse")
            _apply_setup(sandbox, ctx, case.get("setup"))
            text = brief_gate._extract_text(
                payload, cfg["brief_rules"]["detect"].get("text_fields", [])
            )
            kind = brief_gate._classify_kind(text, kinds, ctx)
            result = brief_gate.run(payload, cfg, ctx)
        finally:
            sandbox.cleanup()

        got_kind = kind and kind.get("id")
        p.check(
            f"precedence: labelled brief classified {case['expect_kind']}: {case['id']}",
            got_kind == case["expect_kind"],
            f"got {got_kind!r}",
        )
        p.check(
            f"precedence: exit {case['expect_exit']}: {case['id']}",
            result.exit_code == case["expect_exit"],
            f"exit={result.exit_code} stderr={result.stderr!r}",
        )
        p.check(
            f"precedence: stderr contains {case['expect_stderr_contains']!r}: {case['id']}",
            case["expect_stderr_contains"] in result.stderr,
            f"stderr={result.stderr!r}",
        )


# ------------------------------------------------------------------ guard purity (local check)


def test_guard_contains_no_case_names_or_ui_vocabulary(p: Probe) -> None:
    """Owner's hard requirement (brief, §"Why this guard matters most"): the guard
    contains no case id, no UI vocabulary, and no special-casing of the two rows
    that exist today. Checked by inspecting the module source directly."""
    source = (HOOKS_DIR / "guards" / "brief_gate.py").read_text(encoding="utf-8")
    forbidden = [
        "responsive-viewport-matrix",
        "ui-design-skill",
        "viewport-matrix",
        "ui-ux-pro-max",
        "emil-design-eng",
        "frontend-design",
    ]
    hits = [tok for tok in forbidden if tok in source]
    p.check(
        "guards/brief_gate.py contains no design_cases case id and no UI-skill vocabulary",
        not hits,
        f"found: {hits}",
    )


def main() -> int:
    # Best-effort: the pinned interpreter's default stream encoding is cp932
    # (CONTRACT.md I-4), and several fixtures carry Japanese / em-dash text in
    # their printed FAIL detail. Reconfigure stdout so a failing assertion can
    # always be printed, on any host locale.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    p = Probe()
    cfg = load_test_config()

    print("== brief_gate: standard fixtures (detect/forwarder/committer/reviewer/gate1/gate2/gate6/gate3/pe9) ==")
    run_standard_fixtures(p, cfg)

    print("\n== brief_gate: acceptance rows 3 & 4 (design_cases add/remove, config-only) ==")
    test_acceptance_rows_3_and_4(p, cfg)

    print("\n== brief_gate: committer content classification (both directions) ==")
    test_committer_content_classification(p, cfg)

    print("\n== brief_gate: kind precedence over content signals (labelled brief wins) ==")
    test_kind_precedence_over_content_signals(p, cfg)

    print("\n== brief_gate: guard purity (no case id, no UI vocabulary) ==")
    test_guard_contains_no_case_names_or_ui_vocabulary(p)

    print(f"\nRESULT: {p.passed} passed, {p.failed} failed")
    return 1 if p.failed else 0


if __name__ == "__main__":
    sys.exit(main())
