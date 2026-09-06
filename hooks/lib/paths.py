"""hooks/lib/paths.py -- platform-correct path normalisation and matching.

CONTRACT.md #2 (lib/paths.py) and M-9a/M-9b/F-19. Two DISTINCT normalisation
strategies that must never be confused:

  - norm_for_compare() / is_under()  -> EQUALITY.  pm-delete-allow entries vs.
    command tokens, containment tests (commander / skill_anchor "outside cwd").
  - matches_any()                     -> REGEX.     exempt_patterns, allowed_path_pattern,
    deploy_guard extension/name matching.
"""
# ponytail: lexical normalisation only (os.path.normpath / GetFullPath-equivalent),
# never Path.resolve() -- a symlink/junction/reparse point is judged by its literal
# path, not its target (M-9a, F-19), and normalisation still works on paths that do
# not exist, which Rule 3 (tool_discipline delete) requires. Switch to resolve() if
# a check ever needs to compare real targets instead of literal paths.
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

_VAR_PATTERN = re.compile(r"\$\{(TEMP|CWD|DEPLOY_DIR|SKILL_DIR)\}")


def to_posix(p: str) -> str:
    """Backslashes -> forward slashes. Applied to every path BEFORE regex matching,
    so config stores one forward-slash pattern set that is correct on both OSes."""
    return p.replace("\\", "/")


def _strip_win32_component_padding(posix_path: str) -> str:
    """Windows only. Win32 DISCARDS trailing dots and spaces from every path
    component before it reaches the filesystem, so `.claude./state/x` and
    `.claude/state/x` are the SAME FILE -- but os.path.normpath keeps the dot,
    so a component-wise compare called them different paths.

    That was not a cosmetic divergence. It was one Write away from the whole
    authority mechanism: `<home>/.claude./state/<grants dir>/<rule>/<id>`
    measured rc=0 through the real dispatcher and the file landed in the real
    `.claude/state/...`, i.e. an agent could write its own grant, and the same
    spelling reached the user overlay and the pm-delete-allow ledger. Fixed HERE
    rather than in guards/grant_files.py because norm_for_compare is the single
    door every equality/containment check goes through (grant_files, commander's
    outside-cwd test, skill_anchor, the pm-delete-allow ledger compare) -- a
    per-caller fix would have left every sibling caller open.

    `.` and `..` are left alone: normpath has already resolved what it can, and
    stripping them would turn a legitimate relative segment into an empty one. A
    component that is ENTIRELY dots/spaces is also left alone (Win32 rejects it,
    so it can never name a real file, and an empty component would corrupt the
    split). POSIX is untouched: there a trailing dot is an ordinary character
    and `x.` really is a different file from `x`."""
    out = []
    for i, part in enumerate(posix_path.split("/")):
        if part in (".", ".."):
            out.append(part)
            continue
        # ALTERNATE DATA STREAM. `x.json::$DATA` opens the SAME FILE as
        # `x.json` on NTFS -- measured: writing through the ADS spelling created
        # the plain file -- and ':' is illegal in a Windows filename otherwise,
        # so everything from the first ':' on is a stream name, not path. The
        # one legitimate ':' is the drive letter: component 0, two characters.
        if ":" in part and not (i == 0 and len(part) == 2 and part[0].isalpha()):
            part = part.split(":", 1)[0]
        out.append(part.rstrip(". ") or part)
    return "/".join(out)


def _strip_win32_path_prefix(p: str) -> str:
    r"""Windows only, applied BEFORE normpath. The extended-length (`\\?\`) and
    device (`\\.\`) prefixes name the same file as the plain path, but
    `ntpath.normpath` keeps them -- so the first component of `\\?\C:\...`
    compared as `?` and every containment test missed it. Measured rc=0 on a
    Write to a grant path spelled that way, and the file was really created.

    `\\?\UNC\host\share\...` maps back to `\\host\share\...`, which is what the
    prefix means, and then the ADMIN SHARE rewrite below finishes the job:
    `\\host\C$\...` becomes `C:\...`. `\\localhost\C$\<the grant path>` measured
    rc=0 and really created the file, and realpath does not resolve it either
    (it stays UNC), so a lexical rule is the only thing that reaches it.

    The share rewrite deliberately does NOT check the host. Enumerating every
    name this machine answers to (`localhost`, `127.0.0.1`, the NetBIOS name,
    the FQDN, an alias) is not a closable set, so the host is ignored and every
    `\\<anything>\<letter>$` is treated as this machine's drive. The cost is a
    deliberate OVER-BLOCK: writing to ANOTHER machine's admin share at exactly
    ULTHAR's authority path is refused too. That is not routine work by any
    measure, and over-blocking is the safe direction here."""
    q = p.replace("/", "\\")
    up = q.upper()
    if up.startswith("\\\\?\\UNC\\"):
        q = "\\\\" + q[len("\\\\?\\UNC\\"):]
    elif up.startswith("\\\\?\\") or up.startswith("\\\\.\\"):
        q = q[4:]
    if q.startswith("\\\\"):
        parts = q[2:].split("\\", 2)
        if len(parts) >= 2 and len(parts[1]) == 2 and parts[1][1] == "$" and parts[1][0].isalpha():
            rest = parts[2] if len(parts) > 2 else ""
            return f"{parts[1][0]}:\\{rest}"
    return q


