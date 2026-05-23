"""Resolver stage. See docs/resolver.md.

Top-level entry: `resolve(events) -> list[Event]`. Dispatches
per event_type; merges duplicates; derives cross-event fields.
"""

from claims.resolver.appointments import resolve_appointments
from claims.resolver.provider import canonicalize_provider
from claims.resolver.reserve_change import resolve_reserve_changes
from claims.resolver.resolver import resolve

__all__ = [
    "canonicalize_provider",
    "resolve",
    "resolve_appointments",
    "resolve_reserve_changes",
]
