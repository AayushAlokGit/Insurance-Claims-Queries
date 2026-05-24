"""Tests for the DD-019 per-claim appointment reconciliation.

Three areas:
- `_normalize_party`: stoplist + honorific stripping (deterministic)
- `_precluster`: the clustering rules — exact-date, ±1 day,
  empty-party absorption, dateless singletons (deterministic)
- `_encounter_date` / `_notice_date`: deterministic date logic,
  including the retrospective-recap exclusion that prevents
  Q4's negative-lag bug
- `reconcile_appointments`: end-to-end with a fake LLM
"""

from __future__ import annotations

from datetime import date

import pytest

from claims.extractor.appointment_reconciliation import (
    _ClusterResolution,
    _can_join,
    _encounter_date,
    _normalize_party,
    _notice_date,
    _precluster,
    reconcile_appointments,
)
from claims.models import AppointmentAttributes, AppointmentEvidence, Event


# --- _normalize_party ------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Dr. Harmon", "harmon"),
        ("Harmon, MD", "harmon"),
        ("DR HARMON", "harmon"),
        ("Spine & Neurology Group", "spine and neurology group"),
        # Generic / role labels rejected
        ("Patient A", None),
        ("the claimant", None),
        ("FCM", None),
        ("Ortho", None),
        ("Pain Mgmt", None),
        # Edge cases
        ("", None),
        (None, None),
        ("   ", None),
    ],
)
def test_normalize_party(raw: str | None, expected: str | None) -> None:
    assert _normalize_party(raw) == expected


# --- _precluster -----------------------------------------------


def _appt(
    *,
    eid: str,
    occurred_on: date | None = None,
    scheduled_for_date: date | None = None,
    parties: tuple[str, ...] = (),
    status: str = "attended",
    source_note_dates: tuple[date, ...] = (),
    evidence_quote: str | None = None,
) -> Event:
    anchor = occurred_on or scheduled_for_date or date(2025, 1, 1)
    evidence = tuple(
        AppointmentEvidence(note_date=nd, quote=evidence_quote or "")
        for nd in source_note_dates
    )
    return Event(
        event_id=eid,
        claim_id="C",
        event_type="appointment",
        event_date=anchor,
        attributes=AppointmentAttributes(
            parties=parties,
            occurred_on=occurred_on,
            scheduled_for_date=scheduled_for_date,
            status=status,  # type: ignore[arg-type]
            evidence=evidence,
        ),
        extraction_method="llm",
    )


def test_can_join_exact_date_same_party() -> None:
    a = _appt(eid="a", occurred_on=date(2025, 6, 27), parties=("Harmon",))
    b = _appt(eid="b", occurred_on=date(2025, 6, 27), parties=("Harmon",))
    assert _can_join(a, b)


def test_can_join_off_by_one_same_party() -> None:
    """The note-write-date vs DOS off-by-one is the canonical
    failure DD-019 fixes via ±1 day tolerance."""
    a = _appt(eid="a", occurred_on=date(2025, 6, 26), parties=("Harmon",))
    b = _appt(eid="b", occurred_on=date(2025, 6, 27), parties=("Harmon",))
    assert _can_join(a, b)


def test_can_join_same_date_empty_party_absorbs() -> None:
    """A candidate with no named parties merges into a same-date
    named cluster — Marker-style empty candidates shouldn't strand."""
    a = _appt(eid="a", occurred_on=date(2025, 6, 27), parties=("Harmon",))
    b = _appt(eid="b", occurred_on=date(2025, 6, 27), parties=())
    assert _can_join(a, b)


def test_can_join_within_three_day_tolerance() -> None:
    """A 3-day drift with shared party still merges (catches the
    Sinclair 3/11 vs 3/14 and Vega 5/29 vs 6/2 same-visit splits)."""
    a = _appt(eid="a", occurred_on=date(2025, 5, 29), parties=("Vega",))
    b = _appt(eid="b", occurred_on=date(2025, 6, 1), parties=("Vega",))
    assert _can_join(a, b)


def test_no_join_when_date_drift_exceeds_tolerance() -> None:
    """Same-party PT sessions >= 4 days apart stay separate visits."""
    a = _appt(eid="a", occurred_on=date(2025, 6, 7), parties=("Valley PT Group",))
    b = _appt(eid="b", occurred_on=date(2025, 6, 12), parties=("Valley PT Group",))
    assert not _can_join(a, b)


def test_no_join_same_date_disjoint_named_parties() -> None:
    """Same-day same-facility different-clinician is two visits."""
    a = _appt(eid="a", occurred_on=date(2025, 6, 27), parties=("Harmon",))
    b = _appt(eid="b", occurred_on=date(2025, 6, 27), parties=("Vega",))
    assert not _can_join(a, b)


