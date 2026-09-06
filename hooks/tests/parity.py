#!/usr/bin/env python3
"""hooks/tests/parity.py -- the gate that authorises the settings.json rewrite
(acceptance row 2, invariant I-10).

CONTRACT.md #6 'Parity-test contract'. WINDOWS-ONLY (it needs the .ps1 originals);
prints an explicit SKIPPED line elsewhere.

    python tests/parity.py [--fixtures-dir DIR]

Prints "PARITY: <n> matched / <m> cases, <k> intentional divergences" and exits
non-zero on any unexplained mismatch.

IMPLEMENTATION NOTE (U-0, staging machine): the real guard fixtures do not exist
yet (U-1..U-5 land them at fan-out) and the brief explicitly forbids a full parity
sweep as an experiment on this machine ("develop parity.py against a sandbox only.
Do not run a full parity sweep on this machine as an experiment"). This file
therefore implements the FULL mechanism -- sandbox_env construction, the four hard
preconditions, the stderr normaliser, the PE-id cross-check, per-case orchestration
-- and is unit-tested (see test_parity_sandbox() below and test_contract.py) against
pieces that never spawn a real .ps1 or touch the real machine. Running it against
real fixtures happens once U-1..U-5 have populated tests/fixtures/*.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
HOOKS_DIR = TESTS_DIR.parent
SKILL_ROOT = HOOKS_DIR.parent
FIXTURES_DIR = TESTS_DIR / "fixtures"

OLD_SIDE_TIMEOUT = 25  # seconds. S-9d: this must be JUST OVER
# thresholds.subprocess_timeout_remote_sec (20s, the budget the NEW side's gh call is
# bounded to). 45s (the earlier value) defeated the point: the master has no timeout
# of its own, and PE-7's fixture deliberately makes `gh` hang, so a 45s harness-side
# bound would stall that single case for 45s+ instead of catching the master at ~20s+.
# ponytail: hand-tuned fixed timeout, hardcoded against today's
# thresholds.subprocess_timeout_remote_sec value; if that config value changes,
# read it from cfg here instead of re-tuning this constant by hand.
NEW_SIDE_TIMEOUT = 30

_PE_ID_RE = re.compile(r"\*\*?PE-(\d+)\*\*?")


def _find_upward(filename: str, start: Path) -> Path:
    """Pins a depth-counting defect: depth-counting from a module's own
    location (`SKILL_ROOT.parent / "CONTRACT.md"`, hand-counting how many
    `..`s to climb) broke on the FIRST relayout the code ever saw -- the
    pre-promotion tree had an extra build level the promoted
    `~/.claude/skills/ulthar/` tree does not. Depth-counting is
    layout-specific by construction; searching UPWARD from `start` for the
    first ancestor directory containing `filename` is not -- it survives
    exactly the kind of relayout this project's own hooks/README says is
    coming next (moving the skill into a repo)."""
    for ancestor in [start, *start.parents]:
        candidate = ancestor / filename
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        f"{filename!r} not found in any ancestor directory of {start} -- a missing "
        f"{filename} is a broken installation, not a condition to silently fall back "
        f"from (the same class of hard error as an unresolved placeholder or an "
        f"unknown fixture key)."
    )


def _derive_pe_ids() -> set[str]:
    """Pins against a hand-maintained `PE_IDS = {... range(1, N)}` literal --
    exactly the kind of thing that gets forgotten the next time a PE is
    declared (PE-17 shipped in CONTRACT-ARCHIVE.md before such a constant was ever
    widened to notice it) -- so this DERIVES the id set from the PE table itself
    (`PE-<n>`, optionally bolded) rather than restating it by hand.

    Two candidate source files, tried in order: `CONTRACT-ARCHIVE.md` (the live
    skill's full history, §7) and `docs/PARITY-EXCEPTIONS.md` (the public fork's
    rewritten equivalent -- CONTRACT-ARCHIVE.md is deliberately excluded from that
    fork, so it ships the PE table under this second name instead). Both live
    trees never carry both files at once, but trying both keeps this one module
    byte-identical across live and fork rather than forking it too.

    Pins against silent degradation: a fallback to a hardcoded floor with a
    printed WARNING when neither source could be found or parsed would mean
    a PE beyond that floor going unenforced while the run still prints a clean
    result. Now a HARD error either way -- no silent fallback, no printed
    warning standing in for a real failure."""
    start = Path(__file__).resolve().parent
    source = None
    for filename in ("CONTRACT-ARCHIVE.md", "docs/PARITY-EXCEPTIONS.md"):
        try:
            source = _find_upward(filename, start)
            break
        except FileNotFoundError:
            continue
    if source is None:
        raise FileNotFoundError(
            "neither 'CONTRACT-ARCHIVE.md' nor 'docs/PARITY-EXCEPTIONS.md' found in "
            f"any ancestor directory of {start} -- a missing PE-id source is a broken "
            f"installation, not a condition to silently fall back from (the same class "
            f"of hard error as an unresolved placeholder or an unknown fixture key)."
        )
    text = source.read_text(encoding="utf-8")
    found = {int(m) for m in _PE_ID_RE.findall(text)}
    if not found:
        raise ValueError(f"{source} contains no 'PE-<n>' ids -- cannot derive PE_IDS")
    return {f"PE-{n}" for n in range(1, max(found) + 1)}


PE_IDS = _derive_pe_ids()

# ------------------------------------------------------------------ PE-citation scan (source tree)

_SOURCE_PE_CITE_RE = re.compile(r"PE-(\d+)")

# Where a PE id gets DECLARED (via _PE_ID_RE above, which additionally
# requires the bold markdown-table shape) -- excluded from the scan below,
# which looks for where a citation slips in WITHOUT being declared.
_PE_DECLARATION_FILENAMES = {"CONTRACT-ARCHIVE.md", "PARITY-EXCEPTIONS.md"}


def scan_source_pe_citations(root: Path) -> dict[int, list[Path]]:
    """Scans every .py/.json/.md file under `root` for a bare `PE-<n>` mention
    and returns {id: [files that mention it]}, excluding the declaration
    files themselves (_PE_DECLARATION_FILENAMES) and __pycache__.

    Closes a channel a 2026-09-05 send-back found open: `_derive_pe_ids` and
    `cross_check_parity_exceptions` (test_contract.py check7) only ever look
    at CONTRACT-ARCHIVE.md's own table and fixture `parity_exception` fields
    -- a PE id cited in a docstring, a code comment, or a config.json `_note`
    is invisible to both, which is exactly how a stray undeclared PE id shipped
    without ever being declared. This scans the WHOLE tracked-shape tree
    instead, so any such citation is caught regardless of which file it
    hides in."""
    found: dict[int, list[Path]] = {}
    for pattern in ("*.py", "*.json", "*.md"):
        for f in root.rglob(pattern):
            if "__pycache__" in f.parts or f.name in _PE_DECLARATION_FILENAMES:
                continue
            try:
                text = f.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for m in _SOURCE_PE_CITE_RE.findall(text):
                found.setdefault(int(m), []).append(f)
    return found


def undeclared_source_pe_citations(root: Path, pe_ids: set[str] = PE_IDS) -> dict[int, list[Path]]:
    """{id: [files]} for every PE-<n> mention under `root` whose id exceeds
    the highest id `pe_ids` actually declares -- an id this far past the
    declared table is either a typo or an undeclared citation (the failure mode: a renumber that touched a citation but never added the
    §7 entry declaring it)."""
    max_declared = max(int(pe.split("-")[1]) for pe in pe_ids)
    cited = scan_source_pe_citations(root)
    return {n: files for n, files in cited.items() if n > max_declared}


def is_windows() -> bool:
    return sys.platform.startswith("win")


# ------------------------------------------------------------------ retirement (post .ps1 deletion)


def _masters_missing(ps1_dir: Path, cases: list[dict] | None = None) -> bool:
    """True once ANY .ps1 master a live sweep would need is gone. Checked
    STRUCTURALLY: `dispatch.ps1` (every old-side run wraps a copy of it, see
    _build_dispatch_wrapper) OR, when `cases` is given, any distinct guard
    `.ps1` a loaded fixture names via its `ps1` field. Checking dispatch.ps1
    alone is not enough -- the owner deletes the masters via `pm-delete-allow`,
    one path per line, so an individual guard `.ps1` can vanish before
    `dispatch.ps1` does. Without this wider check the sweep would still start,
    then CRASH per case inside `_build_dispatch_wrapper`'s `copy2` (a missing
    guard `.ps1`, not a missing `dispatch.ps1`) -- landing on `run_all.py` as a
    permanent FAIL rather than the graceful retirement this function exists to
    enable. This is a structural check on disk, never a flag someone has to
    remember to pass -- the same discipline this file already applies to
    unknown fixture keys and unresolved placeholders (a silent no-op there was
    always the bug, never the fix)."""
    if not (ps1_dir / "dispatch.ps1").is_file():
        return True
    if cases:
        needed = {c["ps1"] for c in cases if c.get("ps1")}
        for name in needed:
            if not (ps1_dir / name).is_file():
                return True
    return False


PARITY_RETIRED_MESSAGE = (
    "PARITY: RETIRED -- parity testing compares a Python guard against its "
    "PowerShell predecessor, but those .ps1 masters are not present at "
    "{ps1_dir} on this machine and are not part of this distribution. A live "
    "sweep cannot run here -- there is nothing on this machine to compare the "
    "Python guards against. This is not a skip and not a failure, it is "
    "retirement. Nothing was compared in this run."
)


# ------------------------------------------------------------------ sandbox_env (S-1)


class SandboxExemptCollisionError(Exception):
    """Amends CONTRACT §6 precondition 1. The
    precondition used to read 'the resolved sandbox home is under the session
    scratchpad' -- but 'session scratchpad' was only ever a PROXY for the real
    requirement (disposable, not the real home). Nesting sandbox_root under it
    (`...\\Temp\\claude\\<workspace>\\<session>\\scratchpad\\...`) put every
    sandbox path inside paths.exempt_patterns, which lists '/Temp/claude/' and
    '/scratchpad/' as blanket substrings -- so commander and skill_anchor
    exempted every fixture under the sandbox regardless of the is_under() /
    matches_any() logic actually under test. Two PE-3 cases
    (skill-anchor-w4, commander-pe3-repo-backup-sibling) were PASSING through
    parity for the wrong reason: they expect 'exempt' and got 'exempt' from
    the sandbox path colliding with the exemption list, not from the PE-3
    logic being exercised. The fix: sandbox_root now resolves under bare
    %TEMP% (what the original PowerShell golden harnesses did, and what is
    NOT itself on the exempt list), and _assert_sandbox_not_exempt() checks
    that fact at startup through the SAME matches_any(exempt_patterns) the
    guards use, aborting loudly rather than silently certifying a
    harness that sits inside the very exemption list it means to test."""


def _resolve_sandbox_parent() -> Path:
    """CONTRACT §6 precondition 1 (amended): the sandbox root's
    PARENT must be disposable and outside the real home -- bare %TEMP% (via
    tempfile.gettempdir(), which honours TEMP/TMP) satisfies that without
    also matching paths.exempt_patterns the way the session scratchpad does.
    See SandboxExemptCollisionError for why the previous session-scratchpad
    placement was wrong, not merely suboptimal."""
    return Path(tempfile.gettempdir())


def _assert_sandbox_not_exempt(sandbox_root: Path, cfg: dict) -> None:
    """Resolves the sandbox root through the SAME matches_any(exempt_patterns)
    commander/skill_anchor use (kind:"path" flags, via lib.ctx.compile_patterns)
    and raises SandboxExemptCollisionError if it matches. A harness sitting
    inside the exemption list it is testing cannot measure that list -- this
    must run BEFORE any case seeds state or invokes either side."""
    sys.path.insert(0, str(HOOKS_DIR))
    from lib import ctx as ctx_mod, paths as paths_mod

    class _MinimalCtx:
        is_windows = is_windows()

    patterns = cfg["paths"]["exempt_patterns"]["patterns"]
    compiled = ctx_mod.compile_patterns(patterns, "path", _MinimalCtx(), "paths.exempt_patterns.patterns")
    if paths_mod.matches_any(str(sandbox_root), compiled, _MinimalCtx()):
        raise SandboxExemptCollisionError(
            f"sandbox_root {sandbox_root} matches paths.exempt_patterns {patterns} "
            "-- every fixture under it would be silently exempted by commander/"
            "skill_anchor regardless of the logic under test. This must be placed "
            "under bare %TEMP%, never under the session scratchpad."
        )


def build_sandbox_env(sandbox_root: Path) -> dict:
    """S-1, normative. COPY the parent environment then override -- never build from
    {} (a replacement loses PATH/SystemRoot/PATHEXT and powershell will not start,
    and git/gh must stay reachable). USERPROFILE is the variable that matters on
    Windows (S-1b): every master reads $env:USERPROFILE / $env:TEMP directly, none
    of them uses ~ or $HOME.

    Pins the same "harness world diverges from what's under test"
    family as the frozen clock / exempt-list-collision / gh shim: this
    subdirectory used to be named 'tmp' -- but paths.exempt_patterns' entry is
    the literal substring '/Temp/claude/', matching the REAL Windows %TEMP%
    convention (…\\AppData\\Local\\Temp\\…). 'tmp' and 'Temp' are different
    STRINGS, not merely different case (kind:"path" IGNORECASE would have
    covered a case difference; it cannot cover a missing letter) -- so every
    ${TEMP}-anchored fixture path (e.g. commander's 'allow-scratchpad' case)
    could never genuinely match the exemption it means to exercise. Renamed
    to 'Temp' so a sandboxed ${TEMP} path is faithful to what the exempt-
    pattern list, and the real guards, actually expect to see."""
    tmp = sandbox_root / "Temp"
    env = os.environ.copy()
    env.update(
        {
            "USERPROFILE": str(sandbox_root),
            "HOME": str(sandbox_root),
            "TEMP": str(tmp),
            "TMP": str(tmp),
            "HOMEDRIVE": sandbox_root.drive or "C:",
            "HOMEPATH": str(sandbox_root)[len(sandbox_root.drive):] if sandbox_root.drive else str(sandbox_root),
        }
    )
    return env


def assert_sandbox_preconditions(sandbox_env: dict, sandbox_root: Path, real_home: Path) -> list[str]:
    """The four hard preconditions (§6). Asserted against sandbox_env ITSELF -- the
    dict actually handed to the child -- not against a Python-side Ctx (S-1a).
    Returns a list of violated preconditions (empty = all satisfied). Failing these
    aborts the suite rather than degrading it."""
    problems: list[str] = []
    real_home_str = str(real_home)

    # 1 + 2: resolved sandbox home is not the real home, under the session scratchpad,
    # and every redirected variable in sandbox_env itself resolves under sandbox_root.
    for key in ("USERPROFILE", "HOME", "TEMP", "TMP"):
        val = sandbox_env.get(key, "")
        if not val.startswith(str(sandbox_root)):
            problems.append(f"sandbox_env[{key!r}]={val!r} does not resolve under sandbox_root")
        # "not the real home": the value must not be the real profile root itself
        # (or the real profile's own %TEMP%). Being nested somewhere under the real
        # user's filesystem tree is unavoidable on this machine -- the session
        # scratchpad IS under the real home -- so this checks exact identity with
        # the real HOME / real %TEMP%, not mere ancestry.
        if val == real_home_str:
            problems.append(f"sandbox_env[{key!r}]={val!r} IS the real home")

    homedrive = sandbox_env.get("HOMEDRIVE", "")
    homepath = sandbox_env.get("HOMEPATH", "")
    if not (homedrive + homepath).startswith(str(sandbox_root)):
        problems.append("HOMEDRIVE+HOMEPATH does not resolve under sandbox_root")

    return problems


def capture_pm_delete_allow_state(real_home: Path) -> tuple[float, int] | None:
    """Precondition 3: the real pm-delete-allow's mtime and byte length, captured
    before the run and re-checked after (in `finally`); any change fails the suite
    loudly. Returns None if the file does not exist (nothing to protect)."""
    p = real_home / ".claude" / "state" / "pm-delete-allow"
    if not p.exists():
        return None
    st = p.stat()
    return (st.st_mtime, st.st_size)


def assert_pm_delete_allow_unchanged(real_home: Path, before: tuple[float, int] | None) -> str | None:
    p = real_home / ".claude" / "state" / "pm-delete-allow"
    after = (p.stat().st_mtime, p.stat().st_size) if p.exists() else None
    if before != after:
        return f"REAL pm-delete-allow changed during the run: before={before} after={after}"
    return None


# ------------------------------------------------------------------ setup builders (§6 step 2)
#
# Contract addendum: run_case() previously never read case["setup"], so
# every class-B fixture (gate 6, gate-2-satisfied, gate 3, PE-9, U-1's consume-on-use,
# U-3's commander-mode cases, U-5's marker cases) ran against an EMPTY sandbox and
# passed for the wrong reason -- both sides take the same "missing state" branch and
# silently agree. This section is the fix: a small, closed set of named builders that
# seed state into the SANDBOX home / redirected TEMP, NEVER the real ones.
#
# A fixture's `setup` value is either one step-dict or a list of them:
#   {"kind": "approval_file", "name": "...", ...}
#   [{"kind": "commander_mode", "mode": "enforce"}, {"kind": "approval_file", ...}]
#
# An unknown `kind` is a HARD ERROR (raises SetupError) -- never a silent no-op, so a
# typo in a fixture cannot degrade that case into an unseeded pass (the exact failure
# mode this section exists to close).


class SetupError(Exception):
    """Raised by run_setup() on an unknown `kind` or malformed step. Propagates out
    of run_case() as a hard failure -- never caught and downgraded to a pass."""


def _expand_in_sandbox(value: str, sandbox_root: Path, cwd: Path, temp_dir: Path) -> Path:
    """Same four variables lib/paths.expand() recognises (~, ${TEMP}, ${CWD},
    ${DEPLOY_DIR}/${SKILL_DIR} are not needed by any setup builder today), resolved
    against the SANDBOX tree only -- this is the seam that keeps setup from ever
    writing outside the sandbox by accident. No os.environ / Path.home() read here."""
    v = value.replace("${TEMP}", str(temp_dir)).replace("${CWD}", str(cwd))
    if v.startswith("~"):
        rest = v[1:].lstrip("/\\")
        return sandbox_root / rest if rest else sandbox_root
    p = Path(v)
    return p if p.is_absolute() else (cwd / v)


def _set_mtime(path: Path, age_sec: float | None) -> None:
    """Controlled mtime (gate 6 needs to pin a file's age on EITHER side of
    min_age_sec, so 'just written' is not sufficient). age_sec=None leaves the
    filesystem's own just-written timestamp. age_sec=N sets mtime = now - N seconds.

    Pins a residual: `now` used to be threaded in
    as a single constant frozen once at sweep start (`main()`'s
    `datetime.datetime(2026, 9, 4, 12, 0, 0)`), reused across all 162 cases
    and both sides of each case. But NEITHER side ever receives that clock --
    the master reads `(Get-Date) - $ap.LastWriteTime` and the new side reads
    `ctx.now()`, both the REAL system clock in their own subprocess. Anchoring
    the seeded mtime to a frozen constant while both guards measure elapsed
    time against the real clock means a case run late enough in a long sweep
    (gh-shim stalls, per-case process-spawn overhead) computes a FALSE age --
    both sides agree on the wrong answer, which is what made this surface as
    a residual instead of a mismatch. Fixed by reading the live clock HERE,
    at the moment the file is actually written (immediately before the
    subprocess under test runs), not from a value computed once, sweep-wide,
    long before either side's guard reads its own clock."""
    if age_sec is None:
        return
    now = datetime.datetime.now().astimezone()
    ts = (now - datetime.timedelta(seconds=age_sec)).timestamp()
    os.utime(path, (ts, ts))


def _setup_approval_file(step: dict, sandbox_root: Path, cwd: Path, temp_dir: Path,
                          cfg: dict) -> None:
    """Seeds ~/.claude/state/brief-approvals/<name> (gate 6's `dir`, resolved from
    cfg to avoid a second hardcoded copy). `age_sec` pins the file's age relative to
    the LIVE clock read inside _set_mtime at call time (see
    _set_mtime's docstring); `ok_marker` appends the configured OWNER-OK line
    (raw substring match, F-15) so a case can seed a young-but-approved file."""
    g6 = [r for r in cfg["brief_rules"]["kinds"][3]["rules"] if r["id"] == "gate6-approval-handshake"][0]
    approval_dir = _expand_in_sandbox(g6["dir"], sandbox_root, cwd, temp_dir)
    approval_dir.mkdir(parents=True, exist_ok=True)
    path = approval_dir / step["name"]
    content = step.get("content", "presented understanding + acceptance-criteria deltas\n")
    if step.get("ok_marker"):
        content += f"\n{g6['ok_marker']} approved by owner\n"
    path.write_text(content, encoding="utf-8", newline="\n")
    _set_mtime(path, step.get("age_sec"))


def _setup_commander_mode(step: dict, sandbox_root: Path, cwd: Path, temp_dir: Path,
                           cfg: dict) -> None:
    """Seeds a commander-mode marker. `scope: 'global'` (default) writes
    ~/.claude/state/commander-mode; `scope: 'repo'` writes ${CWD}/.claude/state/
    commander-mode (marker_lookup_order's repo-override entry). `mode` is a TOKEN
    (e.g. 'enforce'/'observe'/'off'), written as the file's first line."""
    scope = step.get("scope", "global")
    template = cfg["commander"]["marker_lookup_order"][0 if scope == "repo" else 1]
    path = _expand_in_sandbox(template, sandbox_root, cwd, temp_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(step["mode"] + "\n", encoding="utf-8", newline="\n")


def _setup_pm_delete_allow(step: dict, sandbox_root: Path, cwd: Path, temp_dir: Path,
                            cfg: dict) -> None:
    """Seeds ~/.claude/state/pm-delete-allow with one path per line (I-6). Needed for
    U-1's consume-on-use cases: run twice against a freshly rebuilt sandbox each time
    (per §6 step 2), so the SECOND run's fixture must seed the allowlist again rather
    than rely on the first run's consumption."""
    dele = {r["id"]: r for r in cfg["command_rules"]["tool_discipline"]}["delete-outside-disposable"]
    path = _expand_in_sandbox(dele["pm_allowlist"]["file"], sandbox_root, cwd, temp_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(step["paths"]) + "\n", encoding="utf-8", newline="\n")


def _setup_artifact_hygiene_marker(step: dict, sandbox_root: Path, cwd: Path, temp_dir: Path,
                                    cfg: dict) -> None:
    """Seeds the once-per-session marker under the REDIRECTED ${TEMP} (never the
    real one) so a 'QUIET 2nd time (marker)' style case can start from
    already-fired state. `session_id` is sanitised exactly as the guard does
    (artifact_dirs.session_id_sanitise_pattern / session_id_fallback)."""
    marker_dir = _expand_in_sandbox(cfg["artifact_dirs"]["marker_dir"], sandbox_root, cwd, temp_dir)
    marker_dir.mkdir(parents=True, exist_ok=True)
    raw = step.get("session_id", "")
    sanitised = re.sub(cfg["artifact_dirs"]["session_id_sanitise_pattern"], "_", raw) or \
        cfg["artifact_dirs"]["session_id_fallback"]
    (marker_dir / sanitised).write_text("fired\n", encoding="utf-8", newline="\n")


def _setup_artifact_file(step: dict, sandbox_root: Path, cwd: Path, temp_dir: Path,
                          cfg: dict) -> None:
    """Contract addendum 5: seeds a file inside a gitignored artifact
    directory (artifact_dirs.dirs, e.g. '.playwright-mcp') UNDER cwd, with a
    controlled mtime -- the same age_sec mechanism gate 6 needed
    (_set_mtime), because 'FIRE stale artifact' vs 'QUIET' golden cases turn on
    the file's age relative to artifact_dirs.grace_minutes, not on merely
    existing. Params: `dir` (must be one of artifact_dirs.dirs), `name`
    (filename, may include subdirectories for the recursive-scan cases -- M-7),
    `content`, `age_sec` (None leaves the just-written timestamp), `hidden`
    (bool, prefixes the leaf name with '.' to exercise the guard's unconditional
    hidden-directory scan)."""
    dirs = cfg["artifact_dirs"]["dirs"]
    if step["dir"] not in dirs:
        raise SetupError(f"artifact_file: dir {step['dir']!r} is not in artifact_dirs.dirs {dirs}")
    name = step["name"]
    if step.get("hidden"):
        parts = name.rsplit("/", 1)
        parts[-1] = "." + parts[-1]
        name = "/".join(parts)
    path = cwd / step["dir"] / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(step.get("content", "artifact\n"), encoding="utf-8", newline="\n")
    _set_mtime(path, step.get("age_sec"))


def _setup_git_repo(step: dict, sandbox_root: Path, cwd: Path, temp_dir: Path,
                     cfg: dict) -> None:
    """Throwaway git repo for git_discipline / pr_state branch-state cases (§6
    'Stubbing external commands'), and U-2's ${MAIN_REPO}/${FEATURE_REPO}/
    ${NO_REPO} convention. `name`: "main"|"feature"|"none"|<anything> resolves the
    location via _repo_path_for (so it lines up with _resolve_payload_cwd /
    cwd_kind without the fixture having to spell out the path); `at_cwd: true`
    (default) overrides that to use the payload's own cwd instead. `commits`:
    list of commit messages (one empty commit each); `branch`: checked out
    (created) after the commits, if given. `name: "none"` (or `no_git: true`)
    deliberately does NOT run `git init` -- an ordinary, non-repo directory, for
    the NO_REPO case class."""
    if step.get("at_cwd", True):
        repo_dir = cwd
    else:
        repo_dir = _repo_path_for(step.get("name", "repo"), sandbox_root)
    repo_dir.mkdir(parents=True, exist_ok=True)

    if step.get("no_git") or step.get("name") == "none":
        return  # NO_REPO: a plain directory, deliberately not a git repo

    # Pins: these four subprocess.run calls previously passed no env=,
    # so `git config`/`git commit` read and wrote the REAL user gitconfig (and
    # `git init` could pick up real global config) instead of staying inside the
    # sandbox -- build_sandbox_env is a pure function of sandbox_root, so it is
    # recomputed here rather than threading a new parameter through every
    # SETUP_BUILDERS signature.
    git_env = build_sandbox_env(sandbox_root)
    run = lambda *argv: subprocess.run(argv, cwd=str(repo_dir), capture_output=True,
                                        timeout=10, check=True, env=git_env)
    run("git", "init", "-q")  # idempotent: git init on an existing repo is a safe no-op
    run("git", "config", "user.email", "parity@example.invalid")
    run("git", "config", "user.name", "parity")
    for msg in step.get("commits", ["initial"]):
        run("git", "commit", "--allow-empty", "-q", "-m", msg)

    default_branch = {"main": cfg["git_policy"]["protected_branches"][0],
                       "feature": "feature/parity-fixture"}.get(step.get("name"))
    branch = step.get("branch", default_branch)
    if branch:
        # -B (not -b): idempotent across the two seed() calls per side (§6 step 2) --
        # -b would raise "branch already exists" on the second, same-case reseed.
        run("git", "checkout", "-q", "-B", branch)


def _setup_skill_anchor_state(step: dict, sandbox_root: Path, cwd: Path, temp_dir: Path,
                               cfg: dict) -> None:
    """`kind: "skill_anchor_state"`. Recognised STRUCTURALLY as well (see
    run_setup): U-3's real fixtures (tests/fixtures/write_edit/skill_anchor.json,
    found by inventorying every landed fixture, not guessed) write this shape
    WITHOUT a `kind` key -- `{"active_skills": "<content>", "anchors": {<name>:
    {"text": ...} | {"repeat": ..., "length": ...}}}`. Seeds the GLOBAL
    active-skills list (skill_anchor.list_lookup_order[-1], resolved in-sandbox)
    with `active_skills` verbatim, and one `skill_anchor.anchor_file_name` per
    entry in `anchors` under `~/.claude/skills/<name>/`, content either the
    literal `text` or `repeat * length` (truncation-limit fixtures)."""
    sa = cfg["skill_anchor"]
    list_path = _expand_in_sandbox(sa["list_lookup_order"][-1], sandbox_root, cwd, temp_dir)
    list_path.parent.mkdir(parents=True, exist_ok=True)
    list_path.write_text(step.get("active_skills", ""), encoding="utf-8", newline="\n")

    for name, spec in step.get("anchors", {}).items():
        skill_dir = sandbox_root / ".claude" / "skills" / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        if "text" in spec:
            content = spec["text"]
        elif "repeat" in spec and "length" in spec:
            content = (spec["repeat"] * spec["length"])[: spec["length"]]
        else:
            content = ""
        (skill_dir / sa["anchor_file_name"]).write_text(content, encoding="utf-8", newline="\n")


SETUP_BUILDERS = {
    "skill_anchor_state": _setup_skill_anchor_state,
    "approval_file": _setup_approval_file,
    "commander_mode": _setup_commander_mode,
    "pm_delete_allow": _setup_pm_delete_allow,
    "artifact_hygiene_marker": _setup_artifact_hygiene_marker,
    "artifact_file": _setup_artifact_file,
    "git_repo": _setup_git_repo,
}


def run_setup(setup_spec, sandbox_root: Path, cwd: Path, temp_dir: Path,
              cfg: dict) -> None:
    """Applies every step of a fixture's `setup` value (a dict, or a list of them).
    Unknown `kind` is a HARD ERROR (SetupError), never a silent no-op -- a typo in a
    fixture must not degrade that case into an unseeded pass. `None`, `{}` and `[]`
    all mean "no setup for this case" (a hermetic class-A case may legitimately
    omit `setup` or declare it empty) -- only a step that HAS content but is
    missing `kind`, or names an unknown `kind`, is an error."""
    if not setup_spec:
        return
    steps = setup_spec if isinstance(setup_spec, list) else [setup_spec]
    for step in steps:
        if not isinstance(step, dict):
            raise SetupError(f"malformed setup step (not an object): {step!r}")
        kind = step.get("kind")
        if kind is None and "active_skills" in step:
            # STRUCTURAL recognition (found by inventorying real fixtures, not a
            # second competing schema invented here): U-3's skill_anchor fixtures
            # never carry an explicit `kind`. Accepting the shape as-authored
            # avoids asking another unit to reformat 16 already-landed, otherwise
            # correct fixtures for a convention they could not have known this
            # harness would later adopt.
            kind = "skill_anchor_state"
        if kind is None:
            raise SetupError(f"malformed setup step (missing 'kind'): {step!r}")
        builder = SETUP_BUILDERS.get(kind)
        if builder is None:
            raise SetupError(
                f"unknown setup kind {kind!r} -- known kinds: {sorted(SETUP_BUILDERS)}"
            )
        builder(step, sandbox_root, cwd, temp_dir, cfg)


# ------------------------------------------------------------------ stderr normalisation


# gate6's "approval window not elapsed (Ns < 180s)" message embeds a COMPUTED
# elapsed-seconds value that the old and new side each measure at their own,
# genuinely different, wall-clock instant (they run sequentially, seconds
# apart) -- the live-clock fix (see _set_mtime) is what makes this value
# volatile in the first place. Comparing it literally makes the case flaky by
# construction: it passes or fails depending on how many seconds happened to
# elapse between two subprocess launches. This is NOT a blanket "ignore all
# digits" rule -- only the SPECIFIC computed value in THIS specific message
# shape is normalised; the threshold (180, a config constant) and every
# other digit anywhere else in any other message (branch names, PR numbers,
# port numbers, ...) must still compare exactly, the same discipline as the
# existing line-ending/whitespace normalisation just below.
_GATE6_ELAPSED_RE = re.compile(r"\(\d+s < (\d+)s\)")


def normalise_stderr(raw: bytes) -> str:
    """Decode UTF-8; \\r\\n -> \\n; strip trailing whitespace per line; strip
    leading/trailing blank lines; normalise gate6's volatile elapsed-seconds
    figure to a fixed placeholder (see _GATE6_ELAPSED_RE).
    Path separators and every OTHER interpolated value are NOT normalised
    away -- a message naming the wrong file is a real failure."""
    text = raw.decode("utf-8", errors="replace")
    text = text.replace("\r\n", "\n")
    text = _GATE6_ELAPSED_RE.sub(r"(Ns < \1s)", text)
    lines = [line.rstrip() for line in text.split("\n")]
    while lines and lines[0] == "":
        lines.pop(0)
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


# ------------------------------------------------------------------ PE cross-check (test_contract check 7)


def cross_check_parity_exceptions(fixtures_dir: Path) -> tuple[set[str], set[str]]:
    """Returns (cited, uncited_pe_ids). Every case with parity:false must carry a
    parity_exception id from §7; every id in §7 must be cited by at least one such
    case -- a parity exception with no fixture is not an exception, it is an
    untested change."""
    cited: set[str] = set()
    if fixtures_dir.exists():
        for f in fixtures_dir.rglob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            entries = data if isinstance(data, list) else [data]
            for entry in entries:
                if isinstance(entry, dict) and entry.get("parity") is False:
                    exc = entry.get("parity_exception")
                    if exc:
                        cited.add(exc)
    return cited, PE_IDS - cited


# ------------------------------------------------------------------ per-case orchestration


# Contract addendum 4: the STDOUT channel matters just as much as
# stderr. skill_anchor (I-2) ALWAYS returns exit 0 and communicates ENTIRELY
# through stdout (the raw anchor text on SessionStart, the JSON envelope on every
# other event) -- so a run_case() that only compares exit code + stderr would
# certify skill_anchor as "matched" on every fixture regardless of what it
# actually emitted, silently exempting the one guard PE-2 and PE-3 change from
# ever being checked. §6 step 7 requires `new.stdout == old.stdout` as BYTES --
# no normalisation, unlike stderr -- because the envelope is byte-sensitive (fixed
# key order, no extraneous whitespace, ensure_ascii=False, no trailing newline);
# any "cleanup" pass could itself paper over a real encoding or shape regression.
STDOUT_EMITTERS = {"skill_anchor"}


def _build_dispatch_wrapper(ps1_dir: Path, guard_ps1_name: str, event: str, sandbox_root: Path) -> Path:
    """Pins that parity invokes the OLD side through a dispatch.ps1 wrapper, not
    the bare guard `.ps1` directly -- the NEW side always goes through
    `dispatch.py` -- and on exit 0 the dispatcher DROPS non-blocking stderr
    (F-6). A guard that emits an informational stderr line on ALLOW
    (tool_discipline's 'PM allowlist consumed for: ...' is the live example)
    then compared old="message" against new="" at matching exit codes: not a
    real behaviour divergence (production runs BOTH sides through their own
    dispatcher, and both drop it identically), but a defect in the HARNESS's
    model -- comparing a bare script to a wrapped one compares two different
    things. Fixed by wrapping the OLD side too: a throwaway copy of the REAL
    `dispatch.ps1` plus the ONE guard `.ps1` under test, sitting together
    under the sandbox with a minimal `dispatch-map.json` that routes every
    tool_name (`"tools": "*"`) to just that one guard. `dispatch.ps1` resolves
    everything relative to `$PSScriptRoot`, so both files must be co-located;
    the map is deliberately narrowed to ONE guard so this isolates the
    wrapping model (stderr-drop-on-allow, stdout-passthrough, first-exit-2-
    wins) without pulling in whatever OTHER real guards happen to be routed
    for this event in production. `dispatch.ps1` itself sets
    `[Console]::OutputEncoding` to UTF-8 internally (the same fix this
    project's very first pass made), so a plain `-File` invocation of THIS
    wrapper needs no `-Command`/`exit $LASTEXITCODE` trick -- that was only
    ever needed for the bare masters, which do not set it themselves. As a
    bonus this exercises `dispatch.ps1`'s own routing/wrapping logic, which
    was previously untested by parity at all."""
    wrapper_dir = sandbox_root / ".dispatch-wrapper"
    wrapper_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ps1_dir / "dispatch.ps1", wrapper_dir / "dispatch.ps1")
    shutil.copy2(ps1_dir / guard_ps1_name, wrapper_dir / guard_ps1_name)
    dispatch_map = {event: [{"tools": "*", "scripts": [guard_ps1_name]}]}
    (wrapper_dir / "dispatch-map.json").write_text(
        json.dumps(dispatch_map), encoding="utf-8"
    )
    return wrapper_dir / "dispatch.ps1"


def run_old_side(ps1_dir: Path, guard_ps1_name: str, event: str, payload: dict, env: dict,
                  sandbox_root: Path, spawn_cwd: str | None = None) -> tuple[int | str, bytes, bytes]:
    """Old side: routes through a throwaway copy of the REAL dispatch.ps1
    (see _build_dispatch_wrapper), never the bare guard script directly.
    subprocess.run(["powershell", "-File", <wrapper>, "-Event",
    <event>], input=<payload>, env=env, timeout=OLD_SIDE_TIMEOUT). On timeout
    returns the sentinel "TIMEOUT" (PE-7). Returns (returncode, stderr, stdout).

    `spawn_cwd` (`needs_chdir` fixture field): a
    handful of masters resolve relative-path escapes (e.g. `..`) against the
    HOOK PROCESS's own cwd, not payload.cwd -- M-9b/F-18. Without setting the
    SUBPROCESS's own cwd, that resolution silently happened against
    parity.py's own real working directory (wherever it was invoked from),
    which is neither the sandbox nor anything the fixture controls. `None`
    (the default) leaves cwd unset, inheriting the parent -- unchanged
    behaviour for every case that does not need this."""
    wrapper_ps1 = _build_dispatch_wrapper(ps1_dir, guard_ps1_name, event, sandbox_root)
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(wrapper_ps1),
             "-Event", event],
            input=json.dumps(payload).encode("utf-8"),
            capture_output=True,
            env=env,
            cwd=spawn_cwd,
            timeout=OLD_SIDE_TIMEOUT,
        )
        return proc.returncode, proc.stderr, proc.stdout
    except subprocess.TimeoutExpired:
        return "TIMEOUT", b"", b""


