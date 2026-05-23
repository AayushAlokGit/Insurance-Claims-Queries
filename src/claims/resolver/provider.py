"""Provider canonicalization. See resolver.md §5.

Used only for merge-key construction — the canonical form does
not replace the event's stored provider string (which keeps
human-readable form for audit). Two events with the same
canonical key may be from the same provider; differing keys
definitely aren't.

The MVP algorithm:
- Person providers (`Dr. X` / `X, MD`) → lowercased last name.
- Organizations / clinics → whole string, lowercased, whitespace
  collapsed. Different beast from people; we don't try to extract
  a "last word" since clinic names are multi-word noun phrases.
- None → None.
"""

from __future__ import annotations

import re

_HONORIFIC = re.compile(r"^(dr|mr|mrs|ms|prof|rev)\.?\s+", re.IGNORECASE)
_DEGREE_SUFFIX = re.compile(
    r",?\s*(MD|DO|NP|PA|PT|DPT|OT|DC|PhD|DDS)\.?$",
    re.IGNORECASE,
)
_WS = re.compile(r"\s+")


def _is_person(name: str) -> bool:
    """Heuristic: a name is a person if it starts with an
    honorific or ends with a degree suffix, OR is short (1-2
    words) without organization-y words."""
    if _HONORIFIC.match(name):
        return True
    if _DEGREE_SUFFIX.search(name):
        return True
    words = name.split()
    if len(words) <= 2 and not any(
        w.lower()
        in {
            "group",
            "clinic",
            "center",
            "centre",
            "associates",
            "hospital",
            "evaluators",
            "therapy",
            "services",
            "medical",
            "health",
            "rehabilitation",
            "rehab",
        }
        for w in words
    ):
        return True
    return False


def canonicalize_provider(raw: str | None) -> str | None:
    """Return a provider key suitable for grouping events. Two
    events whose canonicals match are the same provider; the
    inverse is not guaranteed (different spellings can canonicalize
    differently)."""
    if raw is None:
        return None
    name = raw.strip()
    if not name:
        return None

    if _is_person(name):
        # Person: strip honorific + degree, take the last word.
        stripped = _HONORIFIC.sub("", name)
        stripped = _DEGREE_SUFFIX.sub("", stripped).strip()
        last = stripped.split()[-1] if stripped.split() else stripped
        return last.lower()

    # Organization: whole name, lowercased, whitespace collapsed.
    return _WS.sub(" ", name).strip().lower()
