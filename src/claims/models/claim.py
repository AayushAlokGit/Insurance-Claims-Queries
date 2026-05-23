"""The Claim entity. See data-modeling.md §2."""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

ClaimType = Literal["injury", "illness"]


class Claim(BaseModel):
    """A single workers' comp claim.

    Seven fields per DD-012 (YAGNI trim). The injury/illness axis
    (DD-010) is a first-class column — corpus aggregations that mix
    the two populations are misleading.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    claim_id: str
    account: str | None
    jurisdiction: str | None
    claim_type: ClaimType
    date_of_loss: date
    source_file: str
    ingested_at: datetime
