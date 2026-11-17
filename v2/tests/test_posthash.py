"""R1-d AC-9 (b) / AC-16, review r1d-1 #4: the in-process post-hash (ulthar.posthash) never
differs from `git hash-object`. In a temp repository, with the user's global config
redirected to the sandbox home: a plain LF file is hashed in-process and equals git's;
with core.attributesFile set (pointing at `*.txt filter=up`, a clean filter that
upper-cases), with an [include] in the repository config, or with a CRLF file, the hook
asks git -- and its post-hash equals git's filtered blob, not the raw bytes' id."""
from __future__ import annotations

import os
import subprocess
import sys

from helpers import Probe, Sandbox
from ulthar import hook, posthash


def _git(repo, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          check=True).stdout.strip()


def check_post_hash(p: Probe) -> None:
    sb = Sandbox()
    saved = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME")}
    os.environ.update(HOME=str(sb.home), USERPROFILE=str(sb.home))
    os.environ.pop("XDG_CONFIG_HOME", None)
    try:
        repo = sb.root / "repo"
        repo.mkdir()
        _git(repo, "init", "-q")
        _git(repo, "config", "core.autocrlf", "false")
        plain = repo / "plain.txt"
        plain.write_bytes(b"lower case text\n")
        inproc = posthash.blob(plain)
        p.check("an LF file in a plain repository is hashed in-process, equal to git hash-object",
                inproc is not None and inproc == _git(repo, "hash-object", "plain.txt"), str(inproc))

        attrs = sb.root / "attrs"
        attrs.write_text("*.txt filter=up\n", encoding="utf-8")
        _git(repo, "config", "core.attributesFile", attrs.as_posix())
        _git(repo, "config", "filter.up.clean", "tr a-z A-Z")
        filtered = _git(repo, "hash-object", "plain.txt")
        raw = _git(repo, "hash-object", "--no-filters", "plain.txt")
        got = hook.blob_hash(str(plain))
        p.check("core.attributesFile with a clean filter -> not in-process; the post-hash is git's "
                "filtered blob, not the raw bytes'", posthash.blob(plain) is None
                and got == filtered and filtered != raw, f"{got} {filtered} {raw}")
        _git(repo, "config", "--unset", "core.attributesFile")

        (sb.root / "inc").write_text(f"[core]\n\tattributesFile = {attrs.as_posix()}\n",
                                     encoding="utf-8")
        _git(repo, "config", "include.path", (sb.root / "inc").as_posix())
        got = hook.blob_hash(str(plain))
        p.check("an [include] in the repository config -> git decides (filtered blob)",
                posthash.blob(plain) is None and got == _git(repo, "hash-object", "plain.txt")
                == filtered, str(got))
        _git(repo, "config", "--unset", "include.path")

        crlf = repo / "crlf.md"
        crlf.write_bytes(b"line\r\n")
        p.check("a CRLF file -> git decides", posthash.blob(crlf) is None
                and hook.blob_hash(str(crlf)) == _git(repo, "hash-object", "crlf.md"))
        (repo / ".gitattributes").write_text("*.md text\n", encoding="utf-8")
        lf = repo / "lf.md"
        lf.write_bytes(b"line\n")
        p.check("a .gitattributes on the way -> git decides", posthash.blob(lf) is None
                and hook.blob_hash(str(lf)) == _git(repo, "hash-object", "lf.md"))
        os.environ["GIT_CONFIG_PARAMETERS"] = "'core.attributesfile'='x'"
        try:
            p.check("config given through the environment -> git decides",
                    posthash.blob(plain) is None)
        finally:
            os.environ.pop("GIT_CONFIG_PARAMETERS", None)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        sb.cleanup()


def main() -> int:
    p = Probe()
    print("\n== review r1d-1 #4: the in-process post-hash equals git's ==")
    check_post_hash(p)
    return p.result()


if __name__ == "__main__":
    sys.exit(main())
