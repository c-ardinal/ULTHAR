"""R1-d AC-6 (SKL-02 lint rule H, SKL-06 default ACs and the pack evaluator) and AC-7
(`ulthar run`, TRUST-07 second half) with AC-14's `ulthar run`, through the production
hook entry and the CLI."""
from __future__ import annotations

import json
import sys

import yaml

from helpers import Probe
from r1c_helpers import cli, contract, make_unit, quant_ac, write_contract
from r1d_helpers import (CREATOR, EVALUATOR, UI_AC, WINDOW, advance, git, new_r1d_sandbox,
                         pack_defs, run_ulthar, start_agent, to_build, write)


def _submit(sb, doc, name: str):
    unit_id = make_unit(sb)
    return unit_id, cli(sb, "contract", "submit", unit_id, "--file", str(write_contract(sb, doc, name)))


def check_lint_and_defaults(p: Probe) -> None:
    sb = new_r1d_sandbox(packs=pack_defs(default_acs=[UI_AC]))
    try:
        held = 0
        unit_id, _ = _submit(sb, contract(packs=[], scope_globs=["src/**"]), "noui.yaml")
        lint = cli(sb, "contract", "lint", unit_id)
        ok = lint.returncode != 0 and "LINT-H" in lint.stdout and "['api', 'ui']" in lint.stdout
        held += ok
        p.check("AC-6: packs [] with a scope that holds UI paths -> LINT-H", ok, lint.stdout[-400:])
        unit_id, sub = _submit(sb, contract(packs=["ui"], scope_globs=["src/ui/**"]), "ui.yaml")
        lint = cli(sb, "contract", "lint", unit_id)
        ok = sub.returncode == 0 and lint.returncode == 0 and "LINT-H" not in lint.stdout
        held += ok
        p.check("AC-6: packs that match the scope's inferred packs pass the lint", ok,
                sub.stderr[-300:] + lint.stdout[-400:])
        rec = json.loads(cli(sb, "unit", "show", unit_id).stdout)
        saved = yaml.safe_load((sb.kr / ".ulthar" / rec["contract"]["path"]).read_text(encoding="utf-8"))
        ok = [a["id"] for a in saved["acs"]] == ["AC-1", "UI-1"] and rec["contract"]["packs"] == ["ui"]
        held += ok
        p.check("AC-6: submit adds the pack's default AC (UI-1) to the saved version", ok,
                str([a["id"] for a in saved["acs"]]))
        lines = len(sb.jsonl("units.jsonl"))
        _, clash = _submit(sb, contract(acs=[quant_ac("UI-1")], packs=["ui"],
                                        scope_globs=["src/ui/**"]), "clash.yaml")
        ok = clash.returncode != 0 and "UI-1" in clash.stderr and len(sb.jsonl("units.jsonl")) == lines + 1
        held += ok
        p.check("AC-6: a contract AC id that a default AC also uses refuses the submission "
                "(the unit stays intake)", ok, clash.stderr[-300:])
        p.check(f"AC-6 lint/defaults: {held}/4", held == 4)
    finally:
        sb.cleanup()


def check_evaluator(p: Probe) -> None:
    sb = new_r1d_sandbox()
    try:
        unit_id = to_build(sb)
        start_agent(sb, "bld-1", CREATOR)
        write(sb, "bld-1", "src/ui/a.txt", "ui v1\n", CREATOR)
        run_ulthar(sb, "bld-1", ["verify", unit_id, "--self"], agent_type=CREATOR)
        advance(sb, unit_id, "quant_verified")
        start_agent(sb, "gp-r", "general-purpose")
        run_ulthar(sb, "gp-r", ["verify", unit_id], agent_type="general-purpose")
        _, other = run_ulthar(sb, "gp-r", ["review", "record", "--unit", unit_id, "--kind",
                                           "verify", "--result", "pass"], agent_type="general-purpose")
        reviews = json.loads(cli(sb, "unit", "show", unit_id).stdout).get("reviews") or []
        ok = (other is not None and other.returncode != 0 and "ulthar-evaluator@ui" in other.stderr
              and not [r for r in reviews if r["kind"] == "verify"])
        p.check("AC-6: a verify review from an agent without the ui evaluator's receipt is "
                "refused", ok, other.stderr[-300:] if other else "blocked")
        start_agent(sb, "ev-1", EVALUATOR)
        _, mine = run_ulthar(sb, "ev-1", ["review", "record", "--unit", unit_id, "--kind",
                                          "verify", "--result", "pass"], agent_type=EVALUATOR)
        reviews = json.loads(cli(sb, "unit", "show", unit_id).stdout).get("reviews") or []
        p.check("AC-6: the ui evaluator's verify review is recorded", mine is not None
                and mine.returncode == 0 and reviews[-1]["agent_id"] == "ev-1",
                mine.stderr[-300:] if mine else "blocked")
    finally:
        sb.cleanup()


