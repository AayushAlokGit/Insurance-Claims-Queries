"""Reserve-change resolution. See resolver.md §3 Pass 4 + Q3 §6.

Within each (claim_id, bucket), order by event_date and compute
delta = new_amount - previous_amount. The first event in a
bucket has previous = 0 (Q3 §5 first-set convention).

Events with delta = 0 are DROPPED — restatements of an unchanged
reserve aren't a "change", and counting them would inflate Q3."""

from __future__ import annotations

from decimal import Decimal

from claims.models import Event, ReserveChangeAttributes


def resolve_reserve_changes(events: list[Event]) -> list[Event]:
    """Compute previous_amount + delta for each reserve_change
    event, ordered within its (claim_id, bucket). Returns a new
    list with delta=0 entries omitted."""
    groups: dict[tuple[str, str], list[Event]] = {}
    for ev in events:
        if not isinstance(ev.attributes, ReserveChangeAttributes):
            continue
        key = (ev.claim_id, ev.attributes.bucket)
        groups.setdefault(key, []).append(ev)

    out: list[Event] = []
    for group in groups.values():
        group.sort(key=lambda e: (e.event_date, e.event_id))
        previous = Decimal("0")
        for ev in group:
            attrs = ev.attributes
            assert isinstance(attrs, ReserveChangeAttributes)
            delta = attrs.new_amount - previous
            if delta == 0:
                # Restatement of the current value; not a change.
                previous = attrs.new_amount
                continue
            out.append(
                ev.model_copy(
                    update={
                        "attributes": attrs.model_copy(
                            update={
                                "previous_amount": previous,
                                "delta": delta,
                            }
                        ),
                    }
                )
            )
            previous = attrs.new_amount
    return out