def run_new_side(dispatch_py: Path, event: str, guard: str, payload: dict, env: dict,
                  spawn_cwd: str | None = None) -> tuple[int, bytes, bytes]:
    """New side: dispatch.py --event <E> --guard <id>, same sandbox_env plus
    ULTHAR_CONFIG, same serialised bytes as the old side. `spawn_cwd`: see
    run_old_side's docstring (`needs_chdir`). Returns
    (returncode, stderr, stdout)."""
    proc = subprocess.run(
        [sys.executable, str(dispatch_py), "--event", event, "--guard", guard],
        input=json.dumps(payload).encode("utf-8"),
        capture_output=True,
        cwd=spawn_cwd,
        env=env,
        timeout=NEW_SIDE_TIMEOUT,
    )
    return proc.returncode, proc.stderr, proc.stdout


def _repo_path_for(name: str, sandbox_root: Path) -> Path:
    """Fixed location for a named throwaway repo under one sandbox: repos/main,
    repos/feature, repos/none (U-2's ${MAIN_REPO}/${FEATURE_REPO}/${NO_REPO}
    convention -- see _setup_git_repo and _resolve_payload_cwd)."""
    return sandbox_root / "repos" / name


def _resolve_payload_cwd(case: dict, payload: dict, sandbox_root: Path) -> Path:
    """A fixture's payload.cwd may carry placeholders that cannot be known until
    run time (the sandbox root is fresh per case): '${SANDBOX}' (the sandbox root
    itself), and U-2's throwaway-repo convention '${MAIN_REPO}' / '${FEATURE_REPO}'
    / '${NO_REPO}' -- three FIXED locations under the sandbox
    (repos/main, repos/feature, repos/none) that a `git_repo` setup step (name:
    "main"|"feature"|"none") seeds to match. A case may instead declare
    `"cwd_kind": "main"|"feature"|"none"` directly instead of spelling the
    placeholder out in payload.cwd.

    Deliberately narrow: this ONLY resolves the handful of repo/sandbox
    placeholders needed to know where the sandboxed cwd itself lives, BEFORE the
    full placeholder map (which needs that cwd value) can be built. Every other
    placeholder -- including a literal '${CWD}' elsewhere in payload.cwd -- is
    resolved uniformly by _substitute_placeholders() below, which is applied to
    the WHOLE payload, not just this field."""
    cwd_kind = case.get("cwd_kind")
    if cwd_kind:
        return _repo_path_for(cwd_kind, sandbox_root)
    raw = payload.get("cwd")
    if raw is None:
        return sandbox_root
    v = str(raw)
    v = v.replace("${MAIN_REPO}", str(_repo_path_for("main", sandbox_root)))
    v = v.replace("${FEATURE_REPO}", str(_repo_path_for("feature", sandbox_root)))
    v = v.replace("${NO_REPO}", str(_repo_path_for("none", sandbox_root)))
    # '${CWD}' is U-3's spelling of the same concept as '${SANDBOX}' (the sandbox
    # root itself) -- found by inventorying real fixtures (tests/fixtures/
    # write_edit/skill_anchor.json), not guessed. Both resolve identically.
    v = v.replace("${SANDBOX}", str(sandbox_root))
    v = v.replace("${CWD}", str(sandbox_root))
    return Path(v)


