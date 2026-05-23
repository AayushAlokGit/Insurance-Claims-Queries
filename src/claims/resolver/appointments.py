"""Appointment resolution. See resolver.md §3 Pass 2-3 + §6.

Two events merge if they describe the same encounter — same
claim, same canonical provider, anchor dates within a small
window. The Marker extractor's status="unknown" past visits get
promoted to "attended"/"missed"/"cancelled" when an LLM-emitted
event in the same window provides that evidence.

Status precedence (resolver.md §3, Pass 3):
    attended > missed > cancelled > scheduled > unknown
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

from claims.models import AppointmentAttributes, AppointmentStatus, Event
from claims.resolver.provider import canonicalize_provider

# Q2 dedup window. Q4 schedule-to-seen wants N=14 (the design
# doc's "two strategies registered" point); we'll register that
# strategy separately once Q4's query layer needs it.
_DEFAULT_WINDOW_DAYS = 7

_STATUS_RANK: dict[AppointmentStatus, int] = {
    "attended": 4,
    "missed": 3,
    "cancelled": 2,
    "scheduled": 1,
    "unknown": 0,
}


def _anchor_date(attrs: AppointmentAttributes) -> date | None:
    """resolver.md §3: anchor = scheduled_for_date ?? occurred_on."""
    return attrs.scheduled_for_date or attrs.occurred_on


def _should_merge(a: Event, b: Event, window_days: int) -> bool:
    if a.claim_id != b.claim_id:
        return False
    a_attrs = a.attributes
    b_attrs = b.attributes
    assert isinstance(a_attrs, AppointmentAttributes)
    assert isinstance(b_attrs, AppointmentAttributes)
    if canonicalize_provider(a_attrs.provider) != canonicalize_provider(
        b_attrs.provider
    ):
        return False
    a_anchor = _anchor_date(a_attrs)
    b_anchor = _anchor_date(b_attrs)
    if a_anchor is None or b_anchor is None:
        # Without an anchor we can't tell — fall back to claim+provider
        # equality. Conservative; could be tightened later.
        return True
    return abs((a_anchor - b_anchor).days) <= window_days


def _stronger_status(
    a: AppointmentStatus, b: AppointmentStatus
) -> AppointmentStatus:
    return a if _STATUS_RANK[a] >= _STATUS_RANK[b] else b


def _merge(events: list[Event]) -> Event:
    """Combine a group of events that all describe the same
    appointment. Status follows precedence; date fields union;
    provider takes the longest available form."""
    first = events[0]
    base_attrs = first.attributes
    assert isinstance(base_attrs, AppointmentAttributes)

    status: AppointmentStatus = base_attrs.status
    occurred_on = base_attrs.occurred_on
    scheduled_for_date = base_attrs.scheduled_for_date
    scheduled_notice_date = base_attrs.scheduled_notice_date
    provider = base_attrs.provider
    specialty = base_attrs.specialty
    appointment_type = base_attrs.appointment_type

    for ev in events[1:]:
        attrs = ev.attributes
        assert isinstance(attrs, AppointmentAttributes)
        status = _stronger_status(status, attrs.status)
        occurred_on = occurred_on or attrs.occurred_on
        scheduled_for_date = (
            scheduled_for_date or attrs.scheduled_for_date
        )
        # Latest scheduled_notice_date wins (reschedules — Q4 §5).
        if attrs.scheduled_notice_date is not None and (
            scheduled_notice_date is None
            or attrs.scheduled_notice_date > scheduled_notice_date
        ):
            scheduled_notice_date = attrs.scheduled_notice_date
        # Most specific provider form (longest string).
        if attrs.provider and (
            provider is None or len(attrs.provider) > len(provider)
        ):
            provider = attrs.provider
        specialty = specialty or attrs.specialty
        appointment_type = appointment_type or attrs.appointment_type

    merged_attrs = AppointmentAttributes(
        provider=provider,
        specialty=specialty,
        scheduled_notice_date=scheduled_notice_date,
        scheduled_for_date=scheduled_for_date,
        occurred_on=occurred_on,
        status=status,
        appointment_type=appointment_type,
    )
    # event_date follows the storage convention from data-modeling
    # §4.2: occurred_on if set, else scheduled_for_date.
    event_date = (
        merged_attrs.occurred_on
        or merged_attrs.scheduled_for_date
        or first.event_date
    )

    return Event(
        event_id=str(uuid.uuid4()) if len(events) > 1 else first.event_id,
        claim_id=first.claim_id,
        event_type="appointment",
        event_date=event_date,
        attributes=merged_attrs,
        extraction_method=(
            "merged" if len(events) > 1 else first.extraction_method
        ),
    )


def resolve_appointments(
    events: list[Event], *, window_days: int = _DEFAULT_WINDOW_DAYS
) -> list[Event]:
    """Group appointment events by (claim, canonical_provider,
    anchor_date ± window_days) and merge each group. Quadratic
    in group size; fine for per-claim batches in this corpus.
    """
    by_claim: dict[str, list[Event]] = {}
    for ev in events:
        if not isinstance(ev.attributes, AppointmentAttributes):
            continue
        by_claim.setdefault(ev.claim_id, []).append(ev)

    out: list[Event] = []
    for claim_events in by_claim.values():
        # Sort by anchor so the earliest seeds each group.
        claim_events.sort(
            key=lambda e: (
                _anchor_date(e.attributes)  # type: ignore[arg-type]
                or e.event_date,
                e.event_id,
            )
        )
        clusters: list[list[Event]] = []
        for ev in claim_events:
            placed = False
            for cluster in clusters:
                if _should_merge(cluster[0], ev, window_days):
                    cluster.append(ev)
                    placed = True
                    break
            if not placed:
                clusters.append([ev])
        for cluster in clusters:
            out.append(_merge(cluster))
    return out
