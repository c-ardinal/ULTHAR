"""R1-c AC-13: `ulthar issue add|list`, `ulthar unit new|show|advance|list`, `ulthar
contract submit|lint|show`, `ulthar review record` and `ulthar verify` run and exit 0 on
success -- five command groups."""
from __future__ import annotations

import json
import sys

from helpers import Probe
from r1c_helpers import answer, cli, contract, new_sandbox, runner, write_contract


def check_cli(p: Probe) -> None:
    sb = new_sandbox()
    try:
        groups, runs = {}, []
        added = cli(sb, "issue", "add", "--title", "一覧を速くする")
        listed = cli(sb, "issue", "list")
        runs += [added, listed]
        issue_id = added.stdout.split("\t")[0]
        groups["issue"] = all(r.returncode == 0 for r in (added, listed)) and issue_id in listed.stdout
        new = cli(sb, "unit", "new", "--issue", issue_id)
        unit_id = new.stdout.split("\t")[0]
        submit = cli(sb, "contract", "submit", unit_id, "--file", str(write_contract(sb, contract())))
        lint = cli(sb, "contract", "lint", unit_id)
        _, review = runner(sb, "rev-1", ["review", "record", "--unit", unit_id, "--kind",
                                         "contract", "--result", "pass"])
        show_contract = cli(sb, "contract", "show", unit_id)
        answer(sb, json.loads(show_contract.stdout), ["確定"], "cli")
        advance = cli(sb, "unit", "advance", unit_id, "contract_confirmed")
        cli(sb, "unit", "advance", unit_id, "build")
        _, verify = runner(sb, "bld-1", ["verify", unit_id, "--self"])
        show = cli(sb, "unit", "show", unit_id)
        units = cli(sb, "unit", "list")
        runs += [new, submit, lint, review, show_contract, advance, verify, show, units]
        groups["unit"] = (all(r.returncode == 0 for r in (new, advance, show, units))
                          and json.loads(show.stdout)["state"] == "build" and unit_id in units.stdout)
        groups["contract"] = (all(r.returncode == 0 for r in (submit, lint, show_contract))
                              and "CONTRACT-CONFIRM:" in show_contract.stdout)
        groups["review"] = review is not None and review.returncode == 0
        groups["verify"] = verify is not None and verify.returncode == 0 and "PASS" in verify.stdout
        for name, ok in groups.items():
            p.check(f"ulthar {name} ... -> exit 0", ok)
        p.check(f"AC-13: {sum(groups.values())}/5 command groups", all(groups.values()),
                "\n".join(r.stderr[-200:] for r in runs if r is not None and r.returncode))
    finally:
        sb.cleanup()


def main() -> int:
    p = Probe()
    check_cli(p)
    return p.result()


if __name__ == "__main__":
    sys.exit(main())
