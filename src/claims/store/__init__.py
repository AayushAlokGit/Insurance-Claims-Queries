"""SQLite persistence layer.

Two-table schema (claim, event) with JSON `attributes`. See
data-modeling.md §1-§5 for the rationale.
"""

from claims.store.db import connect
from claims.store.repo import (
    get_claim,
    get_events_for_claim,
    insert_claim,
    insert_event,
    sum_reserve_deltas_per_claim,
)
from claims.store.schema import create_schema

__all__ = [
    "connect",
    "create_schema",
    "get_claim",
    "get_events_for_claim",
    "insert_claim",
    "insert_event",
    "sum_reserve_deltas_per_claim",
]
