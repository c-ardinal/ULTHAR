"""Fail-closed regressions from review round 5:
  * no cross-process config cache: a planted cache file beside the package changes
    nothing, config.json is parsed and validated on every call;
  * an ImportError raised while a guard runs (lazily imported subprocess/signal in
    ctx.run_cmd and the process-tree kill) blocks a PreToolUse and is recorded --
    and warns + records on other events -- instead of failing open;
  * blob_hash records hash_failed when subprocess cannot be imported.
"""
from __future__ import annotations

import copy
import json
import marshal
import os
import sys
import zlib
from pathlib import Path

from helpers import V2_ROOT, Probe, Sandbox

from ulthar import hook

PACKAGE = V2_ROOT / "ulthar"
CONFIG = PACKAGE / "config.json"

STUB_ALLOW = "from ulthar.lib.io import ALLOW\n\ndef run(payload, cfg, ctx):\n    return ALLOW\n"
STUB_RUN_CMD = ("from ulthar.lib.io import ALLOW\n\ndef run(payload, cfg, ctx):\n"
                "    ctx.run_cmd(['git', '--version'], 'local')\n    return ALLOW\n")
STUB_KILL = ("from ulthar.lib.io import ALLOW\n\nclass _Proc:\n    pid = 999999\n"
             "    def kill(self):\n        pass\n\ndef run(payload, cfg, ctx):\n"
             "    ctx._kill_process_tree(_Proc())\n    return ALLOW\n")
BASH = {"session_id": "fc", "tool_name": "Bash", "tool_input": {"command": "ls"}}


class _Env:
    """In-process hook.process with a sandbox db, optional guard stubs and config, and
    one stdlib module made unimportable (sys.modules[name] = None)."""

    def __init__(self, sb: Sandbox, guard_src: str | None = None, block: str | None = None,
                 config: dict | None = None):
        self.sb, self.block = sb, block
        self.env = {"ULTHAR_RUNTIME_DB": str(sb.db), "HOME": str(sb.home),
                    "USERPROFILE": str(sb.home)}
        if guard_src is not None:
            stubs = sb.root / "guards"
            stubs.mkdir(exist_ok=True)
            (stubs / "tool_discipline.py").write_text(guard_src, encoding="utf-8")
            (stubs / "git_discipline.py").write_text(STUB_ALLOW, encoding="utf-8")
            self.env["ULTHAR_GUARDS_DIR"] = str(stubs)
        if config is not None:
            path = sb.root / "config.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            self.env["ULTHAR_CONFIG"] = str(path)

    def __enter__(self):
        self.saved_env = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        self.saved_mod = sys.modules.get(self.block, False) if self.block else False
        if self.block:
            sys.modules[self.block] = None
        return self

    def __exit__(self, *exc):
        if self.block:
            if self.saved_mod is False:
                sys.modules.pop(self.block, None)
            else:
                sys.modules[self.block] = self.saved_mod
        for k, v in self.saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _import_error(text: str) -> bool:
    return "ImportError" in text or "ModuleNotFoundError" in text


def _last_event(sb: Sandbox, session: str) -> tuple:
    rows = sb.query("SELECT decision, guard_id, detail, post_hashes FROM events "
                    "WHERE session_id=? ORDER BY id", (session,))
    return rows[-1] if rows else (None, None, "{}", None)


def check_planted_config_cache_has_no_effect(p: Probe) -> None:
    sb = Sandbox()
    planted = []
    try:
        weak = json.loads(CONFIG.read_text(encoding="utf-8"))
        weak["dispatch"]["events"] = {}  # a policy with no guards at all
        st = os.stat(CONFIG)
        cache_dir = PACKAGE / "__pycache__"
        cache_dir.mkdir(exist_ok=True)
        for spelling in {str(CONFIG), str(CONFIG.resolve())}:
            key = (spelling, st.st_mtime_ns, st.st_size)
            f = cache_dir / f"config-{zlib.crc32(spelling.encode('utf-8')):08x}.marshal"
            f.write_bytes(marshal.dumps((key, weak)))
            planted.append(f)
        proc = sb.hook("PreToolUse", {"session_id": "fc-cache", "tool_name": "Bash",
                                      "tool_input": {"command": "git push --force origin main"}})
        p.check("P0: a forged config cache beside the package does not weaken the policy "
                "(force-push still blocked by git_discipline)",
                proc.returncode == 2 and "git discipline" in proc.stderr,
                f"rc={proc.returncode} {proc.stderr[:200]}")
        sources = [f.relative_to(V2_ROOT) for f in PACKAGE.rglob("*.py")
                   if "marshal" in f.read_text(encoding="utf-8")]
        p.check("P0: no runtime module reads or writes a marshal config cache", not sources,
                str(sources))
    finally:
        for f in planted:
            f.unlink(missing_ok=True)
        sb.cleanup()


