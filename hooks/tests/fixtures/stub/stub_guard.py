"""tests/fixtures/stub/stub_guard.py -- a STUB guard, for exercising dispatch.py
end-to-end before the real guards (U-1..U-5) land. NEVER placed under guards/.

Reads a deterministic instruction out of `payload["tool_input"]["command"]`:
  "ALLOW"                 -> Result(0)
  "BLOCK:<message>"       -> Result(2, "", "<message>")
  "BLOCK_NO_REASON"       -> Result(2, "", "")            (I-3b row 3 fixture)
  "RAISE"                 -> raises RuntimeError           (I-3 fail-open fixture)
  "BAD_TYPE"              -> returns None                  (I-3b row 1 fixture)
  "BAD_EXIT"              -> returns Result(1, "", "")     (I-3b row 2 fixture)
  "STDOUT_LEAK"           -> returns Result(0, "leak", "") (I-3b row 4 fixture)
  "CHECK_APPROVAL:<name>" -> ALLOW if ctx.state_dir/brief-approvals/<name> exists,
                             else BLOCK. Exists so parity.py's setup-builder self-test
                             can prove a class-B outcome truly depends on seeded state
                             (contract addendum) without needing the real brief_gate.py.
  anything else           -> Result(0)
"""
from __future__ import annotations


def run(payload: dict, cfg: dict, ctx):
    from lib.io import Result, ALLOW  # dispatch.py puts hooks/ on sys.path, so `lib` resolves

    cmd = str(payload.get("tool_input", {}).get("command", ""))
    if cmd == "BLOCK_NO_REASON":
        return Result(2, "", "")
    if cmd.startswith("BLOCK:"):
        return Result(2, "", cmd[len("BLOCK:"):])
    if cmd == "RAISE":
        raise RuntimeError("stub guard forced failure")
    if cmd == "BAD_TYPE":
        return None
    if cmd == "BAD_EXIT":
        return Result(1, "", "")
    if cmd == "STDOUT_LEAK":
        return Result(0, "leak", "")
    if cmd.startswith("CHECK_APPROVAL:"):
        name = cmd[len("CHECK_APPROVAL:"):]
        path = ctx.state_dir / "brief-approvals" / name
        if path.exists():
            return ALLOW
        return Result(2, "", f"approval file missing: {name}")
    return ALLOW
