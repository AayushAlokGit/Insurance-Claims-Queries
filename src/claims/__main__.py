"""Claims CLI.

Two subcommands:

    python -m claims ingest --file sample_claim_notes/sample_claim_notes1.md \\
        --claim-type injury --date-of-loss 2024-12-21 \\
        --jurisdiction NJ --db claims.db

    python -m claims query q1 --claim-id 1-29RT --db claims.db
    python -m claims query q2 --claim-id 1-29RT --db claims.db
    python -m claims query q3 --claim-id 1-29RT --db claims.db
    python -m claims query q4 --claim-id 1-29RT --db claims.db

`ingest` loads .env, runs the pipeline (Loader → Normalizer →
Extractors → Resolver) on a single notes file, and writes the
resolved events to the SQLite database. `query` reads from the
DB and prints the typed result as indented JSON.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from claims.extractor import default_extractors, run_all
from claims.llm import get_client
from claims.loader import infer_claim_metadata, parse_file
from claims.log_config import setup_logging
from claims.models import Claim
from claims.normalizer import normalize
from claims.query import (
    q1_return_to_work,
    q2_appointments_attended,
    q3_reserve_changes,
    q4_schedule_to_seen,
)
from claims.resolver import resolve
from claims.store import (
    connect,
    create_schema,
    insert_claim,
    insert_event,
)


def _cmd_ingest(args: argparse.Namespace) -> int:
    load_dotenv()
    path = Path(args.file)
    # We don't know the claim_id until after parse, so use the
    # file stem as the placeholder for the log filename — set
    # the real one after parse completes.
    log_path = setup_logging(claim_id=path.stem, verbose=args.verbose)
    log = logging.getLogger("claims.cli")
    log.info("ingest start file=%s db=%s log=%s", path.name, args.db, log_path)

    loaded = parse_file(str(path))
    notes = []
    for i, raw in enumerate(loaded.notes):
        n = normalize(raw, index=i)
        if n is None:
            log.debug("note %d rejected by normalizer", i)
            continue
        notes.append(n)
    log.info(
        "loaded claim_id=%s raw_notes=%d normalized=%d",
        loaded.header.claim_id,
        len(loaded.notes),
        len(notes),
    )

    inferred = infer_claim_metadata(loaded)
    dol_str: str | None = args.date_of_loss or (
        inferred.date_of_loss.isoformat() if inferred.date_of_loss else None
    )
    if dol_str is None:
        log.error(
            "date_of_loss could not be inferred from %s; pass --date-of-loss",
            path.name,
        )
        return 1
    jurisdiction = args.jurisdiction or inferred.jurisdiction
    claim_type = args.claim_type or inferred.claim_type
    log.info(
        "metadata date_of_loss=%s jurisdiction=%s claim_type=%s "
        "(inferred where flag absent)",
        dol_str,
        jurisdiction,
        claim_type,
    )

    if args.no_llm:
        extractors = None  # run_all defaults to rule-only
        log.info("extractors: rule-only (--no-llm)")
    else:
        llm = get_client(args.provider)
        extractors = default_extractors(llm)
        provider = args.provider or "google"
        log.info(
            "extractors: full (provider=%s model=%s)", provider, llm.model
        )

    raw_events: list = []
    for note in notes:
        events = run_all(note, extractors=extractors)
        if events:
            log.debug(
                "note %s -> %d events", note.note_id, len(events)
            )
        raw_events.extend(events)
    log.info("raw events: %d", len(raw_events))
    by_type: dict[str, int] = {}
    for ev in raw_events:
        by_type[ev.event_type] = by_type.get(ev.event_type, 0) + 1
    for et, n in sorted(by_type.items()):
        log.info("  raw %s: %d", et, n)

    resolved = resolve(raw_events)
    log.info("resolved events: %d", len(resolved))
    by_type = {}
    for ev in resolved:
        by_type[ev.event_type] = by_type.get(ev.event_type, 0) + 1
    for et, n in sorted(by_type.items()):
        log.info("  resolved %s: %d", et, n)

    conn = connect(args.db)
    create_schema(conn)
    claim = Claim(
        claim_id=loaded.header.claim_id,
        account=loaded.header.account_raw,
        jurisdiction=jurisdiction,
        claim_type=claim_type,
        date_of_loss=date.fromisoformat(dol_str),
        source_file=loaded.header.source_file,
        ingested_at=datetime.now(tz=timezone.utc),
    )
    # Upsert: replace any existing claim+events so re-ingest is idempotent.
    with conn:
        conn.execute(
            "DELETE FROM event WHERE claim_id = ?", (claim.claim_id,)
        )
        conn.execute(
            "DELETE FROM claim WHERE claim_id = ?", (claim.claim_id,)
        )
        insert_claim(conn, claim)
        for ev in resolved:
            insert_event(conn, ev)
    log.info("wrote to %s", args.db)
    log.info("ingest done; log saved at %s", log_path)
    return 0


def _cmd_query(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    if args.which == "q1":
        result = q1_return_to_work(conn, args.claim_id)
    elif args.which == "q2":
        result = q2_appointments_attended(conn, args.claim_id)
    elif args.which == "q3":
        result = q3_reserve_changes(conn, args.claim_id)
    elif args.which == "q4":
        result = q4_schedule_to_seen(conn, args.claim_id)
    else:
        print(f"unknown query: {args.which}", file=sys.stderr)
        return 1
    print(result.model_dump_json(indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="claims", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    ingest = sub.add_parser("ingest", help="Load a claim file into SQLite.")
    ingest.add_argument("--file", required=True, help="Path to notes file.")
    ingest.add_argument(
        "--claim-type",
        choices=["injury", "illness"],
        default=None,
        help="Override the inferred claim type (default: inferred, "
        "fallback 'injury').",
    )
    ingest.add_argument(
        "--date-of-loss",
        default=None,
        help="Override the inferred date of loss (ISO). Required only "
        "if inference fails.",
    )
    ingest.add_argument(
        "--jurisdiction",
        default=None,
        help="Override the inferred jurisdiction (e.g. 'NJ').",
    )
    ingest.add_argument("--db", default="claims.db")
    ingest.add_argument(
        "--no-llm",
        action="store_true",
        help="Skip LLM-backed extractors (rule-only).",
    )
    ingest.add_argument(
        "--provider",
        choices=["google", "openai"],
        default=None,
        help="Override LLM_PROVIDER env var.",
    )
    ingest.add_argument(
        "--verbose",
        action="store_true",
        help="DEBUG-level console output. The log file always "
        "contains DEBUG-level detail regardless.",
    )

    query = sub.add_parser("query", help="Run a canned query.")
    query.add_argument("which", choices=["q1", "q2", "q3", "q4"])
    query.add_argument("--claim-id", required=True)
    query.add_argument("--db", default="claims.db")

    args = parser.parse_args(argv)
    if args.cmd == "ingest":
        return _cmd_ingest(args)
    if args.cmd == "query":
        return _cmd_query(args)
    parser.error(f"unknown command {args.cmd!r}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
