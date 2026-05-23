"""Phase 9: Resolver tests.

Three concerns:
- party-set normalization and overlap (DD-016; load-bearing for
  appointment dedup)
- reserve_change delta derivation (Q3 exit criterion)
- appointment merge with status precedence and the DD-016 merge
  key — (claim, encounter_date exact, party-overlap)

End-to-end pinning against the live extractor output stays in
Phase 11's query-layer tests; here we focus on the resolver
algorithm against hand-constructed events.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from claims.models import (
    AppointmentAttributes,
    Event,
    ReserveChangeAttributes,
    ReturnToWorkAttributes,
)
from claims.resolver import (
    normalize_party,
    parties_overlap,
    resolve,
    resolve_appointments,
    resolve_reserve_changes,
    resolve_rtw,
)


# --- normalize_party --------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Dr. Harmon", "harmon"),
        ("Dr Harmon", "harmon"),
        ("Harmon, MD", "harmon"),
        ("Dr. Caldwell, MD", "caldwell"),
        ("Vega", "vega"),
        # Whole-org names stay whole — no `.split()[-1]` mangling.
        (
            "Bridge Functional Capacity Evaluators",
            "bridge functional capacity evaluators",
        ),
        ("ATI Physical Therapy", "ati physical therapy"),
        # Ampersand → 'and' for cross-spelling match.
        ("Spine & Neurology Group", "spine and neurology group"),
        ("Spine and Neurology Group", "spine and neurology group"),
        (None, None),
        ("   ", None),
        ("", None),
    ],
)
def test_normalize_party_table(
    raw: str | None, expected: str | None
) -> None:
    assert normalize_party(raw) == expected


def test_same_person_different_forms_normalize_equal() -> None:
    forms = ["Dr. Harmon", "Harmon", "Dr Harmon, MD", "  Dr. Harmon  "]
    norms = {normalize_party(f) for f in forms}
    assert norms == {"harmon"}


# --- parties_overlap --------------------------------------------


def test_overlap_when_shared_person() -> None:
    assert parties_overlap(
        ["Dr. Harmon", "Orthopedic & Spine Associates"],
        ["Harmon"],
    )


def test_overlap_when_shared_org() -> None:
    assert parties_overlap(
        ["Dr. Caldwell", "Spine & Neurology Group"],
        ["Spine and Neurology Group"],
    )


def test_no_overlap_when_completely_disjoint() -> None:
    assert not parties_overlap(
        ["Dr. Harmon"], ["Dr. Vega"]
    )


def test_empty_side_does_not_block_merge() -> None:
    """DD-016: when one side has no identifiable party (Marker
    saw only a date), party overlap must NOT block the merge —
    date alone carries it."""
    assert parties_overlap([], ["Dr. Harmon"])
    assert parties_overlap(["Dr. Harmon"], [])
    assert parties_overlap([], [])


def test_overlap_ignores_normalization_noise() -> None:
    assert parties_overlap(
        ["Dr. Harmon, MD"], ["  harmon  "]
    )


# --- reserve_change ---------------------------------------------


def _reserve_event(
    bucket: str, amount: Decimal, day: int, eid: str | None = None
) -> Event:
    return Event(
        event_id=eid or f"e-{bucket}-{day}",
        claim_id="C",
        event_type="reserve_change",
        event_date=date(2025, 5, day),
        attributes=ReserveChangeAttributes(
            bucket=bucket, new_amount=amount
        ),
        extraction_method="rule",
    )


def test_first_reserve_set_has_previous_zero() -> None:
    [out] = resolve_reserve_changes(
        [_reserve_event("Indemnity (2) Lost Time", Decimal("280000"), 1)]
    )
    assert isinstance(out.attributes, ReserveChangeAttributes)
    assert out.attributes.previous_amount == Decimal("0")
    assert out.attributes.delta == Decimal("280000")


def test_sequential_reserves_compute_delta() -> None:
    events = [
        _reserve_event("Indemnity (2) Lost Time", Decimal("280000"), 1),
        _reserve_event("Indemnity (2) Lost Time", Decimal("321014"), 5),
        _reserve_event("Indemnity (2) Lost Time", Decimal("250000"), 10),
    ]
    resolved = resolve_reserve_changes(events)
    deltas = [
        a.delta
        for a in (r.attributes for r in resolved)
        if isinstance(a, ReserveChangeAttributes)
    ]
    assert deltas == [
        Decimal("280000"),  # first set: previous=0
        Decimal("41014"),
        Decimal("-71014"),
    ]


def test_delta_zero_restatement_is_dropped() -> None:
    """A reserve restated at the same value is NOT a change."""
    events = [
        _reserve_event("Indemnity (2) Lost Time", Decimal("280000"), 1),
        _reserve_event("Indemnity (2) Lost Time", Decimal("280000"), 5),
        _reserve_event("Indemnity (2) Lost Time", Decimal("321014"), 10),
    ]
    resolved = resolve_reserve_changes(events)
    assert len(resolved) == 2  # first set + the real change
    amounts = [
        a.new_amount
        for a in (r.attributes for r in resolved)
        if isinstance(a, ReserveChangeAttributes)
    ]
    assert amounts == [Decimal("280000"), Decimal("321014")]


def test_different_buckets_are_independent() -> None:
    events = [
        _reserve_event("Indemnity (1) Medical Details", Decimal("100"), 1),
        _reserve_event("Indemnity (2) Lost Time", Decimal("200"), 1),
        _reserve_event("Indemnity (1) Medical Details", Decimal("150"), 5),
    ]
    resolved = resolve_reserve_changes(events)
    by_bucket = {
        a.bucket: a.delta
        for a in (r.attributes for r in resolved)
        if isinstance(a, ReserveChangeAttributes)
        and a.bucket == "Indemnity (2) Lost Time"
    }
    assert by_bucket["Indemnity (2) Lost Time"] == Decimal("200")


# --- appointments -----------------------------------------------


def _appt(
    *,
    parties: tuple[str, ...] = (),
    occurred_on: date | None = None,
    scheduled_for_date: date | None = None,
    scheduled_notice_date: date | None = None,
    status: str = "scheduled",
    eid: str = "e",
    method: str = "rule",
) -> Event:
    anchor = occurred_on or scheduled_for_date or date(2025, 1, 1)
    return Event(
        event_id=eid,
        claim_id="C",
        event_type="appointment",
        event_date=anchor,
        attributes=AppointmentAttributes(
            parties=parties,
            occurred_on=occurred_on,
            scheduled_for_date=scheduled_for_date,
            scheduled_notice_date=scheduled_notice_date,
            status=status,  # type: ignore[arg-type]
        ),
        extraction_method=method,  # type: ignore[arg-type]
    )


def test_marker_unknown_and_llm_attended_merge_to_attended() -> None:
    """Marker emits a past visit with status=unknown; LLM emits the
    same visit with status=attended. They merge on (claim, date,
    party-overlap) and the result is attended."""
    marker = _appt(
        parties=("Dr. Harmon",),
        occurred_on=date(2025, 11, 14),
        status="unknown",
        eid="marker",
    )
    llm = _appt(
        parties=("Dr. Harmon",),
        occurred_on=date(2025, 11, 14),
        status="attended",
        eid="llm",
        method="llm",
    )
    [merged] = resolve_appointments([marker, llm])
    assert isinstance(merged.attributes, AppointmentAttributes)
    assert merged.attributes.status == "attended"
    assert merged.extraction_method == "merged"
    assert merged.attributes.occurred_on == date(2025, 11, 14)


def test_status_precedence_attended_beats_scheduled() -> None:
    """Scheduled-for 6/1 + attended-on 6/1: same encounter_date,
    overlapping party → merge to attended."""
    a = _appt(
        parties=("Dr. X",),
        scheduled_for_date=date(2025, 6, 1),
        status="scheduled",
        eid="a",
    )
    b = _appt(
        parties=("Dr. X",),
        scheduled_for_date=date(2025, 6, 1),
        occurred_on=date(2025, 6, 1),
        status="attended",
        eid="b",
    )
    [merged] = resolve_appointments([a, b])
    assert isinstance(merged.attributes, AppointmentAttributes)
    assert merged.attributes.status == "attended"


def test_missed_beats_scheduled_but_not_attended() -> None:
    a = _appt(
        parties=("Dr. X",),
        scheduled_for_date=date(2025, 6, 1),
        status="missed",
        eid="a",
    )
    b = _appt(
        parties=("Dr. X",),
        scheduled_for_date=date(2025, 6, 1),
        occurred_on=date(2025, 6, 1),
        status="attended",
        eid="b",
    )
    [merged] = resolve_appointments([a, b])
    assert isinstance(merged.attributes, AppointmentAttributes)
    assert merged.attributes.status == "attended"


def test_different_parties_do_not_merge() -> None:
    a = _appt(
        parties=("Dr. Harmon",),
        occurred_on=date(2025, 6, 1),
        status="attended",
        eid="a",
    )
    b = _appt(
        parties=("Dr. Vega",),
        occurred_on=date(2025, 6, 1),
        status="attended",
        eid="b",
    )
    # Same claim, same date, but parties disjoint → two events.
    assert len(resolve_appointments([a, b])) == 2


def test_different_dates_do_not_merge_under_exact_rule() -> None:
    """DD-016: the ±N day window is gone. Same person, dates one
    day apart, no scheduled_for to anchor — these are two visits."""
    a = _appt(
        parties=("Dr. Harmon",),
        occurred_on=date(2025, 6, 1),
        status="attended",
        eid="a",
    )
    b = _appt(
        parties=("Dr. Harmon",),
        occurred_on=date(2025, 6, 2),
        status="attended",
        eid="b",
    )
    assert len(resolve_appointments([a, b])) == 2


def test_schedule_and_seen_pair_on_exact_date_merge() -> None:
    """Q4 hot path: a scheduled-for-6/1 event and an attended-on-6/1
    event with overlapping parties merge into a single visit with
    both date fields populated."""
    a = _appt(
        parties=("Dr. Harmon",),
        scheduled_for_date=date(2025, 6, 1),
        scheduled_notice_date=date(2025, 5, 15),
        status="scheduled",
        eid="a",
    )
    b = _appt(
        parties=("Dr. Harmon",),
        scheduled_for_date=date(2025, 6, 1),
        occurred_on=date(2025, 6, 1),
        status="attended",
        eid="b",
    )
    [merged] = resolve_appointments([a, b])
    assert isinstance(merged.attributes, AppointmentAttributes)
    assert merged.attributes.status == "attended"
    assert merged.attributes.scheduled_for_date == date(2025, 6, 1)
    assert merged.attributes.occurred_on == date(2025, 6, 1)
    assert merged.attributes.scheduled_notice_date == date(2025, 5, 15)


def test_empty_parties_does_not_block_merge() -> None:
    """DD-016 empty-side escape hatch. Marker emits a date with no
    Provider: line nearby (`parties=()`); the LLM emits the same
    encounter with the doctor named. Date matches → merge."""
    marker = _appt(
        parties=(),
        scheduled_for_date=date(2025, 6, 1),
        scheduled_notice_date=date(2025, 5, 15),
        status="scheduled",
        eid="marker",
    )
    llm = _appt(
        parties=("Dr. Harmon",),
        scheduled_for_date=date(2025, 6, 1),
        occurred_on=date(2025, 6, 1),
        status="attended",
        eid="llm",
        method="llm",
    )
    [merged] = resolve_appointments([marker, llm])
    assert isinstance(merged.attributes, AppointmentAttributes)
    assert merged.attributes.status == "attended"
    assert merged.attributes.parties == ("Dr. Harmon",)


def test_person_and_office_variant_merge() -> None:
    """The fragmentation case from the verification audit:
    `Dr. Harmon` and `Dr. Harmon's office` are the same person.
    The LLM strips the location suffix and emits 'Harmon' both
    times; under the new rule they overlap and merge."""
    a = _appt(
        parties=("Harmon",),
        occurred_on=date(2025, 4, 11),
        status="attended",
        eid="a",
    )
    b = _appt(
        parties=("Harmon", "Orthopedic & Spine Associates"),
        occurred_on=date(2025, 4, 11),
        status="attended",
        eid="b",
    )
    [merged] = resolve_appointments([a, b])
    assert isinstance(merged.attributes, AppointmentAttributes)
    # Union of parties, deduped by normalized form.
    assert "Orthopedic & Spine Associates" in merged.attributes.parties
    assert "Harmon" in merged.attributes.parties


def test_cross_org_doctor_overlap_via_org() -> None:
    """When one note lists doctor + clinic and another lists only
    the clinic for the same date, set overlap on the clinic carries
    the merge."""
    a = _appt(
        parties=("Caldwell", "Spine & Neurology Group"),
        scheduled_for_date=date(2025, 4, 22),
        scheduled_notice_date=date(2025, 3, 26),
        status="scheduled",
        eid="a",
    )
    b = _appt(
        parties=("Spine and Neurology Group",),
        scheduled_for_date=date(2025, 4, 22),
        occurred_on=date(2025, 4, 22),
        status="attended",
        eid="b",
    )
    [merged] = resolve_appointments([a, b])
    assert isinstance(merged.attributes, AppointmentAttributes)
    assert merged.attributes.status == "attended"


def test_latest_scheduled_notice_date_wins() -> None:
    """Q4 §5: reschedules — the most-recent notice anchors the
    Q4 gap math; earlier notices stay in history but don't feed."""
    first_notice = _appt(
        parties=("Dr. X",),
        scheduled_for_date=date(2025, 6, 5),
        scheduled_notice_date=date(2025, 5, 1),
        status="scheduled",
        eid="a",
    )
    reschedule_notice = _appt(
        parties=("Dr. X",),
        scheduled_for_date=date(2025, 6, 5),
        scheduled_notice_date=date(2025, 5, 20),
        status="scheduled",
        eid="b",
    )
    [merged] = resolve_appointments([first_notice, reschedule_notice])
    assert isinstance(merged.attributes, AppointmentAttributes)
    assert merged.attributes.scheduled_notice_date == date(2025, 5, 20)


