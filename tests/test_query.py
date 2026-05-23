"""Phase 11: query-layer tests.

Hand-seeds a SQLite DB with each query's signature scenarios
and asserts the typed return. Avoids the LLM entirely — the
seed events are exactly the resolved shape the upstream stages
would produce."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from sqlite3 import Connection

import pytest

from claims.models import (
    AppointmentAttributes,
    Claim,
    Event,
    RTWTerminalAttributes,
    ReserveChangeAttributes,
    ReturnToWorkAttributes,
)
from claims.query import (
    Q1NeverReturned,
    Q1Pending,
    Q1Returned,
    q1_return_to_work,
    q2_appointments_attended,
    q3_reserve_changes,
    q4_schedule_to_seen,
)
from claims.store import (
    connect,
    create_schema,
    insert_claim,
    insert_event,
)


@pytest.fixture
def conn() -> Connection:
    c = connect(":memory:")
    create_schema(c)
    return c


def _claim(
    conn: Connection, claim_id: str = "C", dol: date = date(2024, 12, 21)
) -> Claim:
    claim = Claim(
        claim_id=claim_id,
        account=None,
        jurisdiction="NJ",
        claim_type="injury",
        date_of_loss=dol,
        source_file="f",
        ingested_at=datetime(2026, 5, 23, tzinfo=timezone.utc),
    )
    insert_claim(conn, claim)
    return claim


# --- Q1 ---------------------------------------------------------


def test_q1_returned(conn: Connection) -> None:
    _claim(conn)
    insert_event(
        conn,
        Event(
            event_id="rtw",
            claim_id="C",
            event_type="return_to_work",
            event_date=date(2025, 11, 10),
            attributes=ReturnToWorkAttributes(
                duty_type="modified", role="scheduling coordinator"
            ),
            extraction_method="llm",
        ),
    )
    result = q1_return_to_work(conn, "C")
    assert isinstance(result, Q1Returned)
    assert result.status == "returned"
    assert result.rtw_date == date(2025, 11, 10)
    assert result.duty_type == "modified"
    # Dec 21 2024 → Nov 10 2025 is 324 days.
    assert result.days == 324


def test_q1_returned_wins_when_terminal_also_present(conn: Connection) -> None:
    """The smoke-test scenario: an LLM-emitted closed_no_rtw is
    in the data, but a positive RTW also exists. Positive wins."""
    _claim(conn)
    insert_event(
        conn,
        Event(
            event_id="rtw",
            claim_id="C",
            event_type="return_to_work",
            event_date=date(2025, 11, 10),
            attributes=ReturnToWorkAttributes(duty_type="modified"),
            extraction_method="llm",
        ),
    )
    insert_event(
        conn,
        Event(
            event_id="term",
            claim_id="C",
            event_type="rtw_terminal",
            event_date=date(2026, 1, 6),
            attributes=RTWTerminalAttributes(
                reason="closed_no_rtw", context="settlement"
            ),
            extraction_method="llm",
        ),
    )
    result = q1_return_to_work(conn, "C")
    assert isinstance(result, Q1Returned)


def test_q1_never_returned(conn: Connection) -> None:
    _claim(conn)
    insert_event(
        conn,
        Event(
            event_id="term",
            claim_id="C",
            event_type="rtw_terminal",
            event_date=date(2026, 4, 12),
            attributes=RTWTerminalAttributes(reason="ptd"),
            extraction_method="llm",
        ),
    )
    result = q1_return_to_work(conn, "C")
    assert isinstance(result, Q1NeverReturned)
    assert result.reason == "ptd"
    assert result.terminal_date == date(2026, 4, 12)


def test_q1_pending(conn: Connection) -> None:
    _claim(conn, dol=date(2025, 1, 1))
    result = q1_return_to_work(conn, "C")
    assert isinstance(result, Q1Pending)
    assert result.days_open >= 0


# --- Q2 ---------------------------------------------------------


def test_q2_counts_only_attended(conn: Connection) -> None:
    _claim(conn)
    for i, status in enumerate(["attended", "attended", "missed", "scheduled"]):
        insert_event(
            conn,
            Event(
                event_id=f"a{i}",
                claim_id="C",
                event_type="appointment",
                event_date=date(2025, 6, i + 1),
                attributes=AppointmentAttributes(
                    status=status,  # type: ignore[arg-type]
                    provider=f"Dr. {status[0].upper()}",
                ),
                extraction_method="llm",
            ),
        )
    result = q2_appointments_attended(conn, "C")
    assert result.count == 2
    assert len(result.appointments) == 2


def test_q2_empty_when_no_attended(conn: Connection) -> None:
    _claim(conn)
    result = q2_appointments_attended(conn, "C")
    assert result.count == 0
    assert result.appointments == []


# --- Q3 ---------------------------------------------------------


def test_q3_per_bucket_summary(conn: Connection) -> None:
    _claim(conn)
    seed = [
        ("Indemnity (2) Lost Time", "280000.00", "0", "280000.00", date(2025, 1, 1), "e1"),
        ("Indemnity (2) Lost Time", "321014.00", "280000.00", "41014.00", date(2025, 5, 1), "e2"),
        ("Expense-Litigation (1) Medical Details", "148.25", "0", "148.25", date(2025, 2, 1), "e3"),
    ]
    for bucket, new, prev, delta, d, eid in seed:
        insert_event(
            conn,
            Event(
                event_id=eid,
                claim_id="C",
                event_type="reserve_change",
                event_date=d,
                attributes=ReserveChangeAttributes(
                    bucket=bucket,
                    new_amount=Decimal(new),
                    previous_amount=Decimal(prev),
                    delta=Decimal(delta),
                ),
                extraction_method="rule",
            ),
        )
    result = q3_reserve_changes(conn, "C")
    by_bucket = {b.bucket: b for b in result.buckets}
    lost_time = by_bucket["Indemnity (2) Lost Time"]
    assert lost_time.count == 2
    assert lost_time.net == Decimal("321014.00")  # 280000 + 41014
    litigation = by_bucket["Expense-Litigation (1) Medical Details"]
    assert litigation.count == 1
    assert litigation.net == Decimal("148.25")


# --- Q4 ---------------------------------------------------------


def test_q4_per_visit_and_distribution(conn: Connection) -> None:
    _claim(conn)
    seed = [
        # (notice, booked, occurred, expected lag, expected on_time_delta)
        (date(2025, 8, 29), date(2025, 9, 15), date(2025, 9, 15), 17, 0),
        (date(2025, 6, 1), date(2025, 6, 10), date(2025, 6, 12), 11, 2),
        (date(2025, 4, 1), date(2025, 5, 1), date(2025, 5, 1), 30, 0),
    ]
    for i, (notice, booked, occurred, _, _) in enumerate(seed):
        insert_event(
            conn,
            Event(
                event_id=f"v{i}",
                claim_id="C",
                event_type="appointment",
                event_date=occurred,
                attributes=AppointmentAttributes(
                    status="attended",
                    provider="Dr. X",
                    scheduled_notice_date=notice,
                    scheduled_for_date=booked,
                    occurred_on=occurred,
                ),
                extraction_method="merged",
            ),
        )
    result = q4_schedule_to_seen(conn, "C")
    lags = [v.lag_days for v in result.per_visit]
    assert sorted(lags) == [11, 17, 30]
    deltas = [v.on_time_delta_days for v in result.per_visit]
    assert sorted(deltas) == [0, 0, 2]
    assert result.lag.median == 17
    assert result.lag.mean is not None
    assert abs(result.lag.mean - (11 + 17 + 30) / 3) < 1e-9


def test_q4_skips_visits_missing_dates(conn: Connection) -> None:
    """An attended event without scheduled_notice_date or
    occurred_on cannot participate in Q4."""
    _claim(conn)
    insert_event(
        conn,
        Event(
            event_id="solo",
            claim_id="C",
            event_type="appointment",
            event_date=date(2025, 6, 1),
            attributes=AppointmentAttributes(
                status="attended",
                provider="Dr. X",
                occurred_on=date(2025, 6, 1),
                # scheduled_notice_date missing — can't compute lag
            ),
            extraction_method="llm",
        ),
    )
    result = q4_schedule_to_seen(conn, "C")
    assert result.per_visit == []
    assert result.lag.median is None
