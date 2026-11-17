"""In-process git blob ids for the TRUST-07 post-hash (R1-d AC-16), hook side.

`blob(path)` returns what `git hash-object <path>` would print, computed here, ONLY when
it can be shown that git stores the file's bytes unchanged; otherwise None and the
caller asks git. A wrong id here could make an overwrite look recorded (TRUST-04 (b)),
so every source that can change what git stores must be ruled out:
  * the bytes: a regular file (git stores a symlink's target string), no CR byte
    (autocrlf / text / eol only ever turn CRLF into LF on the way in), no `$Id`
    (the ident attribute);
  * clean filters and working-tree-encoding, which only an attribute selects: no
    .gitattributes from the file's directory up to the repository root, no
    .git/info/attributes, no user git/attributes; a system gitattributes (next to the git
    executable's etc/) only if no line of it spells `filter` or `encoding` (an attribute
    macro included -- it must spell the attribute it sets);
  * where else attributes or config can come from: no `core.attributesFile`, no
    `[include]` / `[includeIf]`, no `[attr]` / attr.tree in the repository config
    (config, config.worktree), the global config (~/.gitconfig under HOME and
    USERPROFILE, XDG git/config) and the system config, which must be located (the git
    executable's ancestors' etc/gitconfig, %PROGRAMDATA%/Git/config, /etc/gitconfig) --
    when none is found, git is asked; and none of GIT_DIR, GIT_WORK_TREE,
    GIT_CONFIG_GLOBAL, GIT_CONFIG_SYSTEM, GIT_CONFIG_COUNT, GIT_CONFIG_PARAMETERS,
    GIT_ATTR_SOURCE set;
  * a plain repository (.git is a directory; a worktree's .git file -> git).
An unreadable source counts as a blocker. Everything is read afresh on every call.
"""
from __future__ import annotations

import os
from pathlib import Path

try:
    from _sha1 import sha1  # the builtin; hashlib would load OpenSSL (as store's _sha2)
except ImportError:  # pragma: no cover -- interpreters without the builtin module
    from hashlib import sha1

BYTE_BLOCKERS = (b"\r", b"$Id")
CONFIG_BLOCKERS = (b"attributesfile", b"[include", b"[attr", b"attr.tree")  # case-folded
SYSTEM_ATTR_BLOCKERS = (b"filter", b"encoding")
ENV_BLOCKERS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM",
                "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS", "GIT_ATTR_SOURCE")
GIT_EXE = "git.exe" if os.name == "nt" else "git"


def _has(path: Path, marks: tuple) -> bool:
    """Whether the file holds any mark (case-folded); a missing file holds none, an
    unreadable one counts as holding one."""
    try:
        data = path.read_bytes().lower()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return any(m in data for m in marks)


def _git_dir(p: Path) -> Path | None:
    """The .git directory of the plain repository holding `p`, with no .gitattributes on
    the way up; None otherwise."""
    d = p.parent
    while True:
        if (d / ".gitattributes").exists():
            return None
        dot = d / ".git"
        if dot.is_dir():
            return dot
        if dot.exists() or d.parent == d:
            return None
        d = d.parent


def _git_exe() -> Path | None:
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if entry and (Path(entry) / GIT_EXE).is_file():
            return Path(entry) / GIT_EXE
    return None


def _system_files() -> tuple[list[Path], list[Path]] | None:
    """(system config files, system attributes files) that exist; None when no system
    config can be located."""
    exe = _git_exe()
    if exe is None:
        return None
    roots = list(exe.resolve().parents) + [Path("/")]
    configs = [r / "etc" / "gitconfig" for r in roots]
    attrs = [r / "etc" / "gitattributes" for r in roots]
    program_data = os.environ.get("PROGRAMDATA")
    if program_data:
        configs.append(Path(program_data) / "Git" / "config")
    configs = [c for c in dict.fromkeys(configs) if c.is_file()]
    if not configs:
        return None
    return configs, [a for a in dict.fromkeys(attrs) if a.exists()]


def _user_files() -> tuple[list[Path], list[Path]]:
    """(global config files, global attributes files) to consider."""
    homes = {os.environ.get(k) for k in ("HOME", "USERPROFILE")} - {None, ""}
    homes.add(str(Path.home()))
    xdg = os.environ.get("XDG_CONFIG_HOME")
    xdgs = [Path(xdg)] if xdg else [Path(h) / ".config" for h in homes]
    configs = [Path(h) / ".gitconfig" for h in homes] + [x / "git" / "config" for x in xdgs]
    return configs, [x / "git" / "attributes" for x in xdgs]


def blob(p: Path) -> str | None:
    """git's blob id of `p` when provably unfiltered (see the module docstring), else None.
    Raises OSError when `p` cannot be read."""
    if any(os.environ.get(k) for k in ENV_BLOCKERS) or p.is_symlink():
        return None
    data = p.read_bytes()
    if any(mark in data for mark in BYTE_BLOCKERS):
        return None
    git_dir = _git_dir(p)
    if git_dir is None or (git_dir / "info" / "attributes").exists():
        return None
    system = _system_files()
    if system is None:
        return None
    user_configs, user_attrs = _user_files()
    if any(a.exists() for a in user_attrs):
        return None
    configs = [git_dir / "config", git_dir / "config.worktree", *user_configs, *system[0]]
    if any(_has(c, CONFIG_BLOCKERS) for c in configs):
        return None
    if any(_has(a, SYSTEM_ATTR_BLOCKERS) for a in system[1]):
        return None
    return sha1(b"blob %d\0" % len(data) + data).hexdigest()
