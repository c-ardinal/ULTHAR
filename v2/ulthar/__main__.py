"""`python -m ulthar ...` (with v2/ on sys.path) or `python <clone>/v2/ulthar/__main__.py ...`.

Hook registration form (one entry per event):
    "<python>" -S "<clone>/v2/ulthar/__main__.py" hook --event <E>
`-S` skips site-packages, which the hook path never needs (psutil is only used by
lib.procs, which no ported guard imports); it saves ~20 ms per call (AC-9).

The hook path is guarded here, stdlib-only: if the package itself cannot be imported,
a PreToolUse is BLOCKED (exit 2) with the reason and recovery steps instead of
crashing with exit 1, which Claude Code would treat as "allow".
"""
import sys
from pathlib import Path

if not __package__:
    # Run as a file: make the package importable by its own name, and keep this
    # directory off sys.path so ulthar's `lib`/`guards` never shadow anything.
    _here = Path(__file__).resolve().parent
    sys.path = [p for p in sys.path if Path(p or ".").resolve() != _here]
    sys.path.insert(0, str(_here.parent))


def _event(argv: list) -> str:
    for i, arg in enumerate(argv):
        if arg == "--event" and i + 1 < len(argv):
            return argv[i + 1]
    return "<unknown>"


def _hook(argv: list) -> int:
    try:
        from ulthar import hook
    except Exception as exc:  # noqa: BLE001 -- an unguarded tool call is the worse failure
        event = _event(argv)
        msg = (f"BLOCKED (ulthar dispatcher infrastructure): the ulthar package could not be "
               f"imported\n  detail: {type(exc).__name__}: {exc}\n"
               "To recover: restore the package (`git -C <your ULTHAR clone> status`, then "
               "`git -C <clone> checkout <good-commit> -- v2/`), or disable this event's "
               f"(\"{event}\") ulthar hook entry in settings.json until it is fixed.\n")
        sys.stderr.buffer.write(msg.encode("utf-8"))
        return 2 if event == "PreToolUse" else 0
    return hook.main(argv)


if sys.argv[1:2] == ["hook"]:
    sys.exit(_hook(sys.argv[2:]))
if sys.argv[1:2] == ["extract"] and len(sys.argv) == 6 and sys.argv[2:5:2] == ["--episode",
                                                                             "--project-dir"]:
    # the detached worker the UserPromptSubmit hook starts (ulthar.extract.launch): it
    # skips the CLI's imports, as it runs alongside the session's next hook calls
    from ulthar import worker as _worker

    sys.exit(_worker.run_worker(sys.argv[3], sys.argv[5]))

from ulthar.cli import main  # noqa: E402

sys.exit(main())
