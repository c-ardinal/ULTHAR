"""The single dispatcher, end to end as a subprocess:
  AC-2  every hook event writes >= 1 events row with the required columns filled;
  AC-3  PostToolUse of Edit/Write/MultiEdit/NotebookEdit records each path's
        `git hash-object` blob hash in events.post_hashes;
  AC-4  internal failures (runtime.db write, guard import) BLOCK a PreToolUse (exit 2)
        with the reason and recovery steps.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

from helpers import Probe, Sandbox, load_fixture, synthetic_transcript, write_jsonl

REQUIRED = ("session_id", "hook", "tool", "decision", "guard_id")
AGENT_REQUIRED = ("agent_id", "agent_type")


def _subst(node, mapping: dict):
    if isinstance(node, str):
        for key, value in mapping.items():
            node = node.replace(key, value)
        return node
    if isinstance(node, list):
        return [_subst(x, mapping) for x in node]
    if isinstance(node, dict):
        return {k: _subst(v, mapping) for k, v in node.items()}
    return node


def _rows_since(sb: Sandbox, last_id: int) -> list[dict]:
    cols = ["id", "session_id", "agent_id", "agent_type", "hook", "tool", "decision",
            "guard_id", "paths", "post_hashes", "detail"]
    rows = sb.query(f"SELECT {', '.join(cols)} FROM events WHERE id > ? AND guard_id != 'ingest'"
                    " ORDER BY id", (last_id,))
    return [dict(zip(cols, r)) for r in rows]


def _max_id(sb: Sandbox) -> int:
    if not sb.db.exists():
        return 0
    return sb.query("SELECT COALESCE(MAX(id), 0) FROM events")[0][0]


def check_every_event_writes_a_row(p: Probe) -> None:
    sb = Sandbox()
    try:
        transcript = sb.root / "session.jsonl"
        records, expected = synthetic_transcript("ev")
        write_jsonl(transcript, records)
        fixtures = _subst(load_fixture("hook_events.json"),
                          {"${TRANSCRIPT}": str(transcript).replace("\\", "/")})
        written = 0
        for fx in fixtures:
            before = _max_id(sb)
            proc = sb.hook(fx["event"], fx["payload"])
            rows = _rows_since(sb, before)
            need = REQUIRED + (AGENT_REQUIRED if fx["payload"].get("agent_id") else ())
            blanks = [f"{r['id']}.{c}" for r in rows for c in need if not r[c]]
            ok = proc.returncode == 0 and len(rows) >= 1 and not blanks
            written += ok
            p.check(f"{fx['event']}: rc=0, {len(rows)} row(s), blank required columns={blanks}",
                    ok, proc.stderr[-400:])
        p.check(f"AC-2: rows written for {written}/9 events", written == 9)
        kinds = dict(sb.query("SELECT kind, COUNT(*) FROM episodes GROUP BY kind"))
        p.check("the first hook call ingested the transcript (Episodes by kind)",
                kinds == expected, f"{kinds} != {expected}")
        sessions = sb.query("SELECT session_id, transcript_path, \"offset\" FROM sessions")
        p.check("sessions row carries transcript_path and offset",
                len(sessions) == 1 and sessions[0][2] == transcript.stat().st_size, str(sessions))
    finally:
        sb.cleanup()


def check_deny_is_recorded(p: Probe) -> None:
    sb = Sandbox()
    try:
        proc = sb.hook("PreToolUse", {"session_id": "fx-deny", "tool_name": "Bash",
                                      "tool_input": {"command": "git push --force origin main"}})
        rows = _rows_since(sb, 0)
        p.check("a guard block exits 2 and records decision=deny with the guard id",
                proc.returncode == 2 and rows and rows[-1]["decision"] == "deny"
                and rows[-1]["guard_id"] == "git_discipline", f"rc={proc.returncode} {rows}")
    finally:
        sb.cleanup()


def _git_hash_object(path: Path) -> str:
    proc = subprocess.run(["git", "hash-object", str(path)], capture_output=True, text=True,
                          cwd=str(path.parent))
    return proc.stdout.strip()


def check_post_hashes(p: Probe) -> None:
    sb = Sandbox()
    try:
        subprocess.run(["git", "init", "-q", str(sb.work)], check=True)
        work = str(sb.work).replace("\\", "/")
        matched = 0
        for fx in _subst(load_fixture("post_writes.json"), {"${DIR}": work}):
            target = sb.work / fx["file"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(fx["content"].encode("utf-8"))
            before = _max_id(sb)
            proc = sb.hook("PostToolUse", {"session_id": "fx-post", "tool_name": fx["tool_name"],
                                           "tool_input": fx["tool_input"], "tool_response": {}})
            rows = _rows_since(sb, before)
            hashes = json.loads(rows[-1]["post_hashes"] or "{}") if rows else {}
            recorded = list(hashes.values())
            expected = _git_hash_object(target)
            ok = proc.returncode == 0 and recorded == [expected] and len(expected) == 40
            matched += ok
            p.check(f"{fx['tool_name']}: post_hashes {recorded} == git hash-object {expected}",
                    ok, proc.stderr[-300:])
        p.check(f"AC-3: {matched}/4 post hashes match git hash-object", matched == 4)
    finally:
        sb.cleanup()


def _set_db_readonly(sb: Sandbox, readonly: bool) -> None:
    for name in (sb.db.name, sb.db.name + "-wal", sb.db.name + "-shm"):
        f = sb.db.parent / name
        if f.exists():
            os.chmod(f, stat.S_IREAD if readonly else stat.S_IWRITE | stat.S_IREAD)


def _journal_rows(sb: Sandbox) -> list[dict]:
    """Rows still pending: the journal plus claims a failed replay left behind
    (a call whose db open succeeded but whose writes failed), in time order."""
    files = [sb.db.with_name(sb.db.name + ".pending.jsonl"),
             *sb.root.glob(sb.db.name + ".pending.jsonl.*.replaying")]
    rows = [json.loads(ln) for f in files if f.is_file()
            for ln in f.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return sorted(rows, key=lambda r: r["ts"])


def _journaled_events(sb: Sandbox) -> list[dict]:
    return [r for r in _rows_since(sb, 0) if "journaled" in json.loads(r["detail"] or "{}")]


def check_db_write_failure_blocks_and_journals(p: Probe) -> None:
    sb = Sandbox()
    try:
        init = sb.cli("db", "init", "--db", str(sb.db))
        target = sb.work / "kept.txt"
        target.write_text("evidence\n", encoding="utf-8")
        _set_db_readonly(sb, True)
        pre = sb.hook("PreToolUse", {"session_id": "fx-fault", "tool_name": "Bash",
                                     "tool_input": {"command": "ls"}})
        p.check("AC-4 fault 1 (runtime.db read-only): PreToolUse exits 2",
                init.returncode == 0 and pre.returncode == 2, f"rc={pre.returncode}")
        p.check("  ... names the cause, the recovery steps and where the event went",
                "runtime database" in pre.stderr and "To recover" in pre.stderr
                and "fallback journal" in pre.stderr, pre.stderr[:700])
        post = sb.hook("PostToolUse", {"session_id": "fx-fault", "tool_name": "Write",
                                       "tool_input": {"file_path": str(target)},
                                       "tool_response": {}})
        stop = sb.hook("Stop", {"session_id": "fx-fault", "stop_hook_active": False})
        p.check("  ... PostToolUse and Stop proceed (exit 0) with a warning naming the journal",
                post.returncode == 0 and stop.returncode == 0 and "WARNING" in post.stderr
                and "fallback journal" in stop.stderr, f"rc={post.returncode}/{stop.returncode}")
        rows = _journal_rows(sb)
        hooks = [r["hook"] for r in rows]
        p.check("B2: all three events are in the fallback journal while the db is down",
                hooks == ["PreToolUse", "PostToolUse", "Stop"], str(hooks))
        jpost = next((r for r in rows if r["hook"] == "PostToolUse"), {})
        p.check("  ... the journaled PostToolUse keeps its post-write hash",
                (jpost.get("post_hashes") or {}).get(str(target)) == _git_hash_object(target),
                str(jpost.get("post_hashes")))
        p.check("  ... the journaled PreToolUse is recorded as the deny it was",
                rows and rows[0]["decision"] == "deny", str(rows[:1]))

        _set_db_readonly(sb, False)
        ok = sb.hook("SessionStart", {"session_id": "fx-fault"})
        replayed = _journaled_events(sb)
        p.check("B2: the next successful call replays the journal into events, then removes it",
                ok.returncode == 0 and len(replayed) == 3 and not _journal_rows(sb)
                and not list(sb.root.glob("*.replaying")), f"rc={ok.returncode} {len(replayed)}")
        rpost = next((r for r in replayed if r["hook"] == "PostToolUse"), {})
        p.check("  ... with the PostToolUse post_hashes intact",
                json.loads(rpost.get("post_hashes") or "{}").get(str(target))
                == _git_hash_object(target), str(rpost))
        p.check("  ... and the replaying call recorded its own row after them",
                _rows_since(sb, 0)[-1]["hook"] == "SessionStart")
        # A crash between COMMIT and removing the claim must not duplicate rows.
        journal = sb.db.with_name(sb.db.name + ".pending.jsonl")
        journal.write_text("".join(json.dumps(r) + "\n" for r in rows) + "{torn", encoding="utf-8")
        again = sb.hook("SessionStart", {"session_id": "fx-fault"})
        p.check("B2: replaying already-replayed rows (and a torn line) adds no duplicate",
                again.returncode == 0 and len(_journaled_events(sb)) == 3
                and not journal.exists(), str(len(_journaled_events(sb))))
    finally:
        for f in sb.root.iterdir():
            os.chmod(f, stat.S_IWRITE | stat.S_IREAD)
        sb.cleanup()


def check_unrecordable_post_tool_use_is_loud(p: Probe) -> None:
    sb = Sandbox()
    try:
        sb.cli("db", "init", "--db", str(sb.db))
        sb.db.with_name(sb.db.name + ".pending.jsonl").mkdir()  # the journal cannot be opened
        _set_db_readonly(sb, True)
        post = sb.hook("PostToolUse", {"session_id": "fx-lost", "tool_name": "Write",
                                       "tool_input": {"file_path": str(sb.work / "x.txt")},
                                       "tool_response": {}})
        p.check("B2: db and journal both unwritable -> PostToolUse exits 2 saying NOT recorded",
                post.returncode == 2 and "NOT recorded" in post.stderr,
                f"rc={post.returncode} {post.stderr[:400]}")
    finally:
        for f in sb.root.iterdir():
            os.chmod(f, stat.S_IWRITE | stat.S_IREAD)
        sb.cleanup()


def check_hash_failure_is_marked_not_substituted(p: Probe) -> None:
    sb = Sandbox()
    try:
        target = sb.work / "nogit.txt"
        target.write_text("content\n", encoding="utf-8")
        no_git_path = os.pathsep.join(d for d in os.environ.get("PATH", "").split(os.pathsep)
                                      if d and not (Path(d) / "git.exe").exists()
                                      and not (Path(d) / "git").exists())
        proc = sb.hook("PostToolUse", {"session_id": "fx-hash", "tool_name": "Write",
                                       "tool_input": {"file_path": str(target)},
                                       "tool_response": {}}, PATH=no_git_path)
        rows = _rows_since(sb, 0)
        value = json.loads(rows[-1]["post_hashes"] or "{}").get(str(target)) if rows else None
        detail = json.loads(rows[-1]["detail"] or "{}") if rows else {}
        p.check("B2: git hash-object unavailable -> an explicit hash_failed marker, no substitute",
                proc.returncode == 0 and isinstance(value, dict) and "hash_failed" in value
                and detail.get("hash_failed") == [str(target)], f"{value} {detail}")
    finally:
        sb.cleanup()


def check_guard_import_failure_blocks(p: Probe) -> None:
    sb = Sandbox()
    try:
        broken = sb.root / "broken_guards"
        broken.mkdir()
        (broken / "tool_discipline.py").write_text(
            "raise ImportError('injected fault: guard module cannot load')\n", encoding="utf-8")
        proc = sb.hook("PreToolUse", {"session_id": "fx-fault", "tool_name": "Bash",
                                      "tool_input": {"command": "ls"}},
                       ULTHAR_GUARDS_DIR=str(broken))
        p.check("AC-4 fault 2 (guard import fails): PreToolUse exits 2", proc.returncode == 2,
                f"rc={proc.returncode} {proc.stderr[:300]}")
        p.check("  ... names the guard, the cause and the recovery steps",
                "tool_discipline" in proc.stderr and "injected fault" in proc.stderr
                and "To recover" in proc.stderr, proc.stderr[:600])
        rows = _rows_since(sb, 0)
        p.check("  ... and the block is recorded as a deny event",
                bool(rows) and rows[-1]["decision"] == "deny", str(rows))
    finally:
        sb.cleanup()


def main() -> int:
    p = Probe()
    print("\n== AC-2: one entry, one row per call ==")
    check_every_event_writes_a_row(p)
    check_deny_is_recorded(p)
    print("\n== AC-3: post-write blob hashes ==")
    check_post_hashes(p)
    print("\n== AC-4: internal failure blocks ==")
    check_db_write_failure_blocks_and_journals(p)
    check_guard_import_failure_blocks(p)
    print("\n== B2: no event is lost when runtime.db is down ==")
    check_unrecordable_post_tool_use_is_loud(p)
    check_hash_failure_is_marked_not_substituted(p)
    return p.result()


if __name__ == "__main__":
    sys.exit(main())
