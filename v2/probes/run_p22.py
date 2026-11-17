#!/usr/bin/env python3
"""R1-e P-22 (spec 未確認の点): is the main-chain assistant `usage` (input_tokens +
cache_creation_input_tokens + cache_read_input_tokens) usable as the window's context
usage -- in particular, does it drop right after a compaction (`isCompactSummary`)?

    C:/Python314/python.exe -X utf8 v2/probes/run_p22.py <transcript.jsonl>

Read only: prints counts and token figures, never message text (the transcript is a
real project's and is not committed)."""
from __future__ import annotations

import json
import sys


def usage_of(rec: dict) -> int | None:
    u = (rec.get("message") or {}).get("usage")
    if not isinstance(u, dict):
        return None
    return sum(int(u.get(k) or 0) for k in
               ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))


def main() -> int:
    path = sys.argv[1]
    last, seen, compactions, out = None, 0, [], []
    with open(path, "rb") as fh:
        for raw in fh:
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(rec, dict) or rec.get("isSidechain") is True:
                continue
            if rec.get("type") == "user" and rec.get("isCompactSummary") is True:
                compactions.append({"before": last, "after": None})
                continue
            if rec.get("type") == "assistant":
                used = usage_of(rec)
                if used is None:
                    continue
                seen += 1
                if compactions and compactions[-1]["after"] is None:
                    compactions[-1]["after"] = used
                last = used
                out.append(used)
    drops = [c for c in compactions if c["before"] and c["after"] is not None
             and c["after"] < c["before"]]
    print(json.dumps({"assistant_lines_with_usage": seen, "max_usage": max(out) if out else None,
                      "compactions": compactions, "dropped_after_compaction": len(drops),
                      "last_usage": last}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