def _run_events(sb) -> list:
    return sb.query("SELECT agent_id, decision, fence_token, post_hashes, detail FROM events"
                    " WHERE tool='ulthar run' ORDER BY id")


def check_run(p: Probe) -> None:
    sb = new_r1d_sandbox()
    try:
        (sb.work / "src" / "ui" / "old.txt").write_text("old\n", encoding="utf-8")
        git(sb, "add", "src/ui/old.txt")
        git(sb, "commit", "-q", "-m", "old")
        doc = contract(packs=["ui"], scope_globs=["src/ui/**"], write_cmds={
            "fmt": "python tools/fmt.py src/ui/a.txt", "out": "python tools/fmt.py notes.txt",
            "del": "python tools/rm.py src/ui/old.txt"})
        (sb.work / "notes.txt").write_text("n\n", encoding="utf-8")
        unit_id = to_build(sb, doc)
        start_agent(sb, "c-1", CREATOR)
        held = 0
        _, res = run_ulthar(sb, "c-1", ["run", unit_id, "fmt"], agent_type=CREATOR)
        ev = _run_events(sb)
        blob = git(sb, "hash-object", "src/ui/a.txt").stdout.strip()
        window_token = sb.query("SELECT fence_token FROM leases")[0][0]
        post = json.loads(ev[-1][3]) if ev else {}
        ok = (res is not None and res.returncode == 0 and len(ev) == 1 and ev[0][:3] == ("c-1", "allow", window_token)
              and list(post.values()) == [blob] and next(iter(post)).replace("\\", "/").endswith("src/ui/a.txt")
              and "formatted" in (sb.work / "src" / "ui" / "a.txt").read_text(encoding="utf-8"))
        held += ok
        p.check("AC-7/AC-14: a declared in-scope command runs (exit 0) and its change is recorded "
                "with the post-hash, the runner's agent_id and the window's fencing token", ok,
                (res.stdout + res.stderr[-300:] if res else "blocked") + str(ev)[:300])
        _, res = run_ulthar(sb, "c-1", ["run", unit_id, "out"], agent_type=CREATOR)
        ev = _run_events(sb)
        ok = (res is not None and res.returncode != 0 and len(ev) == 2 and ev[1][1] == "deny"
              and "outside the confirmed scope_globs" in res.stderr)
        held += ok
        p.check("AC-7: a declared command that changes a path outside the scope is recorded as "
                "failed (deny)", ok, (res.stderr[-300:] if res else "blocked") + str(ev[1:])[:300])
        before = (sb.work / "src" / "ui" / "a.txt").read_text(encoding="utf-8")
        _, res = run_ulthar(sb, "c-1", ["run", unit_id, "nope"], agent_type=CREATOR)
        ok = (res is not None and res.returncode != 0 and "write_cmds" in res.stderr
              and len(_run_events(sb)) == 2
              and (sb.work / "src" / "ui" / "a.txt").read_text(encoding="utf-8") == before)
        held += ok
        p.check("AC-7: a name the contract does not declare runs nothing", ok,
                res.stderr[-300:] if res else "blocked")
        _, res = run_ulthar(sb, "c-1", ["run", unit_id, "del"], agent_type=CREATOR)
        ev = _run_events(sb)
        post = json.loads(ev[-1][3]) if len(ev) == 3 else {}
        ok = (res is not None and res.returncode == 0 and len(ev) == 3 and ev[2][1] == "allow"
              and list(post.values()) == [None] and not (sb.work / "src" / "ui" / "old.txt").exists())
        held += ok
        p.check("AC-7: a deletion is recorded with no post-hash (deleted)", ok,
                (res.stderr[-300:] if res else "blocked") + str(ev[2:])[:300])
        p.check(f"AC-7: {held}/4 (window session {WINDOW})", held == 4)
    finally:
        sb.cleanup()


def main() -> int:
    p = Probe()
    print("\n== R1-d AC-6 / AC-7: pack lint, default ACs, evaluator; ulthar run ==")
    check_lint_and_defaults(p)
    check_evaluator(p)
    check_run(p)
    return p.result()


if __name__ == "__main__":
    sys.exit(main())
