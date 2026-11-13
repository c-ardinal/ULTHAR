"""R1-c contracts:
  AC-2  `contract submit` copies a valid YAML contract to contracts/<unit>/v<n>.yaml (a
        version is never overwritten), records its sha256 and makes the unit
        contract_draft; a contract missing a required element, a YAML syntax error and
        PyYAML not being importable are refused with the reason, writing nothing;
  AC-3  `contract lint` finds each rule's violation (7 pairs) and nothing in the
        counterpart that keeps the rule.
"""
from __future__ import annotations

import copy
import hashlib
import subprocess
import sys

from helpers import MAIN_PY, Probe
from r1c_helpers import (cli, contract, make_unit, new_sandbox, qual_ac, quant_ac, unit,
                         write_contract)


def _files(sb, unit_id: str) -> list:
    return sorted((sb.kr / ".ulthar" / "contracts" / unit_id).glob("*.yaml"))


def check_submit(p: Probe) -> None:
    sb = new_sandbox()
    try:
        unit_id = make_unit(sb)
        path = write_contract(sb, contract())
        res = cli(sb, "contract", "submit", unit_id, "--file", str(path))
        rec = unit(sb, unit_id)
        v1 = sb.kr / ".ulthar" / "contracts" / unit_id / "v1.yaml"
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        p.check("AC-2: a valid contract is accepted: copied to contracts/<unit>/v1.yaml, sha256 "
                "recorded, unit contract_draft 1/1",
                res.returncode == 0 and v1.read_bytes() == path.read_bytes()
                and rec["contract"]["sha"] == sha and rec["state"] == "contract_draft",
                res.stdout + res.stderr[-300:])
        second = write_contract(sb, contract(non_goals=["配色は変えない", "API は変えない"]), "c2.yaml")
        again = cli(sb, "contract", "submit", unit_id, "--file", str(second))
        p.check("  ... a second submission is v2; v1 is not overwritten",
                again.returncode == 0 and unit(sb, unit_id)["contract"]["version"] == 2
                and v1.read_bytes() == path.read_bytes() and len(_files(sb, unit_id)) == 2,
                again.stderr[-300:])
        fresh = make_unit(sb)
        lines = len(sb.jsonl("units.jsonl"))
        missing = contract()
        del missing["non_goals"]
        cases = {
            "a required element missing (non_goals)": (write_contract(sb, missing, "m.yaml"),
                                                       "non_goals is missing", []),
            "YAML syntax error": (write_contract(sb, "intent: {raw: [unclosed\n", "s.yaml"),
                                  "YAML syntax error", []),
            "PyYAML not importable": (path, "PyYAML is not importable", ["-S"]),
        }
        refused = 0
        for label, (file, reason, flags) in cases.items():
            proc = subprocess.run([sys.executable, *flags, str(MAIN_PY), "contract", "submit",
                                   fresh, "--file", str(file)], capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", env=sb.env(), cwd=str(sb.work))
            ok = (proc.returncode != 0 and reason in proc.stderr and not _files(sb, fresh)
                  and len(sb.jsonl("units.jsonl")) == lines)
            refused += ok
            p.check(f"AC-2: refused with the reason, nothing written: {label}", ok,
                    proc.stderr[-300:])
        p.check(f"AC-2: refusals {refused}/3", refused == 3)
        hook = sb.hook("PreToolUse", {"session_id": "y", "tool_name": "Bash",
                                      "tool_input": {"command": "ls"}})
        p.check("  ... the hook (no PyYAML on its path, -S) works as before", hook.returncode == 0,
                hook.stderr[-300:])
    finally:
        sb.cleanup()


def _variants() -> dict:
    """rule -> (violating contract, keeping counterpart)."""
    base = contract()
    out = {}

    def var(**changes):
        doc = copy.deepcopy(base)
        doc.update(changes)
        return doc

    out["LINT-A"] = (var(acs=[quant_ac(statement="ボタンを適切に減らし、APPROPRIATE に並べる")]),
                     var(acs=[quant_ac(statement="ボタンは 3 個以下")]))
    out["LINT-B"] = (var(acs=[quant_ac(threshold={"lo": None, "hi": 3})]),
                     var(acs=[quant_ac(threshold={"lo": 0, "hi": 3, "lo_incl": True,
                                                  "hi_incl": False})]))
    out["LINT-C"] = (var(acs=[quant_ac(verify_cmd="no-such-tool-xyz --count"),
                              quant_ac("AC-2", verify_cmd="scripts/missing.py"),
                              quant_ac("AC-3", verify_cmd="")]),
                     var(acs=[quant_ac(), quant_ac("AC-2", verify_cmd="checks/value.py x")]))
    out["LINT-D"] = (var(acs=[quant_ac(sources=[])]), var(acs=[quant_ac(sources=["owner:1"])]))
    out["LINT-E"] = (var(non_goals=[]), var(non_goals=["配色は変えない"]))
    quants = [quant_ac(f"AC-{i}") for i in range(1, 8)]
    no_rubric = dict(qual_ac("AC-Q9"), rubric=None)
    out["LINT-F"] = (var(acs=quants[:2] + [qual_ac("AC-Q1"), no_rubric]),
                     var(acs=quants + [qual_ac("AC-Q1"), qual_ac("AC-Q2"), qual_ac("AC-Q3")]))
    out["LINT-G"] = (var(intent={"raw": "r", "interpretation": "C",
                                 "alternatives": [{"id": "A", "text": "案 A"}]}),
                     var(intent={"raw": "r", "interpretation": "案 B",
                                 "alternatives": ["案 A", "案 B"]}))
    return out


def check_lint(p: Probe) -> None:
    sb = new_sandbox()
    try:
        clean = make_unit(sb)
        cli(sb, "contract", "submit", clean, "--file", str(write_contract(sb, contract())))
        base = cli(sb, "contract", "lint", clean)
        p.check("AC-3: the base contract passes the lint (exit 0)",
                base.returncode == 0 and "contract lint: OK" in base.stdout, base.stdout)
        found, false_hits = 0, 0
        for rule, (bad, good) in _variants().items():
            results = []
            for kind, doc in (("bad", bad), ("good", good)):
                unit_id = make_unit(sb)
                sub = cli(sb, "contract", "submit", unit_id, "--file",
                          str(write_contract(sb, doc, f"{rule}-{kind}.yaml")))
                results.append((sub, cli(sb, "contract", "lint", unit_id)))
            (s1, bad_lint), (s2, good_lint) = results
            hit = s1.returncode == 0 and bad_lint.returncode != 0 and rule in bad_lint.stdout
            clean_ok = s2.returncode == 0 and good_lint.returncode == 0
            found += hit
            false_hits += not clean_ok
            p.check(f"AC-3 {rule}: the violation is listed with its rule id (exit != 0); the "
                    "counterpart passes", hit and clean_ok,
                    f"{s1.stderr[-200:]}{bad_lint.stdout[-400:]} | {s2.stderr[-200:]}"
                    f"{good_lint.stdout[-400:]}")
        p.check(f"AC-3: violations found {found}/7, false findings {false_hits}",
                found == 7 and false_hits == 0)
    finally:
        sb.cleanup()


def main() -> int:
    p = Probe()
    print("\n== AC-2: contract submit ==")
    check_submit(p)
    print("\n== AC-3: contract lint ==")
    check_lint(p)
    return p.result()


if __name__ == "__main__":
    sys.exit(main())
