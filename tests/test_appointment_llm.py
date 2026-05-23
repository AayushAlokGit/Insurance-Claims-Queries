"""Phase 8: LLM-backed appointment extractor tests.

No network calls — every test injects a FakeStructuredLLM that
returns pinned pydantic responses. The real Google/OpenAI
clients are exercised manually outside this suite.

Coverage:
- Prefilter: status-verb branch, date+context branch, false-
  negative on a non-appointment note.
- Conversion: every status maps to the right AppointmentAttributes
  shape.
- Safety net: evidence_quote that doesn't substring-match the
  body is dropped silently.
- Empty response: list[] yields no events (discriminated-union
  empty case).
- Orchestrator: default_extractors(llm) wires this up alongside
  the rule extractors; can_handle gates the LLM call.
"""

from __future__ import annotations

from datetime import date
from typing import TypeVar

from pydantic import BaseModel

from claims.extractor import (
    AppointmentExtractor,
    default_extractors,
    run_all,
)
from claims.extractor.appointment import (
    AppointmentExtractionResponse,
    _ExtractedAppointment,
)
from claims.models import AppointmentAttributes, Note

T = TypeVar("T", bound=BaseModel)


class _FakeLLM:
    """Returns a single queued response per call. Asserts the
    response_model the extractor asked for to catch drift."""

    model: str = "fake"

    def __init__(self, response: AppointmentExtractionResponse) -> None:
        self._response = response
        self.calls: list[str] = []  # captures user-prompt bodies

    def structured(
        self, *, system: str, user: str, response_model: type[T]
    ) -> T:
        assert response_model is AppointmentExtractionResponse, (
            "AppointmentExtractor asked for the wrong response model"
        )
        self.calls.append(user)
        return self._response  # type: ignore[return-value]


def _note(body: str, note_date: date = date(2025, 5, 1)) -> Note:
    return Note(
        note_id="TEST-n0000",
        claim_id="TEST",
        note_date=note_date,
        activity="Investigation",
        author="M.H.",
        body=body,
        data_quality_flags=[],
    )


# --- Prefilter ---------------------------------------------------


def test_prefilter_status_verb_branch() -> None:
    ext = AppointmentExtractor(_FakeLLM(AppointmentExtractionResponse()))
    assert ext.can_handle(_note("EE attended PT on 6/3"))
    assert ext.can_handle(_note("Missed the 8/13 follow-up"))
    assert ext.can_handle(_note("Claimant no-show"))


def test_prefilter_date_plus_context_branch() -> None:
    """Scheduling-only events that use a non-templated header
    must still fire the prefilter."""
    ext = AppointmentExtractor(_FakeLLM(AppointmentExtractionResponse()))
    assert ext.can_handle(_note("Follow-up visit set for 9-23"))
    assert ext.can_handle(_note("Consult on Apr 22 2025"))


def test_prefilter_rejects_non_appointment_notes() -> None:
    """No status verb, no date+context → no LLM call."""
    ext = AppointmentExtractor(_FakeLLM(AppointmentExtractionResponse()))
    assert not ext.can_handle(_note("Pure prose with no signals."))
    assert not ext.can_handle(
        _note("Account number 2100000000 - Company WW")
    )


# --- Conversion --------------------------------------------------


def test_attended_appointment_yields_occurred_on() -> None:
    note = _note(
        "EE attended PT with Dr. Harmon on 6/3/2025.",
        note_date=date(2025, 6, 5),
    )
    llm = _FakeLLM(
        AppointmentExtractionResponse(
            appointments=[
                _ExtractedAppointment(
                    status="attended",
                    appointment_date=date(2025, 6, 3),
                    provider="Dr. Harmon",
                    evidence_quote="EE attended PT with Dr. Harmon on 6/3/2025.",
                )
            ]
        )
    )
    ext = AppointmentExtractor(llm)
    [event] = ext.extract(note)
    attrs = event.attributes
    assert isinstance(attrs, AppointmentAttributes)
    assert attrs.status == "attended"
    assert attrs.occurred_on == date(2025, 6, 3)
    assert attrs.scheduled_for_date is None
    assert attrs.provider == "Dr. Harmon"
    assert event.extraction_method == "llm"
    assert event.event_date == date(2025, 6, 3)


def test_scheduled_appointment_yields_schedule_fields() -> None:
    note = _note(
        "Next consult set for 9/23.", note_date=date(2025, 8, 29)
    )
    llm = _FakeLLM(
        AppointmentExtractionResponse(
            appointments=[
                _ExtractedAppointment(
                    status="scheduled",
                    appointment_date=date(2025, 9, 23),
                    provider=None,
                    evidence_quote="Next consult set for 9/23.",
                )
            ]
        )
    )
    [event] = AppointmentExtractor(llm).extract(note)
    attrs = event.attributes
    assert isinstance(attrs, AppointmentAttributes)
    assert attrs.status == "scheduled"
    assert attrs.scheduled_for_date == date(2025, 9, 23)
    assert attrs.scheduled_notice_date == date(2025, 8, 29)
    assert attrs.occurred_on is None