def test_longer_surface_form_preferred_per_identity() -> None:
    """When two events share a normalized party (`harmon`), the
    longer surface form wins as the display label."""
    short = _appt(
        parties=("Harmon",),
        occurred_on=date(2025, 6, 1),
        status="attended",
        eid="a",
    )
    long = _appt(
        parties=("Dr. Harmon, MD",),
        occurred_on=date(2025, 6, 1),
        status="attended",
        eid="b",
    )
    [merged] = resolve_appointments([short, long])
    assert isinstance(merged.attributes, AppointmentAttributes)
    assert merged.attributes.parties == ("Dr. Harmon, MD",)


def test_single_event_passes_through_unchanged() -> None:
    [ev] = resolve_appointments(
        [
            _appt(
                parties=("Dr. X",),
                occurred_on=date(2025, 1, 1),
                status="attended",
                eid="solo",
            )
        ]
    )
    assert ev.event_id == "solo"  # not re-uuid'd
    assert ev.extraction_method == "rule"


# --- return_to_work ---------------------------------------------


def _rtw_event(
    return_date: date,
    duty_type: str = "modified",
    role: str | None = None,
    eid: str = "e",
) -> Event:
    return Event(
        event_id=eid,
        claim_id="C",
        event_type="return_to_work",
        event_date=return_date,
        attributes=ReturnToWorkAttributes(
            duty_type=duty_type,  # type: ignore[arg-type]
            role=role,
        ),
        extraction_method="llm",
    )


