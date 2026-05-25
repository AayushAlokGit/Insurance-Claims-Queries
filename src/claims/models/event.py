"""The Event entity and its discriminated-union attribute payloads.

See data-modeling.md §3-§4. Three active event types per DD-012:
reserve_change, appointment, return_to_work.

The `attributes` field is a discriminated union (tagged by the
inner `type` field). The outer `event_type` on Event must match
`attributes.type` — enforced by the model validator.
"""

from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

EventType = Literal[
    "reserve_change",
    "appointment",
    "return_to_work",
]

ExtractionMethod = Literal["rule", "llm", "merged"]

AppointmentStatus = Literal[
    "attended", "missed", "cancelled", "scheduled", "unknown"
]
AppointmentType = Literal[
    "office_visit",
    "follow_up",
    "ime",
    "fce",
    "procedure",
    "phone",
    "telehealth",
]

RTWDutyType = Literal["modified", "full"]


class _AttributesBase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EventEvidence(_AttributesBase):
    """One (note_date, quote) pair contributing to an event.

    Used by every event type — single-source events have a
    one-element tuple, merged/reconciled events union one entry
    per contributing note. Uniform across reserve_change,
    appointment, and return_to_work (DD-020/023)."""

    note_date: date
    quote: str


class ReserveChangeAttributes(_AttributesBase):
    """Q3 payload. previous_amount and delta are derived by the
    Resolver after events are ordered (data-modeling.md §4.1).
    `evidence` holds `(note_date, quote)` pairs — one per
    contributing note. Reserve events don't merge today, so the
    tuple is one-element; `source_note_dates` and `evidence_quote`
    survive as derived properties for callers."""

    type: Literal["reserve_change"] = "reserve_change"
    bucket: str
    new_amount: Decimal
    previous_amount: Decimal | None = None
    delta: Decimal | None = None
    author: str | None = None
    evidence: tuple[EventEvidence, ...] = ()

    @property
    def source_note_dates(self) -> tuple[date, ...]:
        return tuple(sorted({e.note_date for e in self.evidence}))

    @property
    def evidence_quote(self) -> str | None:
        return self.evidence[0].quote if self.evidence else None


class AppointmentAttributes(_AttributesBase):
    """Q2 + Q4 payload. Three date fields support the Resolver's
    schedule-to-seen merge (data-modeling.md §4.2). `parties` per
    DD-016: the set of named individuals and organizations involved
    in this encounter. Empty tuple means no identifiable party.

    `evidence` holds (note_date, quote) pairs — one per contributing
    note. Single-source per-note events have a one-element tuple;
    reconciled events union all contributors. The recency tiebreaker
    uses `max(e.note_date for e in evidence)`."""

    type: Literal["appointment"] = "appointment"
    parties: tuple[str, ...] = ()
    specialty: str | None = None
    scheduled_notice_date: date | None = None
    scheduled_for_date: date | None = None
    occurred_on: date | None = None
    status: AppointmentStatus
    appointment_type: AppointmentType | None = None
    evidence: tuple[EventEvidence, ...] = ()

    @property
    def source_note_dates(self) -> tuple[date, ...]:
        """Sorted contributing note dates derived from `evidence`.
        Preserved as a property so callers that grouped by note-date
        membership still work."""
        return tuple(sorted({e.note_date for e in self.evidence}))

    @property
    def encounter_date(self) -> date | None:
        """DD-016: stable encounter identity. scheduled_for_date
        takes precedence (booking notes), falls back to occurred_on
        (visit summaries that don't record the original booking
        date)."""
        return self.scheduled_for_date or self.occurred_on


class ReturnToWorkAttributes(_AttributesBase):
    """Q1 positive case (data-modeling.md §4.3). `evidence` holds
    `(note_date, quote)` pairs — one per contributing note — so
    merged events keep every contributor's provenance instead of
    dropping all but one. `source_note_dates` and `evidence_quote`
    survive as derived properties for callers that read them."""

    type: Literal["return_to_work"] = "return_to_work"
    duty_type: RTWDutyType
    role: str | None = None
    evidence: tuple[EventEvidence, ...] = ()

    @property
    def source_note_dates(self) -> tuple[date, ...]:
        return tuple(sorted({e.note_date for e in self.evidence}))

    @property
    def evidence_quote(self) -> str | None:
        return self.evidence[0].quote if self.evidence else None


EventAttributes = Annotated[
    ReserveChangeAttributes
    | AppointmentAttributes
    | ReturnToWorkAttributes,
    Field(discriminator="type"),
]


class Event(BaseModel):
    """A single timeline event on a claim.

    `event_type` is the storage-layer discriminator (matches the
    SQL column). It must agree with `attributes.type`; mismatch
    is rejected at construction.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str
    claim_id: str
    event_type: EventType
    event_date: date
    attributes: EventAttributes
    extraction_method: ExtractionMethod

    @model_validator(mode="after")
    def _event_type_matches_attributes(self) -> "Event":
        if self.event_type != self.attributes.type:
            raise ValueError(
                f"event_type {self.event_type!r} does not match "
                f"attributes.type {self.attributes.type!r}"
            )
        return self
