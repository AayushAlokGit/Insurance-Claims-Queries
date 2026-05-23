"""Phase 9: Resolver tests.

Three concerns:
- provider canonicalization (the load-bearing primitive for
  appointment dedup)
- reserve_change delta derivation (Q3 exit criterion)
- appointment merge with status precedence and proximity
  window (Q2 exit criterion, Q4 partial)

End-to-end pinning against the live extractor output stays in
Phase 11's query-layer tests; here we focus on the resolver
algorithm against hand-constructed events.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from claims.models import (
    AppointmentAttributes,
    Event,
    ReserveChangeAttributes,
    ReturnToWorkAttributes,
)
from claims.resolver import (
    canonicalize_provider,
    resolve,
    resolve_appointments,
    resolve_reserve_changes,
    resolve_rtw,
)


# --- canonicalize_provider ---------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Dr. Harmon", "harmon"),
        ("Dr Harmon", "harmon"),
        ("Harmon, MD", "harmon"),
        ("Dr. Caldwell, MD", "caldwell"),
        ("Vega", "vega"),
        ("Bridge Functional Capacity Evaluators",
         "bridge functional capacity evaluators"),
        ("ATI Physical Therapy", "ati physical therapy"),
        (None, None),
        ("   ", None),
    ],
)
def test_canonicalize_provider_table(
    raw: str | None, expected: str | None
) -> None:
    assert canonicalize_provider(raw) == expected


def test_same_provider_different_forms_canonicalize_equal() -> None:
    forms = ["Dr. Harmon", "Harmon", "Dr Harmon, MD", "  Dr. Harmon  "]
    canonicals = {canonicalize_provider(f) for f in forms}
    assert canonicals == {"harmon"}


# --- reserve_change ---------------------------------------------


def _reserve_event(
    bucket: str, amount: Decimal, day: int, eid: str | None = None
) -> Event:
    return Event(
        event_id=eid or f"e-{bucket}-{day}",
        claim_id="C",
        event_type="reserve_change",
        event_date=date(2025, 5, day),
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


# --- appointments -----------------------------------------------


def _appt(
    *,
    provider: str | None,
    occurred_on: date | None = None,
    scheduled_for_date: date | None = None,
    scheduled_notice_date: date | None = None,
    status: str = "scheduled",
    eid: str = "e",
    method: str = "rule",
) -> Event:
    anchor = occurred_on or scheduled_for_date or date(2025, 1, 1)
    return Event(
        event_id=eid,
        claim_id="C",
        event_type="appointment",
        event_date=anchor,
        attributes=AppointmentAttributes(
            provider=provider,
            occurred_on=occurred_on,
            scheduled_for_date=scheduled_for_date,
            scheduled_notice_date=scheduled_notice_date,
            status=status,  # type: ignore[arg-type]
        ),
        extraction_method=method,  # type: ignore[arg-type]
    )


def test_marker_unknown_and_llm_attended_merge_to_attended() -> None:
    """The integration the design doc cares about: Marker emits a
    past visit with status=unknown; LLM emits the same visit with
    status=attended. They merge and the result is attended."""
    marker = _appt(
        provider="Dr. Harmon",
        occurred_on=date(2025, 11, 14),
        status="unknown",
        eid="marker",
    )
    llm = _appt(
        provider="Dr. Harmon",
        occurred_on=date(2025, 11, 14),
        status="attended",
        eid="llm",
        method="llm",
    )
    [merged] = resolve_appointments([marker, llm])
    assert isinstance(merged.attributes, AppointmentAttributes)
    assert merged.attributes.status == "attended"
    assert merged.extraction_method == "merged"
    assert merged.attributes.occurred_on == date(2025, 11, 14)


def test_status_precedence_attended_beats_scheduled() -> None:
    a = _appt(
        provider="Dr. X",
        scheduled_for_date=date(2025, 6, 1),
        status="scheduled",
        eid="a",
    )
    b = _appt(
        provider="Dr. X",
        occurred_on=date(2025, 6, 2),
        status="attended",
        eid="b",
    )
    [merged] = resolve_appointments([a, b])
    assert isinstance(merged.attributes, AppointmentAttributes)
    assert merged.attributes.status == "attended"


def test_missed_beats_scheduled_but_not_attended() -> None:
    a = _appt(
        provider="Dr. X",
        scheduled_for_date=date(2025, 6, 1),
        status="missed",
        eid="a",
    )
    b = _appt(
        provider="Dr. X",
        occurred_on=date(2025, 6, 1),
        status="attended",
        eid="b",
    )
    [merged] = resolve_appointments([a, b])
    assert isinstance(merged.attributes, AppointmentAttributes)
    assert merged.attributes.status == "attended"


def test_different_providers_do_not_merge() -> None:
    a = _appt(
        provider="Dr. Harmon",
        occurred_on=date(2025, 6, 1),
        status="attended",
        eid="a",
    )
    b = _appt(
        provider="Dr. Vega",
        occurred_on=date(2025, 6, 1),
        status="attended",
        eid="b",
    )
    assert len(resolve_appointments([a, b])) == 2


def test_outside_window_does_not_merge() -> None:
    a = _appt(
        provider="Dr. Harmon",
        occurred_on=date(2025, 6, 1),
        status="attended",
        eid="a",
    )
    b = _appt(
        provider="Dr. Harmon",
        occurred_on=date(2025, 6, 20),  # 19 days later — separate visit
        status="attended",
        eid="b",
    )
    assert len(resolve_appointments([a, b])) == 2


def test_within_window_merges() -> None:
    a = _appt(
        provider="Dr. Harmon",
        scheduled_for_date=date(2025, 6, 1),
        status="scheduled",
        eid="a",
    )
    b = _appt(
        provider="Dr. Harmon",
        occurred_on=date(2025, 6, 4),  # 3 days drift, well inside window
        status="attended",
        eid="b",
    )
    [merged] = resolve_appointments([a, b])
    assert isinstance(merged.attributes, AppointmentAttributes)
    assert merged.attributes.status == "attended"
    assert merged.attributes.scheduled_for_date == date(2025, 6, 1)
    assert merged.attributes.occurred_on == date(2025, 6, 4)


def test_latest_scheduled_notice_date_wins() -> None:
    """Q4 §5: reschedules — the most-recent notice anchors the
    Q4 gap math; earlier notices stay in history but don't feed."""
    first_notice = _appt(
        provider="Dr. X",
        scheduled_for_date=date(2025, 6, 5),
        scheduled_notice_date=date(2025, 5, 1),
        status="scheduled",
        eid="a",
    )
    reschedule_notice = _appt(
        provider="Dr. X",
        scheduled_for_date=date(2025, 6, 5),
        scheduled_notice_date=date(2025, 5, 20),
        status="scheduled",
        eid="b",
    )
    [merged] = resolve_appointments([first_notice, reschedule_notice])
    assert isinstance(merged.attributes, AppointmentAttributes)
    assert merged.attributes.scheduled_notice_date == date(2025, 5, 20)


