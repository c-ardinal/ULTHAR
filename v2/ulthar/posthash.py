"""In-process git blob ids for the TRUST-07 post-hash (R1-d AC-16), hook side; also the
locator of git's config files (`config_files`, used by ulthar.gitref for aliases).

`blob(path)` returns what `git hash-object <path>` would print, computed here, ONLY when
it can be shown that git stores the file's bytes unchanged; otherwise None and the
caller asks git. A wrong id here could make an overwrite look recorded (TRUST-04 (b)) --
a false acceptance -- so every source that can change what git stores must be ruled out:
  * the bytes: a regular file (git stores a symlink's target string), no CR byte
    (autocrlf / text / eol only ever turn CRLF into LF on the way in), no `$Id`
    (the ident attribute);
  * clean filters and working-tree-encoding, which only an attribute selects: no
    .gitattributes in the working tree from the file's directory up to the repository
    root; none in the index either (git -- check-attr, add -- reads a tracked
    .gitattributes from the index when the working-tree file is gone): the index must be
    version 2 or 3 and hold no byte
    string `.gitattributes` at all (a conservative scan of the whole file); no
    .git/info/attributes; no user git/attributes; a system gitattributes (next to the git
    executable's etc/) only if no line of it spells `filter` or `encoding` (an attribute
    macro included -- it must spell the attribute it sets);
  * where else attributes or config can come from: no `core.attributesFile`, no
    `[include]` / `[includeIf]`, no `[attr]` / attr.tree in the repository config
    (config, config.worktree), the global config (~/.gitconfig under HOME and
    USERPROFILE, XDG git/config) and the system config, which must be located (the git
    executable's ancestors' etc/gitconfig, %PROGRAMDATA%/Git/config, /etc/gitconfig) --
    when none is found, git is asked; and none of GIT_DIR, GIT_WORK_TREE, GIT_COMMON_DIR,
    GIT_INDEX_FILE, GIT_CONFIG_GLOBAL, GIT_CONFIG_SYSTEM, GIT_CONFIG_COUNT,
    GIT_CONFIG_PARAMETERS, GIT_ATTR_SOURCE set;
  * a plain repository: .git is a directory with no `commondir` file (a linked worktree,
    whose info/attributes and config live elsewhere -> git).
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
ENV_BLOCKERS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
                "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_COUNT",
                "GIT_CONFIG_PARAMETERS", "GIT_ATTR_SOURCE")
INDEX_VERSIONS = (2, 3)
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


def _index_clean(git_dir: Path) -> bool:
    """No .gitattributes can come from the index: absent, or version 2/3 without the
    byte string anywhere."""
    try:
        data = (git_dir / "index").read_bytes()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if data[:4] != b"DIRC" or int.from_bytes(data[4:8], "big") not in INDEX_VERSIONS:
        return False
    return b".gitattributes" not in data


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
    xdgs = [Path(xdg)] if xdg else [Path(h) / ".config" for h in sorted(homes)]
    configs = [Path(h) / ".gitconfig" for h in sorted(homes)] + [x / "git" / "config" for x in xdgs]
    return configs, [x / "git" / "attributes" for x in xdgs]


def config_files(git_dir: Path) -> list[Path] | None:
    """Every config file git reads for the repository of `git_dir`, lowest precedence first
    (system, global, the repository's -- its common dir's for a linked worktree --
    config.worktree); missing ones included. None when the system config cannot be located
    or config comes from the environment: the caller must not trust its own reading."""
    system = _system_files()
    if system is None or any(os.environ.get(k) for k in ENV_BLOCKERS):
        return None
    common = git_dir
    marker = git_dir / "commondir"
    if marker.exists():
        raw = marker.read_text(encoding="utf-8").strip()
        common = Path(raw) if Path(raw).is_absolute() else git_dir / raw
    return [*system[0], *_user_files()[0], common / "config", git_dir / "config.worktree"]


def blob(p: Path) -> str | None:
    """git's blob id of `p` when provably unfiltered (see the module docstring), else None.
    Raises OSError when `p` cannot be read."""
    if any(os.environ.get(k) for k in ENV_BLOCKERS) or p.is_symlink():
        return None
    data = p.read_bytes()
    if any(mark in data for mark in BYTE_BLOCKERS):
        return None
    git_dir = _git_dir(p)
    if (git_dir is None or (git_dir / "commondir").exists()
            or (git_dir / "info" / "attributes").exists() or not _index_clean(git_dir)):
        return None
    configs = config_files(git_dir)
    system = _system_files()
    if configs is None or system is None:
        return None
    if any(a.exists() for a in _user_files()[1]):
        return None
    if any(_has(c, CONFIG_BLOCKERS) for c in configs):
        return None
    if any(_has(a, SYSTEM_ATTR_BLOCKERS) for a in system[1]):
        return None
    return sha1(b"blob %d\0" % len(data) + data).hexdigest()
