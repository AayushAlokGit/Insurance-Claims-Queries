"""Phase 2 storage integration tests.

Run against an in-memory SQLite per test. Cover the exit criterion:
insert a Claim + a list of Events of every type, read them back
with full fidelity, and run a Q3-shape aggregation against
hand-seeded reserve_change events.
"""

from datetime import date, datetime, timezone
from decimal import Decimal
from sqlite3 import Connection

import pytest

from claims.models import (
    AppointmentAttributes,
    Claim,
    Event,
    ReserveChangeAttributes,
    ReturnToWorkAttributes,
)
from claims.store import (
    connect,
    create_schema,
    get_claim,
    get_events_for_claim,
    insert_claim,
    insert_event,
    sum_reserve_deltas_per_claim,
)


@pytest.fixture
def conn() -> Connection:
    c = connect(":memory:")
    create_schema(c)
    return c


def _claim(claim_id: str = "1-29RT") -> Claim:
    return Claim(
        claim_id=claim_id,
        account="2100000000 - Company WW",
        jurisdiction="NJ",
        claim_type="injury",
        date_of_loss=date(2024, 12, 21),
        source_file="sample_claim_notes1.md",
        ingested_at=datetime(2026, 5, 23, 12, 0, tzinfo=timezone.utc),
    )


def test_claim_round_trip(conn: Connection) -> None:
    insert_claim(conn, _claim())
    fetched = get_claim(conn, "1-29RT")
    assert fetched == _claim()


def test_get_claim_missing(conn: Connection) -> None:
    assert get_claim(conn, "does-not-exist") is None


def test_events_round_trip_all_types(conn: Connection) -> None:
    insert_claim(conn, _claim())

    events = [
        Event(
            event_id="e1",
            claim_id="1-29RT",
            event_type="reserve_change",
            event_date=date(2025, 5, 1),
            attributes=ReserveChangeAttributes(
                bucket="Indemnity (2) Lost Time",
                new_amount=Decimal("321014.00"),
                previous_amount=Decimal("280000.00"),
                delta=Decimal("41014.00"),
                author="M.H.",
            ),
            extraction_method="rule",
        ),
        Event(
            event_id="e2",
            claim_id="1-29RT",
            event_type="appointment",
            event_date=date(2025, 9, 15),
            attributes=AppointmentAttributes(
                parties=("Caldwell", "Spine & Neurology Group"),
                specialty="neurosurgery",
                scheduled_notice_date=date(2025, 8, 29),
                scheduled_for_date=date(2025, 9, 15),
                occurred_on=date(2025, 9, 15),
                status="attended",
                appointment_type="office_visit",
            ),
            extraction_method="merged",
        ),
        Event(
            event_id="e3",
            claim_id="1-29RT",
            event_type="return_to_work",
            event_date=date(2025, 11, 10),
            attributes=ReturnToWorkAttributes(
                duty_type="modified",
                role="scheduling coordinator",
            ),
            extraction_method="llm",
        ),
    ]
    for e in events:
        insert_event(conn, e)

    fetched = get_events_for_claim(conn, "1-29RT")
    assert fetched == events  # ordered by event_date matches insertion order


def test_event_check_constraints(conn: Connection) -> None:
    """Schema CHECK constraints reject bad enum values directly."""
    insert_claim(conn, _claim())
    # Bypass the pydantic model and write directly to exercise the
    # CHECK constraint — defense-in-depth at the storage layer.
    import sqlite3 as _sqlite3

    with pytest.raises(_sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO event VALUES (?,?,?,?,?,?)",
            ("e", "1-29RT", "not_a_real_type", "2025-01-01", "{}", "rule"),
        )


def test_foreign_key_enforced(conn: Connection) -> None:
    """An event referring to a missing claim_id must fail."""
    import sqlite3 as _sqlite3

    event = Event(
        event_id="orphan",
        claim_id="no-such-claim",
        event_type="return_to_work",
        event_date=date(2025, 1, 1),
        attributes=ReturnToWorkAttributes(duty_type="full"),
        extraction_method="llm",
    )
    with pytest.raises(_sqlite3.IntegrityError):
        insert_event(conn, event)


def test_q3_shape_aggregation(conn: Connection) -> None:
    """Exit criterion: a Q3-shape SQL aggregation works against
    hand-seeded reserve_change events. Sums the JSON delta path."""
    insert_claim(conn, _claim("A"))
    insert_claim(conn, _claim("B"))

    seed = [
        ("A", "e1", Decimal("10000.00")),
        ("A", "e2", Decimal("-2500.00")),
        ("A", "e3", Decimal("500.00")),
        ("B", "e4", Decimal("75000.00")),
    ]
    for claim_id, event_id, delta in seed:
        insert_event(
            conn,
            Event(
                event_id=event_id,
                claim_id=claim_id,
                event_type="reserve_change",
                event_date=date(2025, 1, 1),
                attributes=ReserveChangeAttributes(
                    bucket="Indemnity (2) Lost Time",
                    new_amount=Decimal("0"),
                    delta=delta,
                ),
                extraction_method="rule",
            ),
        )

    totals = sum_reserve_deltas_per_claim(conn)
    assert totals == {
        "A": Decimal("8000"),
        "B": Decimal("75000"),
    }


def test_q3_aggregation_skips_null_deltas(conn: Connection) -> None:
    """First-set reserve events have no `delta` (the Resolver
    fills it in based on the prior event). The SUM should treat
    those as zero, not error."""
    insert_claim(conn, _claim("A"))

    insert_event(
        conn,
        Event(
            event_id="first_set",
            claim_id="A",
            event_type="reserve_change",
            event_date=date(2025, 1, 1),
            attributes=ReserveChangeAttributes(
                bucket="Indemnity (2) Lost Time",
                new_amount=Decimal("280000.00"),
                delta=None,
            ),
            extraction_method="rule",
        ),
    )
    insert_event(
        conn,
        Event(
            event_id="later",
            claim_id="A",
            event_type="reserve_change",
            event_date=date(2025, 5, 1),
            attributes=ReserveChangeAttributes(
                bucket="Indemnity (2) Lost Time",
                new_amount=Decimal("321014.00"),
                delta=Decimal("41014.00"),
            ),
            extraction_method="rule",
        ),
    )

    totals = sum_reserve_deltas_per_claim(conn)
    assert totals == {"A": Decimal("41014")}