def _components(p: str, ctx) -> list[str]:
    return [seg for seg in norm_for_compare(p, ctx).split("/") if seg != ""]


def _realpath_is_local(p: str) -> bool:
    """Is resolving `p` cheap and offline? See is_under() for why it matters:
    realpath on an unreachable UNC host measured 21.5 s per tool call.

    A drive letter is checked for existence because a DISCONNECTED mapped drive
    is a network path wearing a letter -- and `os.path.exists('Z:/')` on one
    returns quickly (the redirector answers from its own state) where realpath
    into it would not.

    # ponytail: existence check only, no reachability probe. A mapped drive that
    # is connected but whose server has since gone away can still block inside
    # realpath; a timeout would need a thread or a subprocess, which is a lot of
    # machinery for a second-chance lookup. Revisit if that is ever measured.
    """
    q = _strip_win32_path_prefix(p).replace("\\", "/")
    if q.startswith("//"):
        return False
    drive = q.split("/", 1)[0]
    if len(drive) == 2 and drive[1] == ":" and drive[0].isalpha():
        try:
            return os.path.exists(drive + "/")
        except (OSError, ValueError):
            return False
    return True


def norm_for_compare(p: str, ctx) -> str:
    """EQUALITY compares only. LEXICAL normalisation (os.path.normpath semantics) --
    never Path.resolve() -- then to_posix(), then os.path.normcase() ONLY when
    ctx.is_windows (PowerShell's -eq / StringComparison.OrdinalIgnoreCase is
    Windows-correct and POSIX-wrong: /home/x/Dist and /home/x/dist differ on Linux).

    A malformed/exotic path (embedded NUL, an invalid Windows device path, ...)
    can make os.path.normpath raise; this falls back to the raw string rather
    than letting the exception propagate, because that would stop the guard
    (and via I-3's fail-open, the whole PreToolUse chain) and ALLOW everything
    -- the opposite of Rule 3's intent."""
    if ctx.is_windows:
        p = _strip_win32_path_prefix(p)
    try:
        normalised = os.path.normpath(p)
    except (ValueError, OSError):
        normalised = p
    normalised = to_posix(normalised)
    if ctx.is_windows:
        normalised = _strip_win32_component_padding(normalised)
        try:
            normalised = os.path.normcase(normalised)
        except (ValueError, OSError):
            pass
        # os.path.normcase on posix-hosted Windows testing is a no-op; on real Windows
        # normcase also flips separators back to '\\', so re-apply to_posix afterwards
        # to keep the stored/compared form forward-slash consistently.
        normalised = to_posix(normalised)
    return normalised


def is_under(child: str, parent: str, ctx) -> bool:
    """Component-wise containment on norm_for_compare()d paths. NOT str.startswith --
    PE-3: StartsWith treats C:\\repo-backup\\x as inside C:\\repo. Component-wise
    comparison on the '/'-split, normalised path does not.

    SECOND CHANCE VIA realpath(), and only ever as an ADDITIONAL accept. Some
    Windows spellings name the same file without being lexically derivable from
    it -- an 8.3 short name is the plain case (`GITPOL~1`), and no amount of
    string work turns it back into the long name, because the mapping lives in
    the filesystem. Measured: 8.3 and `\\\\.\\` are both resolved by realpath on
    both sides, `\\\\?\\`, `\\\\?\\UNC\\`, `\\localhost\\C$` and `::$DATA` are
    not (those four are handled lexically above instead).

    Never a REPLACEMENT for the lexical answer, which is what keeps the module's
    "literal path, not its target" rule (M-9a/F-19) intact where it matters:
    this can only ever turn False into True, and every caller uses True as the
    guarding direction -- grant_files and deploy BLOCK on it, commander and
    skill_anchor stop exempting on it. So resolving a junction can tighten a
    verdict and can never loosen one. Costs one filesystem call per target only
    when the cheap answer already said no.

    NEVER ON A REMOTE PATH. `os.path.realpath` on a UNC path talks to the
    network, and an unreachable host does not fail fast: a Write to
    `\\\\10.255.255.1\\share\\notes.md` took 21.5 s per tool call against a 0.4 s
    baseline, on EVERY tool call, because the second chance ran on every
    protected target. A guard that adds twenty seconds to unrelated work gets
    switched off, which is the failure this project keeps designing against. So
    the second chance is skipped when either side is a UNC path or names a drive
    that does not exist, and the lexical answer stands. That is safe precisely
    because this can only ever ADD a True: skipping it returns False on a path
    that is not local, and every caller treats False as "not mine".

    STILL OPEN after the prefix rewrites: a UNC spelling whose share is NOT an
    admin share (`\\\\host\\public\\...` bound to the same volume). The admin
    share form (`\\\\<anything>\\C$\\...`) is closed lexically in
    _strip_win32_path_prefix; a named share would mean resolving the export,
    which is exactly the network round-trip this refuses to make. Also open: an
    8.3 short name for a path that does not exist yet, since nothing can resolve
    a mapping the filesystem has not created."""
    par = _components(parent, ctx)
    c = _components(child, ctx)
    if par and len(par) <= len(c) and c[: len(par)] == par:
        return True
    if not (_realpath_is_local(child) and _realpath_is_local(parent)):
        return False
    try:
        rc = _components(os.path.realpath(child), ctx)
        rp = _components(os.path.realpath(parent), ctx)
    except (OSError, ValueError):
        return False
    return bool(rp) and len(rp) <= len(rc) and rc[: len(rp)] == rp


