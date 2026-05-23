"""Thin repository over the claim/event tables.

One function per storage need — not an ORM. Each function takes a
Connection so callers control transactions. Pydantic does the
JSON serialization for `attributes`; reads round-trip through
`Event.model_validate` so the discriminated-union dispatch happens
in one place.
"""

import json
from datetime import date, datetime
from decimal import Decimal
from sqlite3 import Connection

from claims.models import Claim, Event


def insert_claim(conn: Connection, claim: Claim) -> None:
    conn.execute(
        "INSERT INTO claim "
        "(claim_id, account, jurisdiction, claim_type, "
        " date_of_loss, source_file, ingested_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            claim.claim_id,
            claim.account,
            claim.jurisdiction,
            claim.claim_type,
            claim.date_of_loss.isoformat(),
            claim.source_file,
            claim.ingested_at.isoformat(),
        ),
    )


def insert_event(conn: Connection, event: Event) -> None:
    conn.execute(
        "INSERT INTO event "
        "(event_id, claim_id, event_type, event_date, "
        " attributes, extraction_method) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            event.event_id,
            event.claim_id,
            event.event_type,
            event.event_date.isoformat(),
            event.attributes.model_dump_json(),
            event.extraction_method,
        ),
    )


def get_claim(conn: Connection, claim_id: str) -> Claim | None:
    row = conn.execute(
        "SELECT * FROM claim WHERE claim_id = ?", (claim_id,)
    ).fetchone()
    if row is None:
        return None
    return Claim(
        claim_id=row["claim_id"],
        account=row["account"],
        jurisdiction=row["jurisdiction"],
        claim_type=row["claim_type"],
        date_of_loss=date.fromisoformat(row["date_of_loss"]),
        source_file=row["source_file"],
        ingested_at=datetime.fromisoformat(row["ingested_at"]),
    )


def get_events_for_claim(conn: Connection, claim_id: str) -> list[Event]:
    rows = conn.execute(
        "SELECT * FROM event WHERE claim_id = ? "
        "ORDER BY event_date, event_id",
        (claim_id,),
    ).fetchall()
    return [_row_to_event(row) for row in rows]


def _row_to_event(row: object) -> Event:
    # sqlite3.Row supports mapping access; the model validator
    # dispatches the right *Attributes subclass via the inner
    # `type` discriminator.
    return Event.model_validate(
        {
            "event_id": row["event_id"],  # type: ignore[index]
            "claim_id": row["claim_id"],  # type: ignore[index]
            "event_type": row["event_type"],  # type: ignore[index]
            "event_date": row["event_date"],  # type: ignore[index]
            "attributes": json.loads(row["attributes"]),  # type: ignore[index]
            "extraction_method": row["extraction_method"],  # type: ignore[index]
        }
    )


def sum_reserve_deltas_per_claim(
    conn: Connection,
) -> dict[str, Decimal]:
    """Q3-shape aggregation.

    Sums `attributes.delta` across all reserve_change events for
    each claim. Returns `{claim_id: total_delta}`. NULL deltas
    (e.g. first-set events the Resolver hasn't computed a delta
    for) are skipped by SUM. Cast through str → Decimal to avoid
    float drift on the way out."""
    rows = conn.execute(
        """
        SELECT claim_id,
               TOTAL(CAST(json_extract(attributes, '$.delta') AS REAL))
                   AS total_delta
        FROM event
        WHERE event_type = 'reserve_change'
        GROUP BY claim_id
        """
    ).fetchall()
    return {
        row["claim_id"]: Decimal(str(row["total_delta"]))
        for row in rows
    }
