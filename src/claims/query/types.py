"""Query-result types. Each canned query returns a typed object,
never a bare scalar — see q1 §6 on why a discriminated union or
distribution beats a nullable number at corpus scale."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

# --- Q1 — discriminated union ----------------------------------


class Q1Returned(BaseModel):
    model_config = ConfigDict(frozen=True)
    status: Literal["returned"] = "returned"
    days: int
    rtw_date: date
    duty_type: Literal["modified", "full"]


class Q1NeverReturned(BaseModel):
    model_config = ConfigDict(frozen=True)
    status: Literal["never_returned"] = "never_returned"
    reason: Literal["ptd", "deceased", "separated", "closed_no_rtw"]
    terminal_date: date


class Q1Pending(BaseModel):
    model_config = ConfigDict(frozen=True)
    status: Literal["pending"] = "pending"
    days_open: int


Q1Result = Annotated[
    Union[Q1Returned, Q1NeverReturned, Q1Pending],
    Field(discriminator="status"),
]


# --- Q2 ----------------------------------------------------------


class Q2Appointment(BaseModel):
    """One attended appointment in the Q2 result. `parties` is the
    DD-016 set of named entities (clinicians + facility) extracted
    for this encounter; preserved as a list rather than collapsed
    to a single label so the consumer can see both the doctor and
    the clinic when both were named."""

    model_config = ConfigDict(frozen=True)
    date: date
    status: Literal["attended"] = "attended"
    parties: list[str]
    specialty: str | None
    appointment_type: str | None


class Q2Result(BaseModel):
    model_config = ConfigDict(frozen=True)
    appointments: list[Q2Appointment]
    count: int


# --- Q3 ----------------------------------------------------------


class Q3Swing(BaseModel):
    model_config = ConfigDict(frozen=True)
    date: date
    new_amount: Decimal
    previous_amount: Decimal
    delta: Decimal


class Q3BucketSummary(BaseModel):
    model_config = ConfigDict(frozen=True)
    bucket: str
    count: int
    net: Decimal
    swings: list[Q3Swing]


class Q3Result(BaseModel):
    model_config = ConfigDict(frozen=True)
    buckets: list[Q3BucketSummary]


# --- Q4 ----------------------------------------------------------


class Q4Visit(BaseModel):
    """One merged scheduled-and-seen visit. `parties` carries the
    DD-016 set; null/empty means the merge happened on date alone
    (the schedule note named no party, the visit note named no
    party, or both)."""

    model_config = ConfigDict(frozen=True)
    parties: list[str]
    scheduled_notice_date: date
    scheduled_for_date: date
    occurred_on: date
    lag_days: int
    on_time_delta_days: int


class Q4Distribution(BaseModel):
    model_config = ConfigDict(frozen=True)
    median: float | None
    p90: float | None
    mean: float | None


class Q4Result(BaseModel):
    model_config = ConfigDict(frozen=True)
    per_visit: list[Q4Visit]
    lag: Q4Distribution
