"""SQLite DDL for the two-table model (data-modeling.md §1).

Date fields are stored as ISO-8601 TEXT so `julianday(...)` works
directly. The `attributes` column is JSON-as-TEXT, accessed via
SQLite's JSON1 functions. Money amounts inside `attributes` are
stored as quoted strings to preserve Decimal fidelity — the Q3 sum
is a CAST-to-REAL at query time, not in the storage layer.
"""

from sqlite3 import Connection

SCHEMA = """
CREATE TABLE IF NOT EXISTS claim (
    claim_id      TEXT PRIMARY KEY,
    account       TEXT,
    jurisdiction  TEXT,
    claim_type    TEXT NOT NULL CHECK (claim_type IN ('injury', 'illness')),
    date_of_loss  TEXT NOT NULL,
    source_file   TEXT NOT NULL,
    ingested_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event (
    event_id          TEXT PRIMARY KEY,
    claim_id          TEXT NOT NULL REFERENCES claim(claim_id) ON DELETE CASCADE,
    event_type        TEXT NOT NULL CHECK (event_type IN (
        'reserve_change', 'appointment', 'return_to_work'
    )),
    event_date        TEXT NOT NULL,
    attributes        TEXT NOT NULL,
    extraction_method TEXT NOT NULL CHECK (extraction_method IN (
        'rule', 'llm', 'merged'
    ))
);

CREATE INDEX IF NOT EXISTS idx_event_claim_type_date
    ON event (claim_id, event_type, event_date);
"""


def create_schema(conn: Connection) -> None:
    """Apply the DDL. Idempotent — safe to call on an existing DB."""
    conn.executescript(SCHEMA)
    conn.commit()
