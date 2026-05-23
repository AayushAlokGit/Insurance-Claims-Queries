"""Body text cleanup. See normalizer.md §3.1.

Destructive but lossless: every replacement preserves meaning.
Mojibake repair (UTF-8 misdecoded as Windows-1252), line-ending
normalization, tab→space, trailing whitespace stripped. Body
date strings are deliberately untouched (DD-013).
"""

from __future__ import annotations

# Order matters — more-specific (longer) mojibake sequences must
# be replaced before their shorter prefixes. `â€` alone resolves
# to a right double-quote; longer forms are em-dash, right single
# quote, and left double-quote.
_MOJIBAKE: tuple[tuple[str, str], ...] = (
    ("â€”", "—"),   # â€" → — em dash
    ("â€™", "’"),   # â€™ → ’ right single quote
    ("â€œ", "“"),   # â€œ → " left double quote
    ("â€",       "”"),   # â€  → " right double quote
    ("Â ",       " "),         # Â non-breaking space artifact
)


def clean_text(text: str) -> str:
    out = text
    for bad, good in _MOJIBAKE:
        out = out.replace(bad, good)
    out = out.replace("\r\n", "\n").replace("\r", "\n")
    out = out.replace("\t", " ")
    out = "\n".join(line.rstrip() for line in out.split("\n"))
    return out
