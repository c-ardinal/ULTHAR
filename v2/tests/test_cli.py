"""AC-8: `ulthar db init`, `ulthar ingest --session --transcript` and `ulthar status`
run and exit 0; status lists each session with its events and Episode counts."""
from __future__ import annotations

import json
import sys

from helpers import Probe, Sandbox, synthetic_transcript, write_jsonl


def check_cli(p: Probe) -> None:
    sb = Sandbox()
    try:
        init = sb.cli("db", "init")
        p.check("ulthar db init -> exit 0", init.returncode == 0 and sb.db.exists(),
                init.stdout + init.stderr[-300:])
        path = sb.root / "t.jsonl"
        records, expected = synthetic_transcript()
        write_jsonl(path, records)
        ing = sb.cli("ingest", "--session", "cli-s", "--transcript", str(path))
        counts = json.loads(ing.stdout) if ing.returncode == 0 else {}
        p.check("ulthar ingest -> exit 0 with per-kind counts",
                ing.returncode == 0 and all(counts.get(k) == v for k, v in expected.items()),
                ing.stdout + ing.stderr[-300:])
        sb.hook("PreToolUse", {"session_id": "cli-s", "tool_name": "Bash",
                               "tool_input": {"command": "ls"}})
        st = sb.cli("status")
        line = next((ln for ln in st.stdout.splitlines() if ln.startswith("cli-s\t")), "")
        p.check("ulthar status -> exit 0, the session with events and Episode counts",
                st.returncode == 0 and "events=1" in line and f"episodes={sum(expected.values())}"
                in line, st.stdout + st.stderr[-300:])
    finally:
        sb.cleanup()


def main() -> int:
    p = Probe()
    check_cli(p)
    return p.result()


if __name__ == "__main__":
    sys.exit(main())
