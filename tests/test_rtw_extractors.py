"""Phase 10: RTW extractor tests.

All offline — uses FakeLLMs returning pinned payloads. Covers
the discriminated-union empty case (null payload → no events),
evidence-quote safety net, prefilter gating, and the data shape
of the resulting Event."""

from __future__ import annotations

from datetime import date
from typing import TypeVar

from pydantic import BaseModel

from claims.extractor.rtw import (
    RTWExtractionResponse,
    ReturnToWorkExtractor,
    _ReturnToWorkPayload,
)
from claims.models import Note, ReturnToWorkAttributes

T = TypeVar("T", bound=BaseModel)


class _FakeLLM:
    model: str = "fake"

    def __init__(self, response: BaseModel) -> None:
        self._response = response
        self.call_count = 0

    def structured(
        self, *, system: str, user: str, response_model: type[T]
    ) -> T:
        self.call_count += 1
        return self._response  # type: ignore[return-value]


def _note(body: str, note_date: date = date(2025, 11, 12)) -> Note:
    return Note(
        note_id="TEST-n0000",
        claim_id="TEST",
        note_date=note_date,
        activity="Resolution Strategy",
        author="M.H.",
        body=body,
        data_quality_flags=[],
    )


# --- RTW extractor ----------------------------------------------


def test_rtw_prefilter_signals() -> None:
    e = ReturnToWorkExtractor(_FakeLLM(RTWExtractionResponse(rtw=None)))
    assert e.can_handle(_note("EE returned to modified duty on 11/10"))
    assert e.can_handle(_note("released to full duty"))
    assert e.can_handle(_note("RTW confirmed effective DATE"))
    assert not e.can_handle(_note("Pure unrelated prose."))


def test_rtw_positive_emits_one_event() -> None:
    note = _note(
        "EE returned to modified duty on 11/10/25 as a scheduling coordinator."
    )
    llm = _FakeLLM(
        RTWExtractionResponse(
            rtw=_ReturnToWorkPayload(
                return_date=date(2025, 11, 10),
                duty_type="modified",
                role="scheduling coordinator",
                evidence_quote="EE returned to modified duty on 11/10/25 as a scheduling coordinator.",
            )
        )
    )
    ext = ReturnToWorkExtractor(llm)
    [event] = ext.extract(note)
    assert event.event_type == "return_to_work"
    assert event.event_date == date(2025, 11, 10)
    attrs = event.attributes
    assert isinstance(attrs, ReturnToWorkAttributes)
    assert attrs.duty_type == "modified"
    assert attrs.role == "scheduling coordinator"
    assert event.extraction_method == "llm"


def test_rtw_null_payload_yields_no_events() -> None:
    """Discriminated-union empty case."""
    note = _note("EE declined the modified duty offer.")
    llm = _FakeLLM(RTWExtractionResponse(rtw=None))
    assert ReturnToWorkExtractor(llm).extract(note) == []


def test_rtw_bad_evidence_quote_dropped() -> None:
    note = _note("EE returned to modified duty on 11/10/25.")
    llm = _FakeLLM(
        RTWExtractionResponse(
            rtw=_ReturnToWorkPayload(
                return_date=date(2025, 11, 10),
                duty_type="modified",
                role=None,
                evidence_quote="A quote that is not in the body.",
            )
        )
    )
    assert ReturnToWorkExtractor(llm).extract(note) == []
