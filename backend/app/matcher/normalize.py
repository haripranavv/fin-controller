"""Normalization helpers (PROJECT_SPEC.md section 8.1): case, whitespace,
reference prefixes, punctuation, obvious narration noise.
"""
from __future__ import annotations

import re

_PUNCT_RE = re.compile(r"[^A-Z0-9]+")
_REFERENCE_PREFIXES = ("REF", "INV", "UTR", "TXN")


def normalize_text(value: str | None) -> str:
    """Uppercase, collapse whitespace/punctuation runs to a single space,
    strip. 'NEFT-HDFC-RAJTRADCO' -> 'NEFT HDFC RAJTRADCO'."""
    if not value:
        return ""
    return _PUNCT_RE.sub(" ", value.upper()).strip()


def normalize_reference(value: str | None) -> str:
    """Strip a known reference prefix and all non-alphanumerics, for exact
    identifier comparison (section 8.2). 'INV-88213' -> '88213'."""
    if not value:
        return ""
    token = re.sub(r"[^A-Z0-9]", "", value.upper())
    for prefix in _REFERENCE_PREFIXES:
        if token.startswith(prefix) and len(token) > len(prefix):
            return token[len(prefix) :]
    return token


def contains_reference(haystack: str | None, needle: str) -> bool:
    """Case/punctuation-insensitive substring check. Used for settlement <->
    bank matching, where the bank narration is expected to embed the
    settlement_id verbatim (section 8.2: "prefer strong references and
    exact identifiers"). Both sides go through the same normalization, so a
    literal substring survives even though hyphens/underscores/slashes all
    collapse to spaces along the way."""
    if not haystack or not needle:
        return False
    return normalize_text(needle) in normalize_text(haystack)
