"""Claim-level metadata inference via regex over the loaded file.

Pulls what's safely extractable from the corpus without an LLM call:
date_of_loss from explicit "Date of Injury:" / "Date of Loss:"
lines, jurisdiction from "Jurisdiction: <CODE>" lines. claim_type
has no regex-tractable signal in either sample, so defaults to
"injury" and is overridable by the caller.

If a field cannot be inferred, the corresponding attribute is
None — the CLI surfaces this and falls back to the user-supplied
flag.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Literal

from claims.loader.loader import LoadedClaim
from claims.normalizer.date_parser import parse_date

ClaimType = Literal["injury", "illness"]

_RE_DOL = re.compile(
    r"\bDate\s+of\s+(?:Injury|Loss)\s*#?\s*:\s*(?P<v>[\d./-]+\d)",
    re.IGNORECASE,
)
_RE_JURISDICTION = re.compile(
    r"\bJurisdiction\s*:\s*(?P<v>[A-Z]{2})\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ClaimMetadata:
    date_of_loss: date | None
    jurisdiction: str | None
    claim_type: ClaimType  # defaults to "injury" — see module docstring


def infer_claim_metadata(loaded: LoadedClaim) -> ClaimMetadata:
    """Scan every note body for explicit metadata lines, take
    the first match for each field. The first occurrence wins —
    later restatements of the same value are redundant; if they
    disagree, that's a data-quality concern surfaced by the test
    fixtures rather than something to silently average."""
    dol: date | None = None
    jurisdiction: str | None = None

    for note in loaded.notes:
        if dol is None:
            m = _RE_DOL.search(note.raw_body)
            if m is not None:
                parsed = parse_date(m.group("v"))
                if parsed.iso is not None:
                    dol = date.fromisoformat(parsed.iso)
        if jurisdiction is None:
            m = _RE_JURISDICTION.search(note.raw_body)
            if m is not None:
                jurisdiction = m.group("v").upper()
        if dol is not None and jurisdiction is not None:
            break

    return ClaimMetadata(
        date_of_loss=dol,
        jurisdiction=jurisdiction,
        claim_type="injury",
    )