class UnresolvedPlaceholderError(Exception):
    """Pins that the harness once resolved only 8 of the 10
    placeholders fixtures actually use, and only inside payload.cwd -- every
    OTHER payload field (file_path, notebook_path, transcript_path) carrying a
    placeholder was passed through LITERALLY, unresolved, to both the real
    .ps1 master and dispatch.py. A guard correctly failing to match a literal
    '${DEPLOY_DIR}/x.ps1' string then reported as a guard defect ('new exit 0
    != expect_exit 2') when the fault was entirely the harness handing it the
    wrong input. Same failure class as load_fixtures() swallowing a parse
    error (CONTRACT's own precedent): a harness degrading silently instead of
    failing loudly costs a full review cycle to diagnose and would have cost
    four units a round of pointless churn chasing guards that were never
    broken. An unresolved placeholder is therefore now a HARD error, not a
    literal string handed downstream."""


def _placeholder_map(sandbox_root: Path, cwd: Path, sandbox_env: dict,
                      deploy_dir: Path, skill_hooks_dir: Path) -> dict[str, str]:
    """All TEN placeholders fixtures are measured to use, resolved to concrete
    sandboxed paths:

      ${CWD}              -- this case's resolved cwd (repo path under cwd_kind,
                              else the sandbox root itself)
      ${SANDBOX}           -- the sandbox root itself (same value as ${CWD} when
                              no cwd_kind is set; kept distinct because a case
                              may need "the sandbox" even when cwd is a repo)
      ${MAIN_REPO} / ${FEATURE_REPO} / ${NO_REPO} -- U-2's throwaway repos
      ${TEMP}              -- this run's redirected TEMP (sandbox_env['TEMP'])
      ${DEPLOY_DIR}        -- <sandbox_root>/.claude/hooks -- matches BOTH
                              sides: the old-side master hardcodes
                              Join-Path $env:USERPROFILE '.claude\\hooks', and
                              USERPROFILE is redirected to sandbox_root by
                              build_sandbox_env; the new side's ctx.deploy_dir
                              is config_path.parent, so run_case now writes a
                              per-case config.json COPY at exactly this path
                              and points ULTHAR_CONFIG at it (see run_case).
      ${SKILL_HOOKS_DIR}   -- the skill's MASTER hooks source dir, deliberately
                              NOT under ${DEPLOY_DIR} -- proves editing the
                              master is never blocked by the deploy guard.
      ${MAIN_TRANSCRIPT}   -- a transcript path with NO '/subagents/' segment
                              (lib.ctx.is_main_chain's path-based signal reads
                              this as main-chain)
      ${SUB_TRANSCRIPT}    -- a transcript path CONTAINING a '/subagents/'
                              segment (reads as a subagent transcript)
      ${ELSEWHERE}         -- a sibling directory outside both the sandbox cwd
                              and any repo/deploy path, same convention as the
                              existing '${CWD}-backup' PE-3 fixtures use."""
    return {
        "CWD": str(cwd),
        "SANDBOX": str(sandbox_root),
        "MAIN_REPO": str(_repo_path_for("main", sandbox_root)),
        "FEATURE_REPO": str(_repo_path_for("feature", sandbox_root)),
        "NO_REPO": str(_repo_path_for("none", sandbox_root)),
        "TEMP": str(sandbox_env["TEMP"]),
        "DEPLOY_DIR": str(deploy_dir),
        "SKILL_HOOKS_DIR": str(skill_hooks_dir),
        "MAIN_TRANSCRIPT": str(sandbox_root / "transcripts" / "main.jsonl"),
        "SUB_TRANSCRIPT": str(sandbox_root / "transcripts" / "subagents" / "sub1.jsonl"),
        "ELSEWHERE": str(sandbox_root) + "-elsewhere",
    }


