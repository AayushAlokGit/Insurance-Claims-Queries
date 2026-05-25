"""Resolver stage. See docs/resolver.md.

Top-level entry: `resolve(events) -> list[Event]`. Dispatches
per event_type; merges duplicates; derives cross-event fields.

Appointments do not flow through the resolver under DD-019 —
they are handled by per-claim reconciliation (see
`claims.extractor.reconciliation`). The resolver still handles
reserve_change and return_to_work.
"""

from claims.resolver.reserve_change import resolve_reserve_changes
from claims.resolver.resolver import resolve
from claims.resolver.rtw import resolve_rtw

__all__ = [
    "resolve",
    "resolve_reserve_changes",
    "resolve_rtw",
]
