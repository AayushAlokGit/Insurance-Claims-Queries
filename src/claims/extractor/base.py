"""The Extractor protocol. See extractor.md §4."""

from __future__ import annotations

from typing import Protocol

from claims.models import Event, Note


class Extractor(Protocol):
    """One per event type (or sub-strategy for an event type).

    `can_handle` is the cheap cost gate — for LLM extractors,
    failing this avoids the API call entirely. `extract` returns
    a list because a single note can describe multiple events of
    the same type, and an empty list is a first-class result
    (not an error)."""

    event_type: str

    def can_handle(self, note: Note) -> bool: ...

    def extract(self, note: Note) -> list[Event]: ...
