"""R1-b knowledge store:
  AC-1  8 processes x 50 locked appends: seq unique and gap-free, every common and
        type-specific field present, and the incrementally kept index equals the one
        `ulthar index rebuild` regenerates; the default knowledge_root;
  AC-3  a knowledge_root operated by another PC refuses writes (exit != 0, 0 lines);
  AC-14 five known secret formats are replaced by [REDACTED:<kind>] before writing.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

from helpers import V2_ROOT, Probe, Sandbox

from ulthar import store as store_mod

WRITERS = 8
PER_WRITER = 50
FIELDS = {
    "decision": {"status": "provisional", "text": "d", "raw_episode_id": "E-x", "scope": "project",
                 "tags": ["t"], "paths": ["src/**"], "binding": False, "confirm_token_id": None},
    "fact": {"text": "f", "src_path": "a.txt", "src_line": 1, "src_blob": "0" * 40,
             "status": "valid", "valid_from": "2026-11-11T00:00:00Z", "invalid_at": None},
    "term": {"term": "t", "preferred": "p", "forbidden": ["x"], "decision_id": "D-x"},
    "episode": {"session_id": "s", "kind": "owner_msg", "unit_id": None, "text": "e",
                "transcript_ref": None},
}

WRITER_SCRIPT = """
import json, sys
sys.path.insert(0, sys.argv[1])
from ulthar import store as store_mod
fields = json.loads(sys.argv[4])
kinds = list(fields)
store = store_mod.Store.for_cwd(sys.argv[2])
for i in range(int(sys.argv[3])):
    kind = kinds[i % len(kinds)]
    with store.transaction({"session_id": "w" + sys.argv[5], "hook": "test"}) as tx:
        tx.add(kind, dict(fields[kind], text=f"w{sys.argv[5]}-{i}") if "text" in fields[kind]
               else dict(fields[kind], term=f"w{sys.argv[5]}-{i}"))
