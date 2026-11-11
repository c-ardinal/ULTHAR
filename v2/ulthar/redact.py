"""Secret scrubbing before anything is written to the knowledge store (MEM-10, R1-b AC-14).

Every known-format match is replaced by `[REDACTED:<kind>]`. Order matters: the
Anthropic pattern runs before the generic `sk-` one so it is named precisely.
"""
from __future__ import annotations

import re

PATTERNS: tuple[tuple[str, re.Pattern], ...] = tuple((kind, re.compile(rx)) for kind, rx in (
    ("private_key", r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----"
                    r"(?:[\s\S]*?-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----|[\s\S]*\Z)"),
    ("anthropic_key", r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    ("openai_key", r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{20,}"),
    ("github_token", r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})"),
    ("aws_access_key", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("slack_token", r"\bxox[abposr]-[A-Za-z0-9\-]{10,}"),
    ("google_api_key", r"\bAIza[0-9A-Za-z_\-]{35}"),
))


def redact_text(text: str) -> str:
    for kind, pattern in PATTERNS:
        if pattern.search(text):
            text = pattern.sub(f"[REDACTED:{kind}]", text)
    return text


def redact(value):
    """Every string inside `value` (str / list / dict), scrubbed; a new object."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    return value
