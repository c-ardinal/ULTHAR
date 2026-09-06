#!/usr/bin/env python3
"""hooks/install.py -- self-deploying installer/verifier for the hook runtime.

CONTRACT.md #5 ('install.py (U-0)'), M-16 (copy table), I-5/I-7/I-13, PE-11.

    python install.py                 # default deploy: validate config, check deps,
                                       # copy the M-16 set, refuse on any failure
    python install.py --verify        # hash-compare master vs deploy + dependency
                                       # check + smoke test (cd-compound via --guard)
    python install.py --uninstall     # remove exactly the copied set, file by file;
                                       # never touches anything it did not deploy

Copy set (M-16), exactly six rows -- never `hooks/tests/` (persists inside the
skill, owner-confirmed), never anything already present in the deploy directory
that is not on this list (third-party hooks, the retained *.ps1 rollback set).

That last promise is per-FILE, including inside `lib/` and `guards/`: deploy
overlays its own files onto whatever is already there and uninstall removes only
the paths COPY_TABLE ships, so a file another tool or the user put in a managed
directory survives both. Both commands name any such file on stdout rather than
working around it silently. The one exception is `__pycache__`/`*.pyc` under a
managed directory, treated as regenerable cache the running dispatcher wrote and
removed on uninstall. ULTHAR's own staging names inside the deploy dir --
`<name>.tmp-deploy` and `<dir>.old-deploy` -- are overwritten if they exist.

A SYMLINK (or Windows junction) sitting at a path the copy set ships is the one
state deploy refuses outright: overlaying onto it would write through the link
to a file outside the deploy directory, and removing it would delete something
this installer did not deploy. It names the link and its target and writes
nothing (`link_collisions`).

One file in the deploy dir is GENERATED rather than copied:
`deployed-from.txt` (lib.paths.DEPLOY_ORIGIN_FILE), holding the absolute path of
the master this deploy came from. It is what lets the deployed runtime name its
own master in a recovery message, lets skill_anchor find ANCHOR.md for a clone
that does not sit under skills_root, and lets THIS file refuse to run when it is
the deployed copy rather than a master (see deployed_copy_refusal).
"""
from __future__ import annotations

import argparse
import filecmp
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent          # <skill>/hooks  when run from the skill
SKILL_ROOT = HERE.parent                          # <skill>/

sys.path.insert(0, str(HERE))
from lib import paths as paths_mod  # noqa: E402  -- single resolve_python_exe() resolution point
from lib import ctx as ctx_mod  # noqa: E402  -- ConfigError, user-overlay merge


def _default_deploy_dir() -> Path:
    home = Path(os.environ.get("USERPROFILE") or os.environ.get("HOME") or Path.home())
    return home / ".claude" / "hooks"


def _merge_user_overlay(cfg: dict, deploy_dir: Path) -> dict:
    """Merges ONLY the USER overlay layer (CONTRACT.md 'overlays') -- deliberately
    NOT the project layer dispatch.py also merges. install.py performs GLOBAL
    operations (deploy to ~/.claude/hooks, rewrite settings.json); the cwd repo
    is untrusted and has no business influencing them. Concretely: the
    intersection of `overlays.project_allowed_paths` (the only paths a project
    overlay may ever touch) with anything install.py reads (paths.python_exe,
    dependencies, dispatch.events) is EMPTY -- merging the project layer here
    would buy nothing and cost a new failure mode (a malformed
    `.claude/ulthar-overlay.json` in some unrelated cwd aborting a GLOBAL
    --verify). This also means install.py need not care that it resolves cwd
    via os.getcwd() while dispatch.py uses the payload's cwd: the user path is
    `~`-rooted and cwd-independent, so the two can never diverge on it.

    A malformed USER overlay is config.json-equivalent trust and SHOULD still
    stop install (dispatch.py blocks on it too) -- raises ConfigError, which
    every caller catches and reports the same way as any other invalid-config
    refusal, never a raw traceback."""
    import copy

    ov = cfg.get("overlays", {})
    if not ov.get("enabled") or not ov.get("user_path"):
        return cfg
    ctx = ctx_mod.build_ctx({}, cfg, "install", deploy_dir)
    user_path = ctx.expand(ov["user_path"])
    if not user_path.exists():
        return cfg
    result = copy.deepcopy(cfg)
    # _merge_layer (not _merge_walk) so a user overlay's top-level `disable` list is
    # honoured and validated here too -- otherwise `install.py --verify` would pass
    # on a `disable` naming a rule that does not exist while the dispatcher blocked
    # on it at the next tool call. Raises ConfigError on malformed JSON, as before.
    pending = ctx_mod._merge_layer(result, user_path, ov.get("list_merge", {}), None, "user")
    ctx_mod._apply_disables(result, pending)
    return result


# M-16 copy table. Sources are relative to SKILL_ROOT; destinations relative to
# the deploy dir. "file" entries copy one file; "tree" entries copy a directory.
COPY_TABLE = [
    ("file", "config.json", "config.json"),
    ("file", "hooks/dispatch.py", "dispatch.py"),
    ("file", "hooks/install.py", "install.py"),
    ("file", "hooks/stop_dev_orphans.py", "stop_dev_orphans.py"),
    ("tree", "hooks/lib", "lib"),
    ("tree", "hooks/guards", "guards"),
]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _iter_tree_files(root: Path) -> list[Path]:
    return sorted(
        p for p in root.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
    )


