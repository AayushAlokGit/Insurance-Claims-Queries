"""Compute Q1 (return-to-work) via the current per-note architecture.

Runs the per-note `ReturnToWorkExtractor` + `RtwTerminalExtractor`
on every note, then resolver dedup, then applies Q1's precedence
(returned > never_returned > pending) to pick the canonical answer.

Writes JSON to scripts/RtwScripts/<claim_id>-pernote-rtw.json with
the same shape as the whole-claim script for direct comparison.

Usage:
    python scripts/RtwScripts/rtw_via_per_note.py \\
        --file sample_claim_notes/sample_claim_notes1.md
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

from claims.extractor.rtw import ReturnToWorkExtractor  # noqa: E402
from claims.extractor.rtw_terminal import RtwTerminalExtractor  # noqa: E402
from claims.llm import get_client  # noqa: E402
from claims.loader import infer_claim_metadata, parse_file  # noqa: E402
from claims.log_config import setup_logging  # noqa: E402
from claims.models import (  # noqa: E402
    ReturnToWorkAttributes,
    RTWTerminalAttributes,
)
from claims.normalizer import normalize  # noqa: E402
from claims.resolver.rtw import resolve_rtw  # noqa: E402

_DEFAULT_OUT_DIR = REPO_ROOT / "scripts" / "RtwScripts"


def _pick_q1(rtws, terms, dol):  # type: ignore[no-untyped-def]
    """Returned wins over never_returned wins over pending.
    Same precedence as `claims.query.queries.q1_return_to_work`."""
    if rtws:
        rtws_sorted = sorted(rtws, key=lambda e: e.event_date)
        ev = rtws_sorted[0]
        a = ev.attributes
        assert isinstance(a, ReturnToWorkAttributes)
        return {
            "status": "returned",
            "days": (ev.event_date - dol).days if dol else None,
            "rtw_date": ev.event_date.isoformat(),
            "duty_type": a.duty_type,
            "role": a.role,
            "extraction_method": ev.extraction_method,
            "source_note_dates": [d.isoformat() for d in a.source_note_dates],
            "evidence_quote": a.evidence_quote,
        }
    if terms:
        terms_sorted = sorted(terms, key=lambda e: e.event_date)
        ev = terms_sorted[0]
        a = ev.attributes
        assert isinstance(a, RTWTerminalAttributes)
        return {
            "status": "never_returned",
            "reason": a.reason,
            "terminal_date": ev.event_date.isoformat(),
            "context": a.context,
            "extraction_method": ev.extraction_method,
            "source_note_dates": [d.isoformat() for d in a.source_note_dates],
            "evidence_quote": a.evidence_quote,
        }
    days_open = (date.today() - dol).days if dol else None
    return {"status": "pending", "days_open": days_open}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--file", required=True)
    p.add_argument("--provider", choices=["google", "openai"], default=None)
    p.add_argument("--out", default=None)
    args = p.parse_args()

    load_dotenv()
    path = Path(args.file)
    log_path = setup_logging(
        subcommand="rtw_via_per_note",
        claim_id=path.stem,
        tags={"mode": args.provider or "google"},
    )
    print(f"log: {log_path}", file=sys.stderr)

    loaded = parse_file(str(path))
    notes = [
        n
        for i, raw in enumerate(loaded.notes)
        if (n := normalize(raw, index=i)) is not None
    ]
    inferred = infer_claim_metadata(loaded)
    dol = inferred.date_of_loss

    llm = get_client(args.provider)
    rtw_ex = ReturnToWorkExtractor(llm)
    term_ex = RtwTerminalExtractor(llm)

    rtw_events = []
    term_events = []
    for note in notes:
        if rtw_ex.can_handle(note):
            rtw_events.extend(rtw_ex.extract(note))
        if term_ex.can_handle(note):
            term_events.extend(term_ex.extract(note))

    rtw_events = resolve_rtw(rtw_events)
    # rtw_terminal is pass-through in the production resolver.

    print(
        f"per-note candidates: {len(rtw_events)} rtw, "
        f"{len(term_events)} terminal (after resolver)",
        file=sys.stderr,
    )

    answer = _pick_q1(rtw_events, term_events, dol)
    payload = {
        "claim_id": loaded.header.claim_id,
        "method": "per-note + resolver",
        "date_of_loss": dol.isoformat() if dol else None,
        "q1": answer,
        "raw_rtw_events": [
            {
                "event_date": e.event_date.isoformat(),
                "duty_type": e.attributes.duty_type,  # type: ignore[union-attr]
                "role": e.attributes.role,  # type: ignore[union-attr]
                "source_note_dates": [
                    d.isoformat() for d in e.attributes.source_note_dates  # type: ignore[union-attr]
                ],
                "evidence_quote": e.attributes.evidence_quote,  # type: ignore[union-attr]
            }
            for e in rtw_events
        ],
        "raw_terminal_events": [
            {
                "event_date": e.event_date.isoformat(),
                "reason": e.attributes.reason,  # type: ignore[union-attr]
                "context": e.attributes.context,  # type: ignore[union-attr]
                "source_note_dates": [
                    d.isoformat() for d in e.attributes.source_note_dates  # type: ignore[union-attr]
                ],
                "evidence_quote": e.attributes.evidence_quote,  # type: ignore[union-attr]
            }
            for e in term_events
        ],
    }

    out_path = Path(args.out) if args.out else (
        _DEFAULT_OUT_DIR / f"{loaded.header.claim_id}-pernote-rtw.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"wrote {out_path} (status={answer['status']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