def check_run_cmd_import_failure_blocks(p: Probe) -> None:
    sb = Sandbox()
    try:
        with _Env(sb, STUB_RUN_CMD, block="subprocess"):
            out = hook.process("PreToolUse", dict(BASH, cwd=str(sb.work)))
        decision, guard_id, detail, _ = _last_event(sb, "fc")
        err = json.loads(detail).get("pipeline_error", {})
        p.check("P1: subprocess unimportable inside ctx.run_cmd -> PreToolUse exits 2",
                out.result.exit_code == 2 and "could not be imported" in out.result.stderr
                and "To recover" in out.result.stderr, out.result.stderr[:300])
        p.check("  ... recorded as deny / dispatcher-error with the ImportError",
                (decision, guard_id) == ("deny", "dispatcher-error")
                and _import_error(err.get("type", "")), f"{decision} {guard_id} {err}")

        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
        stop_cfg = copy.deepcopy(cfg)
        stop_cfg["dispatch"]["events"]["Stop"] = [{"tools": "*", "guards": ["tool_discipline"]}]
        with _Env(sb, STUB_RUN_CMD, block="subprocess", config=stop_cfg):
            stop = hook.process("Stop", {"session_id": "fc-stop", "cwd": str(sb.work),
                                         "stop_hook_active": False})
        decision, guard_id, detail, _ = _last_event(sb, "fc-stop")
        p.check("P1: the same failure on Stop warns (exit 0) and is recorded, not a silent allow",
                stop.result.exit_code == 0 and "WARNING" in stop.result.stderr
                and guard_id == "dispatcher-error"
                and _import_error(json.loads(detail).get("pipeline_error", {}).get("type", "")),
                f"rc={stop.result.exit_code} {guard_id}")
    finally:
        sb.cleanup()


def check_process_tree_kill_import_failure_blocks(p: Probe) -> None:
    sb = Sandbox()
    try:
        with _Env(sb, STUB_KILL, block="signal"):
            out = hook.process("PreToolUse", dict(BASH, session_id="fc-kill", cwd=str(sb.work)))
        decision, guard_id, detail, _ = _last_event(sb, "fc-kill")
        p.check("P1: signal unimportable in the process-tree kill -> PreToolUse exits 2, recorded",
                out.result.exit_code == 2 and (decision, guard_id) == ("deny", "dispatcher-error")
                and _import_error(json.loads(detail).get("pipeline_error", {}).get("type", "")),
                f"rc={out.result.exit_code} {decision} {guard_id}")
    finally:
        sb.cleanup()


def check_blob_hash_import_failure_marks_hash_failed(p: Probe) -> None:
    sb = Sandbox()
    try:
        target = sb.work / "w.txt"
        target.write_text("x\n", encoding="utf-8")
        with _Env(sb, block="subprocess"):
            direct = hook.blob_hash(str(target))
            out = hook.process("PostToolUse", {"session_id": "fc-hash", "cwd": str(sb.work),
                                               "tool_name": "Write", "tool_response": {},
                                               "tool_input": {"file_path": str(target)}})
        _, _, detail, post = _last_event(sb, "fc-hash")
        recorded = json.loads(post or "{}").get(str(target))
        p.check("P1: blob_hash with subprocess unimportable returns a hash_failed marker",
                isinstance(direct, dict) and _import_error(direct.get("hash_failed", "")),
                str(direct))
        p.check("  ... and PostToolUse completes (exit 0) recording hash_failed for the path",
                out.result.exit_code == 0 and isinstance(recorded, dict)
                and json.loads(detail).get("hash_failed") == [str(target)], str(recorded))
    finally:
        sb.cleanup()


def main() -> int:
    p = Probe()
    check_planted_config_cache_has_no_effect(p)
    check_run_cmd_import_failure_blocks(p)
    check_process_tree_kill_import_failure_blocks(p)
    check_blob_hash_import_failure_marks_hash_failed(p)
    return p.result()


if __name__ == "__main__":
    sys.exit(main())
