"""Stand-in for `claude -p --model haiku` in the tests (the real one is only used by the
P-15 probe). Reads the prompt on stdin, then by mode:

  none                    prints [{"kind": "none"}]
  file <path>             prints the file's content (a canned extractor answer)
  sleep <sec> <path>      waits, then like `file` (an extraction still running)
  badjson                 prints prose that is not JSON
  fail                    exits 3 with a message on stderr
  recurse <payload.json>  runs the hook (UserPromptSubmit, inherited env, so with
                          ULTHAR_EXTRACTOR=1) on that payload, then like `none`
  console <out.json> [s]  (Windows) writes its own and a grandchild's console window
                          handle and visibility to out.json, stays alive s seconds
                          (default 3), then like `none` (R1-b AC-18)
  hang <marker>           starts a grandchild sleeper whose command line carries
                          <marker>, then sleeps 90 s (the timeout test, AC-19)

$FAKE_EXTRACTOR_LOG, when set, receives each prompt and the ULTHAR_EXTRACTOR value.
"""
import json
import os
import sys
import time
from pathlib import Path

MAIN_PY = Path(__file__).resolve().parents[1] / "ulthar" / "__main__.py"
CONSOLE_PROBE = """
import ctypes, json, os
k32, u32 = ctypes.windll.kernel32, ctypes.windll.user32
k32.GetConsoleWindow.restype = ctypes.c_void_p
hwnd = k32.GetConsoleWindow() or 0
print(json.dumps({"pid": os.getpid(), "console_hwnd": hwnd,
                  "visible": bool(hwnd) and bool(u32.IsWindowVisible(ctypes.c_void_p(hwnd)))}))
"""


def _console_state() -> str:
    import contextlib  # noqa: PLC0415
    import io  # noqa: PLC0415

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        exec(CONSOLE_PROBE, {})  # noqa: S102 -- the same probe as the grandchild runs
    return buf.getvalue()


def main() -> int:
    sys.stdin.reconfigure(encoding="utf-8")  # as the real claude CLI: UTF-8 on both pipes
    sys.stdout.reconfigure(encoding="utf-8")
    prompt = sys.stdin.read()
    log = os.environ.get("FAKE_EXTRACTOR_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ULTHAR_EXTRACTOR": os.environ.get("ULTHAR_EXTRACTOR"),
                                 "prompt": prompt}, ensure_ascii=False) + "\n")
    mode, args = sys.argv[1], sys.argv[2:]
    if mode == "none":
        print('[{"kind": "none"}]')
    elif mode == "file":
        print(Path(args[0]).read_text(encoding="utf-8"))
    elif mode == "sleep":
        time.sleep(float(args[0]))
        print(Path(args[1]).read_text(encoding="utf-8"))
    elif mode == "badjson":
        print("Sure! Here is what I found: the owner wants blue buttons.")
    elif mode == "fail":
        print("fake extractor: simulated crash", file=sys.stderr)
        return 3
    elif mode == "console":
        import subprocess  # noqa: PLC0415

        child = subprocess.run([sys.executable, "-S", "-c", CONSOLE_PROBE], capture_output=True,
                               text=True, timeout=60)
        report = {"extractor": json.loads(_console_state()), "worker_pid": os.getppid(),
                  "extractor_pid": os.getpid(), "grandchild": json.loads(child.stdout)}
        Path(args[0]).write_text(json.dumps(report), encoding="utf-8")
        time.sleep(float(args[1]) if len(args) > 1 else 3)  # alive while the test looks
        print('[{"kind": "none"}]')
    elif mode == "hang":
        import subprocess  # noqa: PLC0415

        subprocess.Popen([sys.executable, "-S", "-c", "import time; time.sleep(90)", args[0]])
        time.sleep(90)
        print('[{"kind": "none"}]')
    elif mode == "recurse":
        import subprocess  # only this mode needs it; keeps the common modes' start-up light

        payload = Path(args[0]).read_text(encoding="utf-8")
        subprocess.run([sys.executable, "-S", str(MAIN_PY), "hook", "--event", "UserPromptSubmit"],
                       input=payload, text=True, capture_output=True, timeout=60)
        print('[{"kind": "none"}]')
    else:
        print(f"unknown mode {mode}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
