"""Run every registered extractor over a single note.

Two registry levels:
- `default_rule_extractors()` — rule-only extractors that need no
  LLM client (Reserve only under DD-019; Marker was retired when
  the LLM appointment extractor became the sole candidate source).
- `default_extractors(llm)` — full registry: Reserve + the three
  LLM extractors. Pass a StructuredLLM (real or fake).

`run_all` accepts an explicit extractors list. When none is
given, it falls back to the rule-only registry — so existing
callers that don't have an LLM client wired up keep working.
"""

from __future__ import annotations

import logging

from claims.extractor.appointment import AppointmentExtractor
from claims.extractor.base import Extractor
from claims.extractor.reserve_change import ReserveChangeExtractor
from claims.extractor.rtw import ReturnToWorkExtractor
from claims.extractor.rtw_terminal import RtwTerminalExtractor
from claims.llm import StructuredLLM
from claims.models import AppointmentAttributes, Event, Note

_log = logging.getLogger(__name__)


def _summarize(ev: Event) -> str:
    """One-line audit summary of an emitted event. Designed to be
    greppable by date / party when diagnosing why a given
    appointment lands with the wrong status or never appears."""
    attrs = ev.attributes
    if isinstance(attrs, AppointmentAttributes):
        enc = attrs.scheduled_for_date or attrs.occurred_on or ev.event_date
        kind = (
            "occurred" if attrs.occurred_on
            else "scheduled" if attrs.scheduled_for_date
            else "?"
        )
        parties = "|".join(attrs.parties) or "-"
        quote = (attrs.evidence_quote or "").replace("\n", " ")[:80]
        return (
            f"appointment date={enc} kind={kind} status={attrs.status} "
            f"parties=[{parties}] evidence=\"{quote}\""
        )
    quote = (getattr(attrs, "evidence_quote", "") or "").replace("\n", " ")[:80]
    return f"{ev.event_type} date={ev.event_date} evidence=\"{quote}\""


def default_rule_extractors() -> list[Extractor]:
    return [
        ReserveChangeExtractor(),
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
            for ev in produced:
                _log.info(
                    "extract note=%s note_date=%s by=%s :: %s",
                    note.note_id,
                    note.note_date,
                    type(ext).__name__,
                    _summarize(ev),
                )
        events.extend(produced)
    return events
