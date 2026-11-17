"""The Notifier port and its adapters (PORT-01/02/04, R1-e AC-10/AC-11), CLI side only
(the hook path never imports this module: no network on a hook).

A Notifier sends one message: `send(title, text, important) -> (ok, detail)`, never
raising for a delivery failure. The adapter is chosen by one config line,
`.claude/ulthar.json` "notify": {"adapter": "ntfy", ...} (PORT-04; default ntfy).
`claude_push` is listed by the capability registry as unavailable (P-09, spec 事前決定 4):
no adapter exists for it.

ntfy: POST the text (UTF-8) to `notify.ntfy_url` (the topic URL), with an ASCII Title
header and Priority high for an important notification.

`send_recorded` is the background sender (`ulthar notify-send`): it sends one queued
notification and records the result on its row; a send through the ntfy adapter is also
a real call, so it updates the ntfy capability (ulthar.caps).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from . import caps
from . import notify
from . import store as store_mod

TIMEOUT_SEC = 10


class NotifyError(Exception):
    """The adapter cannot be built (an unknown adapter, a missing setting)."""


class NtfyNotifier:
    name = "ntfy"

    def __init__(self, url: str, timeout: float = TIMEOUT_SEC):
        self.url, self.timeout = url, timeout

    @classmethod
    def from_config(cls, cfg: dict) -> "NtfyNotifier":
        url = (cfg.get("notify") or {}).get("ntfy_url") if isinstance(cfg.get("notify"), dict) else None
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise NotifyError("notify.ntfy_url is not set in .claude/ulthar.json (an http(s) topic URL)")
        return cls(url)

    def send(self, title: str, text: str, important: bool) -> tuple[bool, str]:
        req = urllib.request.Request(self.url, data=text.encode("utf-8"), method="POST", headers={
            "Title": title.encode("ascii", "replace").decode("ascii"),
            "Priority": "high" if important else "default", "Tags": "ulthar",
            "Content-Type": "text/plain; charset=utf-8"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                code = resp.status
        except urllib.error.HTTPError as exc:
            return False, f"HTTP {exc.code} from {self.url}"
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return False, f"{type(exc).__name__}: {exc}"
        return (200 <= code < 300), f"HTTP {code}"


ADAPTERS = {"ntfy": NtfyNotifier.from_config}


def notifier(cfg: dict):
    name = notify.adapter_name(cfg)
    if name not in ADAPTERS:
        raise NotifyError(f"notify.adapter {name!r} is not one of {sorted(ADAPTERS)}")
    return ADAPTERS[name](cfg)


def send_recorded(rt, store: store_mod.Store, nid: str) -> bool:
    row = rt.execute("SELECT kind, text, important, detail, retries FROM notifications WHERE nid=?",
                     (nid,)).fetchone()
    if row is None:
        return False
    kind, text, important, detail, retries = row
    try:
        adapter = notifier(store.config)
        ok, why = adapter.send(f"ULTHAR {kind}", text or "", bool(important))
    except NotifyError as exc:
        adapter, ok, why = None, False, str(exc)
    try:
        attempts = json.loads(detail) if detail else []
        attempts = attempts if isinstance(attempts, list) else [str(attempts)]
    except ValueError:
        attempts = [detail]
    attempts.append({"ts": store_mod.now_iso(), "ok": ok, "detail": why, "resend": retries})
    rt.execute("BEGIN IMMEDIATE")
    try:
        rt.execute("UPDATE notifications SET sent_ok=?, detail=? WHERE nid=?",
                   (int(ok), json.dumps(attempts, ensure_ascii=False), nid))
        if adapter is not None and adapter.name == "ntfy":
            caps.record(rt, "ntfy", ok, f"notification {nid}: {why}")
        rt.execute("COMMIT")
    except BaseException:
        rt.execute("ROLLBACK")
        raise
    return ok