def test_rtw_identical_events_merge() -> None:
    a = _rtw_event(date(2025, 11, 10), role="scheduling coordinator", eid="a")
    b = _rtw_event(date(2025, 11, 10), role="scheduling coordinator", eid="b")
    [merged] = resolve_rtw([a, b])
    assert merged.event_date == date(2025, 11, 10)
    assert merged.extraction_method == "merged"


def test_rtw_different_dates_do_not_merge() -> None:
    a = _rtw_event(date(2025, 11, 10), "modified", eid="a")
    b = _rtw_event(date(2026, 1, 5), "full", eid="b")
    assert len(resolve_rtw([a, b])) == 2


def test_rtw_longer_role_form_preferred() -> None:
    a = _rtw_event(date(2025, 11, 10), "modified", role=None, eid="a")
    b = _rtw_event(
        date(2025, 11, 10),
        "modified",
        role="scheduling coordinator",
        eid="b",
    )
    [merged] = resolve_rtw([a, b])
    assert isinstance(merged.attributes, ReturnToWorkAttributes)
    assert merged.attributes.role == "scheduling coordinator"


# --- resolve() top-level ----------------------------------------


def test_resolve_dispatches_per_event_type() -> None:
    events = [
        _reserve_event("Indemnity (2) Lost Time", Decimal("100"), 1),
        _appt(
            parties=("Dr. X",),
            occurred_on=date(2025, 6, 1),
            status="attended",
            eid="a",
        ),
    ]
    resolved = resolve(events)
    by_type = {e.event_type for e in resolved}
    assert by_type == {"reserve_change", "appointment"}
