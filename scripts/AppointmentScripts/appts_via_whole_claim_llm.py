"""Extract appointments via a single whole-claim LLM call.

One LLM call receives ALL note bodies for a claim and returns
the canonical appointment list directly. No per-note extraction,
no reconciliation pass. This is the "Option A" architecture from
the earlier menu — fully claim-level extraction.

Writes a JSON file at
sample_claim_notes/query_outputs/<claim_id>-whole.json
with the same shape as appts_via_reconciliation.py for direct
side-by-side comparison.

Usage:
    python scripts/appts_via_whole_claim_llm.py \\
        --file sample_claim_notes/sample_claim_notes1.md
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

from claims.extractor._evidence import EVIDENCE_QUOTE_GUIDANCE  # noqa: E402
from claims.llm import get_client  # noqa: E402
from claims.llm.base import LLMError  # noqa: E402
from claims.loader import infer_claim_metadata, parse_file  # noqa: E402
from claims.log_config import setup_logging  # noqa: E402
from claims.models import AppointmentStatus  # noqa: E402
from claims.normalizer import normalize  # noqa: E402

_DEFAULT_OUT_DIR = REPO_ROOT / "scripts" / "AppointmentScripts"


# --- Output schema (matches appts_via_reconciliation.py JSON) ----


class _Appointment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: AppointmentStatus
    occurred_on: date | None = None
    scheduled_for_date: date | None = None
    scheduled_notice_date: date | None = Field(
        default=None,
        description=(
            "Earliest note_date that referenced this visit as "
            "forward-looking. MUST NOT exceed the visit date — "
            "retrospective recaps do not qualify as a notice."
        ),
    )
    parties: list[str] = Field(default_factory=list)
    source_note_dates: list[date] = Field(default_factory=list)
    evidence_quote: str


class _ClaimAppointmentList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    appointments: list[_Appointment] = Field(default_factory=list)


# --- Prompt ------------------------------------------------------

_SYSTEM_PROMPT = """You are extracting the canonical APPOINTMENT list for a workers'-comp claim. You see every note for the claim in chronological order. Produce ONE entry per real clinical encounter.

STATUS (asymmetric precedence — apply across ALL notes that mention the encounter):
- If ANY note says the visit was missed/cancelled/no-show, status is `missed` (or `cancelled` if no `missed` evidence). missed > cancelled.
- Otherwise: `attended` if any note shows it happened (a `Date of Appointment:` + `Plan:` block counts), else `scheduled` if a future date is named, else `unknown`.
- Tiebreak by the most recent note_date.

DATE FIELDS:
- `occurred_on`: the date the visit happened (set when status is attended/missed/cancelled/unknown after the visit date passes).
- `scheduled_for_date`: a future visit date (set when status is scheduled).
- `scheduled_notice_date`: the EARLIEST note_date that referenced this visit as a forward-looking schedule. MUST be on or before the visit date. Retrospective recaps (e.g. an August Resolution Strategy note recapping a February visit) DO NOT qualify. Null if no forward-looking note exists.
- `source_note_dates`: every note_date that mentions this visit, sorted.

PROXIMITY: extract a visit only when at least ONE note contains the visit date + a named party + a status verb (or templated medical-record block) in the same clause. Do not assemble (date, party, status) from disjoint sentences.

DO NOT EXTRACT:
- Pure communications (emails, texts, fax cover sheets, records-receipt notices) without a visit-action verb.
- Care assignments and orders ("Dr. X agreed to assume care", "referred to pain mgmt", "MRI ordered") — these establish treatment, not a dated visit.
- Status notations in recap lists ("Dr. Vega (Neurology) — MMI") — these record state, not a visit.
- Hospital inpatient days (consecutive daily evals during one admission). One inpatient stay is not many appointments.
- Mentions of a provider only in the diagnosis line, history, or plan that don't pair with a date in the same clause.

DEDUP: each canonical appointment appears ONCE in your output. If multiple notes describe the same visit (a contact-note "yesterday" reference + a medical-record `Date of Appointment:` block; or a Resolution Strategy recap), collapse them. Same-day same-facility different-clinician visits are TWO appointments.

PARTIES — for each canonical appointment, union the named parties across contributing notes:
- Attending clinician(s): named persons only.
- Facility / clinic / hospital: named organizations only.
- Strip honorifics ("Dr.", "Mr.") and degree suffixes (", MD", ", DO"). Treat "Dr. Harmon" and "Harmon" as one party.
- DROP claimant labels ("claimant", "patient", "Patient A", "EE", "IE", "IW"), unnamed roles ("FCM", "TCM", "the doctor"), specialty words used as names ("Ortho", "Spine", "Pain Mgmt", "Neurology"), and single-letter abbreviations.

{evidence_guidance}

If no real appointment appears in the claim, return appointments: [].""".format(
    evidence_guidance=EVIDENCE_QUOTE_GUIDANCE
)


def _user_prompt(claim_id: str, dol: date | None, notes) -> str:  # type: ignore[no-untyped-def]
    lines = [
        f"Claim: {claim_id}",
        f"Date of loss: {dol.isoformat() if dol else 'unknown'}",
        f"Note count: {len(notes)}",
        "",
        "Notes (chronological, oldest first):",
        "",
    ]
    # Sort by note_date ascending for the LLM's chronological intuition.
    for n in sorted(notes, key=lambda n: n.note_date):
        lines.append(
            f"--- note_id={n.note_id} note_date={n.note_date} "
            f"activity={n.activity} ---"
        )
        lines.append(n.body)
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--file", required=True)
    p.add_argument("--provider", choices=["google", "openai", "groq"], default=None)
    p.add_argument("--out", default=None)
    args = p.parse_args()

    load_dotenv()
    path = Path(args.file)
    log_path = setup_logging(
        subcommand="appts_via_whole_claim_llm",
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
    user = _user_prompt(loaded.header.claim_id, dol, notes)
    print(
        f"whole-claim LLM call: {len(notes)} notes, ~{len(user)} chars",
        file=sys.stderr,
    )
    try:
        response = llm.structured(
            system=_SYSTEM_PROMPT,
            user=user,
            response_model=_ClaimAppointmentList,
        )
    except LLMError as exc:
        print(f"LLM call failed: {exc}", file=sys.stderr)
        return 1

    # Defensive: drop appointments where scheduled_notice_date >
    # the visit (same deterministic guard the reconciliation path
    # uses) so the two outputs are apples-to-apples.
    appts = []
    for a in response.appointments:
        notice = a.scheduled_notice_date
        encounter = a.occurred_on or a.scheduled_for_date
        if notice and encounter and notice > encounter:
            notice = None
        appts.append(
            {
                "status": a.status,
                "occurred_on": (
                    a.occurred_on.isoformat() if a.occurred_on else None
                ),
                "scheduled_for_date": (
                    a.scheduled_for_date.isoformat()
                    if a.scheduled_for_date
                    else None
                ),
                "scheduled_notice_date": notice.isoformat() if notice else None,
                "parties": list(a.parties),
                "source_note_dates": [d.isoformat() for d in a.source_note_dates],
                "evidence_quote": a.evidence_quote,
            }
        )
    appts.sort(
        key=lambda d: (d["occurred_on"] or d["scheduled_for_date"] or "9999")
    )

    out_path = Path(args.out) if args.out else (
        _DEFAULT_OUT_DIR / f"{loaded.header.claim_id}-whole-appts-list.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(
            {
                "claim_id": loaded.header.claim_id,
                "method": "single whole-claim LLM call",
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
