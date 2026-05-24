"""Return-to-work event resolution. See resolver.md §3 Pass 2-3.

Match key: same `(claim_id, duty_type)` and `event_date` within
`_DATE_WINDOW_DAYS` of each other. Two notes describing the same
return collapse even when their reported dates drift slightly —
the common case is an "approximately N days ago" recap doing
date math from a later note and landing a day or two off the
canonical "EE returned on DATE" testimony.

Greedy single-pass clustering by event_date order: each cluster's
survivor is the **earliest** event_date in the group (matches
the Q1 query precedence of `ORDER BY event_date LIMIT 1`).
Role is preserved by longest-form-wins."""

from __future__ import annotations

from datetime import date

from claims.models import Event, EventEvidence, ReturnToWorkAttributes

_DATE_WINDOW_DAYS = 7


def _merge(events: list[Event]) -> Event:
    survivor = events[0]
    base = survivor.attributes
    assert isinstance(base, ReturnToWorkAttributes)
    role = base.role
    evidence_set: set[tuple[date, str]] = {
        (e.note_date, e.quote) for e in base.evidence
    }
    for ev in events[1:]:
        attrs = ev.attributes
        assert isinstance(attrs, ReturnToWorkAttributes)
        if attrs.role and (role is None or len(attrs.role) > len(role)):
            role = attrs.role
        evidence_set.update((e.note_date, e.quote) for e in attrs.evidence)
    if len(events) == 1:
        return survivor
    merged_evidence = tuple(
        EventEvidence(note_date=nd, quote=q)
        for nd, q in sorted(evidence_set, key=lambda p: (p[0], p[1]))
    )
    return survivor.model_copy(
        update={
            "attributes": base.model_copy(
                update={
                    "role": role,
                    "evidence": merged_evidence,
                }
            ),
            "extraction_method": "merged",
        }
    )


def _cluster(events: list[Event]) -> list[list[Event]]:
    """Greedy ±_DATE_WINDOW_DAYS clustering on event_date.
    Events are assumed pre-sorted by event_date ascending. The
    cluster's anchor is the earliest event_date in it; later
    events join if within the window of that anchor."""
    clusters: list[list[Event]] = []
    for ev in events:
        if clusters:
            anchor = clusters[-1][0].event_date
            if abs((ev.event_date - anchor).days) <= _DATE_WINDOW_DAYS:
                clusters[-1].append(ev)
                continue
        clusters.append([ev])
    return clusters


def resolve_rtw(events: list[Event]) -> list[Event]:
    """Window-merge within `(claim_id, duty_type)` on event_date.
    One event per distinct return; the Q1 query function decides
    'returned vs. pending' on top of this."""
    by_partition: dict[tuple[str, str], list[Event]] = {}
    for ev in events:
        attrs = ev.attributes
        if not isinstance(attrs, ReturnToWorkAttributes):
            continue
        key = (ev.claim_id, attrs.duty_type)
        by_partition.setdefault(key, []).append(ev)

    out: list[Event] = []
    for group in by_partition.values():
        group.sort(key=lambda e: e.event_date)
        for cluster in _cluster(group):
            out.append(_merge(cluster))
    return out
