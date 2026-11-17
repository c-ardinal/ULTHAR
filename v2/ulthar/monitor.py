"""`ulthar monitor` -- the minimal local monitor (MON-01/02/03/04, R1-e AC-12/AC-13), CLI only,
standard library only.

Listens on 127.0.0.1 only. Each start makes a new random token, prints the URL with it,
and keeps it (with the port) in `<state>/monitor.token` and the port in runtime.db
`monitor` (the hooks refuse Claude's reads of that file and its requests to that port,
ulthar.isolation; written atomically, mode 0600 where the OS honours it). Every request
needs `?token=<token>` (or the X-Ulthar-Token header), a Host header naming this server
(127.0.0.1:<port> or localhost:<port>) and, when present, an Origin of this server:
otherwise 403.

GET /        the page;  GET /state  the same data as JSON (`snapshot`):
  sessions (project, units in progress with their state, last activity), context usage
  and compactions per session, blocks per guard and their rate among PreToolUse calls,
  evasion suspicions, questions awaiting an answer, capabilities, each unit's AC results
  and measurements (latest evidence), project flags, recent notifications (failed
  sends included), and `claude agents --json` (or why it could not be read).
POST /op     op = freeze | resume | relay, project_id = <id>: the only operations (spec
  事前決定 5). freeze / resume set the project's frozen flag; relay sets relay_due. Each
  is recorded as an owner action (an events row, hook "monitor", and owner_actions --
  the "read" mark of notifications). Nothing else: no command, push, deploy or edit.
"""
from __future__ import annotations

import hmac
import html
import json
import os
import subprocess
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import caps
from . import db as db_mod
from . import isolation
from . import relay
from . import store as store_mod

HOST = "127.0.0.1"
OPS = ("freeze", "resume", "relay")
AGENTS_TIMEOUT_SEC = 15
MAX_BODY = 4096


# ---------------------------------------------------------------------------- data

def _stores(rt) -> dict[str, store_mod.Store]:
    """Project id -> store, for every project a session ran in."""
    out = {}
    for (cwd,) in rt.execute("SELECT DISTINCT project_id FROM sessions WHERE project_id IS NOT NULL"):
        try:
            store = store_mod.Store.for_cwd(cwd)
        except Exception:  # noqa: BLE001 -- shown as missing, never fatal for the page
            continue
        out.setdefault(store.project_id, store)
    return out


def _units(store: store_mod.Store, rt) -> list[dict]:
    with store.reader(rt) as conn:
        return store_mod.current(conn, "unit") if conn is not None else []


def _ac_results(rt, unit_id: str) -> dict | None:
    row = rt.execute("SELECT id, result, tree_hash FROM evidence WHERE unit_id=? ORDER BY id DESC"
                     " LIMIT 1", (unit_id,)).fetchone()
    if row is None:
        return None
    res = json.loads(row[1] or "{}")
    return {"evidence_id": row[0], "self": bool(res.get("self")), "tree": row[2],
            "acs": res.get("acs") or {}}


