"""Shared helpers for the v2 tests: a temp home + runtime.db per test, the hook and
CLI as subprocesses (process start included, as in production), and a Probe that
counts checks like the v1 suites do."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
V2_ROOT = TESTS_DIR.parent
MAIN_PY = V2_ROOT / "ulthar" / "__main__.py"
HOOK_ARGV = [sys.executable, "-S", str(MAIN_PY), "hook"]
if str(V2_ROOT) not in sys.path:
    sys.path.insert(0, str(V2_ROOT))


class Probe:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0

    def check(self, name: str, cond: bool, detail: str = "") -> None:
        if cond:
            self.passed += 1
            print(f"OK   {name}")
        else:
            self.failed += 1
            print(f"FAIL {name}" + (f"\n       {detail}" if detail else ""))

    def result(self) -> int:
        print(f"\nRESULT: {self.passed} passed, {self.failed} failed")
        return 0 if self.failed == 0 else 1


class Sandbox:
    """A temp root with home/, work/ and runtime.db; removed by cleanup()."""

    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="ulthar-v2-test-"))
        self.home = self.root / "home"
        self.work = self.root / "work"
        self.home.mkdir()
        self.work.mkdir()
        self.db = self.root / "runtime.db"

    def env(self, **extra: str) -> dict:
        env = dict(os.environ, HOME=str(self.home), USERPROFILE=str(self.home),
                   ULTHAR_RUNTIME_DB=str(self.db))
        env.pop("ULTHAR_CONFIG", None)
        env.pop("ULTHAR_GUARDS_DIR", None)
        env.update(extra)
        return env

    def hook(self, event: str, payload: dict, **extra_env: str) -> subprocess.CompletedProcess:
        body = dict(payload, hook_event_name=event)
        body.setdefault("cwd", str(self.work))
        return subprocess.run(HOOK_ARGV + ["--event", event], input=json.dumps(body),
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=self.env(**extra_env), timeout=60)

    def cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(MAIN_PY), *args], capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              env=self.env(), timeout=600)

    def query(self, sql: str, params: tuple = ()) -> list[tuple]:
        """Through ulthar.db.connect, i.e. with the production connection settings: a
        default sqlite3 close checkpoints and deletes the WAL, which would make every
        following hook call pay for re-creating it and skew AC-9."""
        from ulthar import db as db_mod  # noqa: PLC0415

        conn = db_mod.connect(self.db)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


def write_jsonl(path: Path, records: list[dict], mode: str = "w") -> None:
    with open(path, mode, encoding="utf-8", newline="\n") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _user(uuid: str, content, **flags) -> dict:
    return {"type": "user", "uuid": uuid, "timestamp": "2026-11-10T00:00:00Z",
            "message": {"role": "user", "content": content}, **flags}


def _assistant(uuid: str, blocks: list) -> dict:
    return {"type": "assistant", "uuid": uuid, "message": {"role": "assistant", "content": blocks}}


def synthetic_transcript(tag: str = "t") -> tuple[list[dict], dict]:
    """Records covering every Episode kind and every owner_msg exclusion, with the
    Episode counts they must produce."""
    u = lambda n: f"{tag}-{n}"  # noqa: E731
    records = [
        {"type": "system", "uuid": u("sys"), "content": "init"},
        _user(u(1), "please build the thing"),
        _user(u(2), [{"type": "text", "text": "second owner message"}]),
        _user(u(3), "<system-reminder>noise</system-reminder>"),
        _user(u(4), "[Request interrupted by user]"),
        _user(u(5), "Caveat: local command output"),
        _user(u(6), "<command-name>/clear</command-name>"),
        _user(u(7), "<local-command-stdout>x</local-command-stdout>"),
        _user(u(8), "meta text", isMeta=True),
        _user(u(9), "sidechain prompt", isSidechain=True),
        _user(u(10), "This session is being continued from a previous conversation.",
              isCompactSummary=True),
        _user(u(11), "<task-notification><result>done: report</result></task-notification>"),
        _assistant(u(12), [{"type": "tool_use", "id": f"{tag}-ask", "name": "AskUserQuestion",
                            "input": {"questions": [{"question": "OK?"}]}}]),
        _user(u(13), [{"type": "tool_result", "tool_use_id": f"{tag}-ask",
                       "content": "Your questions have been answered: OK"}]),
        _assistant(u(14), [{"type": "tool_use", "id": f"{tag}-ag", "name": "Agent",
                            "input": {"prompt": "do it"}}]),
        _user(u(15), [{"type": "tool_result", "tool_use_id": f"{tag}-ag",
                       "content": [{"type": "text", "text": "agent final report"}]}]),
        _assistant(u(16), [{"type": "tool_use", "id": f"{tag}-ag2", "name": "Agent",
                            "input": {"prompt": "bg"}}]),
        _user(u(17), [{"type": "tool_result", "tool_use_id": f"{tag}-ag2",
                       "content": [{"type": "text", "text": "Async agent launched successfully."}]}]),
        _user(u(18), "third owner message"),
    ]
    expected = {"owner_msg": 3, "ask": 1, "answer": 1, "compact_summary": 1, "agent_report": 2}
    return records, expected


def load_fixture(name: str):
    return json.loads((TESTS_DIR / "fixtures" / name).read_text(encoding="utf-8"))
