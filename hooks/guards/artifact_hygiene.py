"""hooks/guards/artifact_hygiene.py -- Stop guard.

Flags stale files sitting in gitignored artifact directories (.playwright-mcp/,
test-results/, playwright-report/ -- config-driven, not hardcoded) that
`git status` structurally cannot see.

`stop_hook_active` is not handled here: `dispatch.py` skips this guard entirely
on a Stop event with `stop_hook_active` true, because this guard's id is
listed in `dispatch.honour_stop_hook_active_for`.

Ordering is load-bearing: the once-per-session marker is written only when
`found_lines` is non-empty, and the early "nothing found" return happens
before that write -- a session that scans clean must not consume its marker,
or it could never fire again later once artifacts actually appear.
"""
from __future__ import annotations

import os
from pathlib import Path

from lib import ctx as ctx_mod
from lib import io as io_mod


def _stale_files_under(root: Path, cutoff_ts: float) -> list[Path]:
    """Walks `root` and returns files whose mtime is older than `cutoff_ts`.

    Errors are handled per entry, never per directory: an unlistable
    subdirectory (permission denied, a broken junction) must not erase files
    already found in its siblings, so `os.walk(..., onerror=...)` is used
    instead of `Path.rglob()` -- the walk continues with the next directory
    rather than aborting. A file that vanishes between being listed and being
    `stat()`-ed is a race, not a directory-tree failure, so `stat()` is
    wrapped in its own per-file try/except -- one vanished file must not
    propagate out of `run()` and get silently turned into exit 0 by
    dispatch.py's fail-open wrapper, since this guard's only job is to flag
    things."""
    stale: list[Path] = []

    def _on_dir_error(_exc: OSError) -> None:
        pass  # unreadable / broken subdirectory: skip it, keep walking siblings

    for dirpath, _dirnames, filenames in os.walk(root, onerror=_on_dir_error):
        for name in filenames:
            fp = Path(dirpath) / name
            try:
                mtime = fp.stat().st_mtime
            except OSError:
                continue  # vanished / became unreadable between listing and stat()
            if mtime < cutoff_ts:
                stale.append(fp)
    return stale


def run(payload: dict, cfg: dict, ctx) -> io_mod.Result:
    ad = cfg["artifact_dirs"]
    if not ad.get("enabled", True):
        return io_mod.ALLOW

    cwd = ctx.cwd
    if not cwd.exists():
        return io_mod.ALLOW

    session_id = str(payload.get("session_id") or "")
    sanitise = ctx_mod.compile_pattern(
        ad["session_id_sanitise_pattern"], "text", ctx, "artifact_dirs.session_id_sanitise_pattern"
    )
    safe_id = sanitise.sub("_", session_id) if sanitise else session_id
    if not safe_id.strip():
        safe_id = ad["session_id_fallback"]

    marker_dir = ctx.expand(ad["marker_dir"])
    marker = marker_dir / f"{safe_id}.done"

    # Directory creation happens regardless of whether anything is eventually
    # found -- it must exist before the marker-existence check below.
    try:
        marker_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return io_mod.ALLOW

    if ad.get("one_fire_per_session", True) and marker.exists():
        return io_mod.ALLOW

    cutoff_ts = ctx.now().timestamp() - (ad["grace_minutes"] * 60)

    found_lines: list[str] = []
    for dir_name in ad["dirs"]:
        p = cwd / dir_name
        if not p.exists():
            continue
        # Descends the whole subtree and does not filter dotfiles/dot-directories --
        # Windows has no POSIX-style hidden-file convention to bypass.
        stale = sorted(_stale_files_under(p, cutoff_ts), key=lambda f: f.name)
        if not stale:
            continue
        max_named = ad["max_named"]
        names = ", ".join(f.name for f in stale[:max_named])
        more = ""
        if len(stale) > max_named:
            more = ad["found_more_format"].replace("{n}", str(len(stale) - max_named))
        line = (
            ad["found_line_format"]
            .replace("{dir}", dir_name)
            .replace("{count}", str(len(stale)))
            .replace("{names}", names)
            .replace("{more}", more)
        )
        found_lines.append(line)

    if not found_lines:
        return io_mod.ALLOW

    try:
        marker.touch(exist_ok=True)
    except OSError:
        pass  # a marker write failure must not turn a real finding into a crash

    message = ad["message"].replace("{found}", "\n".join(found_lines))
    return io_mod.block(message)
