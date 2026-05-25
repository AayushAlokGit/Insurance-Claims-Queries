"""Post-LLM substring check for evidence_quote.

Every LLM extractor that returns a quote from the note body must
have that quote verified against the body before the resulting
event is trusted (extractor.md §6.3).

Two relaxations on a strict substring check:

1. **Whitespace-tolerant**: runs of whitespace collapse to single
   spaces so the LLM's trivial reformatting (different newlines,
   stripped indentation) doesn't kill a valid quote.

2. **Skipped-line-tolerant**: the LLM frequently emits "verbatim"
   quotes that drop one or two internal lines from a templated
   medical-record block (e.g. omitting the `Next Office Visit:`
   line in the middle of a `Date of Appointment:` block). To
   accept these without accepting pure fabrications, the quote is
   split into lines and each non-empty line must independently
   appear in the body. An LLM could in theory splice two real but
   non-adjacent body sentences this way; in practice the structured
   fields (date, status, parties) are still anchored to body
   content, so the precision loss is small and the recall gain on
   templated records is large.
"""

from __future__ import annotations

import re

_WS = re.compile(r"\s+")


def _collapse(s: str) -> str:
    return _WS.sub(" ", s).strip()


def quote_in_body(quote: str, body: str) -> bool:
    """Return True if every non-empty line of `quote` appears in
    `body`, ignoring differences in run-length whitespace. Accepts
    quotes that omit interior lines from a contiguous block."""
    if not quote:
        return False
    collapsed_body = _collapse(body)
    for line in quote.splitlines():
        line = _collapse(line)
        if not line:
            continue
        if line not in collapsed_body:
            return False
    # If the quote had no newlines, splitlines returns one chunk —
    # equivalent to the original whole-quote substring check.
    return True
