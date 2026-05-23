"""Run every registered extractor over a single note.

Two registry levels:
- `default_rule_extractors()` — rule-only extractors that need no
  LLM client (Reserve, Marker). Cheap; safe to call from any
  test.
- `default_extractors(llm)` — full registry including the LLM
  appointment extractor. Pass a StructuredLLM (real or fake).

`run_all` accepts an explicit extractors list. When none is
given, it falls back to the rule-only registry — so existing
callers that don't have an LLM client wired up keep working.
"""

from __future__ import annotations

import logging

from claims.extractor.appointment import AppointmentExtractor
from claims.extractor.appointment_marker import AppointmentMarkerExtractor
from claims.extractor.base import Extractor
from claims.extractor.reserve_change import ReserveChangeExtractor
from claims.extractor.rtw import ReturnToWorkExtractor
from claims.extractor.rtw_terminal import RtwTerminalExtractor
from claims.llm import StructuredLLM
from claims.models import Event, Note

_log = logging.getLogger(__name__)


def default_rule_extractors() -> list[Extractor]:
    return [
        ReserveChangeExtractor(),
        AppointmentMarkerExtractor(),
    ]


def default_extractors(llm: StructuredLLM) -> list[Extractor]:
    return [
        *default_rule_extractors(),
        AppointmentExtractor(llm),
        ReturnToWorkExtractor(llm),
        RtwTerminalExtractor(llm),
    ]


def run_all(
    note: Note, *, extractors: list[Extractor] | None = None
) -> list[Event]:
    chosen = extractors if extractors is not None else default_rule_extractors()
    events: list[Event] = []
    for ext in chosen:
        if not ext.can_handle(note):
            continue
        produced = ext.extract(note)
        if produced:
            _log.debug(
                "%s emitted %d event(s) for note %s",
                type(ext).__name__,
                len(produced),
                note.note_id,
            )
        events.extend(produced)
    return events
