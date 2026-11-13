"""The shape of one acceptance condition (R1-c AC-2), shared by the contract reader
(CLI) and the skill-pack definitions' default_acs (R1-d AC-1, read by hooks too).
No imports: safe on the hook path."""
from __future__ import annotations

KINDS = ("quant", "qual")
REQUIRED = ("id", "statement", "kind", "verify_cmd", "evidence_kind", "sources")


def is_str_list(v) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def check_threshold(where: str, th) -> list[str]:
    if th is None:
        return []
    if not isinstance(th, dict) or set(th) - {"lo", "hi", "lo_incl", "hi_incl"}:
        return [f"{where}.threshold must be a mapping of lo, hi, lo_incl, hi_incl (or null)"]
    out = [f"{where}.threshold.{k} must be a number or null"
           for k in ("lo", "hi") if th.get(k) is not None and not is_num(th.get(k))]
    out += [f"{where}.threshold.{k} must be true or false"
            for k in ("lo_incl", "hi_incl") if k in th and not isinstance(th[k], bool)]
    return out


def check_ac(where: str, ac) -> list[str]:
    if not isinstance(ac, dict):
        return [f"{where} must be a mapping"]
    out = [f"{where}.{k} is missing" for k in REQUIRED if k not in ac]
    out += [f"{where}.{k} must be a string" for k in ("id", "statement", "verify_cmd",
                                                      "evidence_kind")
            if k in ac and not isinstance(ac[k], str)]
    if "kind" in ac and ac["kind"] not in KINDS:
        out.append(f"{where}.kind must be one of {KINDS}")
    if "sources" in ac and not is_str_list(ac["sources"]):
        out.append(f"{where}.sources must be a list of strings")
    if "artifacts" in ac and not is_str_list(ac["artifacts"]):
        out.append(f"{where}.artifacts must be a list of repository paths")
    if ac.get("rubric") is not None and not isinstance(ac["rubric"], dict):
        out.append(f"{where}.rubric must be a mapping (levels, good, bad) or null")
    return out + check_threshold(where, ac.get("threshold"))
