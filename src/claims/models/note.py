"""The Note entity — Normalizer output. See normalizer.md §2."""

from datetime import date

from pydantic import BaseModel, ConfigDict, Field


class Note(BaseModel):
    """A single normalized note within a claim.

    Header fields are typed (note_date is ISO-parsed); the body is
    character-clean but body-date strings are preserved verbatim
    (DD-013). data_quality_flags carries any non-fatal issues found
    during normalization.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    note_id: str
    claim_id: str
    note_date: date
    activity: str | None
    author: str | None
    body: str
    data_quality_flags: list[str] = Field(default_factory=list)
