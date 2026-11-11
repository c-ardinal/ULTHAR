"""Project knowledge store (CORE-04/05, design §6.1, R1-b AC-1/AC-3/AC-20).

`<knowledge_root>/.ulthar/{decisions,facts,terms,episodes}.jsonl` are the source of
truth; `index.db` (SQLite) is a cache regenerated from them. A write happens only under
the project lock (`.ulthar/lock`): host check -> settle (finish or undo an interrupted
transaction; fail closed on content ulthar did not write) -> seq = max + 1 per record
-> write-ahead manifest -> JSONL appends -> index commit (with each file's size, mtime
and chain value) -> the chain values in runtime.db -> manifest removed (ulthar.integrity
has the protocol). Every open compares each file's size and mtime with the index; a
difference is resolved by the chain (an unchanged file only had its mtime touched) or
is an IntegrityError: a malformed, inserted, deleted or rewritten line is never
skipped or absorbed silently.

Records never change in place: a status change appends a new version with the same
`id` and a higher seq (the index's `current` table holds the latest version per id);
`supersedes` lists other ids a record replaces (MEM-11).

knowledge_root: `<repo>/.claude/ulthar.json` {"knowledge_root": "..."} (relative to the
repo root), else `<state>/projects/<project_id>/` with <state> = $ULTHAR_STATE_DIR or
~/.claude/ulthar (U-06). The PC that first writes a knowledge_root records its host id
there; any other host id is refused (D-22).
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import zlib
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

try:
    from _sha2 import sha256  # the builtin; hashlib would load OpenSSL (~8 ms per hook call)
except ImportError:  # pragma: no cover -- interpreters without the builtin module
    from hashlib import sha256

from . import db as db_mod
from . import integrity
from . import journal
from .integrity import IntegrityError, StoreError
from .redact import redact

STATE_ENV = "ULTHAR_STATE_DIR"
PROJECT_CONFIG = Path(".claude") / "ulthar.json"
STORE_DIRNAME = ".ulthar"
FILES = {"decision": "decisions.jsonl", "fact": "facts.jsonl", "term": "terms.jsonl",
         "episode": "episodes.jsonl"}
ID_PREFIX = {"decision": "D", "fact": "F", "term": "T", "episode": "E"}
COMMON_FIELDS = ("id", "seq", "project_id", "writer", "ts", "supersedes")
KIND_FIELDS = {
    "decision": ("status", "text", "raw_episode_id", "scope", "tags", "paths", "binding",
                 "confirm_token_id"),
    "fact": ("text", "src_path", "src_line", "src_blob", "status", "valid_from", "invalid_at"),
    "term": ("term", "preferred", "forbidden", "decision_id"),
    "episode": ("session_id", "kind", "unit_id", "text", "transcript_ref"),
}
DEFAULTS = {"supersedes": [], "tags": [], "paths": [], "forbidden": [], "binding": False,
            "scope": "project"}
GLOBAL_PROJECT_ID = "(global)"
INDEX_VERSION = 2
WAL_TRUNCATE_BYTES = 1 << 20

_INDEX_DDL = """
CREATE TABLE IF NOT EXISTS records (
  seq INTEGER PRIMARY KEY, kind TEXT NOT NULL, id TEXT NOT NULL, session_id TEXT,
  dedupe_key TEXT, json TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS records_dedupe ON records(kind, session_id, dedupe_key)
  WHERE dedupe_key IS NOT NULL;
CREATE TABLE IF NOT EXISTS current (
  kind TEXT NOT NULL, id TEXT NOT NULL, seq INTEGER NOT NULL, status TEXT, json TEXT NOT NULL,
  PRIMARY KEY (kind, id));
CREATE TABLE IF NOT EXISTS files (
  name TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, chain TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS chain (
  name TEXT NOT NULL, size INTEGER NOT NULL, chain TEXT NOT NULL, PRIMARY KEY (name, size));
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class HostMismatch(StoreError):
    pass


def state_dir() -> Path:
    raw = os.environ.get(STATE_ENV)
    return Path(raw) if raw else Path.home() / ".claude" / "ulthar"


def _machine_id() -> str | None:
    if os.name == "nt":
        import winreg  # noqa: PLC0415 -- platform-specific stdlib (as journal's msvcrt)

        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0,
                                winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
                return str(winreg.QueryValueEx(key, "MachineGuid")[0])
        except OSError:
            return None
    try:
        return Path("/etc/machine-id").read_text(encoding="ascii").strip() or None
    except OSError:
        return None


def local_host_id() -> str:
    """This PC's id: a hash of the OS machine id (Windows MachineGuid, /etc/machine-id),
    never the raw value (knowledge_root may be shared through git); without one, a
    random id created once in <state>/host_id."""
    machine = _machine_id()
    if machine:
        return "m-" + sha256(f"ulthar-host\0{machine}".encode("utf-8")).hexdigest()[:32]
    path = state_dir() / "host_id"
    try:
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    value = os.urandom(16).hex()
    path.write_text(value + "\n", encoding="utf-8")
    return value


def repo_root(cwd: str | Path) -> Path:
    start = Path(cwd).resolve()
    for d in (start, *start.parents):
        if (d / ".git").exists():
            return d
    return start


def project_id_for(root: Path) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", str(root))


def project_config(root: Path) -> dict:
    path = root / PROJECT_CONFIG
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise StoreError(f"{path}: {exc}") from exc
    try:
        cfg = json.loads(raw)
    except ValueError as exc:
        raise StoreError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(cfg, dict):
        raise StoreError(f"{path} must hold a JSON object")
    return cfg


def make_id(kind: str, project_id: str, seq: int, body: str) -> str:
    digest = sha256(f"{kind}\0{project_id}\0{seq}\0{body}".encode("utf-8")).hexdigest()
    return f"{ID_PREFIX[kind]}-{digest[:12]}"


def text_key(text: str) -> str:
    return f"{zlib.crc32(text.encode('utf-8')):08x}-{len(text)}"


class Store:
    def __init__(self, root: Path, project_id: str, repo: Path | None = None,
                 config: dict | None = None):
        self.root = Path(root)
        self.project_id = project_id
        self.repo = repo
        self.config = config or {}
        self.dir = self.root / STORE_DIRNAME
        self.index_path = self.dir / "index.db"

    # ------------------------------------------------------------------ construction
    @classmethod
    def for_cwd(cls, cwd: str | Path) -> "Store":
        repo = repo_root(cwd)
        cfg = project_config(repo)
        project_id = str(cfg.get("project_id") or project_id_for(repo))
        raw = cfg.get("knowledge_root")
        if raw is not None and (not isinstance(raw, str) or not raw):
            raise StoreError(f"{repo / PROJECT_CONFIG}: knowledge_root must be a non-empty string")
        root = (repo / raw) if raw else state_dir() / "projects" / project_id
        return cls(root, project_id, repo, cfg)

    @classmethod
    def global_store(cls) -> "Store":
        return cls(state_dir() / "global", GLOBAL_PROJECT_ID)

    def exists(self) -> bool:
        return self.index_path.exists() or any((self.dir / f).exists() for f in FILES.values())

    @property
    def chain_key(self) -> str:
        """This store's key in runtime.db store_chain."""
        return os.path.normcase(os.path.abspath(self.dir))

    # ------------------------------------------------------------------ index
    def _connect(self) -> sqlite3.Connection:
        """Same latency settings as runtime.db (ulthar.db.connect): schema applied once,
        WAL without fsync per commit and without a checkpoint on every close. An index
        of an older layout is emptied (`_needs_rebuild` then regenerates it)."""
        conn = sqlite3.connect(str(self.index_path), timeout=10, isolation_level=None)
        if conn.execute("PRAGMA user_version").fetchone()[0] != INDEX_VERSION:
            conn.execute("PRAGMA journal_mode=WAL")
            for table in ("records", "current", "files", "chain"):
                conn.execute(f"DROP TABLE IF EXISTS {table}")
            conn.executescript(_INDEX_DDL)
            conn.execute(f"PRAGMA user_version={INDEX_VERSION}")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)
        return conn

    def _close(self, conn: sqlite3.Connection) -> None:
        """Closes, first folding a WAL past WAL_TRUNCATE_BYTES back into the index. With
        SQLite's own auto-checkpoint, a WAL once grown by a bulk write (a full transcript
        ingest) was never reset: every later commit paid a checkpoint fsync and every open
        a WAL scan (~80 ms per hook call, measured)."""
        try:
            wal = os.stat(str(self.index_path) + "-wal").st_size
        except OSError:
            wal = 0
        try:
            if wal > WAL_TRUNCATE_BYTES:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()

    def _file_state(self, name: str) -> tuple[int, int]:
        try:
            st = (self.dir / name).stat()
        except FileNotFoundError:
            return 0, 0
        return st.st_size, st.st_mtime_ns

    def _mismatched(self, conn: sqlite3.Connection) -> list[str]:
        """Files whose (size, mtime) differ from what the index recorded at the last write."""
        known = {n: (s, m) for n, s, m in conn.execute("SELECT name, size, mtime_ns FROM files")}
        out = []
        for name in FILES.values():
            size, mtime = self._file_state(name)
            if known.get(name, (0, 0)) != (size, mtime) and not (size == 0 and name not in known):
                out.append(name)
        return out

    def _settle(self, conn: sqlite3.Connection, rt=None) -> None:
        """Caller holds the lock. Finishes or undoes an interrupted transaction, rebuilds
        a missing/old index, and fails closed (IntegrityError) on any file content the
        chain does not account for. A same-size, same-content touch only refreshes mtime."""
        plan = integrity.read_manifest(self.dir)
        if plan is not None:
            indexed = dict(conn.execute("SELECT name, size FROM files"))
            if all(indexed.get(n) == step["new"] for n, step in plan["files"].items()):
                self._record_runtime_chain(rt, [(n, s["new"], s["chain"])
                                                for n, s in plan["files"].items()])
                integrity.clear_manifest(self.dir)
            else:
                integrity.roll_back(self.dir, plan)
        if not conn.execute("SELECT 1 FROM files LIMIT 1").fetchone() and any(
                self._file_state(n)[0] for n in FILES.values()):
            self._reindex_all(conn, rt)
            return
        for name in self._mismatched(conn):
            size, mtime = self._file_state(name)
            rows = conn.execute("SELECT size, chain FROM chain WHERE name=? ORDER BY size",
                                (name,)).fetchall()
            data = (self.dir / name).read_bytes() if size else b""
            problems = integrity.verify_file(data, name, {"index.db": rows})
            if problems or (not rows and size):
                raise IntegrityError("; ".join(problems) or f"{name}: not recorded by ulthar")
            conn.execute("UPDATE files SET mtime_ns=? WHERE name=?", (mtime, name))

    def _reindex_all(self, conn: sqlite3.Connection, rt=None) -> int:
        """Regenerates the index from the JSONL files, strictly (any malformed line is an
        IntegrityError), checking each file against runtime.db's chain records. Without
        records (a store older than AC-20) a file counts as one append."""
        recorded = self._runtime_chain(rt)
        loaded, files = [], []
        for kind, name in FILES.items():
            path = self.dir / name
            data = path.read_bytes() if path.exists() else b""
            loaded += [(kind, rec) for rec in integrity.parse_strict(
                data, name, COMMON_FIELDS + KIND_FIELDS[kind])]
            rows = recorded.get(name, [])
            problems = integrity.verify_file(data, name, {"runtime.db": rows})
            if problems:
                raise IntegrityError("; ".join(problems))
            if rows:
                final, named = rows[-1][1], [(name, s, c) for s, c in rows]
            elif data:
                final = integrity.chain_next(integrity.CHAIN_START, data)
                named = [(name, len(data), final)]
            else:
                final, named = integrity.CHAIN_START, []
            files.append((name, len(data), self._file_state(name)[1], final, named))
        conn.execute("BEGIN IMMEDIATE")
        try:
            for table in ("records", "current", "files", "chain"):
                conn.execute(f"DELETE FROM {table}")
            for kind, rec in sorted(loaded, key=lambda kr: kr[1]["seq"]):
                _index_record(conn, kind, rec)
            for name, size, mtime, final, named in files:
                conn.execute("INSERT INTO files VALUES (?,?,?,?)", (name, size, mtime, final))
                conn.executemany("INSERT OR REPLACE INTO chain VALUES (?,?,?)", named)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        return len(loaded)

    # ------------------------------------------------------------------ runtime.db chain
    @contextmanager
    def _rt(self, rt):
        if rt is not None:
            yield rt
            return
        own = db_mod.connect(db_mod.resolve_path())
        try:
            yield own
        finally:
            own.close()

    def _record_runtime_chain(self, rt, rows: list[tuple[str, int, str]]) -> None:
        with self._rt(rt) as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO store_chain (store_dir, name, size, chain, ts)"
                " VALUES (?,?,?,?,?)", [(self.chain_key, n, s, c, now_iso()) for n, s, c in rows])

    def _runtime_chain(self, rt=None) -> dict[str, list[tuple[int, str]]]:
        with self._rt(rt) as conn:
            out: dict[str, list[tuple[int, str]]] = {}
            for name, size, chain in conn.execute(
                    "SELECT name, size, chain FROM store_chain WHERE store_dir=? ORDER BY size",
                    (self.chain_key,)):
                out.setdefault(name, []).append((size, chain))
        return out

    # ------------------------------------------------------------------ reading
    @contextmanager
    def reader(self, rt=None):
        """A read connection on an index that matches the files (None: no store yet).
        Raises IntegrityError when the files hold content ulthar did not write."""
        if not self.exists():
            yield None
            return
        try:
            conn = self._connect()
        except sqlite3.Error as exc:
            raise StoreError(f"index {self.index_path}: {exc}") from exc
        try:
            if (self._mismatched(conn) or (self.dir / integrity.MANIFEST).exists()
                    or not conn.execute("SELECT 1 FROM files LIMIT 1").fetchone()):
                with self._lock():
                    self._settle(conn, rt)
            yield conn
        except sqlite3.Error as exc:
            raise StoreError(f"index {self.index_path}: {exc}") from exc
        finally:
            self._close(conn)

    def rebuild(self, rt=None) -> int:
        """`ulthar index rebuild`: regenerates index.db from the JSONL files (strictly)."""
        self.dir.mkdir(parents=True, exist_ok=True)
        with self._lock():
            try:
                conn = self._connect()
                try:
                    plan = integrity.read_manifest(self.dir)
                    if plan is not None:
                        self._settle(conn, rt)
                    return self._reindex_all(conn, rt)
                finally:
                    self._close(conn)
            except sqlite3.Error as exc:
                raise StoreError(f"index {self.index_path}: {exc}") from exc

    def verify(self, rt=None) -> list[str]:
        """`ulthar store verify`: every JSONL file recomputed from its first byte against
        the chain values recorded in index.db and runtime.db. [] = untouched."""
        if not self.exists():
            return []
        with self._lock():
            index_rows: dict[str, list] = {}
            try:
                conn = self._connect()
                try:
                    for name, size, chain in conn.execute("SELECT name, size, chain FROM chain"):
                        index_rows.setdefault(name, []).append((size, chain))
                finally:
                    self._close(conn)
            except sqlite3.Error as exc:
                return [f"index.db unreadable: {exc}"]
            runtime_rows = self._runtime_chain(rt)
            problems = []
            if (self.dir / integrity.MANIFEST).exists():
                problems.append(f"{integrity.MANIFEST}: an unfinished transaction is pending")
            for name in FILES.values():
                path = self.dir / name
                data = path.read_bytes() if path.exists() else b""
                if data and not index_rows.get(name) and not runtime_rows.get(name):
                    problems.append(f"{name}: {len(data)} bytes with no chain record at all")
                    continue
                problems += integrity.verify_file(
                    data, name, {"index.db": index_rows.get(name, []),
                                 "runtime.db": runtime_rows.get(name, [])})
            return problems

    # ------------------------------------------------------------------ writing
    @contextmanager
    def _lock(self):
        try:
            with journal.file_lock(self.dir / "lock"):
                yield
        except OSError as exc:
            raise StoreError(f"project lock {self.dir / 'lock'}: {exc}") from exc

    def _check_host(self) -> None:
        mine = local_host_id()
        path = self.dir / "host.json"
        try:
            owner = json.loads(path.read_text(encoding="utf-8")).get("host_id")
        except FileNotFoundError:
            path.write_text(json.dumps({"host_id": mine}) + "\n", encoding="utf-8")
            (self.dir / ".gitignore").write_text(f"index.db*\nlock\n{integrity.MANIFEST}\n",
                                                 encoding="utf-8")
            return
        except (OSError, ValueError, AttributeError) as exc:
            raise StoreError(f"{path} is unreadable: {exc}") from exc
        if owner != mine:
            raise HostMismatch(
                f"knowledge_root {self.root} is operated by another PC (host id {owner}); this "
                f"PC is {mine}. One project is run from one PC only (D-22): writes refused.")

    @contextmanager
    def transaction(self, writer: dict, rt=None):
        """Locked write: yields a Tx; on clean exit its JSONL appends, index rows and chain
        values (index.db and runtime.db) are written as one recoverable unit. Nothing is
        written if the body raises. `rt`: the caller's runtime.db connection (needed when
        the caller holds a runtime.db write transaction; else one is opened)."""
        self.dir.mkdir(parents=True, exist_ok=True)
        with self._lock():
            self._check_host()
            try:
                conn = self._connect()
            except sqlite3.Error as exc:
                raise StoreError(f"index {self.index_path}: {exc}") from exc
            try:
                self._settle(conn, rt)
                conn.execute("BEGIN IMMEDIATE")
                tx = Tx(self, conn, writer)
                try:
                    yield tx
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
                tx.commit(rt)
            except sqlite3.Error as exc:
                raise StoreError(f"index {self.index_path}: {exc}") from exc
            finally:
                self._close(conn)


