"""Compute Q1 (return-to-work) via a single whole-claim LLM call.

One LLM call receives every note body for a claim in chronological
order and returns the canonical Q1 answer directly — one of
{returned, never_returned, pending} with evidence. No per-note
extraction, no resolver.

Writes JSON to scripts/RtwScripts/<claim_id>-whole-rtw.json with the
same shape as the per-note script for direct side-by-side comparison.

Usage:
    python scripts/RtwScripts/rtw_via_whole_claim_llm.py \\
        --file sample_claim_notes/sample_claim_notes1.md
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

from claims.extractor._evidence import EVIDENCE_QUOTE_GUIDANCE  # noqa: E402
from claims.llm import get_client  # noqa: E402
from claims.llm.base import LLMError  # noqa: E402
from claims.loader import infer_claim_metadata, parse_file  # noqa: E402
from claims.log_config import setup_logging  # noqa: E402
from claims.normalizer import normalize  # noqa: E402

_DEFAULT_OUT_DIR = REPO_ROOT / "scripts" / "RtwScripts"


# --- Output schema -----------------------------------------------


class _ReturnedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["returned"] = "returned"
    rtw_date: date
    duty_type: Literal["modified", "full"]
    role: str | None = None
    source_note_dates: list[date] = Field(default_factory=list)
    evidence_quote: str


class _NeverReturnedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["never_returned"] = "never_returned"
    reason: Literal["ptd", "deceased", "separated", "closed_no_rtw"]
    terminal_date: date
    context: str | None = None
    source_note_dates: list[date] = Field(default_factory=list)
    evidence_quote: str


class _PendingPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["pending"] = "pending"
    rationale: str | None = Field(
        default=None,
        description="One-line rationale for picking pending over returned/never_returned.",
    )


class _Q1Response(BaseModel):
    """One of three payloads. Exactly one of `returned`, `never_returned`,
    `pending` is non-null; the others are null."""

    model_config = ConfigDict(extra="forbid")

    returned: _ReturnedPayload | None = None
    never_returned: _NeverReturnedPayload | None = None
    pending: _PendingPayload | None = None


# --- Prompt ------------------------------------------------------

_SYSTEM_PROMPT = """You decide the Q1 RETURN-TO-WORK status for a workers'-comp claim. You see every note for the claim in chronological order. Pick exactly ONE of three answers.

Q1 PRECEDENCE (resolve contradictions this way):
- If the claimant explicitly RETURNED TO WORK on a specific date in any note, the answer is `returned` — even if a later note closes the claim or declares permanency. A successful return wins over later closure language.
- Otherwise, if a note positively declares a terminal state (PTD declared, deceased, separated without RTW, claim closed/settled with no RTW), the answer is `never_returned`.
- Otherwise, `pending`.

RETURNED — strict evidence rules (same as the per-note extractor):
- The return must have OCCURRED, not be planned, offered, or future-dated.
- "Released to modified duty by Dr. X" is a clearance, NOT a return.
- "Light-duty offer extended effective 8/25" is an offer, NOT a return.
- POSITIVE evidence: "EE returned to modified duty on 11/10/25 as a scheduling coordinator", "She started back at work on DATE", "RTW confirmed effective DATE".
- `duty_type`: `modified` if returning on restrictions, `full` if returning to pre-injury role without restrictions.
- `rtw_date`: the date the claimant actually returned.

NEVER_RETURNED — positive evidence only:
- `ptd` declared permanent total disability
- `deceased` claimant died
- `separated` employment ended without RTW (terminated, resigned, retired)
- `closed_no_rtw` claim closed/settled (lump-sum, full and final, etc.) with NO RTW on record. If a return-to-work event exists earlier in the timeline, this is NOT a terminal — pick `returned` instead.
- `terminal_date`: the date the terminal state was declared / effective.

PENDING — none of the above are positively evidenced.

{evidence_guidance}

Output exactly one of `returned`, `never_returned`, `pending` populated; leave the other two fields null.""".format(
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
    for n in sorted(notes, key=lambda n: n.note_date):
        lines.append(
            f"--- note_id={n.note_id} note_date={n.note_date} "
            f"activity={n.activity} ---"
        )
        lines.append(n.body)
        lines.append("")
    return "\n".join(lines)


def _serialize(resp: _Q1Response, dol: date | None) -> dict:
    if resp.returned is not None:
        r = resp.returned
        return {
            "status": "returned",
            "days": (r.rtw_date - dol).days if dol else None,
            "rtw_date": r.rtw_date.isoformat(),
            "duty_type": r.duty_type,
            "role": r.role,
            "source_note_dates": [d.isoformat() for d in r.source_note_dates],
            "evidence_quote": r.evidence_quote,
        }
    if resp.never_returned is not None:
        n = resp.never_returned
        return {
            "status": "never_returned",
            "reason": n.reason,
            "terminal_date": n.terminal_date.isoformat(),
            "context": n.context,
            "source_note_dates": [d.isoformat() for d in n.source_note_dates],
            "evidence_quote": n.evidence_quote,
        }
    p = resp.pending
    days_open = (date.today() - dol).days if dol else None
    return {
        "status": "pending",
        "days_open": days_open,
        "rationale": p.rationale if p else None,
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--file", required=True)
    p.add_argument("--provider", choices=["google", "openai"], default=None)
    p.add_argument("--out", default=None)
    args = p.parse_args()

    load_dotenv()
    path = Path(args.file)
    log_path = setup_logging(
        subcommand="rtw_via_whole_claim_llm",
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
            response_model=_Q1Response,
        )
    except LLMError as exc:
        print(f"LLM call failed: {exc}", file=sys.stderr)
        return 1

    # Defensive: exactly one of the three should be set.
    populated = [
        name
        for name, val in (
            ("returned", response.returned),
            ("never_returned", response.never_returned),
            ("pending", response.pending),
        )
        if val is not None
    ]
    if len(populated) != 1:
        print(
            f"WARN: expected exactly one populated field, got {populated}",
            file=sys.stderr,
        )

    answer = _serialize(response, dol)
    payload = {
        "claim_id": loaded.header.claim_id,
        "method": "single whole-claim LLM call",
        "date_of_loss": dol.isoformat() if dol else None,
        "q1": answer,
    }

    out_path = Path(args.out) if args.out else (
        _DEFAULT_OUT_DIR / f"{loaded.header.claim_id}-whole-rtw.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"wrote {out_path} (status={answer['status']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
