#!/usr/bin/env python3
"""Per-phase profile of one hook call per event, in the AC-16 measurement state
(not a test; run by hand):

    ULTHAR_REPLAY_TRANSCRIPT=<real transcript> C:/Python314/python.exe -X utf8 v2/tests/profile_hook.py [OUT]

State: a sandbox whose store holds the real transcript's Episodes, 200 decisions (20
confirmed), and a session transcript that gets one fresh owner line before every call
-- as test_replay does. For PreToolUse (Bash `ls -la`), SubagentStart and
UserPromptSubmit it reports:
  * wall time of the production hook command (process start included), p50 of N calls;
  * interpreter start + imports (`-X importtime` of the same command);
  * in-process time per phase of the dispatcher (wrapping its functions), averaged.
The report goes to OUT (default v2/tests/results/hook-phases.txt); paths are relative.
"""
from __future__ import annotations

import json
import os
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path

from helpers import HOOK_ARGV, TESTS_DIR, Sandbox

CALLS = 25
OUT = TESTS_DIR / "results" / "hook-phases.txt"


def payloads(sb: Sandbox, transcript: Path) -> dict:
    base = {"session_id": "prof", "transcript_path": str(transcript), "cwd": str(sb.work)}
    return {
        "PreToolUse": lambda i: dict(base, tool_name="Bash", tool_input={"command": "ls -la"}),
        "PreToolUse:Edit": lambda i: dict(base, tool_name="Edit", agent_id="prof-builder",
                                          agent_type="ulthar-creator", tool_input={
                                              "file_path": str(sb.work / "src" / f"v{i}.py"),
                                              "old_string": "a", "new_string": "b"}),
        "SubagentStart": lambda i: dict(base, agent_id=f"a-{i}", agent_type="ulthar-creator"),
        "UserPromptSubmit": lambda i: dict(base, prompt=f"profile prompt {i}",
                                           prompt_id=f"prof-{os.getpid()}-{i}"),
    }


def append_line(transcript: Path, tag: str) -> None:
    rec = {"type": "user", "uuid": f"{tag}-{time.perf_counter_ns()}", "message": {
        "role": "user", "content": f"timing line {tag}"}}
    with open(transcript, "ab") as fh:
        fh.write((json.dumps(rec) + "\n").encode())


def wait_idle(sb: Sandbox) -> None:
    """As test_replay (AC-16): the previous call's extraction has ended -- its row is no
    longer pending and no worker/extractor process is left."""
    from helpers import FAKE_EXTRACTOR, processes_matching  # noqa: PLC0415

    for _ in range(600):
        if not (sb.query("SELECT COUNT(*) FROM extractions WHERE status='pending'")[0][0]
                or processes_matching(str(sb.root)) or processes_matching(FAKE_EXTRACTOR.name)):
            return
        time.sleep(0.05)


def wall(sb: Sandbox, transcript: Path, event: str, make) -> list[float]:
    env, times = sb.env(), []
    event = event.split(":")[0]
    for i in range(CALLS):
        append_line(transcript, event)
        if event == "UserPromptSubmit":
            wait_idle(sb)
        body = json.dumps(dict(make(i), hook_event_name=event))
        t0 = time.perf_counter()
        subprocess.run(HOOK_ARGV + ["--event", event], input=body, capture_output=True,
                       text=True, encoding="utf-8", env=env)
        times.append((time.perf_counter() - t0) * 1000)
    return times


def import_ms(sb: Sandbox, transcript: Path, event: str, make) -> tuple[float, list]:
    event = event.split(":")[0]
    append_line(transcript, event)
    body = json.dumps(dict(make(999), hook_event_name=event))
    argv = [HOOK_ARGV[0], "-X", "importtime"] + HOOK_ARGV[1:] + ["--event", event]
    proc = subprocess.run(argv, input=body, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=sb.env())
    rows = [l for l in proc.stderr.splitlines() if l.startswith("import time:")]
    total = 0.0
    top = []
    for line in rows:
        parts = [p.strip() for p in line.split("|")]
        if len(parts) == 3 and parts[1].isdigit():
            cum, name = int(parts[1]), parts[2]
            if not name.startswith(" "):
                pass
            if line.split("|")[2].startswith(" ") and not line.split("|")[2].startswith("  "):
                total += cum / 1000
                top.append((cum / 1000, name))
    return total, sorted(top, reverse=True)[:6]