def test_longer_provider_form_preferred() -> None:
    short = _appt(
        provider="Harmon",
        occurred_on=date(2025, 6, 1),
        status="attended",
        eid="a",
    )
    long = _appt(
        provider="Dr. Harmon, MD",
        occurred_on=date(2025, 6, 1),
        status="attended",
        eid="b",
    )
    [merged] = resolve_appointments([short, long])
    assert isinstance(merged.attributes, AppointmentAttributes)
    assert merged.attributes.provider == "Dr. Harmon, MD"


def test_single_event_passes_through_unchanged() -> None:
    [ev] = resolve_appointments(
        [
            _appt(
                provider="Dr. X",
                occurred_on=date(2025, 1, 1),
                status="attended",
                eid="solo",
            )
        ]
    )
    assert ev.event_id == "solo"  # not re-uuid'd
    assert ev.extraction_method == "rule"


# --- return_to_work ---------------------------------------------


def _rtw_event(
    return_date: date,
    duty_type: str = "modified",
    role: str | None = None,
    eid: str = "e",
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
    """Same claim, same date, same duty_type → one merged event.
    This catches the future-dated-confirmation + later-summary
    pattern observed in the corpus."""
    a = _rtw_event(date(2025, 11, 10), role="scheduling coordinator", eid="a")
    b = _rtw_event(date(2025, 11, 10), role="scheduling coordinator", eid="b")
    [merged] = resolve_rtw([a, b])
    assert merged.event_date == date(2025, 11, 10)
    assert merged.extraction_method == "merged"


def test_rtw_different_dates_do_not_merge() -> None:
    """A modified return on 11/10 and a full return on 1/5 are
    distinct events; both should survive."""
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
    events = [
        _reserve_event("Indemnity (2) Lost Time", Decimal("100"), 1),
        _appt(
            provider="Dr. X",
            occurred_on=date(2025, 6, 1),
            status="attended",
            eid="a",
        ),
    ]
    resolved = resolve(events)
    by_type = {e.event_type for e in resolved}
    assert by_type == {"reserve_change", "appointment"}