def validate_config(cfg_path: Path) -> list[str]:
    """Returns a list of problems (empty = valid). Folds in the checks
    tests/test_contract.py performs, at the level install.py needs before deploy:
    JSON parses, every pattern_fields path resolves and compiles, no dangling
    *_named reference (PE-11)."""
    problems: list[str] = []
    try:
        with open(cfg_path, "r", encoding="utf-8-sig") as fh:  # tolerate a UTF-8 BOM, which some editors add
            cfg = json.load(fh)
    except Exception as exc:
        return [f"config.json does not parse: {exc}"]

    sys.path.insert(0, str(HERE))
    from lib import ctx as ctx_mod  # noqa: E402

    try:
        ctx_mod._resolve_named_alternations(cfg)
    except ctx_mod.ConfigError as exc:
        problems.append(str(exc))

    import re

    def walk(node, parts) -> tuple[list, bool]:
        """Returns (values, hit_empty_list). Accumulates across `*`/`[]` fan-out
        exactly like the pre-round1 version -- a fan-out branch that does not
        match (e.g. a dispatch rule using `tools_named` instead of `tools`) simply
        contributes nothing, same as always. `hit_empty_list` is the ONE addition
        (the config-generalisation round): True when a `[]` step lands on a list that is
        legitimately EMPTY (command_rules.dev_server / git_policy.deploy_refs now
        ship empty by default, the former one-time-owner's-convention rule/branches
        having moved to examples/*.example.json) -- the path still counts as
        RESOLVED in that case even though it yields zero values, whereas a path
        that resolves to nothing because a key is simply missing/mistyped does
        not."""
        if not parts:
            return [node], False
        head, rest = parts[0], parts[1:]
        out: list = []
        hit_empty = False
        if head == "*":
            if isinstance(node, dict):
                for v in node.values():
                    vals, he = walk(v, rest)
                    out += vals
                    hit_empty = hit_empty or he
        elif head == "[]":
            if isinstance(node, list):
                if not node:
                    hit_empty = True
                for v in node:
                    vals, he = walk(v, rest)
                    out += vals
                    hit_empty = hit_empty or he
        elif head.isdigit():
            if isinstance(node, list) and int(head) < len(node):
                out, hit_empty = walk(node[int(head)], rest)
        else:
            if isinstance(node, dict) and head in node:
                out, hit_empty = walk(node[head], rest)
        return out, hit_empty

    def parse_path(spec: str) -> list[str]:
        out: list[str] = []
        for seg in spec.split("."):
            base, brackets = seg, []
            while base.endswith("]"):
                i = base.rindex("[")
                inner = base[i + 1 : -1]
                brackets.insert(0, "[]" if inner == "" else inner)
                base = base[:i]
            out.append(base)
            out += brackets
        return out

    for f in cfg.get("regex", {}).get("pattern_fields", []):
        vals, hit_empty = walk(cfg, parse_path(f["path"]))
        if not vals and not hit_empty:
            problems.append(f"pattern_fields path does not resolve: {f['path']}")
            continue
        for v in vals:
            if not isinstance(v, str) or v == f.get("sentinel"):
                continue
            src = v.replace("{ref}", "X") if f.get("template") else v
            for windows in (True, False):
                flags = re.IGNORECASE if (f["kind"] == "text" or windows) else 0
                try:
                    re.compile(src, flags)
                except re.error as exc:
                    problems.append(f"{f['path']} fails to compile: {exc}")
    return problems


def check_dependencies(cfg: dict) -> list[str]:
    """I-13.1: imports every entry in config.dependencies.required and fails with an
    explicit message naming the module, expected version and install location if the
    import raises. Run on the default deploy path as well as --verify.

    Runs as a subprocess under the pinned `python_exe` (not whichever
    interpreter happens to run install.py) -- dispatch.py is invoked as
    `"{python_exe}" "{dispatch_py}" --event ...`, so a dependency present for
    the operator's own python/venv but absent for the pinned interpreter
    would otherwise pass this check and fail at runtime on every real tool
    call. Imports each module by name directly, not by proxy: `psutil` is
    imported lazily (lib/procs.py, I-13.2 -- only reachable from the Stop
    path), so importing `guards.*` here would never actually exercise it."""
    problems: list[str] = []
    python_exe = paths_mod.resolve_python_exe(cfg)
    for dep in cfg.get("dependencies", {}).get("required", []):
        module = dep["module"]
        code = f"import {module}"
        try:
            proc = subprocess.run([python_exe, "-c", code], capture_output=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired) as exc:
            problems.append(
                f"required dependency '{module}' (expected {dep.get('verified_version')}, "
                f"install location {dep.get('install_location')}) could not be checked -- "
                f"the pinned interpreter '{python_exe}' could not be run: {exc}"
            )
            continue
        if proc.returncode != 0:
            stderr_tail = proc.stderr.decode("utf-8", "replace").strip().splitlines()[-1:]
            problems.append(
                f"required dependency '{module}' (expected {dep.get('verified_version')}, "
                f"install location {dep.get('install_location')}) is NOT importable under "
                f"the PINNED interpreter '{python_exe}': {stderr_tail}"
            )
    return problems


IGNORE_CACHE = shutil.ignore_patterns("__pycache__", "*.pyc")


def ulthar_tree_rel_paths(src: Path) -> set[Path]:
    """The relative paths a `tree` row of COPY_TABLE actually ships, i.e. exactly
    the files ULTHAR owns inside that directory. Anything else found there
    belongs to someone else."""
    return {p.relative_to(src) for p in _iter_tree_files(src)}


def foreign_files(src: Path, dst: Path) -> list[Path]:
    """Files under a deployed `tree` destination that ULTHAR does not ship, as
    paths relative to `dst`. `__pycache__`/`*.pyc` are excluded: they are
    regenerable cache the running dispatcher writes, not anyone's data."""
    if not dst.exists():
        return []
    ours = ulthar_tree_rel_paths(src)
    return sorted(
        p.relative_to(dst) for p in _iter_tree_files(dst)
        if p.relative_to(dst) not in ours
    )


class DeployRefused(Exception):
    """A deploy-time condition install.py will not resolve on the operator's
    behalf. Carries its own complete, already-formatted message (headline plus
    what to do); cmd_default prints it verbatim and exits 1."""


def _is_link(path: Path) -> bool:
    """True for anything that redirects to another path. `is_symlink()` alone is
    not enough on Windows: a directory JUNCTION is a reparse point that
    `os.path.islink` reports as False, while writes through it land at the
    target exactly as a symlink's do.

    `os.path.isjunction` is 3.12+ and this project's floor is 3.11 (README
    'Requirements'), so it is looked up rather than called directly -- on 3.11
    the junction case degrades to "not a link" instead of raising
    AttributeError on every single deploy."""
    return path.is_symlink() or getattr(os.path, "isjunction", lambda _p: False)(path)


