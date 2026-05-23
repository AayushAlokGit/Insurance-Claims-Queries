"""RawNoteBlock → Note orchestration. See normalizer.md §2.

Strips markdown wrapping from the header, splits its three
pipe-separated fields, parses the date via `parse_date`, applies
body cleanup, and emits a `Note`. The Activity and Noted By
fields are optional — if missing, the corresponding Note field
is `None` rather than rejecting the whole note (normalizer.md §6).

Notes with unparseable header dates are rejected (returned as
`None`). The caller is responsible for logging and moving on.
"""

from __future__ import annotations

from datetime import date

from claims.loader import RawNoteBlock
from claims.models import Note
from claims.normalizer.cleanup import clean_text
from claims.normalizer.date_parser import parse_date

# Header-date plausibility window. Below 2000 is almost certainly
# a parsing bug; more than five years past today is also suspect.
_MIN_PLAUSIBLE_YEAR = 2000
_MAX_FUTURE_YEARS = 5


def normalize(raw: RawNoteBlock, *, index: int) -> Note | None:
    """Turn a RawNoteBlock into a typed Note.

    `index` is the note's position within its claim (0-based) and
    is used to construct a deterministic `note_id` of the form
    `<claim_id>-n<NNNN>`. Returns None if the header date cannot
    be parsed at all — the calling pipeline drops the note and
    logs.
    """
    parts = _parse_header(raw.raw_header)
    if parts is None:
        return None
    date_str, activity, author = parts

    parsed = parse_date(date_str)
    if parsed.iso is None:
        return None
    note_date = date.fromisoformat(parsed.iso)

    flags: list[str] = list(parsed.flags)
    if _is_implausible(note_date):
        flags.append("header_date_implausible")

    return Note(
        note_id=f"{raw.claim_id}-n{index:04d}",
        claim_id=raw.claim_id,
        note_date=note_date,
        activity=activity,
        author=author,
        body=clean_text(raw.raw_body),
        data_quality_flags=flags,
    )


def _parse_header(header: str) -> tuple[str, str | None, str | None] | None:
    """Pull (date, activity, author) out of the header line.

    The header looks like
        Date: 01/08/2026 10:02am CT | Activity: X | Noted By: Y
    optionally wrapped in `**...**` (file 2's dialect). Activity
    and Noted By may be missing; date is required."""
    s = header.strip()
    while s.startswith("*"):
        s = s[1:]
    while s.endswith("*"):
        s = s[:-1]
    s = s.strip()

    date_part: str | None = None
    activity_part: str | None = None
    author_part: str | None = None
    for chunk in (c.strip() for c in s.split("|")):
        if chunk.startswith("Date:"):
            date_part = chunk[len("Date:") :].strip() or None
        elif chunk.startswith("Activity:"):
            activity_part = chunk[len("Activity:") :].strip() or None
        elif chunk.startswith("Noted By:"):
            author_part = chunk[len("Noted By:") :].strip() or None

    if date_part is None:
        return None
    return date_part, activity_part, author_part


def _is_implausible(d: date) -> bool:
    if d.year < _MIN_PLAUSIBLE_YEAR:
        return True
    today = date.today()
    return d.year > today.year + _MAX_FUTURE_YEARS