def test_dateless_candidate_stays_singleton() -> None:
    """A dateless candidate must NOT absorb anything — that was the
    cluster-magnet bug from the old DD-016 design."""
    dateless = _appt(eid="d", parties=("Harmon",))  # no occurred/scheduled
    dated = _appt(eid="x", occurred_on=date(2025, 6, 27), parties=("Harmon",))
    assert not _can_join(dated, dateless)
    assert not _can_join(dateless, dated)


def test_precluster_collapses_off_by_one() -> None:
    cands = [
        _appt(eid="a", occurred_on=date(2025, 6, 26), parties=("Harmon",)),
        _appt(eid="b", occurred_on=date(2025, 6, 27), parties=("Harmon",)),
        _appt(eid="c", occurred_on=date(2025, 6, 27), parties=("Harmon",)),
    ]
    clusters = _precluster(cands)
    assert len(clusters) == 1
    assert {e.event_id for e in clusters[0]} == {"a", "b", "c"}


def test_precluster_separates_disjoint_doctors_same_day() -> None:
    cands = [
        _appt(eid="a", occurred_on=date(2025, 6, 27), parties=("Harmon",)),
        _appt(eid="b", occurred_on=date(2025, 6, 27), parties=("Vega",)),
    ]
    clusters = _precluster(cands)
    assert len(clusters) == 2


def test_precluster_dateless_singletons() -> None:
    cands = [
        _appt(eid="a", parties=("Harmon",)),  # dateless
        _appt(eid="b", parties=("Harmon",)),  # dateless
        _appt(eid="c", occurred_on=date(2025, 6, 27), parties=("Harmon",)),
    ]
    clusters = _precluster(cands)
    # 'c' is a singleton (dated); 'a' and 'b' are both dateless
    # singletons (no merging at all for dateless).
    assert len(clusters) == 3


# --- _encounter_date -------------------------------------------


def test_encounter_date_prefers_occurred_over_scheduled() -> None:
    cluster = [
        _appt(eid="a", scheduled_for_date=date(2025, 6, 27)),
        _appt(eid="b", occurred_on=date(2025, 6, 27)),
    ]
    assert _encounter_date(cluster) == date(2025, 6, 27)


def test_encounter_date_mode_with_earliest_tiebreak() -> None:
    """When candidates disagree, pick the most common date; ties
    broken by earliest. The 6/26 vs 6/27 ambiguity should resolve
    to 6/27 if more candidates name 6/27."""
    cluster = [
        _appt(eid="a", occurred_on=date(2025, 6, 26)),
        _appt(eid="b", occurred_on=date(2025, 6, 27)),
        _appt(eid="c", occurred_on=date(2025, 6, 27)),
    ]
    assert _encounter_date(cluster) == date(2025, 6, 27)


def test_encounter_date_none_when_all_dateless() -> None:
    cluster = [_appt(eid="a"), _appt(eid="b")]
    assert _encounter_date(cluster) is None


# --- _notice_date ----------------------------------------------


def test_notice_date_is_earliest_pre_encounter_note() -> None:
    cluster = [
        _appt(
            eid="a",
            occurred_on=date(2025, 5, 16),
            source_note_dates=(date(2025, 4, 10),),  # forward-looking
        ),
        _appt(
            eid="b",
            occurred_on=date(2025, 5, 16),
            source_note_dates=(date(2025, 5, 6),),  # also forward-looking
        ),
    ]
    assert _notice_date(cluster, date(2025, 5, 16)) == date(2025, 4, 10)


def test_notice_date_excludes_retrospective_recap() -> None:
    """The retrospective treatment-summary trap: an August Resolution
    Strategy note recapping a February visit must NOT be used as the
    scheduling notice (would produce a negative lag). This was the
    main Q4 regression DD-019 had to fix."""
    cluster = [
        _appt(
            eid="a",
            occurred_on=date(2025, 2, 5),
            source_note_dates=(date(2025, 2, 1),),  # legitimate notice
        ),
        _appt(
            eid="b",
            occurred_on=date(2025, 2, 5),
            source_note_dates=(date(2025, 8, 9),),  # retrospective recap
        ),
    ]
    notice = _notice_date(cluster, date(2025, 2, 5))
    assert notice == date(2025, 2, 1)
    # Crucially, NOT 8/9 — that would produce lag=-185.


def test_notice_date_none_when_only_retrospective_notes() -> None:
    cluster = [
        _appt(
            eid="a",
            occurred_on=date(2025, 2, 5),
            source_note_dates=(date(2025, 8, 9),),  # all retrospective
        ),
    ]
    assert _notice_date(cluster, date(2025, 2, 5)) is None


# --- reconcile_appointments (end-to-end with fake LLM) ---------


class _FakeLLM:
    """Replays one queued ClusterResolution per cluster, in order.
    Asserts the requested response_model to catch drift."""

    model: str = "fake"

    def __init__(self, replies: list[_ClusterResolution]) -> None:
        self._replies = list(replies)
        self.calls = 0

    def structured(self, *, system: str, user: str, response_model):  # type: ignore[no-untyped-def]
        assert response_model is _ClusterResolution
        if not self._replies:
            raise AssertionError("no fake reply queued for this cluster")
        self.calls += 1
        return self._replies.pop(0)


