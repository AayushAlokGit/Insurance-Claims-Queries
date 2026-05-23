"""Run every registered extractor over a single note.

The registry is a module-level list; new extractors are appended
as later phases add them (Marker, Appointment-LLM, RTW, RTW
terminal). Extractors are independent — the order here doesn't
affect correctness, only test reproducibility.
"""

from __future__ import annotations

from claims.extractor.appointment_marker import AppointmentMarkerExtractor
from claims.extractor.base import Extractor
from claims.extractor.reserve_change import ReserveChangeExtractor
from claims.models import Event, Note

EXTRACTORS: list[Extractor] = [
    ReserveChangeExtractor(),
    AppointmentMarkerExtractor(),
]


def run_all(note: Note) -> list[Event]:
    events: list[Event] = []
    for ext in EXTRACTORS:
        if ext.can_handle(note):
            events.extend(ext.extract(note))
    return events
