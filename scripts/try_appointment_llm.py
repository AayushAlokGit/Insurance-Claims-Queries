"""Manual smoke test for AppointmentExtractor against a real
sample claim. Loads .env, normalizes the file, runs the LLM-
backed extractor against every note that passes the prefilter,
and prints what came back.

NOT a pytest test — this hits the live LLM provider and burns
quota. Run it deliberately, with --limit, when iterating on the
prompt or comparing providers.

Usage:
    py -3.12 -m uv run python scripts/try_appointment_llm.py
    py -3.12 -m uv run python scripts/try_appointment_llm.py --claim 2 --limit 3
    py -3.12 -m uv run python scripts/try_appointment_llm.py --provider openai
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLES = REPO_ROOT / "sample_claim_notes"
LOGS_DIR = REPO_ROOT / "logs"


class _Tee:
    """Write to multiple text streams (stdout + log file)."""

    def __init__(self, *streams) -> None:
        self._streams = streams

    def write(self, data: str) -> int:
        for s in self._streams:
            s.write(data)
            s.flush()
        return len(data)

    def flush(self) -> None:
        for s in self._streams:
            s.flush()


def main() -> int:
    load_dotenv(REPO_ROOT / ".env")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--claim",
        choices=["1", "2"],
        default="1",
        help="Which sample claim to process (default: 1).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process at most N notes that pass the prefilter.",
    )
    parser.add_argument(
        "--provider",
        choices=["google", "openai"],
        default=None,
        help="Override LLM_PROVIDER env var for this run.",
    )
    parser.add_argument(
        "--body-preview",
        type=int,
        default=160,
        help="Number of body characters to print per note "
        "(default: 160).",
    )
    parser.add_argument(
        "--no-log",
        action="store_true",
        help="Disable writing a log file under ./logs/.",
    )
    args = parser.parse_args()

    # Imports after load_dotenv so the SDK clients see the env vars.
    from claims.extractor import AppointmentExtractor
    from claims.llm import get_client
    from claims.loader import parse_file
    from claims.normalizer import normalize

    path = SAMPLES / f"sample_claim_notes{args.claim}.md"

    log_fh = None
    if not args.no_log:
        script_log_dir = LOGS_DIR / Path(__file__).stem
        script_log_dir.mkdir(parents=True, exist_ok=True)
        started_at = datetime.now(timezone.utc)
        stamp = started_at.strftime("%Y%m%d-%H%M%S")
        provider_for_name = args.provider or os.environ.get(
            "LLM_PROVIDER", "google"
        )
        log_path = (
            script_log_dir
            / f"{stamp}-{path.stem}-claim{args.claim}-{provider_for_name}.log"
        )
        log_fh = open(log_path, "w", encoding="utf-8")
        sys.stdout = _Tee(sys.__stdout__, log_fh)
        print("==================== RUN START ====================")
        print(f"  started_at_utc : {started_at.isoformat(timespec='seconds')}")
        print(f"  log_file       : {log_path}")
        print(f"  cwd            : {Path.cwd()}")
        print(f"  argv           : {' '.join(sys.argv)}")
        print(f"  args           : {vars(args)}")
        print(f"  python         : {sys.version.split()[0]}")
        print("===================================================")
        print()
    run_start = datetime.now(timezone.utc)
    exit_code = 0
    try:
        print(f"== Loading {path.name} ==")
        loaded = parse_file(str(path))
        notes = []
        for i, raw in enumerate(loaded.notes):
            n = normalize(raw, index=i)
            if n is not None:
                notes.append(n)
        print(
            f"  claim_id={loaded.header.claim_id}  "
            f"raw_notes={len(loaded.notes)}  normalized={len(notes)}"
        )

        try:
            llm = get_client(args.provider)
        except Exception as exc:
            print(f"\nFailed to construct LLM client: {exc}", file=sys.stderr)
            exit_code = 1
            return exit_code

        provider = args.provider or os.environ.get("LLM_PROVIDER", "google")
        print(f"== LLM provider={provider}  model={llm.model} ==")

        extractor = AppointmentExtractor(llm)

        eligible = [n for n in notes if extractor.can_handle(n)]
        print(f"  {len(eligible)} of {len(notes)} notes pass the prefilter")
        if args.limit is not None:
            eligible = eligible[: args.limit]
            print(f"  --limit applied: running on {len(eligible)} notes")

        if not eligible:
            print("Nothing to do.")
            return 0

        print()
        by_status: dict[str, int] = {}
        total = 0
        for note in eligible:
            body_preview = " ".join(note.body.split())[: args.body_preview]
            print(
                f"--- {note.note_id}  date={note.note_date}  "
                f"activity={note.activity!r} ---"
            )
            print(f"  body: {body_preview}…")
            try:
                events = extractor.extract(note)
            except Exception as exc:
                print(f"  ! extraction failed: {exc}")
                continue
            if not events:
                print("  -> no appointments")
                print()
                continue
            for ev in events:
                attrs = ev.attributes
                assert attrs.type == "appointment"
                occurred = attrs.occurred_on
                scheduled = attrs.scheduled_for_date
                date_str = occurred or scheduled or "—"
                parties_str = (
                    ", ".join(attrs.parties) if attrs.parties else "—"
                )
                print(
                    f"  -> status={attrs.status:<9}  date={date_str}  "
                    f"parties=[{parties_str}]"
                )
                print(f"     quote: {attrs.evidence_quote!r}")
                by_status[attrs.status] = by_status.get(attrs.status, 0) + 1
            total += len(events)
            print()

        print("== Summary ==")
        print(f"  events emitted: {total}")
        for status in ("attended", "missed", "cancelled", "scheduled"):
            if status in by_status:
                print(f"    {status}: {by_status[status]}")
        return 0
    finally:
        if log_fh is not None:
            run_end = datetime.now(timezone.utc)
            duration = (run_end - run_start).total_seconds()
            print()
            print("===================== RUN END =====================")
            print(f"  ended_at_utc   : {run_end.isoformat(timespec='seconds')}")
            print(f"  duration_sec   : {duration:.2f}")
            print(f"  exit_code      : {exit_code}")
            print("===================================================")
            sys.stdout = sys.__stdout__
            log_fh.close()


if __name__ == "__main__":
    raise SystemExit(main())
