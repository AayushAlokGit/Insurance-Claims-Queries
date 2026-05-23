"""Return-to-work event resolution. See resolver.md §3 Pass 2-3.

Match key is identity over (claim_id, event_date, duty_type) —
two notes describing the same return collapse to one event.
This catches the common pattern of (a) a forward-dated confirmation
note and (b) a later note that summarizes the same return after
the fact: per-note extractors emit both with identical return
fields, the Resolver merges them.

Role is preserved by longest-form-wins (keeps "scheduling
coordinator" over an empty role)."""

from __future__ import annotations

from claims.models import Event, ReturnToWorkAttributes


def _merge(events: list[Event]) -> Event:
    first = events[0]
    base = first.attributes
    assert isinstance(base, ReturnToWorkAttributes)
    role = base.role
    for ev in events[1:]:
        attrs = ev.attributes
        assert isinstance(attrs, ReturnToWorkAttributes)
        if attrs.role and (role is None or len(attrs.role) > len(role)):
            role = attrs.role
    if len(events) == 1:
        return first
    return first.model_copy(
        update={
            "attributes": base.model_copy(update={"role": role}),
            "extraction_method": "merged",
        }
    )


def resolve_rtw(events: list[Event]) -> list[Event]:
    """Identity-merge by (claim_id, event_date, duty_type). One
    event per distinct return; the Q1 query function decides
    'returned vs. pending' on top of this."""
    groups: dict[tuple[str, str, str], list[Event]] = {}
    for ev in events:
        attrs = ev.attributes
        if not isinstance(attrs, ReturnToWorkAttributes):
            continue
        key = (ev.claim_id, ev.event_date.isoformat(), attrs.duty_type)
        groups.setdefault(key, []).append(ev)
    return [_merge(group) for group in groups.values()]
