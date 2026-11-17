"""Direct writes to external tools are blocked (PORT-03, TRUST-01; R1-e AC-8, spec 事前決定 3),
hook side, in a project that has `.claude/ulthar.json`:

  (a) an MCP tool (`mcp__<server>__<name>`) passes only when its last name starts with a
      read verb (READ_VERBS, followed by the end, `_`, `-` or an upper-case letter) or the
      config's `mcp_allow` lists its full name -- an unknown tool is blocked (allow list);
  (b) `gh issue|pr|release <verb>` passes only for a read verb (GH_READ); `gh api` is
      blocked with a write method (-X / --method POST|PATCH|PUT|DELETE) or a field
      (-f / -F / --field / --raw-field / --input);
  (c) curl / wget / Invoke-WebRequest / Invoke-RestMethod (and httpie) naming a known
      vendor API host with a write method or a request body.
Shell commands are read with shellwords, also inside `bash -c` / `powershell -Command`.
ulthar's own adapters run in the CLI and never pass through a hook. Known limits (spec):
an MCP tool named like a read but writing passes; a host or method assembled at run time
is not seen.
"""
from __future__ import annotations

import re

from . import shellwords
from . import store as store_mod

GUARD_ID = "external_write"
READ_VERBS = ("get", "list", "search", "query", "read", "fetch", "find", "describe", "whoami")
READ_NAME = re.compile(rf"^(?:{'|'.join(READ_VERBS)})(?:$|[_\-]|(?=[A-Z]))")
GH_GROUPS = frozenset({"issue", "pr", "release"})
GH_READ = frozenset({"list", "view", "status", "diff", "checks"})
WRITE_METHODS = frozenset({"post", "patch", "put", "delete"})
VENDOR_HOSTS = ("api.linear.app", "api.github.com", "api.trello.com")
HTTP_CLIENTS = frozenset({"curl", "wget", "invoke-webrequest", "iwr", "invoke-restmethod", "irm",
                          "http", "https", "xh"})
METHOD_FLAGS = frozenset({"-x", "--request", "-method", "--method"})
BODY_FLAGS_EXACT = frozenset({"-d", "-F", "-T"})  # curl's short flags are case-sensitive (-f = --fail)
BODY_FLAGS = frozenset({"--data", "--data-raw", "--data-binary", "--data-urlencode", "--data-ascii",
                        "--form", "--form-string", "--upload-file", "--json", "-body", "-infile",
                        "--post-data", "--post-file", "--body-data", "--body-file"})
GH_VALUE_FLAGS = frozenset({"-R", "--repo", "-b", "--body", "-t", "--title"})
GH_FIELD_FLAGS = frozenset({"-f", "-F", "--field", "--raw-field", "--input"})


def _block(why: str) -> str:
    return (f"BLOCKED (ulthar external write, PORT-03): {why}. Writes to external tools go "
            "through ulthar's adapters only; read-only calls are allowed. An MCP tool that only "
            "reads may be added to `mcp_allow` in .claude/ulthar.json by the owner.")


def _mcp_allow(cfg: dict) -> list[str]:
    raw = cfg.get("mcp_allow", [])
    if not isinstance(raw, list) or not all(isinstance(x, str) and x for x in raw):
        raise store_mod.StoreError(".claude/ulthar.json: mcp_allow must be a list of MCP tool names")
    return raw


def _mcp(tool: str, cfg: dict) -> str | None:
    last = tool.split("__")[-1]
    if READ_NAME.match(last) or tool in _mcp_allow(cfg):
        return None
    return _block(f"the MCP tool {tool} is not a read tool (its name does not start with "
                  f"{'/'.join(READ_VERBS)}) and is not in mcp_allow")


def _exe(word: str) -> str:
    return shellwords.basename(word).removesuffix(".exe")


def _gh(seg: list[str]) -> str | None:
    for i, word in enumerate(seg):
        if _exe(word) != "gh":
            continue
        rest = seg[i + 1:]
        plain = [w for j, w in enumerate(rest) if not w.startswith("-")
                 and not (j and rest[j - 1] in GH_VALUE_FLAGS)]
        group = plain[0].casefold() if plain else ""
        if group in GH_GROUPS and len(plain) > 1 and plain[1].casefold() not in GH_READ:
            return _block(f"`gh {group} {plain[1]}` writes to GitHub")
        if group == "api" and _gh_api_writes(rest):
            return _block("`gh api` with a write method or a field writes to GitHub")
    return None


def _gh_api_writes(words: list[str]) -> bool:
    for j, w in enumerate(words):
        low = w.casefold()
        name, _, value = low.partition("=")
        if name in ("-x", "--method"):
            method = value or (words[j + 1].casefold() if j + 1 < len(words) else "")
            if method in WRITE_METHODS:
                return True
        if low.startswith("-x") and low[2:] in WRITE_METHODS:
            return True
        if w.split("=", 1)[0] in GH_FIELD_FLAGS or (len(w) > 2 and w[:2] in ("-f", "-F")
                                                     and not w.startswith("--")):
            return True
    return False


def _http(seg: list[str]) -> str | None:
    if not any(_exe(w) in HTTP_CLIENTS for w in seg):
        return None
    host = next((h for h in VENDOR_HOSTS for w in seg if h in w.casefold()), None)
    if host is None:
        return None
    for j, w in enumerate(seg):
        low = w.casefold()
        name, _, value = low.partition("=")
        if name in METHOD_FLAGS:
            method = value or (seg[j + 1].casefold() if j + 1 < len(seg) else "")
            if method in WRITE_METHODS:
                return _block(f"a {method.upper()} request to {host}")
        if w.startswith("-X") and low[2:] in WRITE_METHODS:
            return _block(f"a {low[2:].upper()} request to {host}")
        if w in BODY_FLAGS_EXACT or name in BODY_FLAGS or (
                w.startswith(("-d", "-F", "-T")) and not w.startswith("--") and len(w) > 2):
            return _block(f"a request with a body to {host} (a write)")
    return None


def check(payload: dict, store_of) -> str | None:
    """The block message, or None. `store_of()` gives the project's store, opened only for
    a call that may be an external write."""
    tool = str(payload.get("tool_name") or "")
    if tool.startswith("mcp__"):
        store = store_of()
        return _mcp(tool, store.config) if _configured(store) else None
    if tool not in ("Bash", "PowerShell"):
        return None
    command = str((payload.get("tool_input") or {}).get("command") or "")
    low = command.casefold()
    if "gh" not in low and not any(h in low for h in VENDOR_HOSTS):
        return None
    if not _configured(store_of()):
        return None
    for seg in shellwords.all_segments(command, tool):
        msg = _gh(seg) or _http(seg)
        if msg:
            return msg
    return None


def _configured(store) -> bool:
    return store.repo is not None and (store.repo / store_mod.PROJECT_CONFIG).is_file()
