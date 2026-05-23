"""Top-level resolver: dispatches per-event-type, glues passes.

See resolver.md §3. MVP coverage:
- reserve_change: cross-note delta derivation, drop delta=0
- appointment: dedup + status promotion within a date window
- return_to_work / rtw_terminal: pass-through (Phase 10 fills these)
"""

from __future__ import annotations

from claims.models import Event
from claims.resolver.appointments import resolve_appointments
from claims.resolver.reserve_change import resolve_reserve_changes
from claims.resolver.rtw import resolve_rtw


def resolve(events: list[Event]) -> list[Event]:
    by_type: dict[str, list[Event]] = {}
    for ev in events:
        by_type.setdefault(ev.event_type, []).append(ev)

    out: list[Event] = []
    out.extend(resolve_reserve_changes(by_type.get("reserve_change", [])))
    out.extend(resolve_appointments(by_type.get("appointment", [])))
    out.extend(resolve_rtw(by_type.get("return_to_work", [])))
    out.extend(by_type.get("rtw_terminal", []))
    return out
