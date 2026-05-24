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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from claims.extractor import (
    default_extractors,
    reconcile_appointments,
    run_all,
)
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
    """Run the full pipeline on one claim file and upsert into SQLite."""
    load_dotenv()
    path = Path(args.file)

    # --- Per-run logging --------------------------------------
    # File stem stands in for claim_id in the log filename until
    # the loader has actually parsed the file. Tags carry the
    # other run-distinguishing knobs (extractor mode, parallelism)
    # so successive runs against the same file don't collide.
    mode_tag = "rule-only" if args.no_llm else (args.provider or "google")
    log_path = setup_logging(
        subcommand="ingest",
        claim_id=path.stem,
        tags={
            "mode": mode_tag,
            "workers": f"w{args.workers}",
        },
        verbose=args.verbose,
    )
    log = logging.getLogger("claims.cli")
    log.info("ingest start file=%s db=%s log=%s", path.name, args.db, log_path)

    # --- Loader: file → RawNoteBlocks → Normalizer → Notes ----
    loaded = parse_file(str(path))
    notes = []
    for i, raw in enumerate(loaded.notes):
        n = normalize(raw, index=i)
        if n is None:
            # Normalizer rejects only on unparseable header dates.
            log.debug("note %d rejected by normalizer", i)
            continue
        notes.append(n)
    log.info(
        "loaded claim_id=%s raw_notes=%d normalized=%d",
        loaded.header.claim_id,
        len(loaded.notes),
        len(notes),
    )

    # --- Claim-level metadata (regex inference + CLI overrides) -
    inferred = infer_claim_metadata(loaded)
    dol_str: str | None = args.date_of_loss or (
        inferred.date_of_loss.isoformat() if inferred.date_of_loss else None
    )
    if dol_str is None:
        # Q1 needs date_of_loss; refuse to ingest without it.
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

    # --- Extractor registry ----------------------------------
    # --no-llm short-circuits to the rule-only set (Reserve +
    # Marker). The full set adds the three LLM extractors.
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

    # --- Per-note extraction (serial or parallel) ------------
    # Per-note extraction is independent (DD-006). DD-015 turns
    # that into bounded parallelism; --workers 1 restores the
    # serial path for deterministic / quota-sensitive runs.
    raw_events: list = []
    workers = max(1, args.workers)
    if workers == 1:
        log.info("extraction concurrency: serial")
        for note in notes:
            events = run_all(note, extractors=extractors)
            if events:
                log.debug("note %s -> %d events", note.note_id, len(events))
            raw_events.extend(events)
    else:
        # Threads (not async): LLM SDKs are sync, calls are I/O-bound,
        # GIL releases on I/O. DD-014's retry-with-jitter absorbs the
        # 429s that bunch up at higher worker counts.
        log.info("extraction concurrency: %d workers", workers)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(run_all, n, extractors=extractors): n
                for n in notes
            }
            for fut in as_completed(futures):
                note = futures[fut]
                try:
                    events = fut.result()
                except Exception as exc:
                    # One failing note shouldn't take down the run.
                    log.warning(
                        "note %s extraction failed: %s", note.note_id, exc
                    )
                    continue
                if events:
                    log.debug(
                        "note %s -> %d events", note.note_id, len(events)
                    )
                raw_events.extend(events)
    log.info("raw events: %d", len(raw_events))
    # Per-type counts help spot extraction-stage drop-outs.
    by_type: dict[str, int] = {}
    for ev in raw_events:
        by_type[ev.event_type] = by_type.get(ev.event_type, 0) + 1
    for et, n in sorted(by_type.items()):
        log.info("  raw %s: %d", et, n)

    # --- Reconciliation (DD-019): per-claim LLM call --------
    # Appointments use a per-claim reconciliation pass instead of
    # the resolver's per-pair merge. The reconciliation LLM sees
    # every candidate at once and produces the canonical list —
    # the cross-note attribution context that the per-note layer
    # cannot have. If no LLM is configured (`--no-llm`), pass
    # candidates through unchanged.
    appt_candidates = [e for e in raw_events if e.event_type == "appointment"]
    other_events = [e for e in raw_events if e.event_type != "appointment"]
    if args.no_llm:
        reconciled_appts = appt_candidates
        log.info(
            "reconciliation skipped (--no-llm); %d appt candidates "
            "pass through",
            len(appt_candidates),
        )
    else:
        reconciled_appts = reconcile_appointments(
            loaded.header.claim_id,
            date.fromisoformat(dol_str),
            appt_candidates,
            llm,
        )

    # --- Resolver: dedup + cross-event derivations (non-appt) -
    # Reserve deltas + RTW identity-merge. Appointments now bypass
    # the resolver under DD-019.
    resolved = resolve(other_events) + reconciled_appts
    log.info("resolved events: %d", len(resolved))
    by_type = {}
    for ev in resolved:
        by_type[ev.event_type] = by_type.get(ev.event_type, 0) + 1
    for et, n in sorted(by_type.items()):
        log.info("  resolved %s: %d", et, n)

    # --- Store: upsert the claim and its events --------------
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
    # Replace this claim's prior events so re-ingest is idempotent.
    # FK cascade would also drop them on claim delete, but explicit
    # is safer when the schema evolves.
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
    """Run one of the four canned queries and print typed JSON."""
    log_path = setup_logging(
        subcommand="query",
        claim_id=args.claim_id,
        tags={"which": args.which},
    )
    log = logging.getLogger("claims.cli")
    log.info(
        "query start which=%s claim_id=%s db=%s log=%s",
        args.which,
        args.claim_id,
        args.db,
        log_path,
    )
    conn = connect(args.db)
    # argparse already restricts `which` to q1-q4 via choices=…
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
    # Pydantic JSON respects Decimal precision + ISO date strings.
    print(result.model_dump_json(indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    """Top-level CLI dispatch. argv is exposed so tests can drive
    the CLI without spawning a subprocess."""
    parser = argparse.ArgumentParser(prog="claims", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    # --- ingest subcommand -----------------------------------
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
        "--workers",
        type=int,
        default=4,
        help="Per-note extraction parallelism (default: 4). "
        "Higher values mean faster ingest but more concurrent LLM "
        "calls — keep below the provider's RPM limit. Set to 1 "
        "for fully serial execution.",
    )
    ingest.add_argument(
        "--verbose",
        action="store_true",
        help="DEBUG-level console output. The log file always "
        "contains DEBUG-level detail regardless.",
    )

    # --- query subcommand ------------------------------------
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
