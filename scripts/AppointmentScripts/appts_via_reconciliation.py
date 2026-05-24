"""Extract appointments via the current architecture:
per-note AppointmentExtractor candidates -> reconcile_appointments.

Writes a JSON file at
sample_claim_notes/query_outputs/<claim_id>-recon.json
containing the canonical appointment list in a shape comparable
to the whole-claim approach (see appts_via_whole_claim_llm.py).

Usage:
    python scripts/appts_via_reconciliation.py \\
        --file sample_claim_notes/sample_claim_notes1.md
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

from claims.extractor import (  # noqa: E402
    AppointmentExtractor,
    reconcile_appointments,
)
from claims.llm import get_client  # noqa: E402
from claims.loader import infer_claim_metadata, parse_file  # noqa: E402
from claims.log_config import setup_logging  # noqa: E402
from claims.models import AppointmentAttributes  # noqa: E402
from claims.normalizer import normalize  # noqa: E402

_DEFAULT_OUT_DIR = REPO_ROOT / "scripts" / "AppointmentScripts"


def _serialize_appt(ev) -> dict:  # type: ignore[no-untyped-def]
    a = ev.attributes
    assert isinstance(a, AppointmentAttributes)
    return {
        "status": a.status,
        "occurred_on": a.occurred_on.isoformat() if a.occurred_on else None,
        "scheduled_for_date": (
            a.scheduled_for_date.isoformat()
            if a.scheduled_for_date
            else None
        ),
        "scheduled_notice_date": (
            a.scheduled_notice_date.isoformat()
            if a.scheduled_notice_date
            else None
        ),
        "parties": list(a.parties),
        "evidence": [
            {"note_date": e.note_date.isoformat(), "quote": e.quote}
            for e in a.evidence
        ],
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--file", required=True, help="Path to the claim notes .md file.")
    p.add_argument("--provider", choices=["google", "openai"], default=None)
    p.add_argument("--out", default=None)
    args = p.parse_args()

    load_dotenv()
    path = Path(args.file)
    log_path = setup_logging(
        subcommand="appts_via_reconciliation",
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
    extractor = AppointmentExtractor(llm)

    candidates = []
    for note in notes:
        if extractor.can_handle(note):
            candidates.extend(extractor.extract(note))

    canonical = reconcile_appointments(
        loaded.header.claim_id, dol, candidates, llm
    )
    appts = sorted(
        (_serialize_appt(e) for e in canonical),
        key=lambda d: (d["occurred_on"] or d["scheduled_for_date"] or "9999"),
    )

    out_path = Path(args.out) if args.out else (
        _DEFAULT_OUT_DIR / f"{loaded.header.claim_id}-recon-appts-list.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(
            {
                "claim_id": loaded.header.claim_id,
                "method": "per-note + reconcile",
                "count": len(appts),
                "appointments": appts,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"wrote {out_path} ({len(appts)} appointments)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
