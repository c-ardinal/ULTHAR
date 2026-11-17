"""Shared steps of the R1-e tests (not a test module): an ntfy stand-in on 127.0.0.1 (no real
ntfy is ever contacted), hook calls of a second window, the project's flags, waits for the
background notification sender, and the R1-e project config on top of a sandbox's.

Sessions: WINDOW (= r1c_helpers.SESSION) is the window holding the lease; OTHER is a
second window of the same project."""
from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from helpers import Sandbox
from r1c_helpers import SESSION, tool_use_id

WINDOW = SESSION
OTHER = "r1e-window-b"
STUB_OK = [sys.executable, "-c", "print('OK'); print('remaining: 42')"]
STUB_FAIL = [sys.executable, "-c", "import sys; print('codex: quota exhausted'); sys.exit(1)"]


class NtfyStub:
    """A local HTTP server that records every POST (path, headers, body)."""

    def __init__(self) -> None:
        self.received: list[dict] = []
        received = self.received

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def do_POST(self):  # noqa: N802
                size = int(self.headers.get("Content-Length") or 0)
                received.append({"path": self.path, "headers": dict(self.headers),
                                 "body": self.rfile.read(size).decode("utf-8", "replace")})
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def url(self, topic: str = "ulthar-test") -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/{topic}"

    def wait(self, count: int, timeout: float = 30) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self.received) >= count:
                return True
            time.sleep(0.2)
        return len(self.received) >= count

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def project_id(sb: Sandbox) -> str:
    with sb.inproc():
        return sb.store().project_id


def set_config(sb: Sandbox, **extra) -> None:
    """Adds keys to the project's existing .claude/ulthar.json (the sandbox's other keys stay)."""
    path = sb.work / ".claude" / "ulthar.json"
    cfg = json.loads(path.read_text(encoding="utf-8"))
    cfg.update(extra)
    path.write_text(json.dumps(cfg), encoding="utf-8")


def call(sb: Sandbox, tool: str, tool_input: dict, session: str = WINDOW, agent: str | None = None,
         agent_type: str = "general-purpose", event: str = "PreToolUse", **more):
    body = {"session_id": session, "tool_name": tool, "tool_use_id": tool_use_id(tool),
            "tool_input": tool_input, **more}
    if agent:
        body.update(agent_id=agent, agent_type=agent_type)
    return sb.hook(event, body)


def guard_of_last(sb: Sandbox) -> str:
    return sb.query("SELECT guard_id FROM events WHERE hook='PreToolUse' ORDER BY id DESC LIMIT 1")[0][0]


def flags(sb: Sandbox) -> tuple:
    rows = sb.query("SELECT relay_due, frozen FROM project_flags WHERE project_id=?",
                    (project_id(sb),))
    return rows[0] if rows else (None, None)


def set_relay_due(sb: Sandbox, reason: str = "test") -> None:
    sb.query("INSERT INTO project_flags (project_id, relay_due) VALUES (?,?) ON CONFLICT(project_id)"
             " DO UPDATE SET relay_due=excluded.relay_due", (project_id(sb), reason))


def clear_relay_due(sb: Sandbox) -> None:
    """A test that goes on in the same window after a unit reached done/halted (where a
    relay would follow, R1-e AC-2) clears the flag the way `ulthar relay` does."""
    sb.query("UPDATE project_flags SET relay_due=NULL, relay_due_at=NULL")


def wait_sent(sb: Sandbox, nid: str | None = None, timeout: float = 30) -> list[tuple]:
    """Waits until the notification (or every one) has a recorded send result."""
    sql = "SELECT nid, sent_ok, detail FROM notifications" + (" WHERE nid=?" if nid else "")
    params = (nid,) if nid else ()
    deadline = time.monotonic() + timeout
    while True:
        rows = sb.query(sql, params)
        if (rows and all(r[1] is not None for r in rows)) or time.monotonic() >= deadline:
            return rows
        time.sleep(0.2)


def assistant_line(uuid: str, used: int) -> dict:
    """A main-chain assistant transcript line whose API usage sums to `used` tokens."""
    return {"type": "assistant", "uuid": uuid, "message": {
        "role": "assistant", "content": [{"type": "text", "text": "ok"}],
        "usage": {"input_tokens": 1000, "cache_creation_input_tokens": 2000,
                  "cache_read_input_tokens": used - 3000, "output_tokens": 50}}}