class Tx:
    """One locked write. Records added here are visible to this Tx's queries at once."""

    def __init__(self, store: Store, conn: sqlite3.Connection, writer: dict):
        self.store = store
        self.conn = conn
        self.writer = writer
        self.seq = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM records").fetchone()[0]
        self.pending: dict[str, list[bytes]] = {}
        self.written: list[dict] = []

    def add(self, kind: str, fields: dict, dedupe_key: str | None = None) -> dict:
        self.seq += 1
        named = ("supersedes",) + KIND_FIELDS[kind]
        body = {k: DEFAULTS[k] for k in named if k in DEFAULTS} | fields
        rec = {"id": fields.get("id") or make_id(kind, self.store.project_id, self.seq,
                                                 json.dumps(fields, sort_keys=True,
                                                            ensure_ascii=False)),
               "seq": self.seq, "project_id": self.store.project_id, "writer": self.writer,
               "ts": now_iso()}
        rec.update({k: body.get(k) for k in named})
        rec.update({k: v for k, v in body.items() if k not in rec})
        if dedupe_key is not None:
            rec["dedupe_key"] = dedupe_key
        rec = redact(rec)
        _index_record(self.conn, kind, rec)
        self.pending.setdefault(FILES[kind], []).append(
            (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8"))
        self.written.append(dict(rec, _kind=kind))
        return rec

    def revise(self, kind: str, rec_id: str, **changes) -> dict:
        """A new version of `rec_id` (same id, next seq) with `changes` applied."""
        cur = self.current_one(kind, rec_id)
        if cur is None:
            raise StoreError(f"{kind} {rec_id} does not exist")
        fields = {k: v for k, v in cur.items() if k not in ("seq", "writer", "ts", "project_id")}
        fields.update(changes)
        return self.add(kind, fields)

    def find_dedupe(self, kind: str, session_id: str, key: str) -> dict | None:
        row = self.conn.execute(
            "SELECT json FROM records WHERE kind=? AND session_id IS ? AND dedupe_key=?",
            (kind, session_id, key)).fetchone()
        return json.loads(row[0]) if row else None

    def has_dedupe(self, kind: str, session_id: str, key: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM records WHERE kind=? AND session_id IS ? AND dedupe_key=?",
            (kind, session_id, key)).fetchone() is not None

    def current(self, kind: str, statuses: tuple[str, ...] | None = None) -> list[dict]:
        return current(self.conn, kind, statuses)

    def current_one(self, kind: str, rec_id: str) -> dict | None:
        row = self.conn.execute("SELECT json FROM current WHERE kind=? AND id=?",
                                (kind, rec_id)).fetchone()
        return json.loads(row[0]) if row else None

    def commit(self, rt=None) -> None:
        """manifest -> appends -> index COMMIT (the commit point) -> runtime.db chain ->
        manifest removed. A failure before the commit point cuts the files back; one
        after it leaves the manifest, which the next open completes."""
        if not self.pending:
            self.conn.execute("COMMIT")
            return
        store = self.store
        plan = {"files": {}}
        for name, lines in self.pending.items():
            data = b"".join(lines)
            old = self.conn.execute("SELECT size, chain FROM files WHERE name=?",
                                    (name,)).fetchone() or (0, integrity.CHAIN_START)
            plan["files"][name] = {"old": old[0], "new": old[0] + len(data),
                                   "chain": integrity.chain_next(old[1], data)}
        integrity.write_manifest(store.dir, plan)
        try:
            for name, lines in self.pending.items():
                path = store.dir / name
                with open(path, "ab") as fh:
                    if fh.tell() != plan["files"][name]["old"]:
                        raise IntegrityError(f"{name} changed under the lock")
                    fh.write(b"".join(lines))
                step = plan["files"][name]
                mtime = store._file_state(name)[1]
                self.conn.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?)",
                                  (name, step["new"], mtime, step["chain"]))
                self.conn.execute("INSERT OR REPLACE INTO chain VALUES (?,?,?)",
                                  (name, step["new"], step["chain"]))
            self.conn.execute("COMMIT")
        except BaseException:
            try:
                self.conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            integrity.roll_back(store.dir, plan)
            raise
        store._record_runtime_chain(rt, [(n, s["new"], s["chain"]) for n, s in plan["files"].items()])
        integrity.clear_manifest(store.dir)


