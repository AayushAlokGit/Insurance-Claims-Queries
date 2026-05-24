"""Resolver tests (post-DD-019).

Two concerns:
- reserve_change delta derivation (Q3 exit criterion)
- return_to_work identity-merge

Appointment merge tests were retired with DD-019 — appointments
now go through `claims.extractor.reconciliation`, not the
resolver. End-to-end appointment behavior is exercised by the
reconciliation tests + the ingest smoke runs.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from claims.models import (
    AppointmentAttributes,
    Event,
    ReserveChangeAttributes,
    ReturnToWorkAttributes,
)
from claims.resolver import (
    resolve,
    resolve_reserve_changes,
    resolve_rtw,
)


# --- reserve_change ---------------------------------------------


def _reserve_event(
    bucket: str, amount: Decimal, day: int, *, eid: str | None = None
) -> Event:
    return Event(
        event_id=eid or f"r{day}",
        claim_id="C",
        event_type="reserve_change",
        event_date=date(2025, 1, day),
        attributes=ReserveChangeAttributes(
            bucket=bucket, new_amount=amount
        ),
        extraction_method="rule",
    )


def test_first_reserve_set_has_previous_zero() -> None:
    [out] = resolve_reserve_changes(
        [_reserve_event("Indemnity (2) Lost Time", Decimal("280000"), 1)]
    )
    assert isinstance(out.attributes, ReserveChangeAttributes)
    assert out.attributes.previous_amount == Decimal("0")
    assert out.attributes.delta == Decimal("280000")


def test_sequential_reserves_compute_delta() -> None:
    events = [
        _reserve_event("Indemnity (2) Lost Time", Decimal("280000"), 1),
        _reserve_event("Indemnity (2) Lost Time", Decimal("321014"), 5),
        _reserve_event("Indemnity (2) Lost Time", Decimal("250000"), 10),
    ]
    resolved = resolve_reserve_changes(events)
    deltas = [
        a.delta
        for a in (r.attributes for r in resolved)
        if isinstance(a, ReserveChangeAttributes)
    ]
    assert deltas == [
        Decimal("280000"),  # first set: previous=0
        Decimal("41014"),
        Decimal("-71014"),
    ]


def test_delta_zero_restatement_is_dropped() -> None:
    """A reserve restated at the same value is NOT a change."""
    events = [
        _reserve_event("Indemnity (2) Lost Time", Decimal("280000"), 1),
        _reserve_event("Indemnity (2) Lost Time", Decimal("280000"), 5),
        _reserve_event("Indemnity (2) Lost Time", Decimal("321014"), 10),
    ]
    resolved = resolve_reserve_changes(events)
    assert len(resolved) == 2  # first set + the real change
    amounts = [
        a.new_amount
        for a in (r.attributes for r in resolved)
        if isinstance(a, ReserveChangeAttributes)
    ]
    assert amounts == [Decimal("280000"), Decimal("321014")]


def test_different_buckets_are_independent() -> None:
    events = [
        _reserve_event("Indemnity (1) Medical Details", Decimal("100"), 1),
        _reserve_event("Indemnity (2) Lost Time", Decimal("200"), 1),
        _reserve_event("Indemnity (1) Medical Details", Decimal("150"), 5),
    ]
    resolved = resolve_reserve_changes(events)
    by_bucket = {
        a.bucket: a.delta
        for a in (r.attributes for r in resolved)
        if isinstance(a, ReserveChangeAttributes)
        and a.bucket == "Indemnity (2) Lost Time"
    }
    assert by_bucket["Indemnity (2) Lost Time"] == Decimal("200")


# --- return_to_work ---------------------------------------------


def _rtw_event(
    return_date: date,
    duty_type: str = "modified",
    role: str | None = None,
    eid: str = "rtw",
) -> Event:
    return Event(
        event_id=eid,
        claim_id="C",
        event_type="return_to_work",
        event_date=return_date,
        attributes=ReturnToWorkAttributes(
            duty_type=duty_type,  # type: ignore[arg-type]
            role=role,
        ),
        extraction_method="llm",
    )


def test_rtw_identical_events_merge() -> None:
    a = _rtw_event(date(2025, 11, 10), role="scheduling coordinator", eid="a")
    b = _rtw_event(date(2025, 11, 10), role="scheduling coordinator", eid="b")
    [merged] = resolve_rtw([a, b])
    assert merged.event_date == date(2025, 11, 10)
    assert merged.extraction_method == "merged"


def test_rtw_different_dates_do_not_merge() -> None:
    a = _rtw_event(date(2025, 11, 10), "modified", eid="a")
    b = _rtw_event(date(2026, 1, 5), "full", eid="b")
    assert len(resolve_rtw([a, b])) == 2


def test_rtw_longer_role_form_preferred() -> None:
    a = _rtw_event(date(2025, 11, 10), "modified", role=None, eid="a")
    b = _rtw_event(
        date(2025, 11, 10),
        "modified",
        role="scheduling coordinator",
        eid="b",
    )
    [merged] = resolve_rtw([a, b])
    assert isinstance(merged.attributes, ReturnToWorkAttributes)
    assert merged.attributes.role == "scheduling coordinator"


# --- resolve() top-level ----------------------------------------


def test_resolve_dispatches_per_event_type() -> None:
    """Reserve and RTW each flow through their respective resolvers.
    Appointments do not reach `resolve()` under DD-019 (they're
    handled by reconciliation upstream); if one does, resolve()
    logs a warning and passes it through."""
    reserve_ev = _reserve_event(
        "Indemnity (2) Lost Time", Decimal("100"), 1
    )
    rtw_ev = _rtw_event(date(2025, 6, 1))
    resolved = resolve([reserve_ev, rtw_ev])
    by_type = {e.event_type for e in resolved}
    assert by_type == {"reserve_change", "return_to_work"}


def test_resolve_passes_stray_appointment_through_with_warning() -> None:
    """Defensive check: if an appointment event reaches resolve(),
    it gets passed through (and a warning logged) so we don't lose
    data on a pipeline misroute."""
    appt = Event(
        event_id="stray",
        claim_id="C",
        event_type="appointment",
        event_date=date(2025, 6, 1),
        attributes=AppointmentAttributes(
            status="attended",
            occurred_on=date(2025, 6, 1),
        ),
        extraction_method="llm",
    )
    out = resolve([appt])
    assert len(out) == 1
    assert out[0].event_id == "stray"
