"""Manual smoke test for the RTW + RTW-terminal extractors.

Loads .env, normalizes a sample claim, runs both Q1 extractors
against every note that passes the respective prefilter, prints
what came back. Hits the live LLM provider — burns quota.

Usage:
    py -3.12 -m uv run python scripts/try_rtw_llm.py
    py -3.12 -m uv run python scripts/try_rtw_llm.py --claim 2 --limit 5
    py -3.12 -m uv run python scripts/try_rtw_llm.py --extractor terminal
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
        help="Process at most N notes per extractor that pass the prefilter.",
    )
    parser.add_argument(
        "--extractor",
        choices=["rtw", "terminal", "both"],
        default="both",
        help="Which extractor(s) to run (default: both).",
    )
    parser.add_argument(
        "--provider",
        choices=["google", "openai"],
        default=None,
        help="Override LLM_PROVIDER for this run.",
    )
    parser.add_argument(
        "--body-preview",
        type=int,
        default=160,
        help="Body characters to print per note (default: 160).",
    )
    parser.add_argument(
        "--no-log",
        action="store_true",
        help="Disable writing a log file under ./logs/.",
    )
    args = parser.parse_args()

    from claims.extractor.rtw import ReturnToWorkExtractor
    from claims.extractor.rtw_terminal import RtwTerminalExtractor
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
            / f"{stamp}-{path.stem}-claim{args.claim}-"
            f"{args.extractor}-{provider_for_name}.log"
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
        print(f"== LLM provider={provider}  model={llm.model} ==\n")

        extractors = []
        if args.extractor in ("rtw", "both"):
            extractors.append(("rtw", ReturnToWorkExtractor(llm)))
        if args.extractor in ("terminal", "both"):
            extractors.append(("terminal", RtwTerminalExtractor(llm)))

        for label, extractor in extractors:
            print(f"==== {label} ====")
            eligible = [n for n in notes if extractor.can_handle(n)]
            print(
                f"  {len(eligible)} of {len(notes)} notes pass the prefilter"
            )
            if args.limit is not None:
                eligible = eligible[: args.limit]
                print(f"  --limit applied: running on {len(eligible)} notes")
            if not eligible:
                print("  (nothing to do)\n")
                continue

            emitted = 0
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
                    print("  -> no event")
                    print()
                    continue
                for ev in events:
                    attrs = ev.attributes
                    if attrs.type == "return_to_work":
                        print(
                            f"  -> RTW  duty={attrs.duty_type}  "
                            f"date={ev.event_date}  role={attrs.role!r}"
                        )
                    elif attrs.type == "rtw_terminal":
                        print(
                            f"  -> TERMINAL  reason={attrs.reason}  "
                            f"date={ev.event_date}  context={attrs.context!r}"
                        )
                    emitted += 1
                print()
            print(f"  total events: {emitted}\n")

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