def test_reconcile_empty_returns_empty() -> None:
    llm = _FakeLLM([])
    assert reconcile_appointments("C", None, [], llm) == []
    assert llm.calls == 0


def test_reconcile_merges_off_by_one_and_computes_notice() -> None:
    """End-to-end: two candidates on adjacent dates with shared
    party merge into one canonical event; deterministic notice
    date excludes the retrospective recap; status promoted via
    the per-cluster LLM call."""
    cands = [
        _appt(
            eid="a",
            occurred_on=date(2025, 6, 26),
            parties=("Harmon",),
            status="scheduled",
            source_note_dates=(date(2025, 6, 16),),  # legitimate notice
            evidence_quote="Next Office Visit: 6-26-25",
        ),
        _appt(
            eid="b",
            occurred_on=date(2025, 6, 27),
            parties=("Harmon",),
            status="attended",
            source_note_dates=(date(2025, 6, 27),),  # day-of
            evidence_quote="Date of Appointment: 6-27-25",
        ),
        _appt(
            eid="c",
            occurred_on=date(2025, 6, 27),
            parties=("Harmon",),
            status="scheduled",
            source_note_dates=(date(2025, 8, 9),),  # retrospective recap
            evidence_quote="follow-up with Dr. Harmon was conducted on 6-27-25",
        ),
    ]
    llm = _FakeLLM(
        [
            _ClusterResolution(
                status="attended",
                parties=["Harmon"],
            )
        ]
    )
    out = reconcile_appointments("C", None, cands, llm)
    assert len(out) == 1
    attrs = out[0].attributes
    assert isinstance(attrs, AppointmentAttributes)
    assert attrs.status == "attended"
    assert attrs.occurred_on == date(2025, 6, 27)  # mode wins
    assert attrs.scheduled_for_date is None
    assert attrs.parties == ("Harmon",)
    # Notice date must NOT be the 8/9 retrospective recap.
    assert attrs.scheduled_notice_date == date(2025, 6, 16)
    # All three candidates' note dates appear in the audit trail.
    assert set(attrs.source_note_dates) == {
        date(2025, 6, 16),
        date(2025, 6, 27),
        date(2025, 8, 9),
    }
    assert llm.calls == 1


def test_reconcile_drops_generic_parties_post_llm() -> None:
    """Defense-in-depth: even if the LLM returns 'Patient A' or
    'Ortho', the deterministic stoplist drops them."""
    cands = [
        _appt(
            eid="a",
            occurred_on=date(2025, 4, 22),
            parties=("Caldwell",),
            source_note_dates=(date(2025, 3, 24),),
            evidence_quote="Dr. Caldwell on April 22, 2025",
        )
    ]
    llm = _FakeLLM(
        [
            _ClusterResolution(
                status="attended",
                parties=["Caldwell", "Patient A", "Ortho"],
            )
        ]
    )
    [event] = reconcile_appointments("C", None, cands, llm)
    attrs = event.attributes
    assert isinstance(attrs, AppointmentAttributes)
    assert attrs.parties == ("Caldwell",)


def test_reconcile_scheduled_status_populates_scheduled_for_date() -> None:
    """A future-only appointment populates scheduled_for_date,
    NOT occurred_on. Q4 only fires on visits with occurred_on."""
    cands = [
        _appt(
            eid="a",
            scheduled_for_date=date(2025, 9, 23),
            parties=("Farano",),
            status="scheduled",
            source_note_dates=(date(2025, 8, 29),),
            evidence_quote="NOV is set for 9/23",
        )
    ]
    llm = _FakeLLM(
        [
            _ClusterResolution(
                status="scheduled",
                parties=["Farano"],
            )
        ]
    )
    [event] = reconcile_appointments("C", None, cands, llm)
    attrs = event.attributes
    assert isinstance(attrs, AppointmentAttributes)
    assert attrs.status == "scheduled"
    assert attrs.scheduled_for_date == date(2025, 9, 23)
    assert attrs.occurred_on is None


def test_reconcile_skips_cluster_on_llm_failure() -> None:
    """If the LLM gives up after retries (returns no reply for a
    cluster), the cluster is silently dropped. DD-014 retry already
    handles transient errors; persistent failures shouldn't crash
    the whole claim."""

    class _FailingLLM:
        model = "fake"

        def structured(self, **_):  # type: ignore[no-untyped-def]
            from claims.llm.base import LLMError

            raise LLMError("simulated")

    cands = [
        _appt(
            eid="a",
            occurred_on=date(2025, 6, 27),
            parties=("Harmon",),
            source_note_dates=(date(2025, 6, 16),),
        )
    ]
    out = reconcile_appointments("C", None, cands, _FailingLLM())
    assert out == []
