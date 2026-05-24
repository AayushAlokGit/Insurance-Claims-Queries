"""Appointment marker extractor — rule fast-path.

Catches the templated appointment headers (`Date of Appointment:`,
`Next Office Visit:`, `Schedule Date Time:`) without paying LLM
tokens. Anything that doesn't match these markers — narrative NOV
lines, novel scheduling formats, status statements — falls
through to the LLM-based AppointmentExtractor in a later phase
(extractor.md §5.2-§5.3).

Emission rules (extractor.md §5.2):
- `Date of Appointment: <date>` → past visit. `occurred_on`
  set; `status="unknown"`. The status is the LLM extractor's job
  to fill in from surrounding prose (`attended`, `missed`, etc.).
- Forward-dated markers (`Next Office Visit:`, `Schedule Date
  Time:`) → `scheduled_for_date` set, `scheduled_notice_date` =
  this note's date, `status="scheduled"`.

NOV: lines are deliberately ignored. Every NOV occurrence in the
corpus is narrative ("NOV is set for 9/23. She is on the
cancellation list...") which the LLM is better suited for.
"""

from __future__ import annotations

import re
import uuid
from datetime import date

from claims.models import AppointmentAttributes, Event, Note
from claims.normalizer import parse_date

_MARKER_RE = re.compile(
    r"(?P<marker>Date of Appointment|Next Office Visit|Schedule Date Time)"
    r"\s*:\s*(?P<value>[^\n]+)",
    re.IGNORECASE,
)

# Cheap can_handle gate — substring search, no full parse.
_PREFILTER_RE = re.compile(
    r"\b(Date of Appointment|Next Office Visit|Schedule Date Time)\b",
    re.IGNORECASE,
)

_PROVIDER_RE = re.compile(
    r"^\s*(?:Provider|Name of physician|Physician)\s*:\s*(.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

_PROVIDER_LOOKAHEAD_LINES = 6


def _scheduled_marker(marker: str) -> bool:
    return marker.lower() != "date of appointment"


def _find_provider(body: str, after_offset: int) -> str | None:
    """Look at the next handful of lines for a Provider: / Name of
    physician: line. Returns None if nothing recognisable is
    found within the window."""
    rest = body[after_offset:]
    window = "\n".join(rest.split("\n")[:_PROVIDER_LOOKAHEAD_LINES])
    m = _PROVIDER_RE.search(window)
    return m.group(1).strip() if m else None


class AppointmentMarkerExtractor:
    event_type: str = "appointment"

    def can_handle(self, note: Note) -> bool:
        return _PREFILTER_RE.search(note.body) is not None

    def extract(self, note: Note) -> list[Event]:
        events: list[Event] = []
        for m in _MARKER_RE.finditer(note.body):
            parsed = parse_date(
                m.group("value"), reference_date=note.note_date
            )
            if parsed.iso is None:
                continue
            event_date = date.fromisoformat(parsed.iso)
            provider = _find_provider(note.body, m.end())

            # DD-016: Marker sees only a templated header. If a
            # `Provider:` / `Name of physician:` line sits nearby,
            # promote it to a single-entry parties tuple; otherwise
            # leave empty and let date alone carry the merge.
            parties: tuple[str, ...] = (provider,) if provider else ()

            if _scheduled_marker(m.group("marker")):
                attrs = AppointmentAttributes(
                    parties=parties,
                    scheduled_notice_date=note.note_date,
                    scheduled_for_date=event_date,
                    status="scheduled",
                    source_note_date=note.note_date,
                )
            else:
                attrs = AppointmentAttributes(
                    parties=parties,
                    occurred_on=event_date,
                    status="unknown",
                    source_note_date=note.note_date,
                )

            events.append(
                Event(
                    event_id=str(uuid.uuid4()),
                    claim_id=note.claim_id,
                    event_type="appointment",
                    event_date=event_date,
                    attributes=attrs,
                    extraction_method="rule",
                )
            )
        return events
