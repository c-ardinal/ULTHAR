"""Shared helpers for the v2 tests: a temp home + runtime.db per test, the hook and
CLI as subprocesses (process start included, as in production), and a Probe that
counts checks like the v1 suites do.

Every Sandbox's work/ is a project whose `.claude/ulthar.json` points knowledge_root
at <sandbox>/knowledge and the extractor at tests/fake_extractor.py: no test ever
launches the real `claude`. ULTHAR_STATE_DIR (host id, global store) is
<sandbox>/home/.claude/ulthar for the hook and CLI subprocesses and, inside
`with sb.inproc():`, for in-process calls too."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
V2_ROOT = TESTS_DIR.parent
MAIN_PY = V2_ROOT / "ulthar" / "__main__.py"
HOOK_ARGV = [sys.executable, "-S", str(MAIN_PY), "hook"]
FAKE_EXTRACTOR = TESTS_DIR / "fake_extractor.py"
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

    def __init__(self, extractor: list[str] | None = None) -> None:
        self.root = Path(tempfile.mkdtemp(prefix=SANDBOX_PREFIX))
        self.home = self.root / "home"
        self.work = self.root / "work"
        self.home.mkdir()
        self.work.mkdir()
        self.db = self.root / "runtime.db"
        self.state = self.home / ".claude" / "ulthar"
        self.kr = self.root / "knowledge"
        self.configure(extractor or ["none"])

    def configure(self, extractor: list[str], work: Path | None = None, **extra) -> None:
        """(Re)writes the project's .claude/ulthar.json; `extractor` = fake_extractor args."""
        work = work or self.work
        cfg = {"knowledge_root": str(self.kr if work == self.work else work / "knowledge"),
               "extractor": {"cmd": [sys.executable, "-S", str(FAKE_EXTRACTOR), *extractor]},
               # the R1-a/R1-b tests predate GOAL-08 (R1-c AC-11): every sandbox file is a
               # non-product file unless a test configures its own non_product_globs
               "non_product_globs": ["**"],
               **extra}
        (work / ".claude").mkdir(parents=True, exist_ok=True)
        (work / ".claude" / "ulthar.json").write_text(json.dumps(cfg), encoding="utf-8")

    def env(self, **extra: str) -> dict:
        env = dict(os.environ, HOME=str(self.home), USERPROFILE=str(self.home),
                   ULTHAR_RUNTIME_DB=str(self.db), ULTHAR_STATE_DIR=str(self.state))
        env.pop("ULTHAR_CONFIG", None)
        env.pop("ULTHAR_GUARDS_DIR", None)
        env.pop("ULTHAR_EXTRACTOR", None)
        env.update(extra)
        return env

    @contextmanager
    def inproc(self):
        """In-process calls see this sandbox's state dir and runtime.db."""
        saved = {k: os.environ.get(k) for k in ("ULTHAR_STATE_DIR", "ULTHAR_RUNTIME_DB")}
        os.environ.update(ULTHAR_STATE_DIR=str(self.state), ULTHAR_RUNTIME_DB=str(self.db))
        try:
            yield
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def store(self, work: Path | None = None):
        from ulthar import store as store_mod  # noqa: PLC0415

        return store_mod.Store.for_cwd(work or self.work)

    def kquery(self, sql: str, params: tuple = (), work: Path | None = None) -> list[tuple]:
        """A query on the project's knowledge index (caught up with the JSONL first)."""
        with self.inproc(), self.store(work).reader() as conn:
            return conn.execute(sql, params).fetchall() if conn is not None else []

    def episodes(self, kind: str | None = None, session: str | None = None) -> list[dict]:
        rows = self.kquery("SELECT json FROM records WHERE kind='episode' ORDER BY seq")
        eps = [json.loads(r[0]) for r in rows]
        return [e for e in eps if (kind is None or e["kind"] == kind)
                and (session is None or e["session_id"] == session)]

    def jsonl(self, name: str, work: Path | None = None) -> list[dict]:
        path = (self.kr if work is None else work / "knowledge") / ".ulthar" / name
        if not path.exists():
            return []
        return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]

    def hook(self, event: str, payload: dict, **extra_env: str) -> subprocess.CompletedProcess:
        body = dict(payload, hook_event_name=event)
        body.setdefault("cwd", str(self.work))
        return subprocess.run(HOOK_ARGV + ["--event", event], input=json.dumps(body),
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=self.env(**extra_env), timeout=60)

    def cli(self, *args: str, cwd: Path | None = None, **extra_env: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(MAIN_PY), *args], capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              env=self.env(**extra_env), timeout=600, cwd=str(cwd or self.work))

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
        """Waits (bounded) for this sandbox's background extractions and their processes
        to end before removing it, so no test leaves an extractor behind (R1-b AC-19)."""
        deadline = time.monotonic() + CLEANUP_WAIT_SEC
        while time.monotonic() < deadline:
            pending = 0
            if self.db.exists():
                try:
                    pending = self.query("SELECT COUNT(*) FROM extractions WHERE status='pending'")[0][0]
                except Exception:  # noqa: BLE001 -- a broken db has nothing to wait for
                    pending = 0
            if not pending and not processes_matching(str(self.root)):
                break
            time.sleep(0.2)
        shutil.rmtree(self.root, ignore_errors=True)


CLEANUP_WAIT_SEC = 30
SANDBOX_PREFIX = "ulthar-v2-test-"


def processes_matching(marker: str) -> list[tuple[int, str]]:
    """(pid, command line) of every live process whose command line contains `marker`
    (case-insensitive), this process excluded."""
    import psutil  # noqa: PLC0415 -- tests only (v2/ulthar itself stays psutil-free here)

    needle = marker.lower().replace("\\", "/")
    out = []
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmd = " ".join(proc.info["cmdline"] or [])
        except (psutil.Error, TypeError):
            continue
        if proc.info["pid"] != os.getpid() and needle in cmd.lower().replace("\\", "/"):
            out.append((proc.info["pid"], cmd[:200]))
    return out


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
