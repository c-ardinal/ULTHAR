"""The declared interpreter floor (v2/pyproject.toml `requires-python`) is truthful.

Two static checks over every module in v2/ulthar, so no older interpreter is needed:
  1. syntax: each module parses with ast feature_version = the floor;
  2. API: no attribute or name from VERSION_GATED (stdlib APIs added after 3.10 that
     a hook runtime might plausibly reach for) is used when it is newer than the floor.
The table is deliberately explicit: adding a newer API to the code without raising
the floor fails here, and so does lowering the floor below what the code uses
(e.g. `>=3.11` with sqlite3 `setconfig`, which is 3.12).
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

from helpers import V2_ROOT, Probe

PACKAGE = V2_ROOT / "ulthar"
PYPROJECT = V2_ROOT / "pyproject.toml"

VERSION_GATED: dict[str, tuple[int, int]] = {
    # sqlite3
    "setconfig": (3, 12), "getconfig": (3, 12), "autocommit": (3, 12),
    "SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE": (3, 12), "SQLITE_DBCONFIG_DEFENSIVE": (3, 12),
    "blobopen": (3, 11), "serialize": (3, 11), "deserialize": (3, 11),
    "setlimit": (3, 11), "getlimit": (3, 11),
    # datetime / time / os / pathlib
    "UTC": (3, 11), "walk": (3, 12), "is_junction": (3, 12), "splitroot": (3, 12),
    "from_uri": (3, 13), "full_match": (3, 13), "process_cpu_count": (3, 13),
    # itertools / functools / typing / misc
    "batched": (3, 12), "override": (3, 12), "deprecated": (3, 13), "replace": (3, 13),
    "chdir": (3, 11), "TaskGroup": (3, 11), "add_note": (3, 11), "exception_group": (3, 11),
    "tomllib": (3, 11), "ExceptionGroup": (3, 11), "Self": (3, 11), "assert_never": (3, 11),
}
# Names that are also ordinary pre-3.11 APIs on other objects and must be qualified
# to count (str.replace, os.walk, os.chdir are old; copy.replace / Path.walk are not).
QUALIFIED_ONLY = {"replace": {"copy"}, "walk": {"Path", "path", "p"}, "chdir": {"contextlib"},
                  "override": {"typing"}, "deprecated": {"warnings", "typing"}}


def declared_floor() -> tuple[int, int]:
    text = PYPROJECT.read_text(encoding="utf-8")
    m = re.search(r'requires-python\s*=\s*">=\s*(\d+)\.(\d+)"', text)
    if not m:
        raise AssertionError("requires-python >=X.Y not found in v2/pyproject.toml")
    return int(m.group(1)), int(m.group(2))


def gated_uses(path: Path, floor: tuple[int, int]) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = []
    uses: list[tuple[int, str, str]] = []  # (line, name, qualifying owner)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            owner = node.value
            uses.append((node.lineno, node.attr, owner.id if isinstance(owner, ast.Name) else (
                owner.attr if isinstance(owner, ast.Attribute) else "")))
        elif isinstance(node, ast.ImportFrom):
            uses += [(node.lineno, a.name, (node.module or "").split(".")[-1]) for a in node.names]
        elif isinstance(node, ast.Import):
            uses += [(node.lineno, a.name.split(".")[0], "") for a in node.names]
        elif isinstance(node, ast.Name):
            uses.append((node.lineno, node.id, ""))
    for lineno, name, owner_name in uses:
        need = VERSION_GATED.get(name)
        if need is None or need <= floor:
            continue
        if name in QUALIFIED_ONLY and owner_name not in QUALIFIED_ONLY[name]:
            continue
        found.append(f"{path.relative_to(V2_ROOT)}:{lineno}: {name} needs "
                     f"{need[0]}.{need[1]}")
    return found


def check(p: Probe) -> None:
    floor = declared_floor()
    p.check(f"declared floor parsed: >={floor[0]}.{floor[1]}", floor >= (3, 8))
    p.check("the running interpreter satisfies the declared floor", sys.version_info[:2] >= floor,
            sys.version)
    files = sorted(PACKAGE.rglob("*.py"))
    syntax, apis = [], []
    for f in files:
        try:
            ast.parse(f.read_text(encoding="utf-8"), filename=str(f), feature_version=floor)
        except SyntaxError as exc:
            syntax.append(f"{f.relative_to(V2_ROOT)}:{exc.lineno}: {exc.msg}")
        apis += gated_uses(f, floor)
    p.check(f"syntax of {len(files)} modules is valid for {floor[0]}.{floor[1]}", not syntax,
            "\n       ".join(syntax))
    p.check(f"no stdlib API newer than {floor[0]}.{floor[1]} is used", not apis,
            "\n       ".join(apis))
    sample = PACKAGE / "db.py"
    p.check("self-test: a 3.11 floor would be rejected (db.py uses sqlite3 setconfig)",
            bool(gated_uses(sample, (3, 11))), "the gate did not see setconfig")


def main() -> int:
    p = Probe()
    check(p)
    return p.result()


if __name__ == "__main__":
    sys.exit(main())
