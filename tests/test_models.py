"""Tests for the bits of the domain model that are our own code.

Pydantic's construction, literal enforcement, JSON round-trip, and
`extra="forbid"` are not retested here — that's library behavior.
What we do test:

- The `model_validator` on Event that cross-checks `event_type`
  against `attributes.type`.
- The discriminated-union wiring on `attributes`, since the LLM
  extractor will rely on it dispatching the right subclass from
  raw JSON.
"""

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from claims.models import (
    Event,
    ReserveChangeAttributes,
    ReturnToWorkAttributes,
)


def test_event_type_mismatch_rejected() -> None:
    with pytest.raises(ValidationError):
        Event(
            event_id="e",
            claim_id="c",
            event_type="appointment",
            event_date=date(2025, 1, 1),
            attributes=ReturnToWorkAttributes(duty_type="modified"),
            extraction_method="llm",
        )


def test_discriminated_union_dispatch_from_json() -> None:
    raw = (
        '{"event_id":"x","claim_id":"c","event_type":"reserve_change",'
        '"event_date":"2025-05-01","attributes":'
        '{"type":"reserve_change","bucket":"Indemnity (2) Lost Time",'
        '"new_amount":"321014.00"},"extraction_method":"rule"}'
    )
    event = Event.model_validate_json(raw)
    assert isinstance(event.attributes, ReserveChangeAttributes)
    assert event.attributes.new_amount == Decimal("321014.00")