def _clear_staging(path: Path) -> None:
    """Empties one of ULTHAR's own reserved staging names (`<name>.tmp-deploy`,
    `<dir>.old-deploy`) whatever occupies it. These names are documented as
    ULTHAR's to overwrite, so unlike an owned path a link here is REMOVED rather
    than refused -- but it is still never written THROUGH, which a plain
    `shutil.copy2` onto it would do."""
    if _is_link(path):
        try:
            os.unlink(path)
        except OSError:
            os.rmdir(path)  # Windows directory symlink / junction
    elif path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def link_collisions(deploy_dir: Path) -> list[str]:
    """`"<path> -> <target>"` for every link occupying a path ULTHAR owns.

    Deploy OVERLAYS its files onto whatever the deploy dir already holds, and
    the seeding step keeps existing links as links (`symlinks=True`) so foreign
    ones are preserved rather than flattened. Both are wanted -- together they
    mean a link sitting at a path the copy set ships would be written THROUGH:
    `shutil.copy2` opens the link and truncates its TARGET, a file outside the
    deploy directory that ULTHAR neither deployed nor was asked to touch. A link
    at an ancestor directory redirects the whole subtree the same way.

    Reported, not repaired, and checked BEFORE the first byte is staged so no
    row is half-deployed. Replacing the link would delete something install.py
    did not deploy, which is the exact promise the rest of this file keeps
    (I-7); following it writes outside the tree. Refusing does neither, leaves
    every byte on disk untouched, and names the path and its target -- the one
    outcome the operator can act on, because only they know why the link is
    there.

    A `file` row's own destination is checked too, which is STRICTER than that
    row's hazard: a file row swaps in with `os.replace`, which replaces a link
    rather than following it. Refused anyway, deliberately -- "a link at a path
    ULTHAR owns is refused" is one rule an operator can hold in their head,
    where "refused under lib/ and guards/, silently replaced at the root" is
    two, and the silent half destroys a link this installer did not deploy."""
    hits: dict[Path, str] = {}
    for kind, src_rel, dst_rel in COPY_TABLE:
        dst = deploy_dir / dst_rel
        candidates = [dst]
        if kind == "tree" and dst.exists():
            for rel in ulthar_tree_rel_paths(SKILL_ROOT / src_rel):
                candidates += [dst / p for p in (rel, *rel.parents) if str(p) != "."]
        for cand in candidates:
            if cand not in hits and _is_link(cand):
                try:
                    target = os.readlink(cand)
                except OSError as exc:
                    target = f"<unreadable link target: {exc}>"
                hits[cand] = f"{cand} -> {target}"
    return [hits[k] for k in sorted(hits)]


def copy_deploy(deploy_dir: Path) -> list[str]:
    """Deploys the M-16 set and returns the deploy-dir-relative paths of files it
    found in a managed directory but did not deploy (left untouched, reported by
    the caller).

    Every entry is staged beside its final destination and swapped in with
    `os.replace` (an atomic rename on both platforms, guaranteed same-volume
    since both live under `deploy_dir`). A `file` row is therefore genuinely
    atomic: one rename-over, and a reader sees the old complete file or the new
    one, never a half-written one.

    A `tree` row CANNOT be, and this docstring used to claim otherwise.
    `os.replace` refuses to rename over an existing directory on Windows, so the
    swap is necessarily TWO renames -- old aside, new in -- with a window
    between them in which the destination does not exist. A hook firing inside
    that window sees no `lib/` and fails closed, which is this project's correct
    behaviour and also, briefly, a locked-out operator. The window is two
    adjacent renames wide (metadata only, no copying), it is unavoidable on
    Windows, and it is stated here rather than denied. What IS guaranteed: the
    old tree is renamed, never deleted, until the new one is in place, and if
    the second rename fails the first is undone, so a failed swap leaves the
    previous complete tree back at its own path rather than a missing one. If
    even that restore fails, the exception names the intact `.old-deploy` path
    and the rename to run by hand. A rolled-back tree row leaves the deploy
    INCOMPLETE rather than undone -- the `file` rows above it already landed --
    and the message says exactly that instead of promising a clean revert.

    For a `tree` row the staging directory is seeded with the EXISTING contents
    of the destination before ULTHAR's files are overlaid on top, so a file this
    installer did not deploy survives the swap instead of being discarded with
    the old tree. That seeding step is what makes the docstring's
    'never touches anything it did not deploy' true; it costs one extra copy of
    a directory holding ULTHAR's own files plus whatever else lives there. It
    also runs BEFORE the swap, so an
    unreadable foreign file raises here and aborts the deploy with the previous
    tree still fully intact -- the atomicity property is unchanged."""
    collisions = link_collisions(deploy_dir)
    if collisions:
        raise DeployRefused(
            "REFUSING TO DEPLOY: a link occupies a path ULTHAR owns inside "
            f"{deploy_dir}.\n"
            + "".join(f"  {line}\n" for line in collisions)
            + "Deploy overlays its files onto what is already there, so writing these\n"
            "paths would follow the link and overwrite a file OUTSIDE the deploy\n"
            "directory. install.py does not delete a link it did not deploy, so\n"
            "NOTHING has been written -- every byte on disk is as it was.\n"
            "Remove or move the link(s) above, then run install.py again."
        )
    deploy_dir.mkdir(parents=True, exist_ok=True)
    (deploy_dir / "logs").mkdir(parents=True, exist_ok=True)
    preserved: list[str] = []
    for kind, src_rel, dst_rel in COPY_TABLE:
        src = SKILL_ROOT / src_rel
        dst = deploy_dir / dst_rel
        if kind == "file":
            tmp = dst.with_name(dst.name + ".tmp-deploy")
            _clear_staging(tmp)  # never copy2 THROUGH a link left at a staging name
            shutil.copy2(src, tmp)
            os.replace(tmp, dst)  # atomic rename-over on both platforms for a file
        else:
            preserved += [str(Path(dst_rel) / rel) for rel in foreign_files(src, dst)]
            tmp = dst.with_name(dst.name + ".tmp-deploy")
            _clear_staging(tmp)
            # Seed with what is already deployed (symlinks kept as symlinks, so a
            # foreign symlink is not silently flattened into a copy), then overlay
            # ULTHAR's files. Exclude __pycache__/*.pyc: a regenerated .pyc has a
            # different mtime/hash than master, which would look like drift to
            # --verify's hash compare even though no source changed.
            if dst.exists():
                shutil.copytree(dst, tmp, symlinks=True, ignore=IGNORE_CACHE)
            shutil.copytree(src, tmp, dirs_exist_ok=True, ignore=IGNORE_CACHE)
            old_backup = dst.with_name(dst.name + ".old-deploy")
            moved_aside = False
            if dst.exists():
                _clear_staging(old_backup)
                os.replace(dst, old_backup)  # move OLD complete tree aside (fast rename)
                moved_aside = True
            try:
                os.replace(tmp, dst)         # move NEW complete tree into place (fast rename)
            except OSError as exc:
                # The two-rename window closed on failure. Put the previous tree
                # back rather than leaving the destination absent: a live
                # dispatcher with no lib/ fails closed and locks the operator out
                # of the very tooling they would use to fix it.
                if not moved_aside:
                    raise
                try:
                    os.replace(old_backup, dst)
                except OSError as restore_exc:
                    raise DeployRefused(
                        f"DEPLOY FAILED MID-SWAP: {dst} could not be replaced ({exc}) and the\n"
                        f"previous tree could not be put back ({restore_exc}).\n"
                        f"The COMPLETE previous {dst.name} is intact at:\n"
                        f"  {old_backup}\n"
                        f"Recover by renaming it back to {dst}. Nothing was deleted."
                    ) from exc
                # Precise about what the deploy dir now holds. COPY_TABLE's
                # `file` rows are done by the time a `tree` row swaps, so the
                # root files ALREADY carry the new version -- saying "as it was
                # before this run" would be false, and on a real upgrade it
                # would hide a new dispatch.py sitting over an old lib/.
                raise DeployRefused(
                    f"DEPLOY FAILED: {dst} could not be swapped in ({exc}).\n"
                    f"The previous {dst.name} has been put BACK, so the runtime still loads.\n"
                    f"The deploy is INCOMPLETE, not undone: the deploy dir's top-level files\n"
                    f"(config.json, dispatch.py, install.py, stop_dev_orphans.py) already\n"
                    f"carry the NEW version while {dst.name} is still the old one.\n"
                    f"Fix the cause (on Windows, usually a file in {dst.name} held open by a\n"
                    f"running process) and run install.py again to finish the deploy."
                ) from exc
            if old_backup.exists():
                shutil.rmtree(old_backup, ignore_errors=True)
    _write_deploy_origin(deploy_dir)
    return preserved


