"""R1-b knowledge-store integrity (review r1b-1 finding 4) and AC-20:
  * a malformed middle line fails closed: reads, rebuild and the hook stop with an error
    naming the line, nothing is skipped and the index does not advance;
  * a same-size rewrite is caught (mtime differs -> chain recomputed -> mismatch);
  * a multi-file transaction is all or nothing: a failure while writing the second file
    cuts the first back; an interrupted one (manifest left, torn bytes) is undone at the
    next open;
  * a runtime.db rollback after a store commit (inside a transcript ingest) is repaired
    from the kept manifest, and a missing chain source is reported (review r1b-2 item 4);
  * txn.json is replaced atomically: a failure during the replacement leaves it whole
    and the next open recovers (AC-20);
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


def check_runtime_rollback_recovered(p: Probe) -> None:
    """Review r1b-2 item 4: the store committed during a transcript ingest, then the
    ingest's runtime.db transaction failed (its chain row rolled back). The manifest is
    kept until that commit, so the next open re-records the row; and a chain source that
    is simply missing is reported by verify, not skipped."""
    from ulthar import db as db_mod  # noqa: PLC0415
    from ulthar import ingest as ingest_mod  # noqa: PLC0415

    sb = Sandbox()
    try:
        path = sb.root / "t.jsonl"
        path.write_text(json.dumps({"type": "user", "uuid": "u1", "message": {
            "role": "user", "content": "最初の発言"}}, ensure_ascii=False) + "\n", encoding="utf-8")
        real = ingest_mod._store_episodes

        def then_fail(*a, **k):
            real(*a, **k)
            raise RuntimeError("injected: failure before the ingest's runtime.db commit")

        with sb.inproc():
            ingest_mod._store_episodes = then_fail
            conn = db_mod.connect(sb.db)
            try:
                ingest_mod.ingest(conn, "s", str(path), store=sb.store())
                err = "no error"
            except RuntimeError as exc:
                err = str(exc)
            finally:
                ingest_mod._store_episodes = real
            rows_after_fail = conn.execute("SELECT COUNT(*) FROM store_chain").fetchone()[0]
            conn.close()
        manifest = (sb.kr / ".ulthar" / integrity.MANIFEST).exists()
        pending = _verify(sb)
        p.check("failure after the store commit, before the ingest's runtime commit: the "
                "runtime chain row is gone, the manifest is kept, verify reports it",
                "injected" in err and rows_after_fail == 0 and manifest
                and pending.returncode != 0, f"{err} rows={rows_after_fail} manifest={manifest}")
        assert _read_error(sb) is None
        fixed = _verify(sb)
        chains = sb.query("SELECT name, size FROM store_chain")
        size = (sb.kr / ".ulthar" / "episodes.jsonl").stat().st_size
        p.check("  ... the next open re-records the row from index + manifest; verify is clean",
                fixed.returncode == 0 and chains == [("episodes.jsonl", size)]
                and not (sb.kr / ".ulthar" / integrity.MANIFEST).exists(),
                fixed.stdout[-300:] + str(chains))
        sb.query("DELETE FROM store_chain")
        missing = _verify(sb)
        p.check("a missing runtime.db chain source is reported by verify (never skipped)",
                missing.returncode != 0 and "runtime.db has no chain record" in missing.stdout,
                missing.stdout[-300:])
    finally:
        sb.cleanup()


def check_manifest_replacement_atomic(p: Probe) -> None:
    """AC-20 (spec afa6aae7): txn.json is replaced atomically. A failure injected during
    the replacement leaves the previous manifest whole, and the next open recovers 1/1."""
    sb = Sandbox()
    try:
        _seed(sb, 1)
        store_dir = sb.kr / ".ulthar"
        path = _decisions_path(sb)
        before = path.read_bytes()
        with open(path, "ab") as fh:  # an interrupted transaction: torn bytes + its manifest
            fh.write(b'{"seq": 9, "id": "D-torn", "text": "half')
        integrity.write_manifest(store_dir, {"files": {"decisions.jsonl": {
            "old": len(before), "new": len(before) + 300, "chain": "0" * 64}}})
        original = (store_dir / integrity.MANIFEST).read_bytes()
        real = integrity.os.replace

        def dies(*a, **k):
            raise SystemExit("injected: process died while replacing txn.json")

        integrity.os.replace = dies
        try:
            integrity.write_manifest(store_dir, {"files": {}, "committed": [["x", 1, "y"]]})
            died = False
        except SystemExit:
            died = True
        finally:
            integrity.os.replace = real
        intact = (store_dir / integrity.MANIFEST).read_bytes() == original
        no_temp = not list(store_dir.glob(integrity.MANIFEST + ".*.tmp"))
        err = _read_error(sb)
        recovered = (err is None and path.read_bytes() == before
                     and not (store_dir / integrity.MANIFEST).exists())
        p.check("failure during the txn.json replacement: the previous manifest is intact "
                "(not truncated) and no temp file is left", died and intact and no_temp)
        p.check(f"AC-20: the next open recovers the interrupted transaction "
                f"{int(recovered)}/1", recovered, str(err))
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
    check_runtime_rollback_recovered(p)
    check_manifest_replacement_atomic(p)
    return p.result()


if __name__ == "__main__":
    sys.exit(main())