"""


def _all_records(sb: Sandbox) -> list[tuple[str, dict]]:
    return [(kind, rec) for kind, name in store_mod.FILES.items() for rec in sb.jsonl(name)]


def check_concurrent_appends(p: Probe) -> None:
    sb = Sandbox()
    try:
        procs = [subprocess.Popen([sys.executable, "-c", WRITER_SCRIPT, str(V2_ROOT), str(sb.work),
                                   str(PER_WRITER), json.dumps(FIELDS), str(w)], env=sb.env(),
                                  stderr=subprocess.PIPE, text=True)
                 for w in range(WRITERS)]
        errors = [pr.communicate(timeout=300)[1] for pr in procs]
        p.check("AC-1: all 8 writer processes exit 0",
                all(pr.returncode == 0 for pr in procs), "\n".join(e[-300:] for e in errors if e))
        recs = _all_records(sb)
        seqs = sorted(r["seq"] for _, r in recs)
        total = WRITERS * PER_WRITER
        p.check(f"AC-1: {len(recs)} records, seq duplicates 0 and gaps 0 (1..{total})",
                seqs == list(range(1, total + 1)),
                f"dups={len(seqs) - len(set(seqs))} first={seqs[:5]}")
        missing = [f"{kind}:{r.get('seq')}:{f}" for kind, r in recs
                   for f in store_mod.COMMON_FIELDS + store_mod.KIND_FIELDS[kind] if f not in r]
        p.check(f"AC-1: missing common/type-specific fields: {len(missing)}", not missing,
                str(missing[:5]))
        p.check("AC-1: every record carries this project's id and its writer",
                all(r["project_id"] == sb.store().project_id and r["writer"]["hook"] == "test"
                    for _, r in recs))
        with sb.inproc(), sb.store().reader() as conn:
            before = store_mod.dump_index(conn)
        rebuilt = sb.cli("index", "rebuild")
        with sb.inproc(), sb.store().reader() as conn:
            after = store_mod.dump_index(conn)
        p.check("AC-1: `ulthar index rebuild` exits 0", rebuilt.returncode == 0,
                rebuilt.stdout + rebuilt.stderr[-300:])
        diff = sum(1 for k in before if before[k] != after[k])
        p.check(f"AC-1: index kept while appending == rebuilt index (tables differing: {diff})",
                diff == 0 and len(before["records"]) == total)
    finally:
        sb.cleanup()


def check_index_catches_up(p: Probe) -> None:
    """A crash between the JSONL append and the index commit: the next reader catches up."""
    sb = Sandbox()
    try:
        with sb.inproc():
            store = sb.store()
            with store.transaction({"hook": "test"}) as tx:
                tx.add("episode", FIELDS["episode"])
            line = dict(FIELDS["episode"], id="E-manual", seq=2, project_id=store.project_id,
                        writer={"hook": "test"}, ts="2026-11-11T00:00:00Z", supersedes=[])
            with open(store.dir / "episodes.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps(line) + "\n")
            with store.reader() as conn:
                n = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        p.check("an index behind its JSONL (crash after append) is caught up on open", n == 2, str(n))
    finally:
        sb.cleanup()


def check_default_root(p: Probe) -> None:
    sb = Sandbox()
    try:
        (sb.work / ".claude" / "ulthar.json").unlink()
        with sb.inproc():
            store = sb.store()
        want = sb.state / "projects" / store_mod.project_id_for(sb.work.resolve())
        p.check("knowledge_root defaults to <state>/projects/<project_id>/ (U-06)",
                store.root == want, f"{store.root} != {want}")
    finally:
        sb.cleanup()


def check_other_pc_refused(p: Probe) -> None:
    sb = Sandbox()
    try:
        src = sb.work / "notes.txt"
        src.write_text("line one\n", encoding="utf-8")
        first = sb.cli("fact", "add", "--src", "notes.txt:1", "the first fact")
        host_file = sb.kr / ".ulthar" / "host.json"
        recorded = json.loads(host_file.read_text(encoding="utf-8")).get("host_id")
        p.check("the first write records this PC's id in the knowledge_root",
                first.returncode == 0 and recorded == store_mod.local_host_id(),
                first.stderr[-300:])
        host_file.write_text(json.dumps({"host_id": "another-pc-0000"}), encoding="utf-8")
        before = [len(sb.jsonl(n)) for n in store_mod.FILES.values()]
        refused = sb.cli("fact", "add", "--src", "notes.txt:1", "from the wrong PC")
        hook = sb.hook("UserPromptSubmit", {"session_id": "pc", "prompt": "a new owner message",
                                            "prompt_id": "pc-1"})
        after = [len(sb.jsonl(n)) for n in store_mod.FILES.values()]
        p.check("AC-3: a write from another PC id is refused (exit != 0) naming the reason",
                refused.returncode != 0 and "another PC" in refused.stderr, refused.stderr[-300:])
        p.check("AC-3: ... and 0 JSONL lines were appended (CLI and hook alike)",
                before == after, f"{before} -> {after}")
        p.check("  ... the hook call itself still answers (a warning, not a crash)",
                hook.returncode == 0 and "another PC" in hook.stderr, hook.stderr[-300:])
    finally:
        sb.cleanup()


def secrets() -> dict[str, str]:
    """Fake values in the real formats, assembled at run time (no scanner trips on them)."""
    return {
        "aws_access_key": "AKIA" + "IOSFODNN7EXAMPLE",
        "github_token": "gh" + "p_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8",
        "anthropic_key": "sk-" + "ant-api03-" + "Zx9" * 14,
        "slack_token": "xo" + "xb-" + "123456789012-abcdefABCDEF",
        "private_key": "-----BEGIN " + "RSA PRIVATE KEY-----\nMIIEow" + "Q" * 40
                       + "\n-----END RSA " + "PRIVATE KEY-----",
    }


def check_secret_redaction(p: Probe) -> None:
    sb = Sandbox()
    try:
        values = secrets()
        prompt = "keys for later: " + " | ".join(f"{k}={v}" for k, v in values.items())
        proc = sb.hook("UserPromptSubmit", {"session_id": "sec", "prompt": prompt,
                                            "prompt_id": "sec-1"})
        raw = (sb.kr / ".ulthar" / "episodes.jsonl").read_text(encoding="utf-8")
        found = [k for k in values if f"[REDACTED:{k}]" in raw]
        left = [k for k, v in values.items() if v in raw or v[:12] in raw.replace("\\n", "\n")]
        p.check(f"AC-14: secrets detected {len(found)}/5", proc.returncode == 0 and len(found) == 5,
                f"found={found} rc={proc.returncode} {proc.stderr[-200:]}")
        p.check(f"AC-14: original secret text left in the JSONL: {len(left)}", not left, str(left))
    finally:
        sb.cleanup()


def main() -> int:
    p = Probe()
    print("\n== AC-1: JSONL source of truth + index ==")
    check_concurrent_appends(p)
    check_index_catches_up(p)
    check_default_root(p)
    print("\n== AC-3: one PC per project ==")
    check_other_pc_refused(p)
    print("\n== AC-14: secrets ==")
    check_secret_redaction(p)
    return p.result()


if __name__ == "__main__":
    sys.exit(main())