def matches_any(p: str, compiled: list[re.Pattern], ctx) -> bool:
    """REGEX matching. Calls to_posix(p) ONLY -- separator conversion, NO normcase --
    and re.search()es each already-IGNORECASE-compiled pattern. normcase is never
    applied here: it would lowercase the Windows candidate and stop '/Temp/claude/'
    and '/HANDOFF' fragments from ever matching (case sensitivity for path-kind
    patterns is carried by the compile-time flags, not by this function)."""
    candidate = to_posix(p)
    return any(pat.search(candidate) for pat in compiled)


def resolve_python_exe(cfg: dict, ctx=None) -> str:
    """The ONE resolution point for the effective interpreter (every consumer must
    route through this, not re-implement the fallback): `paths.python_exe` when
    non-empty means "pin this interpreter" (what install.py's
    --register-settings writes into settings.json); empty (the shipped default)
    means "resolve from the interpreter currently running" (sys.executable),
    so a fresh clone with no machine-specific pin still works.

    `ctx` is optional and, when given, applies ctx.expand() to a configured
    value (~ / ${...} support) -- install.py calls this before a Ctx exists, so
    it passes no ctx; the configured value is expected to already be an
    absolute path there."""
    configured = cfg.get("paths", {}).get("python_exe") or ""
    if not configured:
        return sys.executable
    if ctx is not None:
        return str(ctx.expand(configured))
    return configured


def expand(value: str, ctx) -> Path:
    """~ , ${TEMP}, ${CWD}, ${DEPLOY_DIR}, ${SKILL_DIR}. No other variables.
    A value with none of those and no leading '/' resolves against ${CWD}."""
    v = value

    def _sub(m: re.Match) -> str:
        name = m.group(1)
        if name == "TEMP":
            return str(ctx.temp_dir)
        if name == "CWD":
            return str(ctx.cwd)
        if name == "DEPLOY_DIR":
            return str(ctx.deploy_dir)
        if name == "SKILL_DIR":
            # skill root is the parent of skills_root/<this skill> -- but the only
            # thing Ctx carries is skills_root; consumers that need a specific skill
            # directory pass it fully resolved. Fall back to skills_root itself.
            return str(ctx.skills_root)
        return m.group(0)

    v = _VAR_PATTERN.sub(_sub, v)

    if v.startswith("~"):
        rest = v[1:]
        if rest.startswith("/") or rest.startswith("\\"):
            rest = rest[1:]
        base = ctx.home
        expanded = base / rest.replace("\\", "/") if rest else base
        return Path(os.path.normpath(str(expanded)))

    p = Path(v)
    if p.is_absolute():
        return Path(os.path.normpath(str(p)))

    # No variable, no leading '~', not absolute -> resolve against ${CWD}.
    return Path(os.path.normpath(str(ctx.cwd / v)))


# ---------------------------------------------------------------- deploy origin

DEPLOY_ORIGIN_FILE = "deployed-from.txt"
"""One line, written by install.py into the deploy dir: the absolute path of the
MASTER (clone) root it deployed from.

The deployed runtime is otherwise location-blind -- it sits in ~/.claude/hooks and
cannot know whether the clone it came from is ~/.claude/skills/ulthar,
C:/work/ULTHAR or /home/x/src/ulthar. Every message telling a reader to "edit the
master and redeploy", and skill_anchor's ability to find ANCHOR.md for a clone that
is not under skills_root, need exactly that one fact, and deploy time is the only
moment it is known.

Deliberately NOT stamped into the deployed config.json: --verify hash-compares
master against deploy byte-for-byte, so a stamp would read as permanent drift.
Deliberately at the deploy-dir ROOT, outside every COPY_TABLE `tree` row, so it is
invisible to foreign_files() and to --verify's EXTRA scan. dispatch.py keeps its
own copy of this literal for the pre-`lib`-import failure path, where this module
is by definition unavailable."""


def deployed_from(deploy_dir) -> Path | None:
    """The master root recorded by install.py, or None when the deploy dir holds no
    record (deployed by an older ULTHAR, hand-copied, or the file was removed).
    None means "unknown", never a guess -- callers must SAY it is unknown rather
    than name a path that may not exist."""
    try:
        text = Path(deploy_dir, DEPLOY_ORIGIN_FILE).read_text(encoding="utf-8-sig").strip()
    except OSError:
        return None
    return Path(text) if text else None