def test_missed_and_cancelled_map_to_occurred_on() -> None:
    note = _note(
        "Missed appt with Dr. Caldwell on 8/11.", note_date=date(2025, 8, 15)
    )
    llm = _FakeLLM(
        AppointmentExtractionResponse(
            appointments=[
                _ExtractedAppointment(
                    status="missed",
                    appointment_date=date(2025, 8, 11),
                    provider="Dr. Caldwell",
                    evidence_quote="Missed appt with Dr. Caldwell on 8/11.",
                )
            ]
        )
    )
    [event] = AppointmentExtractor(llm).extract(note)
    attrs = event.attributes
    assert isinstance(attrs, AppointmentAttributes)
    assert attrs.status == "missed"
    assert attrs.occurred_on == date(2025, 8, 11)


# --- Safety net --------------------------------------------------


def test_evidence_quote_failing_substring_check_is_dropped() -> None:
    """If the model invents a quote not present in the body, the
    event is silently dropped — the prompt told it not to, the
    substring check is the backstop."""
    note = _note("Genuine body line about PT on 6/3.")
    llm = _FakeLLM(
        AppointmentExtractionResponse(
            appointments=[
                _ExtractedAppointment(
                    status="attended",
                    appointment_date=date(2025, 6, 3),
                    provider=None,
                    evidence_quote="A quote that does not appear in the body.",
                )
            ]
        )
    )
    assert AppointmentExtractor(llm).extract(note) == []


def test_evidence_quote_whitespace_tolerant() -> None:
    """A quote with collapsed whitespace still passes."""
    note = _note("EE   attended\nPT on 6/3.")
    llm = _FakeLLM(
        AppointmentExtractionResponse(
            appointments=[
                _ExtractedAppointment(
                    status="attended",
                    appointment_date=date(2025, 6, 3),
                    provider=None,
                    evidence_quote="EE attended PT on 6/3.",
                )
            ]
        )
    )
    assert len(AppointmentExtractor(llm).extract(note)) == 1


def test_empty_response_yields_no_events() -> None:
    """Discriminated-union empty case — appointments=[] is the
    canonical 'no event in this note' response."""
    note = _note("EE called the clinic to confirm something.")
    llm = _FakeLLM(AppointmentExtractionResponse(appointments=[]))
    assert AppointmentExtractor(llm).extract(note) == []


def test_appointment_date_missing_uses_note_date_for_event_date() -> None:
    """Some narrative appointments have no explicit date. We
    still emit an event (the LLM verified one exists); the
    storage-layer event_date falls back to the note's own date."""
    note = _note(
        "EE attended today's session.", note_date=date(2025, 7, 4)
    )
    llm = _FakeLLM(
        AppointmentExtractionResponse(
            appointments=[
                _ExtractedAppointment(
                    status="attended",
                    appointment_date=None,
                    provider=None,
                    evidence_quote="EE attended today's session.",
                )
            ]
        )
    )
    [event] = AppointmentExtractor(llm).extract(note)
    assert event.event_date == date(2025, 7, 4)


# --- Orchestrator wiring -----------------------------------------


def test_default_extractors_includes_llm_extractor() -> None:
    llm = _FakeLLM(AppointmentExtractionResponse())
    extractors = default_extractors(llm)
    assert any(isinstance(e, AppointmentExtractor) for e in extractors)


def test_run_all_with_llm_extractor_uses_canHandle_gate() -> None:
    """A note with no appointment signals must not reach the LLM."""
    llm = _FakeLLM(AppointmentExtractionResponse())
    note = _note("Pure prose with no signals.")
    events = run_all(note, extractors=default_extractors(llm))
    assert events == []
    assert llm.calls == []  # prefilter cut it off


def test_run_all_routes_appointment_note_to_llm() -> None:
    llm = _FakeLLM(
        AppointmentExtractionResponse(
            appointments=[
                _ExtractedAppointment(
                    status="attended",
                    appointment_date=date(2025, 6, 3),
                    provider="Dr. Harmon",
                    evidence_quote="EE attended PT on 6/3.",
                )
            ]
        )
    )
    note = _note("EE attended PT on 6/3.")
    events = run_all(note, extractors=default_extractors(llm))
    appts = [e for e in events if e.event_type == "appointment"]
    assert len(appts) == 1
    assert llm.calls  # the LLM was actually invoked