def _index_record(conn: sqlite3.Connection, kind: str, rec: dict) -> None:
    try:
        conn.execute("INSERT INTO records VALUES (?,?,?,?,?,?)",
                     (rec["seq"], kind, rec["id"], rec.get("session_id"),
                      rec.get("dedupe_key"), json.dumps(rec, ensure_ascii=False)))
    except sqlite3.IntegrityError as exc:
        raise IntegrityError(f"duplicate seq or dedupe key in the store: {rec['seq']} ({exc})") from exc
    conn.execute(
        "INSERT INTO current VALUES (?,?,?,?,?) ON CONFLICT(kind, id) DO UPDATE SET"
        " seq=excluded.seq, status=excluded.status, json=excluded.json"
        " WHERE excluded.seq > current.seq",
        (kind, rec["id"], rec["seq"], rec.get("status"), json.dumps(rec, ensure_ascii=False)))


def current(conn: sqlite3.Connection | None, kind: str,
            statuses: tuple[str, ...] | None = None) -> list[dict]:
    """Latest version of every record of `kind` (optionally only these statuses), by seq."""
    if conn is None:
        return []
    sql = "SELECT json FROM current WHERE kind=?"
    params: list = [kind]
    if statuses:
        sql += f" AND status IN ({','.join('?' * len(statuses))})"
        params += list(statuses)
    return [json.loads(r[0]) for r in conn.execute(sql + " ORDER BY seq", params)]


def get(conn: sqlite3.Connection | None, kind: str, rec_id: str) -> dict | None:
    if conn is None:
        return None
    row = conn.execute("SELECT json FROM current WHERE kind=? AND id=?", (kind, rec_id)).fetchone()
    return json.loads(row[0]) if row else None


def dump_index(conn: sqlite3.Connection) -> dict:
    """Everything the index holds, for comparing an incremental index with a rebuild."""
    return {
        "records": conn.execute("SELECT seq, kind, id, session_id, dedupe_key, json FROM records"
                                " ORDER BY seq").fetchall(),
        "current": conn.execute("SELECT kind, id, seq, status, json FROM current"
                                " ORDER BY kind, id").fetchall(),
        "files": conn.execute("SELECT name, size, mtime_ns, chain FROM files ORDER BY name").fetchall(),
        "chain": conn.execute("SELECT name, size, chain FROM chain ORDER BY name, size").fetchall(),
    }