PHASES = [
    ("ulthar.hook", "_open_and_catch_up", "runtime.db open + journal + transcript catch-up"),
    ("ulthar.db", "connect", "runtime.db connect"),
    ("ulthar.ingest", "ingest", "transcript catch-up (ingest)"),
    ("ulthar.store", "Store.transaction", None),
    ("ulthar.hook", "_run_guards", "v1 guard pipeline (config load + guards)"),
    ("ulthar.hook", "_run_memory", "memory step (protect, L0/L1, prompt, launch)"),
    ("ulthar.memhook", "protect", "store guard (shellwords, realpath)"),
    ("ulthar.memory", "build_l1", "L1 assembly"),
    ("ulthar.memory", "build_l0", "L0 assembly"),
    ("ulthar.extract", "launch", "extractor spawn"),
    ("ulthar.hook", "_record", "events row"),
    ("ulthar.store", "Store.reader", None),
    ("ulthar.store", "Store.finalize", "store finalize (deferred manifest)"),
    ("ulthar.integrity", "write_manifest", "txn.json write (temp+flush+replace)"),
    ("ulthar.integrity", "clear_manifest", "txn.json remove"),
    ("ulthar.store", "Store._settle", "store settle (manifest, mtime check)"),
    ("ulthar.store", "Store._record_runtime_chain", "runtime.db chain rows"),
    ("ulthar.store", "Store._connect", "index.db connect"),
    ("ulthar.store", "Store._check_host", "host check"),
    ("ulthar.store", "Tx.commit", "JSONL append + index commit (+ manifest)"),
    ("ulthar.store", "Store.for_cwd", "project resolution (repo root, ulthar.json)"),
]


def in_process(sb: Sandbox, transcript: Path, event: str, make) -> dict:
    import contextlib  # noqa: PLC0415
    import importlib  # noqa: PLC0415

    os.environ.update(sb.env())
    acc: dict[str, float] = {}
    patched = []
    for modname, attr, label in PHASES:
        mod = importlib.import_module(modname)
        owner, name = (getattr(mod, attr.split(".")[0]), attr.split(".")[1]) if "." in attr else (mod, attr)
        fn = getattr(owner, name)
        key = label or f"{attr}"
        if attr in ("Store.transaction", "Store.reader"):
            def wrap_cm(f=fn, k=key):
                @contextlib.contextmanager
                def cm(*a, **kw):
                    t = time.perf_counter()
                    with f(*a, **kw) as v:
                        acc[k + " (enter)"] = acc.get(k + " (enter)", 0) + (time.perf_counter() - t) * 1000
                        yield v
                        t = time.perf_counter()
                    acc[k + " (exit)"] = acc.get(k + " (exit)", 0) + (time.perf_counter() - t) * 1000
                return cm
            new = wrap_cm()
        else:
            def wrap(f=fn, k=key):
                def w(*a, **kw):
                    t = time.perf_counter()
                    try:
                        return f(*a, **kw)
                    finally:
                        acc[k] = acc.get(k, 0) + (time.perf_counter() - t) * 1000
                return w
            new = wrap()
        setattr(owner, name, new)
        patched.append((owner, name, fn))
    from ulthar import hook  # noqa: PLC0415

    n = 10
    total = 0.0
    try:
        for i in range(n):
            append_line(transcript, event + "-ip")
            if event == "UserPromptSubmit":
                wait_idle(sb)
            payload = dict(make(500 + i), hook_event_name=event.split(":")[0])
            t = time.perf_counter()
            hook.process(event.split(":")[0], payload)
            total += (time.perf_counter() - t) * 1000
    finally:
        for owner, name, fn in patched:
            setattr(owner, name, fn)
    out = {k: round(v / n, 1) for k, v in acc.items()}
    out["(in-process total)"] = round(total / n, 1)
    return out


def seed(sb: Sandbox) -> None:
    with sb.inproc(), sb.store().transaction({"hook": "profile-setup"}) as tx:
        for i in range(200):
            tx.add("decision", {"status": "confirmed" if i < 20 else "provisional",
                                "text": f"perf decision {i}: UI の余白・角丸・色はトークンに従う（例 {i}）",
                                "raw_episode_id": "E-perf", "scope": "project",
                                "tags": ["design"], "paths": ["src/ui/**"],
                                "binding": i < 20, "confirm_token_id": None})


def main() -> int:
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else OUT
    real = os.environ.get("ULTHAR_REPLAY_TRANSCRIPT")
    sb = Sandbox()
    lines = []
    try:
        if real:
            ing = sb.cli("ingest", "--session", "real", "--transcript", real)
            lines.append(f"store state: real transcript ingested -> {ing.stdout.strip()[:160]}")
        seed(sb)
        from test_replay import seed_scoped_unit  # noqa: PLC0415 -- the AC-14 Edit state
        seed_scoped_unit(sb)
        transcript = sb.root / "prof.jsonl"
        transcript.write_text("", encoding="utf-8")
        makers = payloads(sb, transcript)
        for event, make in makers.items():
            w = wall(sb, transcript, event, make)
            imp, top = import_ms(sb, transcript, event, make)
            phases = in_process(sb, transcript, event, make)
            lines.append(f"\n== {event} ==")
            lines.append(f"wall (process start included), {CALLS} calls: p50 {statistics.median(w):.1f} ms, "
                         f"p95 {sorted(w)[int(0.95 * CALLS) - 1]:.1f} ms")
            lines.append(f"imports (top-level, -X importtime): {imp:.1f} ms; largest: "
                         + ", ".join(f"{n} {t:.1f}" for t, n in top))
            for k, v in sorted(phases.items(), key=lambda kv: -kv[1]):
                lines.append(f"  {v:7.1f} ms  {k}")
        wait_idle(sb)
    finally:
        sb.cleanup()
    text = "\n".join(lines) + "\n"
    text = re.sub(r"[A-Za-z]:[\\/][^\s\"']*", "<path>", text)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
