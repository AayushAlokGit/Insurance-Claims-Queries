"""Phase 5: Q3 reserve-change extractor tests.

Exit criterion: Q3 answerable end-to-end on both sample claims by
chaining Loader → Normalizer → ReserveChangeExtractor → Store →
SQL query. The store integration was already pinned in Phase 2;
here we pin the extractor's output against the real samples and
the SQL aggregation against hand-counted totals."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from claims.extractor import ReserveChangeExtractor, run_all
from claims.loader import parse_file
from claims.models import Note, ReserveChangeAttributes
from claims.normalizer import normalize
from claims.store import (
    connect,
    create_schema,
    insert_claim,
    insert_event,
    sum_reserve_deltas_per_claim,
)
from claims.models import Claim
from datetime import datetime, timezone

SAMPLES = Path(__file__).resolve().parent.parent / "sample_claim_notes"


def _note(
    body: str,
    activity: str | None = "Reserving",
    author: str | None = "M.H.",
) -> Note:
    return Note(
        note_id="TEST-n0000",
        claim_id="TEST",
        note_date=date(2025, 5, 1),
        activity=activity,
        author=author,
        body=body,
        data_quality_flags=[],
    )


def test_can_handle_only_reserving() -> None:
    e = ReserveChangeExtractor()
    assert e.can_handle(_note("anything", activity="Reserving"))
    assert not e.can_handle(_note("anything", activity="Investigation"))
    assert not e.can_handle(_note("anything", activity=None))


def test_extracts_single_match() -> None:
    e = ReserveChangeExtractor()
    events = e.extract(
        _note("Note: Indemnity for (2) Lost Time Changed to $321,014.00")
    )
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == "reserve_change"
    assert ev.extraction_method == "rule"
    attrs = ev.attributes
    assert isinstance(attrs, ReserveChangeAttributes)
    assert attrs.bucket == "Indemnity (2) Lost Time"
    assert attrs.new_amount == Decimal("321014.00")
    assert attrs.previous_amount is None  # Resolver fills this in
    assert attrs.delta is None
    assert attrs.author == "M.H."


def test_expense_litigation_form() -> None:
    """Critical: the alternation must spell out 'Expense-Litigation'
    in full — bare 'Expense' would silently drop 23% of sample
    reserve changes (q3-reserve-changes.md §4)."""
    e = ReserveChangeExtractor()
    events = e.extract(
        _note("Note: Expense-Litigation for (1) Medical Details Changed to $148.25")
    )
    assert len(events) == 1
    attrs = events[0].attributes
    assert isinstance(attrs, ReserveChangeAttributes)
    assert attrs.bucket == "Expense-Litigation (1) Medical Details"
    assert attrs.new_amount == Decimal("148.25")


def test_resolution_strategy_restatement_not_matched() -> None:
    """Resolution Strategy notes summarize *remaining balances*
    ('Reserves: Medical remaining $32,248.62; Indemnity remaining
    $233,145.82'). These are NOT change events; the regex must
    not match them — and can_handle won't fire because the
    activity is wrong anyway. Defense in depth: even on a
    Reserving-labeled note, the regex requires 'Changed to'."""
    e = ReserveChangeExtractor()
    events = e.extract(
        _note(
            "Reserves: Medical remaining $32,248.62; "
            "Indemnity remaining $233,145.82"
        )
    )
    assert events == []


def test_multiple_matches_in_one_note() -> None:
    e = ReserveChangeExtractor()
    body = (
        "Indemnity for (2) Lost Time Changed to $321,014.00\n"
        "Indemnity for (1) Medical Details Changed to $382,844.00"
    )
    events = e.extract(_note(body))
    assert len(events) == 2
    buckets = [
        a.bucket
        for a in (e.attributes for e in events)
        if isinstance(a, ReserveChangeAttributes)
    ]
    assert buckets == [
        "Indemnity (2) Lost Time",
        "Indemnity (1) Medical Details",
    ]


def test_orchestrator_routes_only_reserving_notes() -> None:
    """run_all should produce no events for a non-Reserving note
    that happens to mention a dollar amount."""
    note = _note(
        "EE returned to work; reserve currently $321,014.00.",
        activity="Investigation",
    )
    assert run_all(note) == []


def _normalize_all(claim_file: str) -> tuple[str, list[Note]]:
    loaded = parse_file(claim_file)
    notes: list[Note] = []
    for i, raw in enumerate(loaded.notes):
        n = normalize(raw, index=i)
        if n is not None:
            notes.append(n)
    return loaded.header.claim_id, notes


def test_sample_1_reserve_count() -> None:
    """q3-reserve-changes.md §3: 9 reserve changes in claim 1."""
    _, notes = _normalize_all(str(SAMPLES / "sample_claim_notes1.md"))
    events = [e for n in notes for e in run_all(n)]
    reserves = [e for e in events if e.event_type == "reserve_change"]
    assert len(reserves) == 9


def test_sample_2_reserve_count() -> None:
    """q3-reserve-changes.md §3: 4 reserve changes in claim 2."""
    _, notes = _normalize_all(str(SAMPLES / "sample_claim_notes2.md"))
    events = [e for n in notes for e in run_all(n)]
    reserves = [e for e in events if e.event_type == "reserve_change"]
    assert len(reserves) == 4


def test_sample_1_bucket_distribution() -> None:
    """Pin the bucket distribution against the table in
    q3-reserve-changes.md §3."""
    _, notes = _normalize_all(str(SAMPLES / "sample_claim_notes1.md"))
    events = [e for n in notes for e in run_all(n)]
    buckets = sorted(
        attrs.bucket
        for e in events
        if isinstance(attrs := e.attributes, ReserveChangeAttributes)
    )
    assert buckets == sorted(
        [
            "Expense-Litigation (1) Medical Details",
            "Expense-Litigation (1) Medical Details",
            "Expense-Litigation (1) Medical Details",
            "Indemnity (1) Medical Details",
            "Indemnity (1) Medical Details",
            "Indemnity (1) Medical Details",
            "Indemnity (2) Lost Time",
            "Indemnity (2) Lost Time",
            "Indemnity (2) Lost Time",
        ]
    )


def test_q3_end_to_end_through_store() -> None:
    """Exit criterion: Loader → Normalizer → Extractor → Store →
    SQL aggregation, on a real sample claim. Without the
    Resolver in place yet, the per-event `delta` is null — so
    the SUM total is zero. The wiring itself is what's being
    validated here; deltas will become non-trivial after
    Phase 9 (Resolver)."""
    claim_id, notes = _normalize_all(
        str(SAMPLES / "sample_claim_notes1.md")
    )
    conn = connect(":memory:")
    create_schema(conn)
    insert_claim(
        conn,
        Claim(
            claim_id=claim_id,
            account="2100000000 - Company WW",
            jurisdiction="NJ",
            claim_type="injury",
            date_of_loss=date(2024, 12, 21),
            source_file="sample_claim_notes1.md",
            ingested_at=datetime(2026, 5, 23, tzinfo=timezone.utc),
        ),
    )
    for note in notes:
        for event in run_all(note):
            insert_event(conn, event)

    # Nine rows of reserve_change should be present.
    (row_count,) = conn.execute(
        "SELECT COUNT(*) FROM event WHERE event_type = 'reserve_change'"
    ).fetchone()
    assert row_count == 9

    # The aggregation runs cleanly; pre-Resolver, all deltas are null
    # so the totaled movement is zero. (Phase 9 changes this.)
    totals = sum_reserve_deltas_per_claim(conn)
    assert totals == {claim_id: Decimal("0")}
