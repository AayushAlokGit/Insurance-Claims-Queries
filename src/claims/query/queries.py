"""The four canned query functions. Each takes a sqlite Connection
and a claim_id; reads via JSON1 over the event table; returns a
typed result. See query_feasibility_analysis/ for spec."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from sqlite3 import Connection
from statistics import median

from claims.models import EventEvidence
from claims.query.types import (
    Q1Pending,
    Q1Result,
    Q1Returned,
    Q2Appointment,
    Q2Result,
    Q3BucketSummary,
    Q3Result,
    Q3Swing,
    Q4Distribution,
    Q4Result,
    Q4Visit,
)


def _date_of_loss(conn: Connection, claim_id: str) -> date:
    row = conn.execute(
        "SELECT date_of_loss FROM claim WHERE claim_id = ?", (claim_id,)
    ).fetchone()
    if row is None:
        raise LookupError(f"claim {claim_id!r} not found")
    return date.fromisoformat(row["date_of_loss"])


def _today_iso() -> str:
    return date.today().isoformat()


def q1_return_to_work(conn: Connection, claim_id: str) -> Q1Result:
    """Discriminated-union answer to "how long to return to work?".

    `returned` if any return_to_work event exists; otherwise
    `pending`."""
    dol = _date_of_loss(conn, claim_id)

    rtw = conn.execute(
        """
        SELECT event_id,
               event_date,
               extraction_method,
               json_extract(attributes, '$.duty_type') AS duty_type,
               json_extract(attributes, '$.role')      AS role,
               json_extract(attributes, '$.evidence')  AS evidence
        FROM event
        WHERE claim_id = ?
          AND event_type = 'return_to_work'
        ORDER BY event_date
        LIMIT 1
        """,
        (claim_id,),
    ).fetchone()
    if rtw is not None:
        rtw_date = date.fromisoformat(rtw["event_date"])
        return Q1Returned(
            days=(rtw_date - dol).days,
            rtw_date=rtw_date,
            duty_type=rtw["duty_type"],
            role=rtw["role"],
            event_id=rtw["event_id"],
            extraction_method=rtw["extraction_method"],
            evidence=_parse_evidence(rtw["evidence"]),
        )

    days_open = (date.today() - dol).days
    return Q1Pending(days_open=days_open)


def _parse_parties(raw: str | None) -> list[str]:
    """JSON1 surfaces the stored tuple as a JSON array string;
    decode it into a Python list. Null / missing → empty list."""
    if raw is None:
        return []
    try:
        loaded = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(loaded, list):
        return []
    return [str(x) for x in loaded if isinstance(x, str)]


def _parse_evidence(raw: str | None) -> list[EventEvidence]:
    """Decode the stored `evidence` JSON array into typed pairs.
    Malformed entries are silently skipped."""
    if raw is None:
        return []
    try:
        loaded = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(loaded, list):
        return []
    out: list[EventEvidence] = []
    for x in loaded:
        if not isinstance(x, dict):
            continue
        try:
            out.append(
                EventEvidence(
                    note_date=date.fromisoformat(x["note_date"]),
                    quote=str(x["quote"]),
                )
            )
        except (KeyError, ValueError, TypeError):
            continue
    return out


def q2_appointments_attended(conn: Connection, claim_id: str) -> Q2Result:
    rows = conn.execute(
        """
        SELECT event_id,
               event_date,
               extraction_method,
               json_extract(attributes, '$.parties')           AS parties,
               json_extract(attributes, '$.specialty')         AS specialty,
               json_extract(attributes, '$.appointment_type')  AS appointment_type,
               json_extract(attributes, '$.evidence')          AS evidence
        FROM event
        WHERE claim_id = ?
          AND event_type = 'appointment'
          AND json_extract(attributes, '$.status') = 'attended'
        ORDER BY event_date
        """,
        (claim_id,),
    ).fetchall()
    appointments = [
        Q2Appointment(
            date=date.fromisoformat(r["event_date"]),
            parties=_parse_parties(r["parties"]),
            specialty=r["specialty"],
            appointment_type=r["appointment_type"],
            event_id=r["event_id"],
            extraction_method=r["extraction_method"],
            evidence=_parse_evidence(r["evidence"]),
        )
        for r in rows
    ]
    return Q2Result(appointments=appointments, count=len(appointments))


def q3_reserve_changes(conn: Connection, claim_id: str) -> Q3Result:
    rows = conn.execute(
        """
        SELECT event_id,
               event_date,
               extraction_method,
               json_extract(attributes, '$.bucket')            AS bucket,
               json_extract(attributes, '$.new_amount')        AS new_amount,
               json_extract(attributes, '$.previous_amount')   AS previous_amount,
               json_extract(attributes, '$.delta')             AS delta,
               json_extract(attributes, '$.author')            AS author,
               json_extract(attributes, '$.evidence')          AS evidence
        FROM event
        WHERE claim_id = ?
          AND event_type = 'reserve_change'
        ORDER BY bucket, event_date
        """,
        (claim_id,),
    ).fetchall()
    by_bucket: dict[str, list[Q3Swing]] = {}
    for r in rows:
        # JSON-stored numerics come back as strings; round-trip via Decimal.
        swing = Q3Swing(
            date=date.fromisoformat(r["event_date"]),
            new_amount=Decimal(str(r["new_amount"])),
            previous_amount=Decimal(str(r["previous_amount"])),
            delta=Decimal(str(r["delta"])),
            author=r["author"],
            event_id=r["event_id"],
            extraction_method=r["extraction_method"],
            evidence=_parse_evidence(r["evidence"]),
        )
        by_bucket.setdefault(r["bucket"], []).append(swing)
    summaries = [
        Q3BucketSummary(
            bucket=bucket,
            count=len(swings),
            net=sum((s.delta for s in swings), Decimal("0")),
            swings=swings,
        )
        for bucket, swings in by_bucket.items()
    ]
    summaries.sort(key=lambda s: s.bucket)
    return Q3Result(buckets=summaries)


def _percentile(values: list[int], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    frac = k - lo
    return s[lo] + (s[hi] - s[lo]) * frac


def q4_schedule_to_seen(conn: Connection, claim_id: str) -> Q4Result:
    """Per-visit access-to-care lag, plus a distribution summary.

    Only attended appointments that have both `scheduled_notice_date`
    and `occurred_on` populated participate. The Resolver's
    proximity merge is what pairs the two one-sided extractor
    outputs into a single event."""
    rows = conn.execute(
        """
        SELECT event_id,
               extraction_method,
               json_extract(attributes, '$.parties')              AS parties,
               json_extract(attributes, '$.scheduled_notice_date') AS notice,
               json_extract(attributes, '$.scheduled_for_date')    AS booked,
               json_extract(attributes, '$.occurred_on')           AS occurred,
               json_extract(attributes, '$.evidence')              AS evidence
        FROM event
        WHERE claim_id = ?
          AND event_type = 'appointment'
          AND json_extract(attributes, '$.status') = 'attended'
          AND json_extract(attributes, '$.scheduled_notice_date') IS NOT NULL
          AND json_extract(attributes, '$.occurred_on') IS NOT NULL
        ORDER BY notice
        """,
        (claim_id,),
    ).fetchall()
    visits: list[Q4Visit] = []
    lags: list[int] = []
    for r in rows:
        notice = date.fromisoformat(r["notice"])
        occurred = date.fromisoformat(r["occurred"])
        # `booked` may be null if the event came from a record-only
        # source — fall back to occurred for the on-time delta math.
        booked = (
            date.fromisoformat(r["booked"])
            if r["booked"] is not None
            else occurred
        )
        lag_days = (occurred - notice).days
        on_time_delta = (occurred - booked).days
        visits.append(
            Q4Visit(
                parties=_parse_parties(r["parties"]),
                scheduled_notice_date=notice,
                scheduled_for_date=booked,
                occurred_on=occurred,
                lag_days=lag_days,
                on_time_delta_days=on_time_delta,
                event_id=r["event_id"],
                extraction_method=r["extraction_method"],
                evidence=_parse_evidence(r["evidence"]),
            )
        )
        lags.append(lag_days)

    if lags:
        dist = Q4Distribution(
            median=float(median(lags)),
            p90=_percentile(lags, 0.9),
            mean=sum(lags) / len(lags),
        )
    else:
        dist = Q4Distribution(median=None, p90=None, mean=None)
    return Q4Result(per_visit=visits, lag=dist)