def _substitute_placeholders(obj, mapping: dict[str, str]):
    """Recursively substitutes every '${NAME}' token in every string leaf of
    `obj` (dicts, lists, and bare strings) using `mapping`. Unknown-but-absent
    tokens are left alone here on purpose -- the caller (run_case) scans the
    RESULT for any surviving '${...}' afterwards and raises
    UnresolvedPlaceholderError, so a typo'd or genuinely new placeholder name
    is a loud, named failure rather than a value silently handed through."""
    if isinstance(obj, str):
        out = obj
        for name, value in mapping.items():
            out = out.replace("${%s}" % name, value)
        return out
    if isinstance(obj, dict):
        return {k: _substitute_placeholders(v, mapping) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_substitute_placeholders(v, mapping) for v in obj]
    return obj


_UNRESOLVED_PLACEHOLDER_RE = re.compile(r"\$\{[A-Z_]+\}")


def _find_unresolved_placeholders(obj) -> list[str]:
    """Scans a (post-substitution) structure for any surviving '${NAME}' token."""
    found: list[str] = []
    if isinstance(obj, str):
        found.extend(_UNRESOLVED_PLACEHOLDER_RE.findall(obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            found.extend(_find_unresolved_placeholders(v))
    elif isinstance(obj, list):
        for v in obj:
            found.extend(_find_unresolved_placeholders(v))
    return found


_GH_SHIM_MODULE = '''"""Throwaway gh shim module -- see parity.py's _build_gh_shim(). Reads a
per-case spec (ULTHAR_GH_SPEC) and answers deterministically instead of the real
`gh`, so pr_state cases can declare a canned PR state that a sandbox repo
with no real GitHub remote could never produce. Every invocation is logged
(ULTHAR_GH_LOG) so parity.py can assert argv/cwd/call-count after the run, and
can tell "the shim answered" apart from "nothing answered"/"the real gh
answered"."""
import json
import os
import sys
import time


def main():
    argv = sys.argv[1:]
    head = None
    for i, tok in enumerate(argv):
        if tok == "--head" and i + 1 < len(argv):
            head = argv[i + 1]
            break

    spec_path = os.environ.get("ULTHAR_GH_SPEC")
    spec = {"default": None, "by_head": {}}
    if spec_path and os.path.exists(spec_path):
        with open(spec_path, "r", encoding="utf-8") as fh:
            spec = json.load(fh)

    log_path = os.environ.get("ULTHAR_GH_LOG")
    if log_path:
        entry = {"argv": argv, "cwd": os.getcwd(), "head": head}
        with open(log_path, "a", encoding="utf-8", newline="\\n") as fh:
            fh.write(json.dumps(entry) + "\\n")

    stub = None
    if head is not None and head in spec.get("by_head", {}):
        stub = spec["by_head"][head]
    if stub is None:
        stub = spec.get("default")

    if stub is None:
        # No stub declared for this case/head at all: safe default is "no PRs
        # found" (empty JSON array, rc 0) -- never silently reach the real gh.
        print("[]")
        sys.exit(0)

    if stub.get("timeout"):
        # PE-7: simulate a hung `gh` -- sleep well past any caller-side
        # timeout budget so the CALLER's own timeout (not this shim) fires.
        time.sleep(120)
        sys.exit(1)

    print(json.dumps(stub.get("response", [])))
    sys.exit(stub.get("rc", 0))
'''


def _build_gh_shim(case: dict, sandbox_root: Path) -> Path:
    """CONTRACT §6: 'a mock gh shim placed first on PATH for both sides'.
    Without it, pr_state's BLOCK-expecting cases (canned MERGED/OPEN/CLOSED PR
    states) structurally cannot be produced by a live sweep -- the sandbox
    repo has no real GitHub remote -- so those cases could only ever fail and
    get misattributed to the guard a second time.

    Pins a shim-resolution defect: a first version's `gh.cmd` batch-file relay
    reliably intercepted PowerShell's `& gh` (a real shell, PATHEXT-aware)
    but was silently skipped by `subprocess.run(["gh", ...], shell=False)` --
    `CreateProcess` (what `shell=False` uses, no shell involved) only tries
    `.exe`/`.com`, not `.cmd`/`.bat`. A distlib-generated `.exe` launcher
    fixed the test harness but added a second "looks installed and is not"
    dependency (`distlib`, only ever present as a `pip`-vendored internal) --
    which failed on a machine where it was not resolvable.

    Fixed by removing the need for a compiled launcher
    entirely, at the RIGHT layer -- `lib/ctx.py`'s
    `Ctx.run_cmd` (see its docstring) now resolves `argv[0]` through
    `shutil.which()`, which DOES honour PATHEXT, before invoking
    `subprocess.run`. `guards/pr_state.py` calls `gh` exclusively through
    `ctx.run_cmd(argv, "remote", ...)`, so this fix reaches the new side
    without any harness-only workaround. A plain `.cmd` shim now works for
    BOTH sides again: PowerShell's own resolution already honoured PATHEXT,
    and the new side's `run_cmd` now does too. This is a FIDELITY fix, not
    merely a test convenience -- see `Ctx.run_cmd`'s docstring for why."""
    shim_dir = sandbox_root / ".gh-shim"
    shim_dir.mkdir(parents=True, exist_ok=True)
    (shim_dir / "gh_shim.py").write_text(_GH_SHIM_MODULE + "\nmain()\n", encoding="utf-8")
    python_exe = sys.executable.replace("\\", "\\\\")
    (shim_dir / "gh.cmd").write_text(
        f'@echo off\r\n"{python_exe}" "%~dp0gh_shim.py" %*\r\n', encoding="utf-8"
    )
    spec = {"default": case.get("gh_stub"), "by_head": case.get("gh_stub_by_head", {})}
    (shim_dir / "gh_spec.json").write_text(json.dumps(spec), encoding="utf-8")
    return shim_dir


def _read_gh_log(log_path: Path) -> list[dict]:
    if not log_path.exists():
        return []
    out = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def run_case(case: dict, ps1_dir: Path, dispatch_py: Path, sandbox_env: dict, cfg_path: Path,
             sandbox_root: Path, cfg: dict,
             apply_setup: bool = True) -> list[str]:
    """Runs one fixture on both sides and returns a list of failure descriptions
    (empty = pass). `payload` must include tool_name (misreading #9).

    §6 step 2: `setup` seeds state into the SANDBOX tree (never the real one) BEFORE
    either side runs. Both sides must run against the SAME prepared tree, rebuilt
    between the two runs so a consuming guard (pm-delete-allow) does not poison the
    second run -- so setup is applied once before the old-side run and once more,
    fresh, before the new-side run. `apply_setup=False` is a test-only knob (parity
    self-test) that proves a case's outcome truly depends on setup having run.

    §6 step 7 / contract addendum 4: `new.stdout == old.stdout` is compared as RAW
    BYTES (no normalisation) for stdout-emitting guards (STDOUT_EMITTERS) -- the
    channel skill_anchor communicates through entirely, and the one PE-2/PE-3
    change would otherwise never be checked at all.

    Pins a residual: there is no `now` parameter here
    any more. A single clock value used to be computed ONCE in main() and threaded
    through every case's two _seed() calls -- but neither side's guard ever reads
    THAT clock (the master reads (Get-Date), ctx.now() reads the real system
    clock), so a seeded mtime anchored to a sweep-wide frozen constant drifted out
    of its intended age window over the course of a long sweep, producing a
    both-sides-agree-but-wrong result. _set_mtime() now reads the live clock
    itself, at the moment it actually writes the file -- see its docstring."""
    failures: list[str] = []
    payload = dict(case["payload"])
    if "tool_name" not in payload:
        return [f"{case['id']}: fixture payload is missing tool_name (misreading #9)"]

    cwd = _resolve_payload_cwd(case, payload, sandbox_root)
    # Contract addendum 5: this used to be an UNCONDITIONAL mkdir, which
    # destroys any fixture whose entire point is that cwd does NOT exist (e.g. an
    # artifact_hygiene "missing/never-created scan root" case) -- a harness that
    # creates the very condition a case is trying to observe is worse than no
    # harness. `create_cwd` defaults True (every other case needs cwd to exist)
    # and a case sets it to False to assert against a genuinely absent directory.
    if case.get("create_cwd", True):
        cwd.mkdir(parents=True, exist_ok=True)

    # ${DEPLOY_DIR} matches BOTH sides only if the
    # NEW side's ctx.deploy_dir (= config_path.parent) is made to equal the
    # SAME sandboxed path the OLD side's master computes
    # (Join-Path $env:USERPROFILE '.claude\hooks', and USERPROFILE is
    # redirected to sandbox_root). So a per-case config.json COPY is written at
    # exactly that path and ULTHAR_CONFIG points at the copy, not at the real
    # dev-tree config.json -- guard imports are unaffected (dispatch.py's
    # `guards.<id>` import is a normal package import relative to dispatch.py's
    # own location, never to config_path).
    deploy_dir = sandbox_root / ".claude" / "hooks"
    deploy_dir.mkdir(parents=True, exist_ok=True)
    sandboxed_cfg_path = deploy_dir / "config.json"
    sandboxed_cfg_path.write_bytes(cfg_path.read_bytes())
    skill_hooks_dir = sandbox_root / ".claude" / "skills" / "ulthar" / "hooks"

    placeholders = _placeholder_map(sandbox_root, cwd, sandbox_env, deploy_dir, skill_hooks_dir)
    # Pins a silent-degradation defect: `_check_gh_expectations` used to read `expect_gh_cwd` /
    # `expect_gh_argv_contains` straight off the RAW case dict, which never went
    # through substitution -- only `payload` did. So an assertion written as
    # `expect_gh_cwd: "${CWD}"` compared against the literal string '${CWD}' and
    # reported a false failure while the fixture and the guard were both
    # correct. Fixed by substituting the WHOLE case (not payload in isolation)
    # -- placeholders in an assertion are as much a part of the fixture as
    # placeholders in the payload, and fixing this field-by-field would only
    # leave the next assertion key to be discovered the same way. `case` is
    # reassigned (shadowing the parameter) so every subsequent `case.get(...)`
    # read in this function sees resolved values.
    case = _substitute_placeholders(case, placeholders)
    payload = case["payload"]
    unresolved = _find_unresolved_placeholders(case)
    if unresolved:
        raise UnresolvedPlaceholderError(
            f"{case.get('id', '<no id>')}: unresolved placeholder(s) anywhere in the "
            f"fixture (payload OR an assertion field): {sorted(set(unresolved))} "
            f"-- add them to _placeholder_map() rather than letting a literal "
            f"'${{...}}' string reach either side or silently fail an assertion"
        )
    payload["cwd"] = str(cwd)
    temp_dir = Path(sandbox_env["TEMP"])

    # The gh shim goes FIRST on PATH for BOTH
    # sides unconditionally (not only for cases that declare a stub) -- a
    # case with no gh_stub/gh_stub_by_head still gets the shim, which answers
    # "no PRs found" (rc 0, []) by default rather than letting an unshimmed
    # `gh` leak through to the real network under the owner's token. Mutating
    # sandbox_env directly (not a copy) is deliberate: it is fresh per-case
    # (built once per case in main()'s loop) and both the old-side call below
    # and `new_env = dict(sandbox_env)` need the same PATH.
    gh_shim_dir = _build_gh_shim(case, sandbox_root)
    sandbox_env["PATH"] = str(gh_shim_dir) + os.pathsep + sandbox_env.get("PATH", "")
    gh_log_old = sandbox_root / ".gh-shim" / "log-old.jsonl"
    gh_log_new = sandbox_root / ".gh-shim" / "log-new.jsonl"
    sandbox_env["ULTHAR_GH_SPEC"] = str(gh_shim_dir / "gh_spec.json")

    def _seed() -> None:
        if apply_setup and case.get("setup") is not None:
            run_setup(case["setup"], sandbox_root, cwd, temp_dir, cfg)
        # Pins: U-3's commander fixtures (tests/fixtures/write_edit/
        # commander.json) express their commander-mode marker as a TOP-LEVEL
        # `"marker"` field on the case (a domain-specific shorthand), never inside
        # `setup` -- run_case only ever read case.get("setup"), so every one of
        # these 12 cases ran against a sandbox with NO marker file at all,
        # regardless of what "marker" said, and both sides silently agreed on
        # "no marker -> default mode" for the wrong reason. Same class of bug as
        # the placeholder-resolution defect: the harness quietly ignoring a
        # field it does not recognise instead of consuming or rejecting it.
        if apply_setup and case.get("marker"):
            run_setup({"kind": "commander_mode", "mode": case["marker"]},
                      sandbox_root, cwd, temp_dir, cfg)

    new_env = dict(sandbox_env)
    new_env["ULTHAR_CONFIG"] = str(sandboxed_cfg_path)
    new_env["ULTHAR_GH_LOG"] = str(gh_log_new)
    if case.get("guards_dir"):
        # Meta-fixtures that exercise DISPATCHER plumbing rather than any one real
        # guard's behaviour (PE-8's "a guard forced to raise" is not about what
        # any specific real guard does) route through a test-only guard, e.g.
        # tests/fixtures/stub/. Relative to TESTS_DIR. Never used for a guard's
        # own behavioural fixtures -- those exercise the REAL guards/ tree.
        new_env["ULTHAR_GUARDS_DIR"] = str((TESTS_DIR / case["guards_dir"]).resolve())
    is_emitter = case["guard"] in STDOUT_EMITTERS

    needs_chdir = bool(case.get("needs_chdir"))
    spawn_cwd = str(cwd) if needs_chdir else None

    if not case.get("parity", True):
        _seed()
        new_rc, new_err, new_out = run_new_side(
            dispatch_py, case.get("event", "PreToolUse"), case["guard"], payload, new_env,
            spawn_cwd=spawn_cwd)
        exc = case.get("parity_exception")
        if not exc:
            return [f"{case['id']}: parity:false with no parity_exception id"]
        if new_rc != case["expect_exit"]:
            failures.append(f"{case['id']} ({exc}): new exit {new_rc} != expected {case['expect_exit']}")
        expect_contains = case.get("expect_stderr_contains")
        if expect_contains and expect_contains not in normalise_stderr(new_err):
            failures.append(f"{case['id']} ({exc}): new stderr missing {expect_contains!r}")
        expect_stdout = case.get("expect_stdout")
        if expect_stdout is not None and new_out != expect_stdout.encode("utf-8"):
            failures.append(f"{case['id']} ({exc}): new stdout != expect_stdout")
        failures.extend(_check_stdout_contains(case, new_out, exc))
        failures.extend(_check_gh_expectations(case, _read_gh_log(gh_log_new)))
        return failures

    # Class B: setup runs ONCE PER SIDE, freshly, so a consuming guard's first
    # (old-side) run does not leave the tree in a state the second (new-side) run
    # would silently benefit or suffer from.
    _seed()
    old_event = case.get("event", "PreToolUse")
    old_env = dict(sandbox_env)
    old_env["ULTHAR_GH_LOG"] = str(gh_log_old)
    old_rc, old_err, old_out = run_old_side(ps1_dir, case["ps1"], old_event, payload, old_env,
                                             sandbox_root, spawn_cwd=spawn_cwd)

    _seed()
    new_rc, new_err, new_out = run_new_side(
        dispatch_py, case.get("event", "PreToolUse"), case["guard"], payload, new_env,
        spawn_cwd=spawn_cwd)

    if old_rc != new_rc:
        failures.append(f"{case['id']}: exit mismatch old={old_rc} new={new_rc}")
    if new_rc != case["expect_exit"]:
        failures.append(f"{case['id']}: new exit {new_rc} != expect_exit {case['expect_exit']}")
    if old_rc != "TIMEOUT" and normalise_stderr(old_err) != normalise_stderr(new_err):
        failures.append(
            f"{case['id']}: stderr mismatch\n  old={normalise_stderr(old_err)!r}\n  new={normalise_stderr(new_err)!r}"
        )
    if is_emitter and old_rc != "TIMEOUT" and old_out != new_out:
        failures.append(
            f"{case['id']}: STDOUT mismatch (raw bytes, no normalisation -- {case['guard']} "
            f"communicates only through stdout)\n  old={old_out!r}\n  new={new_out!r}"
        )
    failures.extend(_check_stdout_contains(case, new_out, None))
    # Assert the SHIM (not the real gh) answered,
    # checked against the NEW side's own log -- the primary target for
    # gh_stub/expect_gh_* fields, matching the existing convention that
    # expect_stdout_contains etc. assert against the new side.
    failures.extend(_check_gh_expectations(case, _read_gh_log(gh_log_new)))
    return failures


def _check_gh_expectations(case: dict, log_entries: list[dict]) -> list[str]:
    """Asserts the gh SHIM (not the real gh, and
    not silence) is what actually answered. `expect_no_gh_call` requires zero
    log entries; `expect_gh_argv_contains` requires every listed token to
    appear, in order-agnostic fashion, in SOME logged argv; `expect_gh_cwd`
    requires some logged invocation's cwd to match. A case that declares
    `gh_stub`/`gh_stub_by_head` but asserts nothing else still gets an
    implicit non-empty-log check -- if the shim was never reached (PATH
    override failed to take, or the guard fell through to a real `gh`), that
    must be a loud, attributable failure, not a silent pass."""
    failures: list[str] = []
    cid = case["id"]
    if case.get("expect_no_gh_call"):
        if log_entries:
            failures.append(f"{cid}: expect_no_gh_call, but the gh shim logged {len(log_entries)} call(s)")
        return failures
    if case.get("gh_stub") or case.get("gh_stub_by_head"):
        if not log_entries:
            failures.append(f"{cid}: declares gh_stub/gh_stub_by_head but the shim logged NO calls "
                             f"-- the PATH override may not have taken, or a real `gh` answered instead")
    wanted_argv = case.get("expect_gh_argv_contains")
    if wanted_argv:
        if not any(all(tok in entry["argv"] for tok in wanted_argv) for entry in log_entries):
            failures.append(f"{cid}: no logged gh call's argv contains all of {wanted_argv} "
                             f"(logged: {[e['argv'] for e in log_entries]})")
    wanted_cwd = case.get("expect_gh_cwd")
    if wanted_cwd:
        norm_wanted = wanted_cwd.replace("\\", "/").rstrip("/")
        if not any(entry["cwd"].replace("\\", "/").rstrip("/") == norm_wanted for entry in log_entries):
            failures.append(f"{cid}: no logged gh call's cwd == {wanted_cwd!r} "
                             f"(logged: {[e['cwd'] for e in log_entries]})")
    return failures


def _check_stdout_contains(case: dict, new_out: bytes, exc_label: str | None) -> list[str]:
    """`expect_stdout_contains`: a list of substrings that must ALL appear in the
    NEW side's stdout, decoded as UTF-8 (found in real fixtures --
    tests/fixtures/write_edit/skill_anchor.json -- not part of the original
    design; added once the shape was discovered by inventory). Independent of
    STDOUT_EMITTERS/`expect_stdout` (exact match) -- a case can ask for either,
    both, or neither."""
    wanted = case.get("expect_stdout_contains")
    if not wanted:
        return []
    text = new_out.decode("utf-8", errors="replace")
    label = f" ({exc_label})" if exc_label else ""
    missing = [s for s in wanted if s not in text]
    if missing:
        return [f"{case['id']}{label}: new stdout missing expect_stdout_contains {missing!r}"]
    return []


class FixtureLoadError(Exception):
    """A fixture file did not parse. Pins against a silent
    `except: continue` -- a typo in a fixture file vanished into a green
    'N matched / N cases' with N simply smaller than intended. Loud now: the
    whole load fails rather than quietly dropping one file's cases."""


# "A fixture field or convention the harness does not read" is a first-class
# defect category -- several harness defects (frozen clock threading,
# commander's top-level `marker`, a `{{BAD_PATH}}`/private-field near miss)
# hid behind an unknown key being silently ignored rather than
# rejected. Every key that ANY currently-landed fixture legitimately uses,
# across every unit's files, as of this pass. A new key is added here
# DELIBERATELY, in the same commit that starts consuming it -- never as a
# side effect of a fixture author inventing a name run_case() happens to
# never look at.
KNOWN_FIXTURE_KEYS = {
    "id", "guard", "ps1", "event", "payload", "cwd_kind", "create_cwd",
    "needs_chdir", "marker", "setup", "guards_dir", "via",
    "expect_exit", "expect_stderr_contains", "expect_stdout", "expect_stdout_contains",
    "expect_stdout_not_contains",
    "gh_stub", "gh_stub_by_head", "expect_gh_argv_contains", "expect_gh_cwd", "expect_no_gh_call",
    "parity", "parity_exception",
    "_note", "_why", "_why_setup", "_why_pe16",
}


def _check_unknown_fixture_keys(f: Path, entry: dict) -> None:
    unknown = set(entry) - KNOWN_FIXTURE_KEYS
    if unknown:
        raise FixtureLoadError(
            f"{f}: fixture {entry.get('id', '<no id>')!r} has unrecognised key(s) "
            f"{sorted(unknown)} -- run_case() would silently never read them. Add the "
            f"key to KNOWN_FIXTURE_KEYS in the SAME change that makes run_case() "
            f"actually consume it, or fix the typo."
        )


def load_fixtures(fixtures_dir: Path) -> list[dict]:
    cases = []
    if not fixtures_dir.exists():
        return cases
    for f in sorted(fixtures_dir.rglob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception as exc:
            raise FixtureLoadError(f"{f}: does not parse as JSON: {exc}") from exc
        entries = data if isinstance(data, list) else [data]
        for entry in entries:
            if isinstance(entry, dict) and "guard" in entry and "payload" in entry:
                _check_unknown_fixture_keys(f, entry)
                cases.append(entry)
    return cases


LAST_RUN_RETIRED = False  # set True by main() when it takes the retirement branch;
# read by run_all.py right after calling parity.main() so its own summary can
# print a status distinct from both PASS and SKIP -- see main()'s retirement
# check below. Reset at the top of every main() call, not sticky across calls.


def main(argv: list[str]) -> int:
    global LAST_RUN_RETIRED
    LAST_RUN_RETIRED = False

    # `--no-parity` / `ULTHAR_SKIP_PARITY=1`: still runs the PE-citation cross-check
    # (cheap, no subprocess) but never spawns powershell against the real .ps1
    # masters. Added per contract addendum 5's explicit "do not run a full parity
    # sweep against the live machine until I say so" -- without this flag,
    # run_all.py's unconditional `parity.main([])` call makes "run the whole
    # tree" and "no live sweep" impossible to satisfy at the same time.
    skip_sweep = "--no-parity" in argv or os.environ.get("ULTHAR_SKIP_PARITY") == "1"

    fixtures_dir = FIXTURES_DIR
    if "--fixtures-dir" in argv:
        fixtures_dir = Path(argv[argv.index("--fixtures-dir") + 1])

    real_home = Path(os.environ.get("USERPROFILE") or Path.home())
    ps1_dir = real_home / ".claude" / "hooks"

    cited, uncited = cross_check_parity_exceptions(fixtures_dir)
    if uncited:
        print(f"PARITY: FAIL -- {len(uncited)} parity exception(s) with no citing fixture: {sorted(uncited)}")

    cases = load_fixtures(fixtures_dir)
    if not cases:
        print("PARITY: 0 matched / 0 cases, 0 intentional divergences "
              "(no fixtures yet -- pre-fan-out; mechanism only)")
        return 1 if uncited else 0

    # Pins the loaded count so a fixture silently vanishing (a
    # bad merge, an accidental file deletion) is caught even though
    # load_fixtures() no longer swallows a PARSE error -- this catches the
    # different failure mode of a whole FILE going missing while every
    # remaining file still parses fine. Update this constant, with a stated
    # reason, whenever a unit intentionally adds or removes fixtures.
    # 185 = 179 + 6, PE-19 (2026-09-05): new compact BRIEF: header form
    # (+6 in tests/fixtures/briefs/compact_header.json: header references file for
    # gates 1/3/6, header kind pre-classifies gate 4, header no-ui tag satisfies
    # gate 2; cases prove: reference resolves (ALLOW), young approval blocks (BLOCK),
    # missing file falls back to gate 1 block (BLOCK), no-ui tag satisfies gate 2
    # (ALLOW), non-no-ui tag does not bypass gate 2 (BLOCK), kind=forwarder
    # classifies to forwarder gate 4 (BLOCK on missing .out file))
    # 189 = 185 + 4, 2026-09-05 owner-reported false positive: committer is now
    # ALSO classified by CONTENT, not only by the bracket/compact header, so a
    # Japanese commit dispatch stops falling through to the creator fallback and
    # being blocked by the UI-design case (+4 in
    # tests/fixtures/briefs/committer_content.json; all four carry parity: false
    # -- the .ps1 master classified by header only, so it would BLOCK the first
    # of them; the divergence is deliberate).
    # 206 = 189 + 17, 2026-09-06 self-disclosed live bypass: the delete rule was
    # written in terms of SHELL deletion verbs, and an interpreter invoked from
    # the shell (`python -c "shutil.rmtree(...)"`) is a deletion verb it did not
    # recognise (+17 in tests/fixtures/tool_discipline/interpreter_delete.json:
    # 5 blocking spellings of the bypass -- rmtree / os.remove / Path().unlink /
    # node -e rmSync / heredoc-piped python; 5 must-not-block cases proving it is
    # not a blanket ban on interpreters -- inside scratchpad, inside dist/, the
    # project's own `python run_all.py` suite invocation, an ordinary list
    # `.remove()`, an ordinary read-only one-liner; 1 owner-grant case proving
    # pm-delete-allow still authorises the interpreter form; and 6 for the
    # __pycache__/*.pyc allowlist addition, half of them proving the pattern
    # cannot be widened by a path that merely CONTAINS that substring. All carry
    # parity: false -- these patterns post-date the .ps1 cutover entirely, so
    # there is no master to diverge from.)
    # 216 = 206 + 10, 2026-09-06 final-review M1: quoted shell wrappers
    # (`bash -c "rm -rf ..."`, `sh -c '...'`, `powershell -Command "Remove-Item
    # ..."`, `cmd /c "del ..."`) were not segment starts, so the delete verb
    # inside them was never looked at -- and the rule's own block message names
    # Remove-Item as covered (+9 in the same
    # tests/fixtures/tool_discipline/interpreter_delete.json: 4 blocking wrapper
    # spellings; 5 must-not-block cases, including the THREE commit-message false
    # positives that decided the fix's shape -- a bare quoted verb, then an
    # unanchored wrapper word, both tried and both rejected, so the alternative
    # requires a WRAPPER WORD AT SEGMENT START; and 1 pinning the piped-`xargs`
    # residue as ALLOWED so it is never mistaken for coverage. parity: false for
    # the same reason as the 17 above -- these patterns post-date the .ps1 cutover.)
    # 273 = 263 + 10, 2026-09-06 review ROUND 4, delete rule. Two defects, two
    # residues. (a) The option run stopped at an option's ARGUMENT, so
    # `sudo -u root rm -rf <path>`, `env VAR=1 rm -rf <path>` and
    # `xargs -a list.txt rm -rf <path>` walked straight past; an option may carry
    # one non-flag argument now, and `VAR=value` is its own alternative. (b) The
    # prefixed verb and Windows switches were themselves path-shaped, so
    # `/usr/bin/rm -rf dist/x` and `cmd /c rd /s /q dist` BLOCKED deletes the
    # allowlist PERMITS -- a rule refusing permitted work, which is the failure
    # mode this rule is organised against; the verb token and single-letter `/x`
    # switches are rejected now. +10: 3 blocking option-argument spellings, 3
    # must-not-block ones, 2 permitted-delete ALLOWs, and 2 residues pinned as
    # blocks -- a `|` inside quotes still splitting a segment (the SAME
    # pre-existing class as `git commit -m "x; rm docs/old.md"`, not the token
    # issue it was reported as), and `ri lib/foo.rb`, where a two-letter verb
    # collides with Ruby's doc tool. parity: false as for the 74 above.
    #
    # 263 = 251 + 12, 2026-09-06 review ROUND 3, delete rule: the wrapper branch
    # allowed ANY text between the wrapper word and the verb, so
    # `timeout 10 git rm --cached x` BLOCKED while the unwrapped `git rm --cached x`
    # did not -- a false block on ordinary work, which is the failure mode this
    # rule's whole design is organised around. The option run is explicit now
    # (flags, and a bare number for `timeout 5` / `nice -n 10`), so the verb must
    # be the first word after it. Three more wrapper words went in
    # (time|doas|busybox), and the fact that the set is an ENUMERATION is pinned
    # by an ALLOW fixture for `setsid` rather than left to be rediscovered.
    # +12: 5 wrapped-subcommand ALLOWs, 5 BLOCKs (timeout/nice with an option
    # run, plus the three new words), 1 residue ALLOW, 1 ordinary `time npm run
    # build`. parity: false as for the 62 above.
    #
    # 251 = 235 + 16, 2026-09-06 final review ROUND 2: two residue claims in
    # docs/DESIGN.md were false, and were closed rather than softened -- a QUOTED
    # verb at segment start (`"rm" -rf <path>`), which the docs said could only
    # over-block, and `xargs` carrying a LITERAL path in its own segment
    # (`xargs -I{} rm -rf <path>`), which the docs called out of reach "by
    # construction" -- true only when the path sits in ANOTHER segment, which
    # td-int-26 still pins. Four more prefix words went with them
    # (command|nice|nohup|timeout|eval), each measured rc=0 first. +16: 9
    # blocking spellings and 7 false-positive re-runs -- `docker rm`,
    # `git rm --cached`, `npm run clean`, a gh comment mentioning rm, and the new
    # prefix words wrapping ordinary work. parity: false as for the 46 above.
    #
    # 235 = 216 + 19, 2026-09-06 final review M2: the residue list read as
    # exhaustive and was not. Seven more delete spellings measured ALLOWED
    # through the dispatcher -- `sudo`/`env`/absolute-path verbs, the same three
    # inside M1's own wrapper form, and `find ... -delete` / `-exec rm` -- all of
    # them the spelling an agent reaches for the moment `rm -rf` is refused, so
    # defects rather than residue by this rule's own standard (+19 in the same
    # tests/fixtures/tool_discipline/interpreter_delete.json: 8 blocking
    # spellings; 8 must-not-block cases, including `git rm`, `find -exec git rm`,
    # a fourth commit-message false positive naming `sudo`, and the shell-glob
    # escape hatch; and 3 pinning what stays open or what the `find` branch
    # COSTS -- the PRE-EXISTING bare-name target, the `.`-rooted `find -delete`,
    # and `find dist/ -name '*.map' -delete`, which blocks on the glob token even
    # though `dist/` is allowlisted. parity: false for the same reason as the 27
    # above -- these patterns post-date the .ps1 cutover.)
    # 276 = 273 + 3, 2026-09-06 review ROUND 5: the residue list in
    # docs/DESIGN.md heads itself "each pinned by a fixture" and the round-4
    # verb-token exclusion had no fixture. Its cost is that any path whose
    # BASENAME is a delete verb stops being judged -- `tools/rm`, `tools/rm.exe`,
    # `src/del` -- so all three are pinned ALLOW. parity: false as for the 84
    # above.
    EXPECTED_FIXTURE_COUNT = 276
    count_mismatch = len(cases) != EXPECTED_FIXTURE_COUNT
    if count_mismatch:
        print(f"PARITY: FAIL -- loaded {len(cases)} fixture cases, expected "
              f"{EXPECTED_FIXTURE_COUNT} (a fixture file may be missing or a new one "
              f"was added without updating this pin)")

    # Retirement check: once the .ps1 masters a live sweep would need are gone
    # (checked structurally against the ACTUAL fixtures just loaded -- not
    # just dispatch.ps1, but every distinct guard `.ps1` a `parity: true` case
    # names, since the owner deletes via pm-delete-allow one path per line and
    # a guard `.ps1` can vanish before dispatch.ps1 does), a real sweep can
    # never run again. This is checked AFTER the PE-citation cross-check and
    # the fixture-count pin above (both cheap, no subprocess, independent of
    # whether the masters exist) so that a real fixture-hygiene regression --
    # an uncited PE, a fixture file gone missing -- still turns the suite red
    # even once parity itself is retired; retirement explains why the SWEEP
    # didn't run, it does not excuse an unrelated, still-live check failing.
    # The live sweep spawns a real `powershell` to run the .ps1 masters through --
    # genuinely Windows-only, unlike the hygiene checks above (PE-citation
    # cross-check, fixture-count pin), which are plain file/data checks and now
    # run on every platform. Off Windows there is never a .ps1 master to sweep
    # against either, so "not Windows" folds into the same structural signal
    # _masters_missing already computes rather than adding a second, separate
    # platform gate that could disagree with it.
    retired = (not is_windows()) or _masters_missing(ps1_dir, cases)
    if retired:
        LAST_RUN_RETIRED = True

    if skip_sweep or retired:
        if retired:
            print(PARITY_RETIRED_MESSAGE.format(ps1_dir=ps1_dir))
            print(f"PARITY: RETIRED -- {len(cases)} cases loaded, not run. "
                  f"PE-citation + fixture-count check only.")
        else:
            print(f"PARITY: SWEEP SKIPPED (--no-parity / ULTHAR_SKIP_PARITY=1) -- "
                  f"{len(cases)} cases loaded, not run. PE-citation check only.")
        return 1 if (uncited or count_mismatch) else 0

    matched = 0
    divergences = 0
    all_failures: list[str] = []

    cfg_path = SKILL_ROOT / "config.json"
    with open(cfg_path, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)

    # `real_home` / `ps1_dir` were already resolved above (retirement check) --
    # still the reason ps1_dir is `~/.claude/hooks`, not
    # HOOKS_DIR: the .ps1 MASTERS are not staged alongside the Python
    # hooks in this repo; they are the currently-DEPLOYED rollback set at
    # ~/.claude/hooks/*.ps1 (CONTRACT.md I-10: "the .ps1 chain stays on disk
    # for rollback"). Passing HOOKS_DIR (this repo's own hooks/) as ps1_dir
    # made every old-side run fail with PowerShell's "-File parameter
    # argument ... does not exist", which LOOKS like the fixture ran (it
    # produced an exit code and stderr) but never actually exercised the .ps1
    # master's real logic -- every prior parity run in this session compared
    # against a file-not-found error, not real master behaviour, and
    # therefore never touched real guard state either.
    before = capture_pm_delete_allow_state(real_home)
    sandbox_parent = _resolve_sandbox_parent()
    aborted = False
    try:
        for case in cases:
            if aborted:
                break
            sandbox_root = Path(tempfile.mkdtemp(prefix=f"ulthar-parity-{case['id']}-", dir=str(sandbox_parent)))
            try:
                # A harness sitting inside the
                # exemption list it is testing cannot measure that list. This
                # must run before ANY setup/seed touches the sandbox.
                _assert_sandbox_not_exempt(sandbox_root, cfg)
                env = build_sandbox_env(sandbox_root)
                pre_problems = assert_sandbox_preconditions(env, sandbox_root, real_home)
                if pre_problems:
                    # Pins: this used to `continue` to the next case.
                    # CONTRACT §6 says a failed precondition ABORTS the suite --
                    # a precondition exists to catch "env= was silently dropped
                    # and this run is about to touch the real machine", and
                    # continuing past that for every remaining case is exactly
                    # the wrong response to that signal.
                    all_failures.append(f"{case['id']}: PRECONDITION FAILED -- ABORTING SUITE: {pre_problems}")
                    aborted = True
                    continue
                try:
                    failures = run_case(
                        case, ps1_dir, HOOKS_DIR / "dispatch.py", env, cfg_path,
                        sandbox_root, cfg,
                    )
                except Exception as exc:  # noqa: BLE001
                    # Contract addendum 2/5 (same resilience class as run_all.py's
                    # per-module isolation): ONE malformed or unsupported fixture
                    # (an unrecognised `setup` shape, a builder bug, ...) must not
                    # abort the ENTIRE suite -- a harness that stops at the first
                    # broken fixture cannot tell you how much is broken, and it
                    # would silently under-report every case after it
                    # alphabetically/positionally. Recorded as a failure for THIS
                    # case only; the loop continues to the next one.
                    all_failures.append(f"{case['id']}: CRASHED running the case: "
                                         f"{type(exc).__name__}: {exc}")
                    continue
                # Setup may have written into the sandbox AFTER the precondition
                # check ran; re-assert so a setup bug that escapes the sandbox is
                # never masked by having only checked before setup ran.
                post_problems = assert_sandbox_preconditions(env, sandbox_root, real_home)
                if post_problems:
                    failures.append(f"{case['id']}: PRECONDITION FAILED AFTER SETUP: {post_problems}")
                if failures:
                    all_failures.extend(failures)
                elif not case.get("parity", True):
                    divergences += 1
                else:
                    matched += 1
            finally:
                shutil.rmtree(sandbox_root, ignore_errors=True)
    finally:
        pm_problem = assert_pm_delete_allow_unchanged(real_home, before)
        if pm_problem:
            all_failures.append(pm_problem)

    for f in all_failures:
        print("FAIL " + f)
    print(f"PARITY: {matched} matched / {len(cases)} cases, {divergences} intentional divergences")
    return 1 if (all_failures or uncited or count_mismatch) else 0


if __name__ == "__main__":
    # Harness defect found on the live sweep: this machine's console codepage
    # (cp932) cannot encode a U+FFFD replacement character that can legitimately
    # appear inside a captured stderr diff (e.g. a mismatched byte from the
    # raw-byte stdout comparison for STDOUT_EMITTERS), so `print("FAIL " + f)`
    # crashed with UnicodeEncodeError AFTER the sweep had already run to
    # completion -- losing the summary line and every FAIL detail. Force UTF-8
    # on stdout/stderr for THIS script's own reporting only; it has no effect on
    # what dispatch.py or the .ps1 masters do.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
        sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
    except (AttributeError, ValueError):
        pass
    sys.exit(main(sys.argv[1:]))
