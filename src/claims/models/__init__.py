"""Domain models for the claims pipeline.

The single source of truth for the in-memory shape of every entity that
flows through Loader → Normalizer → Extractor → Resolver → Store.

See data-modeling.md for the rationale behind every field.
"""

from claims.models.claim import Claim
from claims.models.event import (
    AppointmentAttributes,
    AppointmentStatus,
    AppointmentType,
    Event,
    EventAttributes,
    EventType,
    ExtractionMethod,
    ReserveChangeAttributes,
    ReturnToWorkAttributes,
    RTWTerminalAttributes,
    RTWTerminalReason,
)
from claims.models.note import Note

__all__ = [
    "AppointmentAttributes",
    "AppointmentStatus",
    "AppointmentType",
    "Claim",
    "Event",
    "EventAttributes",
    "EventType",
    "ExtractionMethod",
    "Note",
    "RTWTerminalAttributes",
    "RTWTerminalReason",
    "ReserveChangeAttributes",
    "ReturnToWorkAttributes",
]