def _agents() -> dict:
    try:
        proc = subprocess.run(["claude", "agents", "--json"], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                              timeout=AGENTS_TIMEOUT_SEC,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if proc.returncode != 0:
            return {"error": f"claude agents --json: exit {proc.returncode}: {proc.stderr.strip()[-200:]}"}
        return {"agents": json.loads(proc.stdout or "[]")}
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return {"error": f"claude agents --json: {type(exc).__name__}: {exc}"}


def snapshot(rt, with_agents: bool = True) -> dict:
    stores = _stores(rt)
    by_cwd = {}
    units_of: dict[str, list[dict]] = {}
    for pid, store in stores.items():
        try:
            units_of[pid] = _units(store, rt)
        except Exception as exc:  # noqa: BLE001
            units_of[pid] = [{"id": "?", "state": f"unreadable: {exc}"}]
    sessions = []
    for sid, cwd, last, used, window, compactions in rt.execute(
            "SELECT session_id, project_id, last_seen, ctx_used, ctx_window, compactions"
            " FROM sessions ORDER BY last_seen DESC"):
        if cwd not in by_cwd:
            try:
                by_cwd[cwd] = store_mod.Store.for_cwd(cwd).project_id if cwd else None
            except Exception:  # noqa: BLE001
                by_cwd[cwd] = None
        pid = by_cwd[cwd]
        sessions.append({"session_id": sid, "project_id": pid, "cwd": cwd, "last_seen": last,
                         "units": [{"id": u["id"], "state": u.get("state")}
                                   for u in units_of.get(pid, [])
                                   if u.get("state") not in ("done", "intake")],
                         "ctx_used": used, "ctx_window": window,
                         "ctx_pct": round(used * 100 / window, 1) if used is not None and window else None,
                         "compactions": compactions or 0})
    total = rt.execute("SELECT COUNT(*) FROM events WHERE hook='PreToolUse'").fetchone()[0]
    guards = [{"guard_id": g, "blocks": n, "rate": round(n / total, 4) if total else 0.0}
              for g, n in rt.execute("SELECT guard_id, COUNT(*) FROM events WHERE hook='PreToolUse'"
                                     " AND decision='deny' GROUP BY guard_id ORDER BY 2 DESC")]
    suspicions = [{"id": i, "block_event_id": b, "allow_event_id": a, "intent_key": k,
                   "ts": ts, "session_id": s}
                  for i, b, a, k, ts, s in rt.execute(
                      "SELECT x.id, x.block_event_id, x.allow_event_id, x.intent_key, e.ts, e.session_id"
                      " FROM suspicions x LEFT JOIN events e ON e.id=x.allow_event_id ORDER BY x.id")]
    awaiting = [{"project_id": pid, "qid": q, "unit_id": u, "kind": k}
                for pid, store in sorted(stores.items()) for q, u, k in relay.unanswered(rt, store)]
    units = [{"project_id": pid, "unit_id": u["id"], "state": u.get("state"),
              "results": _ac_results(rt, u["id"])} for pid, us in units_of.items() for u in us]
    flags = [{"project_id": p, "relay_due": r, "frozen": bool(f)} for p, r, f in rt.execute(
        "SELECT project_id, relay_due, frozen FROM project_flags ORDER BY project_id")]
    notes = [{"nid": n, "project_id": p, "kind": k, "important": bool(i), "sent_ok": s,
              "retries": r or 0, "acked_by_event_id": a, "ts": ts}
             for n, p, k, i, s, r, a, ts in rt.execute(
                 "SELECT nid, project_id, kind, important, sent_ok, retries, acked_by_event_id, ts"
                 " FROM notifications ORDER BY ts DESC LIMIT 50")]
    return {"sessions": sessions, "guards": guards, "pretooluse_calls": total,
            "suspicions": suspicions, "awaiting_answer": awaiting, "capabilities": caps.listing(rt),
            "units": units, "flags": flags, "notifications": notes,
            "agents": _agents() if with_agents else {"error": "not requested"}}


# ---------------------------------------------------------------------------- operations

def operate(rt, op: str, project_id: str) -> int:
    """One owner operation; returns its events row id. ValueError for anything else."""
    if op not in OPS or not project_id:
        raise ValueError(f"op must be one of {OPS} with a project_id")
    now = store_mod.now_iso()
    rt.execute("BEGIN IMMEDIATE")
    try:
        if op == "relay":
            isolation.set_relay_due(rt, project_id, "the owner asked for a relay (monitor)")
        else:
            isolation.set_frozen(rt, project_id, op == "freeze")
        event_id = rt.execute(
            "INSERT INTO events (ts, session_id, hook, tool, decision, guard_id, detail)"
            " VALUES (?, '(monitor)', 'monitor', ?, 'allow', 'monitor', ?)",
            (now, op, json.dumps({"project_id": project_id}))).lastrowid
        rt.execute("INSERT INTO owner_actions (project_id, ts, kind, event_id) VALUES (?,?,?,?)",
                   (project_id, now, f"monitor:{op}", event_id))
        rt.execute("COMMIT")
    except BaseException:
        rt.execute("ROLLBACK")
        raise
    return event_id


# ---------------------------------------------------------------------------- page

def _table(rows: list[dict], cols: list[str]) -> str:
    head = "".join(f"<th>{html.escape(c)}</th>" for c in cols)
    body = "".join("<tr>" + "".join(f"<td>{html.escape(json.dumps(r.get(c), ensure_ascii=False) if isinstance(r.get(c), (dict, list)) else str(r.get(c)))}</td>"
                                    for c in cols) + "</tr>" for r in rows)
    return f"<table><tr>{head}</tr>{body or '<tr><td>(none)</td></tr>'}</table>"


def page(snap: dict, token: str) -> str:
    q = html.escape(urllib.parse.quote(token))
    forms = "".join(
        f"<form method='post' action='/op?token={q}'><input type='hidden' name='project_id' "
        f"value='{html.escape(f['project_id'] or '')}'>{html.escape(f['project_id'] or '')} "
        + "".join(f"<button name='op' value='{op}'>{op}</button>" for op in OPS) + "</form>"
        for f in [{"project_id": p} for p in sorted({s["project_id"] for s in snap["sessions"] if s["project_id"]})])
    agents = snap["agents"]
    sections = [
        ("セッション", _table(snap["sessions"], ["session_id", "project_id", "units", "last_seen",
                                               "ctx_pct", "ctx_used", "ctx_window", "compactions"])),
        ("ガード別の遮断", _table(snap["guards"], ["guard_id", "blocks", "rate"])
         + f"<p>PreToolUse calls: {snap['pretooluse_calls']}</p>"),
        ("回避の疑い", _table(snap["suspicions"], ["id", "intent_key", "block_event_id",
                                                "allow_event_id", "session_id", "ts"])),
        ("回答待ち", _table(snap["awaiting_answer"], ["project_id", "unit_id", "kind", "qid"])),
        ("能力", _table(snap["capabilities"], ["name", "available", "reason", "last_check", "remaining"])),
        ("単位の AC", _table(snap["units"], ["project_id", "unit_id", "state", "results"])),
        ("プロジェクトの状態", _table(snap["flags"], ["project_id", "relay_due", "frozen"])),
        ("通知", _table(snap["notifications"], ["nid", "kind", "important", "sent_ok", "retries",
                                               "acked_by_event_id", "ts"])),
        ("claude agents", html.escape(json.dumps(agents, ensure_ascii=False)[:4000])),
        ("操作（凍結・再開・リレー要求）", forms or "(no project)"),
    ]
    body = "".join(f"<h2>{html.escape(t)}</h2>{b}" for t, b in sections)
    return ("<!doctype html><meta charset='utf-8'><title>ULTHAR monitor</title><style>"
            "body{font-family:sans-serif;margin:1.5em}table{border-collapse:collapse}"
            "td,th{border:1px solid #bbb;padding:2px 6px;font-size:13px}</style>"
            f"<h1>ULTHAR monitor</h1>{body}")


# ---------------------------------------------------------------------------- server

def _handler(token: str, db_path, port_of):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # noqa: D401 -- quiet: no access log on stderr
            return

        def _authorized(self) -> bool:
            """The token, a Host of this server (against DNS rebinding) and, when a browser
            names one, an Origin of this server (against cross-site POSTs)."""
            hosts = {f"{HOST}:{port_of()}", f"localhost:{port_of()}"}
            if self.headers.get("Host", "") not in hosts:
                return False
            origin = self.headers.get("Origin")
            if origin is not None and origin not in {f"http://{h}" for h in hosts}:
                return False
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            given = (query.get("token") or [""])[0] or self.headers.get("X-Ulthar-Token", "")
            return hmac.compare_digest(given.encode(), token.encode())

        def _send(self, code: int, body: str, kind: str = "application/json") -> None:
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", f"{kind}; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802
            if not self._authorized():
                return self._send(403, '{"error": "forbidden"}')
            route = urllib.parse.urlsplit(self.path).path
            if route not in ("/", "/state"):
                return self._send(404, '{"error": "not found"}')
            rt = db_mod.connect(db_path)
            try:
                snap = snapshot(rt)
            finally:
                rt.close()
            if route == "/state":
                return self._send(200, json.dumps(snap, ensure_ascii=False))
            return self._send(200, page(snap, token), "text/html")

        def do_POST(self):  # noqa: N802
            if not self._authorized():
                return self._send(403, '{"error": "forbidden"}')
            if urllib.parse.urlsplit(self.path).path != "/op":
                return self._send(404, '{"error": "not found"}')
            size = min(int(self.headers.get("Content-Length") or 0), MAX_BODY)
            raw = self.rfile.read(size).decode("utf-8", "replace")
            if (self.headers.get("Content-Type") or "").startswith("application/json"):
                try:
                    form = json.loads(raw or "{}")
                except ValueError:
                    form = {}
            else:
                form = {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}
            rt = db_mod.connect(db_path)
            try:
                event_id = operate(rt, str(form.get("op") or ""), str(form.get("project_id") or ""))
            except ValueError as exc:
                return self._send(400, json.dumps({"error": str(exc)}))
            finally:
                rt.close()
            return self._send(200, json.dumps({"ok": True, "event_id": event_id}))
    return Handler


def _write_token(path, text: str) -> None:
    """Atomically, readable by the owner only where the OS honours the mode (POSIX; on
    Windows the file inherits the user profile's ACL)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(tmp, path)


def serve(db_path, port: int = 0) -> None:
    token = os.urandom(24).hex()
    server = ThreadingHTTPServer((HOST, port), _handler(token, db_path,
                                                        lambda: server.server_address[1]))
    bound = server.server_address[1]
    rt = db_mod.connect(db_path)
    try:
        rt.execute("INSERT INTO monitor (id, port, pid, started) VALUES (1,?,?,?) ON CONFLICT(id)"
                   " DO UPDATE SET port=excluded.port, pid=excluded.pid, started=excluded.started",
                   (bound, os.getpid(), store_mod.now_iso()))
    finally:
        rt.close()
    _write_token(isolation.token_path(), json.dumps({"port": bound, "token": token,
                                                     "pid": os.getpid()}))
    print(f"http://{HOST}:{bound}/?token={token}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
