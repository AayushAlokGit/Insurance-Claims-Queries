"""Phase 6: appointment marker extractor (rule fast-path).

Pins emission against the real samples (24 markers in claim 1, 2
in claim 2) and covers the per-marker status + date assignment
rules from extractor.md §5.2."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from claims.extractor import AppointmentMarkerExtractor
from claims.loader import parse_file
from claims.models import AppointmentAttributes, Note
from claims.normalizer import normalize

SAMPLES = Path(__file__).resolve().parent.parent / "sample_claim_notes"


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


def test_can_handle_requires_marker_substring() -> None:
    e = AppointmentMarkerExtractor()
    assert e.can_handle(_note("Date of Appointment: 5-16-25"))
    assert e.can_handle(_note("Next Office Visit: 6-12-25"))
    assert e.can_handle(_note("Schedule Date Time: Apr 17 2025"))
    assert not e.can_handle(_note("just some prose"))
    # NOV: lines are explicitly NOT in the marker prefilter — the
    # LLM extractor handles them.
    assert not e.can_handle(_note("NOV: Fwp w/ Dr. Farano was on 8/13"))


def test_date_of_appointment_emits_past_visit() -> None:
    e = AppointmentMarkerExtractor()
    [event] = e.extract(_note("Date of Appointment: 11-14-25"))
    attrs = event.attributes
    assert isinstance(attrs, AppointmentAttributes)
    assert attrs.occurred_on == date(2025, 11, 14)
    assert attrs.scheduled_for_date is None
    assert attrs.scheduled_notice_date is None
    assert attrs.status == "unknown"  # LLM fills in attended/missed/...
    assert event.event_date == date(2025, 11, 14)
    assert event.extraction_method == "rule"


def test_next_office_visit_emits_scheduled() -> None:
    e = AppointmentMarkerExtractor()
    note = _note(
        "Next Office Visit: 5-16-25 at 11am", note_date=date(2025, 4, 11)
    )
    [event] = e.extract(note)
    attrs = event.attributes
    assert isinstance(attrs, AppointmentAttributes)
    assert attrs.scheduled_for_date == date(2025, 5, 16)
    assert attrs.scheduled_notice_date == date(2025, 4, 11)
    assert attrs.occurred_on is None
    assert attrs.status == "scheduled"


def test_schedule_date_time_emits_scheduled() -> None:
    e = AppointmentMarkerExtractor()
    note = _note(
        "Schedule Date Time: Apr 17 2025 9:00AM",
        note_date=date(2025, 4, 14),
    )
    [event] = e.extract(note)
    attrs = event.attributes
    assert isinstance(attrs, AppointmentAttributes)
    assert attrs.scheduled_for_date == date(2025, 4, 17)
    assert attrs.status == "scheduled"


def test_unparseable_marker_value_skipped() -> None:
    """`Next Office Visit: PRN` / `NA` / `Pending FCE completion.`
    appears throughout the corpus. These must produce no event,
    not a parse error."""
    e = AppointmentMarkerExtractor()
    assert e.extract(_note("Next Office Visit: PRN")) == []
    assert e.extract(_note("Next Office Visit: NA")) == []
    assert (
        e.extract(_note("Next Office Visit: Pending FCE completion."))
        == []
    )


def test_provider_lookup_in_following_lines() -> None:
    e = AppointmentMarkerExtractor()
    body = (
        "Date of Appointment: 11-14-25\n"
        "Next Office Visit: PRN\n"
        "Name of physician: Dr. Harmon\n"
        "Clinic: Orthopedic & Spine Associates\n"
        "Specialty: Spine\n"
    )
    [past, *_] = e.extract(_note(body))
    attrs = past.attributes
    assert isinstance(attrs, AppointmentAttributes)
    assert attrs.parties == ("Dr. Harmon",)


def test_provider_lookup_finds_provider_keyword() -> None:
    e = AppointmentMarkerExtractor()
    body = (
        "Date of Appointment: 10-14-25\n"
        "Provider: Bridge Functional Capacity Evaluators\n"
    )
    [event] = e.extract(_note(body))
    attrs = event.attributes
    assert isinstance(attrs, AppointmentAttributes)
    assert attrs.parties == ("Bridge Functional Capacity Evaluators",)


def test_provider_absent_yields_empty_parties() -> None:
    e = AppointmentMarkerExtractor()
    [event] = e.extract(_note("Date of Appointment: 11-14-25\nUnrelated line"))
    attrs = event.attributes
    assert isinstance(attrs, AppointmentAttributes)
    assert attrs.parties == ()


def test_multi_marker_note_emits_two_events() -> None:
    """A single note with both a past visit and a future schedule
    emits two events — see q4-schedule-to-seen.md §3."""
    e = AppointmentMarkerExtractor()
    body = (
        "Date of Appointment: 6-12-25\n"
        "Next Office Visit: 7-25-25 at 11am\n"
    )
    events = e.extract(_note(body, note_date=date(2025, 6, 13)))
    assert len(events) == 2
    statuses = {ev.attributes.status for ev in events}
    assert statuses == {"unknown", "scheduled"}


def _markers_on(path: Path) -> list:
    loaded = parse_file(str(path))
    notes: list[Note] = []
    for i, raw in enumerate(loaded.notes):
        n = normalize(raw, index=i)
        if n is not None:
            notes.append(n)
    e = AppointmentMarkerExtractor()
    return [ev for n in notes for ev in e.extract(n)]


def test_sample_1_marker_counts_pinned() -> None:
    events = _markers_on(SAMPLES / "sample_claim_notes1.md")
    assert len(events) == 24
    scheduled = [e for e in events if e.attributes.status == "scheduled"]
    unknown = [e for e in events if e.attributes.status == "unknown"]
    assert len(scheduled) == 6
    assert len(unknown) == 18


def test_sample_2_marker_counts_pinned() -> None:
    events = _markers_on(SAMPLES / "sample_claim_notes2.md")
    assert len(events) == 2
    scheduled = [e for e in events if e.attributes.status == "scheduled"]
    unknown = [e for e in events if e.attributes.status == "unknown"]
    assert len(scheduled) == 1
    assert len(unknown) == 1