def _write_deploy_origin(deploy_dir: Path) -> None:
    """Records WHICH clone this deploy came from (lib.paths.DEPLOY_ORIGIN_FILE).

    The deployed runtime is location-blind: it sits in ~/.claude/hooks and cannot
    otherwise know whether its master is ~/.claude/skills/ulthar or
    C:/work/ULTHAR. Every "edit the master and redeploy" message, and
    skill_anchor's ability to find ANCHOR.md at all for a clone outside
    skills_root, need exactly this one fact -- and deploy time is the only moment
    it is known. Written last and atomically (same tmp+os.replace discipline as
    the copy set), so it can never name a master a half-finished deploy did not
    actually come from."""
    target = deploy_dir / paths_mod.DEPLOY_ORIGIN_FILE
    tmp = target.with_name(target.name + ".tmp-deploy")
    tmp.write_text(str(SKILL_ROOT) + "\n", encoding="utf-8", newline="\n")
    os.replace(tmp, target)


def cmd_default(args: argparse.Namespace) -> int:
    deploy_dir = Path(args.deploy_dir) if args.deploy_dir else _default_deploy_dir()
    cfg_path = SKILL_ROOT / "config.json"

    problems = validate_config(cfg_path)
    if problems:
        print("REFUSING TO DEPLOY: config.json is invalid.")
        for p in problems:
            print(f"  - {p}")
        return 1

    with open(cfg_path, "r", encoding="utf-8-sig") as fh:  # tolerate a UTF-8 BOM, which some editors add
        cfg = json.load(fh)
    try:
        cfg = _merge_user_overlay(cfg, deploy_dir)
    except ctx_mod.ConfigError as exc:
        print(f"REFUSING TO DEPLOY: user overlay is invalid: {exc}")
        return 1
    if cfg.get("dependencies", {}).get("check_on_deploy"):
        dep_problems = check_dependencies(cfg)
        if dep_problems:
            print("REFUSING TO DEPLOY: required dependency check failed.")
            for p in dep_problems:
                print(f"  - {p}")
            return 1

    try:
        preserved = copy_deploy(deploy_dir)
    except DeployRefused as exc:
        print(str(exc))
        return 1
    print(f"Deployed to {deploy_dir}")
    print(f"Recorded master: {deploy_dir / paths_mod.DEPLOY_ORIGIN_FILE} -> {SKILL_ROOT}")
    for line in anchor_status_lines(cfg, deploy_dir):
        print(line)
    # A destructive-looking operation that succeeds silently teaches the operator
    # nothing about what it left behind. Name every file found in a directory
    # ULTHAR manages that ULTHAR did not deploy.
    for rel in preserved:
        print(f"PRESERVED: {rel} is not in ULTHAR's current copy set -- left untouched")

    # These checks used to be --verify-only. A default deploy that installs a
    # broken runtime and says nothing until --verify is run separately is not
    # the loud failure I-13 asks for; run them here too and refuse to report
    # success if either fails. This is a post-deploy check, not a guard
    # against deploying: the files are already on disk, and refusing to
    # deploy would leave the PREVIOUS deploy silently stale instead.
    import_problems = _check_guards_importable(deploy_dir, cfg)
    for p in import_problems:
        print(f"GUARD IMPORT: {p}")
    smoke_ok = _smoke_check(deploy_dir, cfg)
    print("SMOKE: cd-compound via --guard tool_discipline -> " + ("PASS" if smoke_ok else "FAIL"))
    if import_problems or not smoke_ok:
        print("DEPLOYED, BUT POST-DEPLOY CHECKS FAILED -- run install.py --verify for detail.")
        return 1
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    deploy_dir = Path(args.deploy_dir) if args.deploy_dir else _default_deploy_dir()
    ok = True

    # 1. hash compare master vs deploy, exactly the M-16 copy set.
    for kind, src_rel, dst_rel in COPY_TABLE:
        src = SKILL_ROOT / src_rel
        dst = deploy_dir / dst_rel
        if kind == "file":
            if not dst.exists():
                print(f"DRIFT: missing deployed file {dst}")
                ok = False
                continue
            if _sha256(src) != _sha256(dst):
                print(f"DRIFT: {dst} does not match master {src}")
                ok = False
        else:
            src_files = {p.relative_to(src): p for p in _iter_tree_files(src)}
            dst_files = {p.relative_to(dst): p for p in _iter_tree_files(dst)} if dst.exists() else {}
            for rel, sp in src_files.items():
                dp = dst_files.get(rel)
                if dp is None or _sha256(sp) != _sha256(dp):
                    print(f"DRIFT: {dst / rel} does not match master {sp}")
                    ok = False
            # Reported, not failed. Deploy no longer wipes a managed directory,
            # so an extra file here is either someone else's file (which ULTHAR
            # must leave alone) or an inert leftover from an older copy set --
            # and nothing on disk can tell the two apart. Failing would make
            # --verify permanently red for any user who keeps their own helper
            # in lib/. Missing and mismatched files still FAIL, which is what
            # "is the deployed runtime the master runtime" actually rests on.
            # On the happy path (no extras) this branch never fires, so
            # --verify's output and exit code are unchanged.
            for rel in sorted(dst_files.keys() - src_files.keys()):
                print(f"EXTRA: deployed {dst / rel} has no master counterpart "
                      f"-- not in ULTHAR's current copy set, left in place")

    # 2. dependency check (I-13.1), loud.
    with open(SKILL_ROOT / "config.json", "r", encoding="utf-8-sig") as fh:  # tolerate a UTF-8 BOM, which some editors add
        cfg = json.load(fh)
    try:
        cfg = _merge_user_overlay(cfg, deploy_dir)
    except ctx_mod.ConfigError as exc:
        print(f"OVERLAY: user overlay is invalid: {exc}")
        print("VERIFY: FAIL")
        return 1
    dep_problems = check_dependencies(cfg)
    for p in dep_problems:
        print(f"DEPENDENCY: {p}")
    ok = ok and not dep_problems

    # 3. smoke check: the cd-compound fixture through --guard tool_discipline (row 6).
    smoke_ok = _smoke_check(deploy_dir, cfg)
    print("SMOKE: cd-compound via --guard tool_discipline -> " + ("PASS" if smoke_ok else "FAIL"))
    ok = ok and smoke_ok

    # 3b. Every guard id named in dispatch.events must actually be importable
    # from the deployed guards/ tree. An unimportable guard is a deployment
    # defect that dispatch.py now BLOCKS at runtime -- but that must never be
    # the first time anyone notices, so --verify fails loudly here rather
    # than relying on the cd-compound smoke case to happen to exercise it.
    import_problems = _check_guards_importable(deploy_dir, cfg)
    for p in import_problems:
        print(f"GUARD IMPORT: {p}")
    ok = ok and not import_problems

    # 4. settings.json registration check (F-5). Absence of settings.json is
    # a hard FAIL, not a silent skip -- I-10's sequence (backup -> parity
    # green -> rewrite) is unverifiable if this step can quietly report
    # nothing.
    settings_path = Path(os.environ.get("USERPROFILE") or Path.home()) / ".claude" / "settings.json"
    if not settings_path.exists():
        print("SETTINGS: settings.json not found -- cannot verify registration")
        ok = False
    else:
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            print(f"SETTINGS: settings.json does not parse: {exc}")
            settings = None
            ok = False
        if settings is not None:
            reg_problems = _check_dispatcher_registration(settings, cfg, deploy_dir)
            for p in reg_problems:
                print(f"SETTINGS: {p}")
            ok = ok and not reg_problems

    # 5. deploy record (Findings A/B). Without it the deployed runtime cannot name
    # its own master, so every infra recovery message degrades to "unknown" and
    # skill_anchor loses its fallback -- a real deployment defect, hence a FAIL,
    # not a note. A record naming a DIFFERENT root means this deploy came from
    # another clone: whatever this --verify just hash-compared is not what is
    # actually installed from.
    recorded = paths_mod.deployed_from(deploy_dir)
    if recorded is None:
        print(f"ORIGIN: {deploy_dir / paths_mod.DEPLOY_ORIGIN_FILE} is missing -- this deploy "
              f"does not record which clone it came from. Redeploy from your clone "
              f"(python \"{SKILL_ROOT / 'hooks' / 'install.py'}\") to write it.")
        ok = False
    elif not _same_path(str(recorded), str(SKILL_ROOT)):
        print(f"ORIGIN: {deploy_dir} records master {recorded}, but this is {SKILL_ROOT} -- "
              f"the deployed runtime was installed from a DIFFERENT clone. Verify or "
              f"redeploy from that one, or redeploy from this one to take it over.")
        ok = False

    # 6. anchor reachability (Finding A), informational -- never changes `ok`.
    for line in anchor_status_lines(cfg, deploy_dir):
        print(line)

    print("VERIFY: " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


DISPATCH_TOKENS = ("dispatch.ps1", "dispatch.py")


def _count_dispatcher_registrations(settings: dict) -> dict[str, dict[str, int]]:
    """{event: {"dispatch.ps1": n, "dispatch.py": n}} across every rule/hook entry."""
    out: dict[str, dict[str, int]] = {}
    for event, rules in settings.get("hooks", {}).items():
        counts = {tok: 0 for tok in DISPATCH_TOKENS}
        for rule in rules if isinstance(rules, list) else []:
            for h in rule.get("hooks", []) if isinstance(rule, dict) else []:
                cmd = str(h.get("command", "")) if isinstance(h, dict) else ""
                for tok in DISPATCH_TOKENS:
                    if tok in cmd:
                        counts[tok] += 1
        out[event] = counts
    return out


_REGISTERED_CMD_RE = re.compile(r'^"([^"]+)"\s+"([^"]+)"\s+--event\s+(\S+)\s*$')


def _check_dispatcher_registration(settings: dict, cfg: dict, deploy_dir: Path) -> list[str]:
    """Asserts the complete expected set, driven by `cfg["dispatch"]["events"]`
    (the routing table's own declared events, not a second hardcoded list
    that could drift from it): for every expected event, exactly one
    dispatcher registration must exist. Checking only events already present
    in settings.json, or substring-matching `dispatch.py` without validating
    the interpreter, the deployed path, and the `--event` argument, would
    report the cutover as fine even when an event has NO hook at all.

    If a registration is `.py`, its command must match the exact shape
    `cmd_register_settings` writes -- `"<interpreter>" "<deployed
    dispatch.py>" --event <this event>` -- and the deployed dispatch.py path
    must resolve (the file must actually exist, not just be named correctly).

    The interpreter check has TWO modes, because with `paths.python_exe` empty
    there is no pin to compare against -- calling sys.executable-at-verify-time
    "the PINNED interpreter" and failing on any spelling difference (venv vs.
    system python, forward vs. back slashes, drive-letter case) would be a
    false positive, not a real drift:
      - PINNED (`paths.python_exe` configured, directly or via the user
        overlay): registered interpreter must match the pin, compared through
        lib.paths.norm_for_compare (separator- and case-tolerant -- a raw `!=`
        would wrongly flag 'C:/x/python.exe' against 'C:\\x\\python.exe').
      - UNPINNED (empty): nothing is pinned, so identity cannot be asserted;
        only that the REGISTERED interpreter still exists on disk and is
        runnable is checked."""
    problems: list[str] = []
    expected_events = sorted(cfg.get("dispatch", {}).get("events", {}).keys())
    if not expected_events:
        problems.append("cfg['dispatch']['events'] is empty -- cannot determine which "
                         "events settings.json is expected to register")
        return problems

    ctx = ctx_mod.build_ctx({}, cfg, "install", deploy_dir)
    configured_pin = cfg.get("paths", {}).get("python_exe") or ""
    python_exe = paths_mod.resolve_python_exe(cfg, ctx)
    dispatch_py = deploy_dir / "dispatch.py"
    hooks_by_event = settings.get("hooks", {})

    for event in expected_events:
        rules = hooks_by_event.get(event, [])
        matching_cmds = []
        for rule in rules if isinstance(rules, list) else []:
            for h in rule.get("hooks", []) if isinstance(rule, dict) else []:
                if not isinstance(h, dict):
                    continue
                cmd = str(h.get("command", ""))
                if any(tok in cmd for tok in DISPATCH_TOKENS):
                    matching_cmds.append(cmd)

        if not matching_cmds:
            # A hook that never fires is indistinguishable from a working
            # one until something bad happens -- this is this check's
            # primary concern, not something it silently skips.
            problems.append(f"{event}: NO dispatcher registered at all -- this event's "
                             f"hooks will never fire (checked settings.json[\"hooks\"][{event!r}])")
            continue
        if len(matching_cmds) > 1:
            n_ps1 = sum(1 for c in matching_cmds if "dispatch.ps1" in c)
            n_py = sum(1 for c in matching_cmds if "dispatch.py" in c)
            problems.append(
                f"{event}: {len(matching_cmds)} dispatcher registrations found "
                f"(ps1={n_ps1}, py={n_py}) -- expected exactly 1; every tool call "
                f"would run the dispatcher {len(matching_cmds)} times"
            )
            continue

        cmd = matching_cmds[0]
        if "dispatch.ps1" in cmd:
            # Pre-cutover state. --verify answers "is cutover done", so this
            # is reported as FAIL, not folded into "no problem".
            problems.append(f"{event}: NOT YET CUT OVER -- dispatch.ps1 registered, "
                             f"dispatch.py absent")
            continue

        # It is a dispatch.py entry -- validate the COMPLETE expected shape
        # rather than trusting the substring match.
        m = _REGISTERED_CMD_RE.match(cmd)
        if not m:
            problems.append(f"{event}: registered command does not match the expected "
                             f'shape \'"<python_exe>" "<dispatch.py>" --event <Event>\': {cmd!r}')
            continue
        cmd_python_exe, cmd_dispatch_py, cmd_event = m.group(1), m.group(2), m.group(3)
        if configured_pin:
            if paths_mod.norm_for_compare(cmd_python_exe, ctx) != paths_mod.norm_for_compare(python_exe, ctx):
                problems.append(f"{event}: registered interpreter {cmd_python_exe!r} does not "
                                 f"match the PINNED interpreter {python_exe!r}")
        else:
            exe_path = Path(cmd_python_exe)
            if not (exe_path.is_file() and os.access(cmd_python_exe, os.X_OK)):
                problems.append(f"{event}: registered interpreter {cmd_python_exe!r} does not "
                                 f"exist / is not executable (no pin configured -- "
                                 f"paths.python_exe is empty, so only existence is checked, "
                                 f"not identity against the currently-running interpreter)")
        if Path(cmd_dispatch_py) != dispatch_py:
            problems.append(f"{event}: registered dispatch.py path {cmd_dispatch_py!r} "
                             f"does not match the deployed path {str(dispatch_py)!r}")
        elif not Path(cmd_dispatch_py).exists():
            problems.append(f"{event}: registered dispatch.py path {cmd_dispatch_py!r} "
                             f"does not exist on disk -- a partial or reverted deploy")
        if cmd_event != event:
            problems.append(f"{event}: registered --event argument {cmd_event!r} does not "
                             f"match the settings.json key it is registered under ({event!r})")

    return problems


def _unique_backup_path(settings_path: Path) -> Path:
    """A `.bak-<timestamp>` scheme alone can collide and overwrite an earlier
    backup within the same second. Appends a numeric suffix (`-1`, `-2`, ...)
    until the path does not already exist -- unique regardless of clock
    resolution."""
    import datetime as _dt
    base = settings_path.with_suffix(
        settings_path.suffix + f".bak-{_dt.datetime.now().strftime('%Y%m%dT%H%M%S')}")
    if not base.exists():
        return base
    n = 1
    while True:
        candidate = base.with_name(base.name + f"-{n}")
        if not candidate.exists():
            return candidate
        n += 1


def cmd_register_settings(args: argparse.Namespace) -> int:
    """I-10's rewrite step: backup -> rewrite every registered dispatch.ps1
    command line to dispatch.py, using the pinned python_exe. Never runs
    automatically and never invoked by this unit against the real settings.json
    until parity passes and U-6 runs it -- provided so U-6 has a tested
    implementation rather than a hand-edit. Requires --confirm-parity-passed as
    an explicit, unmissable acknowledgement gate.

    LEGACY EVENTS. The pre-cutover settings.json this helper exists to migrate
    has FOUR dispatch.ps1 registrations -- PreToolUse, Stop, SessionStart and a
    PostCompact one that PE-20 removed from `dispatch.events` but deliberately
    LEFT in settings.json (an owner-gated file; deleting the now-dead entry is a
    separate proposal for the owner, not this helper's call). So the count check
    is per-EXPECTED-EVENT, not a total: every event in `dispatch.events` must be
    rewritten exactly once, and any FURTHER dispatch.ps1 registration is
    rewritten too and reported. It is rewritten rather than dropped because
    dispatch.py resolves an event with no config route to zero rules and allows
    with empty stdout -- a harmless no-op -- whereas deleting it would edit an
    owner-gated file beyond what was asked. A total-equality check refused
    exactly the legacy state this helper was written for.

    Transactional and self-verifying: (1) validates the pre-state is exactly
    the expected 'every event .ps1-only' shape before touching anything; (2)
    requires each expected event to be rewritten exactly once, so a mixed or
    partial pre-state is refused rather than silently substring-replaced; (3)
    writes to a temp file beside settings.json and
    `os.replace`s it in (atomic on both platforms, same volume), so an
    interruption mid-write cannot leave settings.json empty; (4) re-reads the
    written file and re-verifies it with the same strict check --verify
    uses; (5) on verification failure, automatically restores the backup and
    reports the failure -- never leaves a half-correct settings.json in
    place."""
    if not args.confirm_parity_passed:
        print("REFUSING: --register-settings requires --confirm-parity-passed "
              "(I-10: backup -> parity green -> rewrite, in that order).")
        return 1
    settings_path = Path(args.settings_path) if args.settings_path else (
        Path(os.environ.get("USERPROFILE") or Path.home()) / ".claude" / "settings.json")
    if not settings_path.exists():
        print(f"REFUSING: {settings_path} does not exist")
        return 1

    with open(settings_path, "r", encoding="utf-8-sig") as fh:
        raw_text = fh.read()
    try:
        settings = json.loads(raw_text)
    except Exception as exc:
        print(f"REFUSING: {settings_path} does not parse as JSON: {exc}")
        return 1

    with open(SKILL_ROOT / "config.json", "r", encoding="utf-8-sig") as fh:
        cfg = json.load(fh)
    deploy_dir = Path(args.deploy_dir) if args.deploy_dir else _default_deploy_dir()
    try:
        cfg = _merge_user_overlay(cfg, deploy_dir)
    except ctx_mod.ConfigError as exc:
        print(f"REFUSING: user overlay is invalid: {exc}")
        return 1
    python_exe = paths_mod.resolve_python_exe(cfg)
    dispatch_py = deploy_dir / "dispatch.py"
    expected_events = sorted(cfg.get("dispatch", {}).get("events", {}).keys())
    if not expected_events:
        print("REFUSING: cfg['dispatch']['events'] is empty -- nothing to register")
        return 1

    # Pre-state validation: every expected event must be exactly ONE
    # dispatch.ps1 registration, nothing else. A mixed, double, or missing
    # registration is refused rather than silently substring-replaced.
    pre_problems: list[str] = []
    hooks_by_event = settings.get("hooks", {})
    for event in expected_events:
        rules = hooks_by_event.get(event, [])
        matching = []
        for rule in rules if isinstance(rules, list) else []:
            for h in rule.get("hooks", []) if isinstance(rule, dict) else []:
                if isinstance(h, dict) and any(tok in str(h.get("command", "")) for tok in DISPATCH_TOKENS):
                    matching.append(str(h.get("command", "")))
        if len(matching) != 1 or "dispatch.ps1" not in matching[0]:
            pre_problems.append(f"{event}: expected exactly one dispatch.ps1 registration "
                                 f"pre-rewrite, found {matching!r}")
    if pre_problems:
        print("REFUSING: settings.json is not in the expected pre-cutover state:")
        for p in pre_problems:
            print(f"  - {p}")
        return 1

    backup_path = _unique_backup_path(settings_path)
    backup_path.write_text(raw_text, encoding="utf-8", newline="\n")
    print(f"Backed up {settings_path} -> {backup_path}")

    changed = 0
    changed_by_event: dict[str, int] = {}
    for event, rules in hooks_by_event.items():
        for rule in rules if isinstance(rules, list) else []:
            for h in rule.get("hooks", []) if isinstance(rule, dict) else []:
                if not isinstance(h, dict):
                    continue
                cmd = str(h.get("command", ""))
                if "dispatch.ps1" in cmd:
                    # I-7: a third-party hook entry this installer did not deploy is
                    # NEVER touched -- it does not contain "dispatch.ps1" so this
                    # substring check cannot reach it.
                    h["command"] = f'"{python_exe}" "{dispatch_py}" --event {event}'
                    changed += 1
                    changed_by_event[event] = changed_by_event.get(event, 0) + 1

    wrong = {e: changed_by_event.get(e, 0) for e in expected_events
             if changed_by_event.get(e, 0) != 1}
    if wrong:
        print(f"REFUSING: every event in dispatch.events must be rewritten exactly "
              f"once; got {wrong} -- no write performed, settings.json is UNCHANGED "
              f"(backup at {backup_path} is harmless to keep)")
        return 1
    legacy = sorted(e for e in changed_by_event if e not in expected_events)

    tmp_path = settings_path.with_name(settings_path.name + ".tmp-register")
    new_text = json.dumps(settings, indent=2, ensure_ascii=False) + "\n"
    tmp_path.write_text(new_text, encoding="utf-8", newline="\n")
    os.replace(tmp_path, settings_path)  # atomic rename-over, same volume
    print(f"Rewrote {changed} dispatch.ps1 -> dispatch.py registration(s) in {settings_path}")
    for event in legacy:
        print(f"LEGACY EVENT: {event} is registered in settings.json but has no "
              f"dispatch.events route -- rewritten to dispatch.py, where it resolves "
              f"to zero rules and allows with no output. Removing the entry is yours "
              f"to decide; this installer does not delete from settings.json.")

    # Post-write self-verification: re-read what actually landed on disk and
    # re-run the SAME strict check --verify uses. Roll back automatically on
    # any discrepancy rather than leaving a half-correct file in place.
    try:
        written = json.loads(settings_path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        print(f"VERIFICATION FAILED: rewritten {settings_path} does not parse ({exc}) "
              f"-- rolling back to {backup_path}")
        os.replace(backup_path, settings_path)
        return 1
    post_problems = _check_dispatcher_registration(written, cfg, deploy_dir)
    if post_problems:
        print("VERIFICATION FAILED: rewritten settings.json does not pass the same "
              f"check --verify uses -- rolling back to {backup_path}:")
        for p in post_problems:
            print(f"  - {p}")
        os.replace(backup_path, settings_path)
        return 1
    print("VERIFIED: rewritten settings.json passes the full dispatcher-registration check.")
    return 0


def _check_guards_importable(deploy_dir: Path, cfg: dict) -> list[str]:
    """Every guard id in dispatch.events must import cleanly and expose a callable
    `run`, from the DEPLOYED tree, in a fresh interpreter (so a stale sys.modules
    entry in this process can never mask a real import failure). Mirrors
    dispatch.py's own GuardLoadError classification without importing dispatch.py
    itself, so a bug in dispatch.py's error handling cannot hide a bug here."""
    ids: set[str] = set()
    for rules in cfg.get("dispatch", {}).get("events", {}).values():
        for rule in rules:
            ids |= set(rule.get("guards", []))

    python_exe = paths_mod.resolve_python_exe(cfg)
    problems: list[str] = []
    for guard_id in sorted(ids):
        code = (
            f"import sys; sys.path.insert(0, {str(deploy_dir)!r}); "
            f"import guards.{guard_id} as m; "
            f"assert callable(getattr(m, 'run', None)), 'no callable run'"
        )
        proc = subprocess.run([python_exe, "-c", code], capture_output=True, timeout=15)
        if proc.returncode != 0:
            problems.append(f"guards.{guard_id} failed to import: "
                             f"{proc.stderr.decode('utf-8', 'replace').strip().splitlines()[-1:]}")
    return problems


def _smoke_check(deploy_dir: Path, cfg: dict) -> bool:
    """Chosen fixture: cd-compound -- needs no setup, is cwd-independent, has no
    external dependency (CONTRACT.md #5).

    exit 2 also means "guard failed to import" -- assert the rule's message
    too, or a guard too broken to load looks like a working one."""
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "cd /tmp && rm -rf foo"},
    }
    python_exe = paths_mod.resolve_python_exe(cfg)
    dispatch_py = deploy_dir / "dispatch.py"
    if not dispatch_py.exists():
        return False
    try:
        proc = subprocess.run(
            [python_exe, str(dispatch_py), "--event", "PreToolUse", "--guard", "tool_discipline"],
            input=json.dumps(payload).encode("utf-8"),
            capture_output=True,
            timeout=15,
        )
    except Exception:
        return False
    if proc.returncode != 2:
        return False
    expected_fragment = "compound command after 'cd' is not allowed"
    return expected_fragment in proc.stderr.decode("utf-8", "replace")


