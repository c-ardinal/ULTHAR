"""Shell-aware reading of a Bash / PowerShell command for the store guard (R1-b AC-4).

`tokenize` applies the quoting rules both shells share -- '...' literal, "..." with its
escape, adjacent pieces concatenated (`-m "ulthar"`, `ul'thar'` -> one word) -- with the
shell's escape character (Bash `\\`, PowerShell backtick). Separators (; && || | & and
newlines) and redirections become their own tokens. Callers read every variant
(`variants`: with the shell's escape and without any), so a backslash in a Windows path
cannot hide a word.

`ulthar_calls` finds every place a word sequence launches the ulthar CLI -- `ulthar`,
`python [opts] -m ulthar[.__main__]` (also `-multhar`), `python [opts] .../ulthar/__main__.py`
or `python .../ulthar` -- at any position, also inside the command-line argument of a shell
launcher (`bash -c "..."`, `cmd /c`, `powershell -Command`; depth 2), and returns the arguments that follow it.
Known limit (spec): a path or word assembled at run time (variables, Join-Path, string
concatenation by the shell) is not seen here.
"""
from __future__ import annotations

import re

SEPARATORS = frozenset({";", "&&", "||", "|", "&", "\n"})
REDIRECT = ">"
INPUT = "<"
BASH_DQ_ESCAPABLE = '$`"\\\n'
ULTHAR_EXES = frozenset({"ulthar", "ulthar.exe", "ulthar.cmd", "ulthar.bat"})
PYTHON_EXE = re.compile(r"^(?:py|python[\d.]*)(?:\.exe)?$", re.I)
PYTHON_OPTS_WITH_VALUE = frozenset({"-X", "-W", "-Q"})
ULTHAR_MODULES = frozenset({"ulthar", "ulthar.__main__"})
ESCAPES = {"Bash": "\\", "PowerShell": "`"}
NESTED_DEPTH = 2
# the argument after these is itself a command line (bash/sh -c, cmd /c /k, powershell -c -Command)
COMMAND_OPTIONS = frozenset({"-c", "/c", "/k", "-command", "-commandwithargs"})


def tokenize(cmd: str, escape: str | None, exact: bool = False) -> list[str]:
    """`exact` (R1-c AC-11, the write-target reading): Bash's own rule inside "..." (a
    backslash escapes only $ ` " \\ and newline; elsewhere it stays), and an input
    redirection `<` is its own token INPUT instead of REDIRECT."""
    toks: list[str] = []
    cur: list[str] = []
    started, quote, i = False, None, 0

    def flush() -> None:
        nonlocal cur, started
        if started:
            toks.append("".join(cur))
        cur, started = [], False

    while i < len(cmd):
        ch = cmd[i]
        if quote == "'":
            if ch == "'":
                quote = None
            else:
                cur.append(ch)
        elif quote == '"':
            if escape and ch == escape and i + 1 < len(cmd) and not (
                    exact and escape == "\\" and cmd[i + 1] not in BASH_DQ_ESCAPABLE):
                i += 1
                cur.append(cmd[i])
            elif ch == '"':
                quote = None
            else:
                cur.append(ch)
        elif escape and ch == escape and i + 1 < len(cmd):
            i += 1
            cur.append(cmd[i])
            started = True
        elif ch in "'\"":
            quote, started = ch, True
        elif ch == "\n" or ch in ";|&":
            flush()
            two = cmd[i:i + 2]
            if two in ("&&", "||"):
                toks.append(two)
                i += 1
            else:
                toks.append(ch)
        elif ch in "<>":
            flush()
            toks.append(INPUT if exact and ch == "<" else REDIRECT)
            while i + 1 < len(cmd) and cmd[i + 1] in "<>&":
                i += 1
        elif ch.isspace():
            flush()
        else:
            cur.append(ch)
            started = True
        i += 1
    flush()
    return toks


def variants(command: str, tool: str | None) -> list[list[str]]:
    escape = ESCAPES.get(tool or "", "\\")
    return [tokenize(command, escape), tokenize(command, None)]


def segments(tokens: list[str]) -> list[list[str]]:
    out, cur = [], []
    for tok in tokens:
        if tok in SEPARATORS:
            out.append(cur)
            cur = []
        else:
            cur.append(tok)
    out.append(cur)
    return [s for s in out if s]


def basename(word: str) -> str:
    return re.split(r"[\\/]", word.rstrip("\\/"))[-1].casefold()


def _python_target(tokens: list[str], j: int) -> list[str] | None:
    while j < len(tokens):
        t = tokens[j]
        if t == "-m":
            module = tokens[j + 1] if j + 1 < len(tokens) else ""
            return tokens[j + 2:] if module.casefold() in ULTHAR_MODULES else None
        if t.startswith("-m") and len(t) > 2:
            return tokens[j + 1:] if t[2:].casefold() in ULTHAR_MODULES else None
        if t in ("-c", "-"):
            return None
        if t in PYTHON_OPTS_WITH_VALUE:
            j += 2
            continue
        if t.startswith("-"):
            j += 1
            continue
        parts = [p.casefold() for p in re.split(r"[\\/]+", t.rstrip("\\/")) if p]
        if parts[-2:] == ["ulthar", "__main__.py"] or (parts and parts[-1] == "ulthar"):
            return tokens[j + 1:]
        return None
    return None


def call_args(tokens: list[str], i: int) -> list[str] | None:
    """The arguments of an ulthar launch starting at tokens[i], else None."""
    word = tokens[i]
    name = basename(word)
    if name in ULTHAR_EXES:
        return tokens[i + 1:]
    parts = [p.casefold() for p in re.split(r"[\\/]+", word) if p]
    if parts[-2:] == ["ulthar", "__main__.py"]:
        return tokens[i + 1:]
    if PYTHON_EXE.match(name):
        return _python_target(tokens, i + 1)
    return None


def ulthar_calls(command: str, tool: str | None, depth: int = 0) -> list[list[str]]:
    found = []
    for toks in variants(command, tool):
        for seg in segments(toks):
            for i in range(len(seg)):
                args = call_args(seg, i)
                if args is not None:
                    found.append([a for a in args if a != REDIRECT])
            if depth < NESTED_DEPTH:
                for prev, tok in zip(seg, seg[1:]):
                    if prev.casefold() in COMMAND_OPTIONS:
                        found += ulthar_calls(tok, tool, depth + 1)
    return found


def all_segments(command: str, tool: str | None, depth: int = 0) -> list[list[str]]:
    """Every segment of every variant, also inside a shell launcher's command-line
    argument (`bash -c "..."`, `powershell -Command ...`; depth 2)."""
    out = []
    for toks in variants(command, tool):
        for seg in segments(toks):
            out.append(seg)
            if depth < NESTED_DEPTH:
                for prev, tok in zip(seg, seg[1:]):
                    if prev.casefold() in COMMAND_OPTIONS:
                        out += all_segments(tok, tool, depth + 1)
    return out


def leading_ulthar(seg: list[str]) -> bool:
    """The segment IS an ulthar CLI call (after VAR=value prefixes), with no redirection."""
    words = list(seg)
    while words and re.match(r"^\w+=", words[0]):
        words = words[1:]
    return bool(words) and REDIRECT not in words and call_args(words, 0) is not None
