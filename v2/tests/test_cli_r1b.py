"""R1-b AC-15: `ulthar decide list|review|confirm`, `ulthar fact add|list`,
`ulthar inject --layer L0|L1 --session <id>` (display only) and `ulthar index rebuild`
run and exit 0 -- four command groups."""
from __future__ import annotations

import sys

from helpers import Probe, Sandbox
from test_decide import add_decisions, ask_and_answer

from ulthar import decide as decide_mod


def check_cli(p: Probe) -> None:
    sb = Sandbox()
    try:
        (sb.work / "README.md").write_text("# product\n", encoding="utf-8")
        add_decisions(sb, {"text": "README は日本語版を先に書く"})
        groups = {}
        listed = sb.cli("decide", "list")
        review, _, _, _, token = ask_and_answer(sb, decide_mod.APPROVE, "tu-cli")
        confirmed = sb.cli("decide", "confirm", "--token", token or "-")
        groups["decide"] = (listed.returncode == review.returncode == confirmed.returncode == 0
                            and "README は日本語版を先に書く" in listed.stdout)
        added = sb.cli("fact", "add", "--src", "README.md:1", "製品名は README の 1 行目")
        facts = sb.cli("fact", "list")
        groups["fact"] = (added.returncode == facts.returncode == 0
                          and "README.md:1" in facts.stdout)
        markers_before = sb.query("SELECT COUNT(*) FROM injections")
        l0 = sb.cli("inject", "--layer", "L0", "--session", "cli")
        l1 = sb.cli("inject", "--layer", "L1", "--session", "cli")
        groups["inject"] = (l0.returncode == l1.returncode == 0 and "[ULTHAR L0" in l0.stdout
                            and "製品名は README の 1 行目" in l1.stdout
                            and sb.query("SELECT COUNT(*) FROM injections") == markers_before)
        rebuilt = sb.cli("index", "rebuild")
        groups["index"] = rebuilt.returncode == 0 and "index rebuilt" in rebuilt.stdout
        for name, ok in groups.items():
            p.check(f"ulthar {name} ... -> exit 0", ok)
        p.check(f"AC-15: {sum(groups.values())}/4 command groups", all(groups.values()),
                "\n".join(x.stderr[-200:] for x in (listed, confirmed, added, l0, l1, rebuilt)))
    finally:
        sb.cleanup()


def main() -> int:
    p = Probe()
    check_cli(p)
    return p.result()


if __name__ == "__main__":
    sys.exit(main())
