"""Post-LLM substring check for evidence_quote.

Every LLM extractor that returns a quote from the note body must
have that quote verified as a verbatim substring of the body
before the resulting event is trusted (extractor.md §6.3). The
check is whitespace-tolerant — runs of whitespace collapse to a
single space — so trivial reformatting by the model doesn't kill
a valid quote.
"""

from __future__ import annotations

import re

_WS = re.compile(r"\s+")


def _collapse(s: str) -> str:
    return _WS.sub(" ", s).strip()


def quote_in_body(quote: str, body: str) -> bool:
    """Return True if `quote` appears in `body`, ignoring
    differences in run-length whitespace."""
    if not quote:
        return False
    return _collapse(quote) in _collapse(body)