def cmd_uninstall(args: argparse.Namespace) -> int:
    """Removes exactly the copied set, file by file. Never touches a third-party
    hook or the retained *.ps1 rollback set (I-7) -- neither is on COPY_TABLE.

    A `tree` row removes only the files that row ships (plus `__pycache__`, which
    is regenerable cache the running dispatcher wrote, not data), then prunes the
    directories it emptied. rmtree'ing the whole directory would take any file a
    user or another tool had put there with it."""
    deploy_dir = Path(args.deploy_dir) if args.deploy_dir else _default_deploy_dir()
    kept: list[str] = []
    for kind, src_rel, dst_rel in COPY_TABLE:
        src = SKILL_ROOT / src_rel
        dst = deploy_dir / dst_rel
        if not dst.exists():
            continue
        if kind == "file":
            dst.unlink()
            continue
        kept += [str(Path(dst_rel) / rel) for rel in foreign_files(src, dst)]
        for rel in ulthar_tree_rel_paths(src):
            target = dst / rel
            if target.is_file():
                target.unlink()
        for cache in dst.rglob("__pycache__"):
            shutil.rmtree(cache, ignore_errors=True)
        # Bottom-up so a nested directory is emptied before its parent is tried.
        for d in sorted(dst.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
        if not any(dst.iterdir()):
            dst.rmdir()
    # This installer wrote it, so this installer removes it -- it is not on
    # COPY_TABLE (it is generated, not copied), so the loop above cannot see it.
    origin = deploy_dir / paths_mod.DEPLOY_ORIGIN_FILE
    if origin.is_file():
        origin.unlink()
    print(f"Uninstalled from {deploy_dir} (copy set only)")
    for rel in kept:
        print(f"KEPT: {rel} is not in ULTHAR's current copy set -- left in place")
    return 0


def _same_path(a: str, b: str) -> bool:
    """Lexical, case-tolerant on Windows -- the same policy as
    lib.paths.norm_for_compare, spelled locally because this check runs before
    any Ctx exists."""
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def deployed_copy_refusal() -> str | None:
    """A message when THIS install.py is the deployed copy rather than a master,
    else None.

    The deployed installer derives its skill root from its own location, so at
    ~/.claude/hooks/install.py that root is ~/.claude: it would read
    ~/.claude/config.json as ULTHAR's config, copy the deployed files over
    themselves, and print "Deployed" -- or, worse, overwrite the deployed
    config.json with an unrelated one. Every mode is wrong the same way, so this
    is checked in main() and not in cmd_default: --verify would hash-compare the
    wrong tree and --uninstall would delete against the wrong copy set.

    The signal is the deploy record ULTHAR itself wrote next to this file: if
    there is one and it names a DIFFERENT root, this file is a deployed copy of
    that root's installer. A master's own hooks/ never holds it (it is written
    into the deploy dir), so `python hooks/install.py --deploy-dir <anywhere>`
    from a clone is unaffected."""
    recorded = paths_mod.deployed_from(HERE)
    if recorded is None or _same_path(str(recorded), str(SKILL_ROOT)):
        return None
    return (
        f"REFUSING: {Path(__file__)} is the DEPLOYED copy of install.py, not a master.\n"
        f"  It cannot deploy or verify anything: it would treat {SKILL_ROOT} as the "
        f"skill root and read {SKILL_ROOT / 'config.json'}, which is not ULTHAR's config.\n"
        f"  This deploy was installed from:\n"
        f"    {recorded}\n"
        f"  Run that clone's installer instead:\n"
        f'    python "{Path(recorded) / "hooks" / "install.py"}"'
    )


def _first_existing_marker(cfg: dict, ctx) -> Path | None:
    for template in cfg.get("skill_anchor", {}).get("list_lookup_order", []):
        candidate = ctx.expand(template)
        if candidate.exists():
            return candidate
    return None


def anchor_status_lines(cfg: dict, deploy_dir: Path) -> list[str]:
    """The loud half of Finding A. skill_anchor may never block and writes only to
    a channel the model reads, so "marker set, no anchor, no explanation" can only
    be made non-silent HERE -- printed on every deploy and every --verify, whether
    or not the anchor is armed. Informational: it never changes an exit code,
    because arming the anchor is the user's choice, not a deployment defect.

    Every filesystem touch is inside one try: this runs AFTER the copy set is on
    disk, so an unreadable marker or a locked directory must degrade to one line
    saying so, never a raw traceback out of an otherwise successful deploy."""
    try:
        return _anchor_status_lines(cfg, deploy_dir)
    except Exception as exc:  # noqa: BLE001 -- informational output cannot fail a deploy
        return [f"ANCHOR: could not determine anchor state ({type(exc).__name__}: {exc})"]


def _anchor_status_lines(cfg: dict, deploy_dir: Path) -> list[str]:
    acfg = cfg.get("skill_anchor", {})
    name = acfg.get("self_skill_name") or ""
    anchor_file = acfg.get("anchor_file_name", "ANCHOR.md")
    if not name:
        return []
    ctx = ctx_mod.build_ctx({}, cfg, "install", deploy_dir)
    lines: list[str] = []

    installed = ctx.skills_root / name / anchor_file
    master_anchor = SKILL_ROOT / anchor_file
    if installed.exists():
        source = installed
    elif master_anchor.exists():
        source = master_anchor
    else:
        return [f"ANCHOR: NO {anchor_file} found -- neither {installed} nor {master_anchor} "
                f"exists, so the '{name}' anchor can never be served. This clone is "
                f"incomplete; re-clone or restore {anchor_file}."]
    lines.append(f"ANCHOR: '{name}' anchor resolves to {source}")

    marker = _first_existing_marker(cfg, ctx)
    order = ", ".join(str(ctx.expand(t)) for t in acfg.get("list_lookup_order", []))
    if marker is None:
        lines.append(f"ANCHOR: NOT ARMED -- no active-skills marker exists (looked in: {order}). "
                     f"Create one containing the single line '{name}' to arm it.")
        return lines
    prefix = acfg.get("comment_prefix", "#")
    entries = [ln.strip() for ln in marker.read_text(encoding="utf-8-sig").splitlines()
               if ln.strip() and not ln.strip().startswith(prefix)]
    if any(e.casefold() == name.casefold() for e in entries):
        lines.append(f"ANCHOR: ARMED -- {marker} lists '{name}'")
    else:
        lines.append(f"ANCHOR: NOT ARMED -- {marker} exists but does not list '{name}' "
                     f"(entries: {entries or 'none'}). Add a line '{name}' to arm it.")
    return lines


def main(argv: list[str]) -> int:
    refusal = deployed_copy_refusal()
    if refusal:
        print(refusal)
        return 1
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--register-settings", action="store_true",
                         help="I-10 rewrite step. Requires --confirm-parity-passed.")
    parser.add_argument("--confirm-parity-passed", action="store_true")
    parser.add_argument("--settings-path", default=None, help="test-only override")
    parser.add_argument("--deploy-dir", default=None, help="test-only override")
    args = parser.parse_args(argv[1:])

    if args.register_settings:
        return cmd_register_settings(args)
    if args.verify:
        return cmd_verify(args)
    if args.uninstall:
        return cmd_uninstall(args)
    return cmd_default(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
