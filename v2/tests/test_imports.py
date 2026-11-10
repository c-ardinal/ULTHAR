"""AC-11: every import in v2/ulthar/ is the standard library, psutil, or ulthar itself
(static AST scan: module-level, function-level and relative imports alike)."""
from __future__ import annotations

import ast
import sys
from pathlib import Path

from helpers import V2_ROOT, Probe

PACKAGE = V2_ROOT / "ulthar"
ALLOWED_THIRD_PARTY = {"psutil"}


def imported_roots(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [(node.lineno, a.name.split(".")[0]) for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            out.append((node.lineno, "." if node.level else (node.module or "").split(".")[0]))
    return out


def check_imports(p: Probe) -> None:
    allowed = set(sys.stdlib_module_names) | ALLOWED_THIRD_PARTY | {"ulthar", "."}
    files = sorted(PACKAGE.rglob("*.py"))
    violations = []
    for f in files:
        for lineno, root in imported_roots(f):
            if root not in allowed:
                violations.append(f"{f.relative_to(V2_ROOT)}:{lineno}: {root}")
    p.check(f"scanned {len(files)} modules under v2/ulthar", len(files) >= 10, str(len(files)))
    p.check(f"AC-11: non-stdlib/psutil imports: {len(violations)}", not violations,
            "\n       ".join(violations))


def main() -> int:
    p = Probe()
    check_imports(p)
    return p.result()


if __name__ == "__main__":
    sys.exit(main())
