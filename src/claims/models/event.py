"""The Event entity and its discriminated-union attribute payloads.

See data-modeling.md §3-§4. Four active event types per DD-012:
reserve_change, appointment, return_to_work, rtw_terminal.

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
    "rtw_terminal",
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
RTWTerminalReason = Literal["ptd", "deceased", "separated", "closed_no_rtw"]


class _AttributesBase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ReserveChangeAttributes(_AttributesBase):
    """Q3 payload. previous_amount and delta are derived by the
    Resolver after events are ordered (data-modeling.md §4.1).
    `source_note_dates` is the sorted tuple of note dates that
    contributed to this event — paired with `evidence_quote` for
    debugging. Single-source events have one entry; merged events
    have one per contributing note."""

    type: Literal["reserve_change"] = "reserve_change"
    bucket: str
    new_amount: Decimal
    previous_amount: Decimal | None = None
    delta: Decimal | None = None
    author: str | None = None
    source_note_dates: tuple[date, ...] = ()
    evidence_quote: str | None = None


class AppointmentAttributes(_AttributesBase):
    """Q2 + Q4 payload. Three date fields support the Resolver's
    schedule-to-seen merge (data-modeling.md §4.2). `parties` per
    DD-016: the LLM emits the set of named individuals and
    organizations involved in this encounter; the resolver merges
    by claim + encounter_date + party-set overlap. Empty list
    means no identifiable party (the merge falls to date alone).
    `source_note_dates` per DD-017: the sorted tuple of note dates
    that contributed to this event. Single-source events have one
    entry; merged events keep one per contributing note for audit.
    The DD-017 recency tiebreaker uses `max(source_note_dates)`."""

    type: Literal["appointment"] = "appointment"
    parties: tuple[str, ...] = ()
    specialty: str | None = None
    scheduled_notice_date: date | None = None
    scheduled_for_date: date | None = None
    occurred_on: date | None = None
    status: AppointmentStatus
    appointment_type: AppointmentType | None = None
    source_note_dates: tuple[date, ...] = ()
    evidence_quote: str | None = None

    @property
    def encounter_date(self) -> date | None:
        """DD-016: stable encounter identity. scheduled_for_date
        takes precedence (booking notes), falls back to occurred_on
        (visit summaries that don't record the original booking
        date)."""
        return self.scheduled_for_date or self.occurred_on


class ReturnToWorkAttributes(_AttributesBase):
    """Q1 positive case (data-modeling.md §4.3). `source_note_dates`
    is the sorted tuple of note dates that contributed to this
    event — paired with `evidence_quote` for debugging."""

    type: Literal["return_to_work"] = "return_to_work"
    duty_type: RTWDutyType
    role: str | None = None
    source_note_dates: tuple[date, ...] = ()
    evidence_quote: str | None = None


class RTWTerminalAttributes(_AttributesBase):
    """Q1 definitive negative — DD-011 (data-modeling.md §4.4).
    `source_note_dates` is the sorted tuple of note dates that
    contributed to this event — paired with `evidence_quote` for
    debugging."""

    type: Literal["rtw_terminal"] = "rtw_terminal"
    reason: RTWTerminalReason
    context: str | None = None
    source_note_dates: tuple[date, ...] = ()
    evidence_quote: str | None = None


EventAttributes = Annotated[
    ReserveChangeAttributes
    | AppointmentAttributes
    | ReturnToWorkAttributes
    | RTWTerminalAttributes,
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
