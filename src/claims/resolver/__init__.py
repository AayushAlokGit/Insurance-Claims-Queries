"""Resolver stage. See docs/resolver.md.

Top-level entry: `resolve(events) -> list[Event]`. Dispatches
per event_type; merges duplicates; derives cross-event fields.
"""

from claims.resolver.appointments import resolve_appointments
from claims.resolver.parties import normalize_party, parties_overlap
from claims.resolver.reserve_change import resolve_reserve_changes
from claims.resolver.resolver import resolve
from claims.resolver.rtw import resolve_rtw

__all__ = [
    "normalize_party",
    "parties_overlap",
    "resolve",
    "resolve_appointments",
    "resolve_reserve_changes",
    "resolve_rtw",
]
