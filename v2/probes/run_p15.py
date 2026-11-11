#!/usr/bin/env python3
"""R1-b AC-7 / P-15: does the real `claude -p --model haiku` answer the extraction prompt
in the format ulthar.worker parses?

    C:/Python314/python.exe -X utf8 v2/probes/run_p15.py [--out FILE]

Five owner messages (a correction, a prohibition, a preference, a term, nothing
durable) go through exactly the production path: worker.build_prompt -> the default
extractor command (`claude -p --model haiku`, ULTHAR_EXTRACTOR=1, run in the ulthar
state dir) -> worker.parse_output. ULTHAR_STATE_DIR is pointed at a throwaway temp
dir so nothing is created under ~/.claude/ulthar. Prints (and writes to --out) one
JSON record per message: parsed or not, the kinds, and the raw answer.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

MESSAGES = [
    ("指摘", "decision", "さっきの設定画面、ボタンの角丸がページごとにバラバラだった。全部 4px に揃えて。"),
    ("禁止", "decision", "本番ブランチへの force push は絶対にしないで。"),
    ("好み", "decision", "コミットメッセージは英語より日本語のほうが好き。今後は日本語で書いて。"),
    ("用語", "term", "画面や文書では「ユーザー」ではなく「利用者」と書いて。"),
    ("該当なし", "none", "今日の進み具合はどう？"),
]
ACTIVE = [{"id": "D-p15sample01", "status": "confirmed", "text": "ボタンの角丸は 8px にする"}]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out")
    args = ap.parse_args()
    os.environ["ULTHAR_STATE_DIR"] = tempfile.mkdtemp(prefix="ulthar-p15-state-")
    from ulthar import worker  # noqa: PLC0415 -- after the state dir is redirected

    argv = worker.extractor_argv({})
    results = []
    for label, expected, message in MESSAGES:
        started = time.perf_counter()
        rec = {"label": label, "expected_kind": expected, "message": message,
               "argv": [Path(argv[0]).name, *argv[1:]]}
        try:
            raw = worker._run_extractor(argv, worker.build_prompt(message, ACTIVE))
            rec["raw"] = raw
            try:
                items = worker.parse_output(raw)
                rec.update(parsed=True, kinds=[i["kind"] for i in items], items=items)
            except ValueError as exc:
                rec.update(parsed=False, error=f"parse: {exc}")
        except Exception as exc:  # noqa: BLE001 -- a probe records every outcome
            rec.update(parsed=False, error=f"{type(exc).__name__}: {exc}")
        rec["seconds"] = round(time.perf_counter() - started, 1)
        results.append(rec)
        print(json.dumps(rec, ensure_ascii=False), flush=True)
    summary = {"parsed": sum(r.get("parsed", False) for r in results), "of": len(results),
               "kind_matches": sum(r.get("parsed", False) and r["expected_kind"] in r["kinds"]
                                   for r in results)}
    print(json.dumps(summary))
    if args.out:
        Path(args.out).write_text(json.dumps({"results": results, "summary": summary},
                                             ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if summary["parsed"] == summary["of"] else 1


if __name__ == "__main__":
    sys.exit(main())
