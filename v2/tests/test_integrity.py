"""R1-b knowledge-store integrity (review r1b-1 finding 4) and AC-20:
  * a malformed middle line fails closed: reads, rebuild and the hook stop with an error
    naming the line, nothing is skipped and the index does not advance;
  * a same-size rewrite is caught (mtime differs -> chain recomputed -> mismatch);
  * a multi-file transaction is all or nothing: a failure while writing the second file
    cuts the first back; an interrupted one (manifest left, torn bytes) is undone at the
    next open;
  * AC-20 `ulthar store verify`: an append made outside the CLI, a same-size rewrite and
    a deleted line are each detected (3/3) with the position; the untouched store and a
    restored one report nothing (false positives 0).
"""
from __future__ import annotations

import json
import os
import sys

from helpers import Probe, Sandbox

from ulthar import integrity
from ulthar import store as store_mod

DECISION = {"status": "provisional", "raw_episode_id": "E-x", "scope": "project", "tags": [],
            "paths": [], "binding": False, "confirm_token_id": None}


def _seed(sb: Sandbox, n: int = 3) -> None:
    with sb.inproc():
        for i in range(n):
            with sb.store().transaction({"hook": "test"}) as tx:
                tx.add("decision", dict(DECISION, text=f"decision number {i}"))


def _decisions_path(sb: Sandbox):
    return sb.kr / ".ulthar" / "decisions.jsonl"


def _read_error(sb: Sandbox) -> str | None:
    try:
        with sb.inproc(), sb.store().reader() as conn:
            conn.execute("SELECT 1").fetchone()
    except store_mod.StoreError as exc:
        return str(exc)
    return None


def check_malformed_middle_line(p: Probe) -> None:
    sb = Sandbox()
    try:
        _seed(sb)
        path = _decisions_path(sb)
        lines = path.read_bytes().split(b"\n")
        lines[1] = b'{"seq": 2, "id": "D-broken", "text": "cut'  # not JSON any more
        path.write_bytes(b"\n".join(lines))
        err = _read_error(sb)
        rebuilt = sb.cli("index", "rebuild")
        hook = sb.hook("PreToolUse", {"session_id": "int", "tool_name": "Agent",
                                      "tool_input": {"prompt": "go"}})
        with sb.inproc():
            conn = store_mod.Store.for_cwd(sb.work)._connect()
            indexed = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
            conn.close()
        p.check("malformed middle line: reading the store fails closed (no silent skip)",
                err is not None, str(err))
        p.check("  ... `ulthar index rebuild` refuses, naming the line",
                rebuilt.returncode != 0 and "line 2" in rebuilt.stderr, rebuilt.stderr[-300:])
        p.check("  ... an Agent launch is blocked (exit 2), and the index did not advance",
                hook.returncode == 2 and indexed == 3, f"rc={hook.returncode} indexed={indexed}")
    finally:
        sb.cleanup()


def check_same_size_rewrite(p: Probe) -> None:
    sb = Sandbox()
    try:
        _seed(sb)
        path = _decisions_path(sb)
        data = path.read_bytes()
        path.write_bytes(data.replace(b"decision number 1", b"decision number 7"))
        same = path.stat().st_size == len(data)
        err = _read_error(sb)
        p.check("same-size rewrite: detected on the next read (chain mismatch), fail closed",
                same and err is not None and "differs" in err, str(err))
    finally:
        sb.cleanup()


def check_multi_file_atomicity(p: Probe) -> None:
    sb = Sandbox()
    try:
        _seed(sb, 1)
        path = _decisions_path(sb)
        before = path.read_bytes()
        (sb.kr / ".ulthar" / "terms.jsonl").mkdir()  # the second file cannot be written
        failed = False
        try:
            with sb.inproc(), sb.store().transaction({"hook": "test"}) as tx:
                dec = tx.add("decision", dict(DECISION, text="term decision"))
                tx.add("term", {"term": "利用者", "preferred": "利用者", "forbidden": ["ユーザー"],
                                "decision_id": dec["id"]})
        except (OSError, store_mod.StoreError):
            failed = True
        (sb.kr / ".ulthar" / "terms.jsonl").rmdir()
        p.check("partial multi-file write: the transaction fails and decisions.jsonl is cut "
                "back (no decision without its term)",
                failed and path.read_bytes() == before
                and not (sb.kr / ".ulthar" / integrity.MANIFEST).exists())
        # an interrupted transaction: manifest written, a torn append left behind
        old = len(before)
        torn = b'{"seq": 9, "id": "D-torn", "text": "half'
        with open(path, "ab") as fh:
            fh.write(torn)
        integrity.write_manifest(sb.kr / ".ulthar", {"files": {"decisions.jsonl": {
            "old": old, "new": old + 200, "chain": "0" * 64}}})
        err = _read_error(sb)
        p.check("interrupted transaction (manifest + torn final bytes): undone at the next "
                "open, the store reads cleanly",
                err is None and path.read_bytes() == before
                and not (sb.kr / ".ulthar" / integrity.MANIFEST).exists(), str(err))
    finally:
        sb.cleanup()


def _verify(sb: Sandbox):
    return sb.cli("store", "verify")


def check_verify(p: Probe) -> None:
    sb = Sandbox()
    try:
        _seed(sb, 4)
        sb.hook("UserPromptSubmit", {"session_id": "int", "prompt": "チェーンの試験", "prompt_id": "c-1"})
        clean = _verify(sb)
        p.check("AC-20: untouched store -> verify exit 0, no problem (false positives 0)",
                clean.returncode == 0 and "OK" in clean.stdout, clean.stdout + clean.stderr[-300:])
        path = _decisions_path(sb)
        original = path.read_bytes()
        lines = original.split(b"\n")
        rec = json.loads(lines[0])
        forged = json.dumps(dict(rec, seq=999, id="D-forged", text="forged")).encode() + b"\n"
        cases = {
            "an append outside the CLI": original + forged,
            "a same-size rewrite": original.replace(b"decision number 2", b"decision number 8"),
            "a deleted line": b"\n".join(lines[:1] + lines[2:]),
        }
        detected = 0
        for label, data in cases.items():
            path.write_bytes(data)
            res = _verify(sb)
            ok = res.returncode != 0 and "decisions.jsonl" in res.stdout and "byte" in res.stdout
            detected += ok
            p.check(f"AC-20: {label} -> verify exit != 0 with the position", ok,
                    res.stdout[-400:] + res.stderr[-200:])
            path.write_bytes(original)
        p.check(f"AC-20: detected {detected}/3", detected == 3)
        restored = _verify(sb)
        p.check("AC-20: after restoring the bytes, verify reports nothing (false positives 0)",
                restored.returncode == 0, restored.stdout[-300:])
        chains = sb.query("SELECT name, size, chain FROM store_chain ORDER BY name, size")
        with sb.inproc():
            conn = sb.store()._connect()
            index_chains = conn.execute("SELECT name, size, chain FROM chain ORDER BY name, size").fetchall()
            conn.close()
        p.check("AC-20: every append's chain value is in both index.db and runtime.db",
                bool(chains) and [tuple(r) for r in chains] == [tuple(r) for r in index_chains],
                f"{len(chains)} vs {len(index_chains)}")
    finally:
        sb.cleanup()


def main() -> int:
    p = Probe()
    print("\n== JSONL integrity (fail closed) ==")
    check_malformed_middle_line(p)
    check_same_size_rewrite(p)
    check_multi_file_atomicity(p)
    print("\n== AC-20: store verify ==")
    check_verify(p)
    return p.result()


if __name__ == "__main__":
    sys.exit(main())
